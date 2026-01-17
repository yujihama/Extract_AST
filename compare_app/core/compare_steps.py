from __future__ import annotations

import json
import os
import shutil
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from compare_app.core.pipeline import CancelledError, RunContext


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _have_llm_key() -> bool:
    return bool(os.getenv("OPENAI_API_KEY") or os.getenv("AZURE_OPENAI_API_KEY"))


def _restore_compare_state_if_needed(ctx: RunContext) -> bool:
    """
    COMPARE_STATEが空、または別のrunのデータで初期化されている場合、
    現在のrunのデータでcompare_setupを再実行して状態を復元する。
    
    compare_setupステップで作成された成果物（ASTファイル、embedding cache）が
    存在することを前提とする。
    
    Returns:
        True: 復元が必要で成功した / 既に初期化済み
        False: 復元に失敗（必要なファイルが存在しないなど）
    """
    from src.tools import COMPARE_STATE
    
    # 既に現在のrunのデータで初期化されている場合は何もしない
    # （run_idを記録して、異なるrunの場合は再初期化する）
    current_run_id = COMPARE_STATE.get("_run_id")
    if current_run_id == ctx.run_id and COMPARE_STATE.get("ast_a") is not None:
        return True
    
    # 別のrunのデータが入っている場合はクリアして再初期化
    if current_run_id is not None and current_run_id != ctx.run_id:
        ctx.events.emit(
            ctx.run_id,
            "compare_state_restore",
            {"ts": _utcnow_iso(), "status": "clearing", "message": f"Clearing COMPARE_STATE from different run: {current_run_id}"},
        )
        COMPARE_STATE.clear()
    
    work_dir = Path(ctx.paths["work_dir"])
    cache_dir = Path(ctx.paths["cache_dir"])
    
    ast_a = work_dir / "ast_a.ast.json"
    ast_b = work_dir / "ast_b.ast.json"
    cache_path = cache_dir / "embedding_cache.json"
    initial_matching_path = work_dir / "initial_matching.json"
    
    # 必要なファイルが存在しない場合は復元不可
    if not ast_a.exists() or not ast_b.exists():
        ctx.events.emit(
            ctx.run_id,
            "compare_state_restore",
            {"ts": _utcnow_iso(), "success": False, "reason": "AST files not found"},
        )
        return False
    
    if not _have_llm_key():
        ctx.events.emit(
            ctx.run_id,
            "compare_state_restore",
            {"ts": _utcnow_iso(), "success": False, "reason": "LLM API key not set"},
        )
        return False
    
    ctx.events.emit(
        ctx.run_id,
        "compare_state_restore",
        {"ts": _utcnow_iso(), "status": "restoring", "message": "Restoring COMPARE_STATE from previous run..."},
    )
    
    try:
        from src.tools import compare_all_chunk_similarity_matching, compare_setup
        import json
        
        # compare_setupを再実行（embedding cacheがあれば高速）
        doc_a_txt = work_dir / "doc_a.txt"
        doc_b_txt = work_dir / "doc_b.txt"
        setup_json = compare_setup.invoke(
            {
                "docA": str(ast_a),
                "docB": str(ast_b),
                "docA_txt": str(doc_a_txt) if doc_a_txt.exists() else None,
                "docB_txt": str(doc_b_txt) if doc_b_txt.exists() else None,
                "embedding_model": ctx.params.get("embedding_model"),
                "cache_path": str(cache_path),
                "batch_size": int(ctx.params.get("embedding_batch_size", 64)),
            }
        )
        
        # initial_matchingが既にファイルとして存在する場合はそれを読み込む
        if initial_matching_path.exists():
            try:
                initial_matching = json.loads(initial_matching_path.read_text(encoding="utf-8"))
                COMPARE_STATE["initial_matching"] = initial_matching
            except Exception:
                pass
        
        # initial_matchingがまだ無い場合は再生成
        if not COMPARE_STATE.get("initial_matching"):
            initial_matching_json = compare_all_chunk_similarity_matching.invoke(
                {
                    "top_k": int(ctx.params.get("match_top_k", 3)),
                    "alpha": float(ctx.params.get("match_alpha", 0.3)),
                    "beta": float(ctx.params.get("match_beta", 0.4)),
                    "min_score": float(ctx.params.get("match_min_score", 0.25)),
                }
            )
            try:
                COMPARE_STATE["initial_matching"] = json.loads(initial_matching_json)
            except Exception:
                pass
        
        # run_idを記録（別のrunを実行した後の復元時に判別するため）
        COMPARE_STATE["_run_id"] = ctx.run_id
        
        ctx.events.emit(
            ctx.run_id,
            "compare_state_restore",
            {"ts": _utcnow_iso(), "success": True, "message": "COMPARE_STATE restored successfully"},
        )
        return True
        
    except Exception as e:
        ctx.events.emit(
            ctx.run_id,
            "compare_state_restore",
            {"ts": _utcnow_iso(), "success": False, "reason": str(e)},
        )
        return False


def _run_with_cancellation(ctx: RunContext, *, label: str, func) -> Any:
    done = threading.Event()
    result: dict[str, Any] = {}
    error: dict[str, Exception] = {}

    def _target() -> None:
        try:
            result["value"] = func()
        except Exception as exc:
            error["error"] = exc
        finally:
            done.set()

    thread = threading.Thread(target=_target, name=f"{label}_worker", daemon=True)
    thread.start()

    while not done.is_set():
        if ctx.cancellation.is_cancelled():
            raise CancelledError(f"cancelled during {label}")
        done.wait(0.2)

    if "error" in error:
        raise error["error"]
    return result.get("value")


@dataclass
class CompareSetupStep:
    """比較用の状態（COMPARE_STATE）と embedding cache を準備する。"""

    name: str = "compare_setup"

    def run(self, ctx: RunContext) -> None:
        run_dir = Path(ctx.paths["run_dir"])
        work_dir = Path(ctx.paths["work_dir"])
        cache_dir = Path(ctx.paths["cache_dir"])
        cache_dir.mkdir(parents=True, exist_ok=True)

        ast_a = work_dir / "ast_a.ast.json"
        ast_b = work_dir / "ast_b.ast.json"
        if not ast_a.exists():
            raise FileNotFoundError(str(ast_a))
        if not ast_b.exists():
            raise FileNotFoundError(str(ast_b))

        cache_path = cache_dir / "embedding_cache.json"
        # 既存の共有キャッシュがあればseedして、API呼び出しを抑える（特に再実行/検証時）
        global_cache = Path("data") / "embedding" / "embedding_cache.json"
        if global_cache.exists() and (not cache_path.exists()):
            try:
                shutil.copy2(global_cache, cache_path)
            except Exception:
                pass

        # Embedding/LLMの鍵が無い場合は、このステップは失敗させる（realモードの前提）
        # ※将来、embedding無しモードを作るならここを条件スキップにする
        if not _have_llm_key():
            raise RuntimeError("Embedding/LLM API key not set (OPENAI_API_KEY or AZURE_OPENAI_API_KEY)")

        from src.tools import COMPARE_STATE, compare_all_chunk_similarity_matching, compare_setup

        setup_json = compare_setup.invoke(
            {
                "docA": str(ast_a),
                "docB": str(ast_b),
                "embedding_model": ctx.params.get("embedding_model"),
                "cache_path": str(cache_path),
                "batch_size": int(ctx.params.get("embedding_batch_size", 64)),
            }
        )

        # warmup matching（統計用）
        initial_matching_json = compare_all_chunk_similarity_matching.invoke(
            {
                "top_k": int(ctx.params.get("match_top_k", 3)),
                "alpha": float(ctx.params.get("match_alpha", 0.3)),
                "beta": float(ctx.params.get("match_beta", 0.4)),
                "min_score": float(ctx.params.get("match_min_score", 0.25)),
            }
        )

        # 永続化（後からUI/APIで参照できるようにする）
        initial_matching_path = work_dir / "initial_matching.json"
        try:
            initial_matching_path.write_text(str(initial_matching_json), encoding="utf-8")
            ctx.events.emit(
                ctx.run_id,
                "artifact_updated",
                {"ts": _utcnow_iso(), "kind": "initial_matching", "path": initial_matching_path.relative_to(run_dir).as_posix()},
            )
        except Exception:
            pass

        try:
            COMPARE_STATE["initial_matching"] = json.loads(initial_matching_json)
        except Exception:
            COMPARE_STATE["initial_matching"] = None

        # run_idを記録（別のrunを実行した後の復元時に判別するため）
        COMPARE_STATE["_run_id"] = ctx.run_id

        # ドキュメントペア成果物として永続化（次runで自動再利用できるようにする）
        # - initial_matching.json
        # - embedding_cache.json
        doc_a_hash = ctx.params.get("doc_a_hash")
        doc_b_hash = ctx.params.get("doc_b_hash")
        if doc_a_hash and doc_b_hash:
            try:
                from compare_app.infra.document_store import DocumentPairRepository

                pair_repo = DocumentPairRepository()

                matching_data = None
                try:
                    matching_data = json.loads(initial_matching_json)
                except Exception:
                    # 既にパース済みがあればそれを使う
                    if isinstance(COMPARE_STATE.get("initial_matching"), dict):
                        matching_data = COMPARE_STATE.get("initial_matching")
                if isinstance(matching_data, dict):
                    pair_repo.save_matching(str(doc_a_hash), str(doc_b_hash), matching_data)

                cache_dir = Path(ctx.paths["cache_dir"])
                cache_path = cache_dir / "embedding_cache.json"
                if cache_path.exists():
                    try:
                        cache_data = json.loads(cache_path.read_text(encoding="utf-8"))
                        if isinstance(cache_data, dict):
                            pair_repo.save_embedding_cache(str(doc_a_hash), str(doc_b_hash), cache_data)
                    except Exception:
                        pass
            except Exception:
                # compare_setup 自体は成功しているので、永続化失敗は致命にしない
                pass

        ctx.events.emit(
            ctx.run_id,
            "compare_setup_done",
            {"ts": _utcnow_iso(), "setup_ok": True, "cache_path": str(cache_path), "setup_json_preview": str(setup_json)[:300]},
        )
        if cache_path.exists():
            rel = cache_path.relative_to(run_dir).as_posix()
            ctx.events.emit(
                ctx.run_id,
                "artifact_updated",
                {"ts": _utcnow_iso(), "kind": "embedding_cache", "path": rel},
            )


@dataclass
class PreAnalysisStep:
    """関係性判定（Pre-Analysis）とテンプレ生成を行い、work/template_draft.md を作る。"""

    name: str = "pre_analysis"

    def should_run(self, ctx: RunContext) -> bool:
        work_dir = Path(ctx.paths["work_dir"])
        out_draft = work_dir / "template_draft.md"
        force = bool(ctx.params.get("force", False))
        return force or (not out_draft.exists())

    def run(self, ctx: RunContext) -> None:
        work_dir = Path(ctx.paths["work_dir"])
        out_draft = work_dir / "template_draft.md"
        out_meta = work_dir / "pre_analysis.json"
        run_dir = Path(ctx.paths["run_dir"])

        ast_a = work_dir / "ast_a.ast.json"
        ast_b = work_dir / "ast_b.ast.json"

        if not ast_a.exists() or not ast_b.exists():
            raise FileNotFoundError("AST files not found for pre_analysis")

        # COMPARE_STATEが空の場合は復元（compare_setupステップがスキップされた場合など）
        _restore_compare_state_if_needed(ctx)

        if not _have_llm_key():
            # フォールバック（LLM無しでもパイプラインは進められる）
            draft = "\n".join(
                [
                    "# 比較レポート（テンプレ・フォールバック）",
                    "",
                    "> LLMキー未設定のため、Pre-Analysisは実行されていません。",
                    "",
                    "## 関係性（relation）",
                    "- relation: unknown",
                    "- reason: LLMキー未設定",
                    "",
                    "## 分析プラン（plan）",
                    "- [ ] （未生成）",
                    "",
                    "## 差分まとめ",
                    "",
                    "(ここに段階的に追記されます)",
                    "",
                ]
            )
            out_draft.write_text(draft, encoding="utf-8")
            out_meta.write_text(
                json.dumps({"relation": "unknown", "reason": "LLMキー未設定", "plan": [], "template_name": None}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            ctx.events.emit(
                ctx.run_id,
                "artifact_updated",
                {"ts": _utcnow_iso(), "kind": "template_draft", "path": out_draft.relative_to(run_dir).as_posix()},
            )
            ctx.events.emit(
                ctx.run_id,
                "artifact_updated",
                {"ts": _utcnow_iso(), "kind": "pre_analysis", "path": out_meta.relative_to(run_dir).as_posix()},
            )
            return

        from deepagents import create_deep_agent
        from deepagents.backends.utils import create_file_data

        from src.prompt import compare_type_analysis_prompt
        from src.schema import PreAnalysisResult
        from src.tools import (
            compare_get_chunk,
            compare_get_grouping,
            compare_search_by_keyphrase,
            compare_specified_chunks_diff,
            compare_specified_chunks_llm,
            extract_regex_matches,
            get_file_length,
            read_ast,
            read_text_file,
            read_text_segment,
        )
        from src.utils import build_llm

        from compare_app.agents.middleware import CompareRestoreConfig, EventSinkMiddleware

        llm_complex = build_llm(model=str(ctx.params.get("llm_complex_model", "gpt-5-mini")))

        # run入力（data/runs/{run_id}/input）を読む analyze_visual_contents を提供（data/input固定依存を回避）
        llm_visual = build_llm(
            model=str(ctx.params.get("llm_visual_model", ctx.params.get("llm_complex_model", "gpt-5-mini")))
        )
        from langchain_core.messages import HumanMessage
        from langchain_core.tools import tool
        import base64
        import fitz  # PyMuPDF

        input_dir = Path(ctx.paths["input_dir"])

        def _get_pdf_page_as_image(pdf_path: Path, page_numbers: list[int], dpi: int = 150) -> list[dict]:
            doc = fitz.open(str(pdf_path))
            try:
                out = []
                for page_number in page_numbers:
                    if page_number < 1 or page_number > len(doc):
                        raise ValueError(f"invalid page {page_number} (total {len(doc)})")
                    page = doc[page_number - 1]
                    mat = fitz.Matrix(dpi / 72, dpi / 72)
                    pix = page.get_pixmap(matrix=mat)
                    img_bytes = pix.tobytes("png")
                    b64_data = base64.b64encode(img_bytes).decode("utf-8")
                    out.append({"page_number": page_number, "base64_data": b64_data})
                return out
            finally:
                doc.close()

        @tool
        def analyze_visual_contents(document_name: str, page_numbers: list[int], prompt: str) -> str:
            """
            Run入力のPDF（data/runs/{run_id}/input）を対象に、指定ページを画像として分析して返す。
            document_name が .txt/.ast.json の場合は .pdf に置換して探索する。
            """
            name = str(document_name or "").strip()
            if name.endswith(".ast.json"):
                name = name.replace(".ast.json", ".pdf")
            elif name.endswith(".txt"):
                name = name.replace(".txt", ".pdf")
            p = Path(name)
            if p.is_absolute() and p.exists():
                pdf_path = p
            else:
                cand = input_dir / name
                if cand.exists():
                    pdf_path = cand
                else:
                    # doc_a.pdf / doc_b.pdf の両方を試す
                    for fallback_name in ["doc_a.pdf", "doc_b.pdf"]:
                        fb = input_dir / fallback_name
                        if fb.exists():
                            pdf_path = fb
                            break
                    else:
                        return f"[analyze_visual_contents] pdf not found for: {document_name} (run has no PDF inputs)."

            images = _get_pdf_page_as_image(pdf_path, page_numbers)
            b64_data_list = [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{item['base64_data']}"}}
                for item in images
            ]
            message = HumanMessage(content=[{"type": "text", "text": prompt}, *b64_data_list])
            response = llm_visual.invoke([message])
            return response.content

        tools = [
            read_ast,
            read_text_file,
            read_text_segment,
            extract_regex_matches,
            get_file_length,
            compare_get_grouping,
            compare_search_by_keyphrase,
            compare_get_chunk,
            compare_specified_chunks_diff,
            compare_specified_chunks_llm,
            analyze_visual_contents,
        ]

        cache_path = Path(ctx.paths["cache_dir"]) / "embedding_cache.json"
        initial_matching_path = work_dir / "initial_matching.json"
        restore_cfg = CompareRestoreConfig(
            ast_a_path=str(ast_a),
            ast_b_path=str(ast_b),
            txt_a_path=str(work_dir / "doc_a.txt"),
            txt_b_path=str(work_dir / "doc_b.txt"),
            cache_path=str(cache_path),
            initial_matching_path=str(initial_matching_path) if initial_matching_path.exists() else "",
            embedding_model=ctx.params.get("embedding_model"),
            embedding_batch_size=int(ctx.params.get("embedding_batch_size", 64)),
            warmup_matching=not initial_matching_path.exists(),
            match_top_k=int(ctx.params.get("match_top_k", 3)),
            match_alpha=float(ctx.params.get("match_alpha", 0.3)),
            match_beta=float(ctx.params.get("match_beta", 0.4)),
            match_min_score=float(ctx.params.get("match_min_score", 0.25)),
        )

        agent = create_deep_agent(
            model=llm_complex,
            tools=tools,
            response_format=PreAnalysisResult,
            system_prompt=compare_type_analysis_prompt,
            middleware=[
                EventSinkMiddleware(
                    run_id=ctx.run_id,
                    events=ctx.events,
                    cancellation=ctx.cancellation,
                    agent_name="pre_analysis_agent",
                    compare_restore=restore_cfg,
                )
            ],
            debug=False,
        )

        # deep_agentのfileツールも使えるように、前ステップの成果物を仮想FSに投入
        input_dir = Path(ctx.paths["input_dir"])
        cache_text = cache_path.read_text(encoding="utf-8", errors="replace") if cache_path.exists() else "{}"

        files = {
            # AST
            "/ast_a.ast.json": create_file_data(ast_a.read_text(encoding="utf-8", errors="replace")),
            "/ast_b.ast.json": create_file_data(ast_b.read_text(encoding="utf-8", errors="replace")),
            # キャッシュ
            "/.embedding_cache.json": create_file_data(cache_text),
        }

        # initial_matching.json を追加（存在すれば）
        if initial_matching_path.exists():
            files["/initial_matching.json"] = create_file_data(
                initial_matching_path.read_text(encoding="utf-8", errors="replace")
            )

        # blueprint を追加（存在すれば）
        for bp_name in ["blueprint_a.json", "blueprint_b.json"]:
            bp_path = work_dir / bp_name
            if bp_path.exists():
                files[f"/{bp_name}"] = create_file_data(
                    bp_path.read_text(encoding="utf-8", errors="replace")
                )

        # 入力テキスト（doc_a.txt, doc_b.txt）を追加（存在すれば）
        for doc_name in ["doc_a.txt", "doc_b.txt"]:
            doc_path = work_dir / doc_name
            if doc_path.exists():
                files[f"/{doc_name}"] = create_file_data(
                    doc_path.read_text(encoding="utf-8", errors="replace")
                )
            else:
                input_doc_path = input_dir / doc_name
                if input_doc_path.exists():
                    files[f"/{doc_name}"] = create_file_data(
                        input_doc_path.read_text(encoding="utf-8", errors="replace")
                    )

        # ユーザー指定の重点比較観点（UI/CLIから注入可能）
        # params.comparison_focus: str | list[str] | None
        user_focus = ctx.params.get("comparison_focus")
        if user_focus:
            if isinstance(user_focus, list):
                focus_lines = [f"- {f}" for f in user_focus]
            else:
                focus_lines = [f"- {user_focus}"]
        else:
            # デフォルトの比較観点
            focus_lines = ["- 変更箇所とその影響の特定"]
        
        # 任意: 重点比較観点ファイル（txtのみ）
        focus_file_name: Optional[str] = None
        focus_file_path: Optional[Path] = None
        focus_param = ctx.params.get("comparison_focus_file")
        if focus_param:
            try:
                focus_param_str = str(focus_param).strip()
                if focus_param_str:
                    cand_path = Path(focus_param_str)
                    if cand_path.is_absolute() and cand_path.exists():
                        focus_file_path = cand_path
                    else:
                        for base in [run_dir, work_dir, input_dir]:
                            cand = base / focus_param_str
                            if cand.exists():
                                focus_file_path = cand
                                break
            except Exception:
                pass
        if not focus_file_path:
            for base in [work_dir, input_dir]:
                cand = base / "comparison_focus.txt"
                if cand.exists():
                    focus_file_path = cand
                    break
        if focus_file_path and focus_file_path.suffix.lower() == ".txt":
            try:
                focus_text = focus_file_path.read_text(encoding="utf-8", errors="replace")
                if focus_text.strip():
                    files["/comparison_focus.txt"] = create_file_data(focus_text)
                    focus_file_name = focus_file_path.name
            except Exception:
                pass

        # LLMに軽量判定の材料を提供するための統計情報を収集
        def _get_doc_stats(ast_path: Path) -> dict:
            """AST JSONから統計情報を抽出（参考用）"""
            try:
                ast_data = json.loads(ast_path.read_text(encoding="utf-8", errors="replace"))
                root = ast_data.get("root", {})
                
                def count_nodes(node, depth=0):
                    total = 1
                    max_d = depth
                    content_chars = len(node.get("content", "") or "")
                    for child in node.get("children", []):
                        if isinstance(child, dict):
                            c, d, chars = count_nodes(child, depth + 1)
                            total += c
                            max_d = max(max_d, d)
                            content_chars += chars
                    return total, max_d, content_chars
                
                sections, max_depth, total_chars = count_nodes(root)
                return {
                    "sections": sections,
                    "max_depth": max_depth,
                    "total_chars": total_chars,
                }
            except Exception:
                return {"sections": 0, "max_depth": 0, "total_chars": 0}

        def _get_text_stats(text_path: Path) -> dict:
            """原文TXTから統計情報を抽出（軽量判定の基準）"""
            try:
                text = text_path.read_text(encoding="utf-8", errors="replace")
                lines = text.splitlines()
                return {
                    "total_chars": len(text),
                    "total_lines": len(lines),
                }
            except Exception:
                return {"total_chars": 0, "total_lines": 0}

        stats_a = _get_doc_stats(ast_a)
        stats_b = _get_doc_stats(ast_b)
        txt_stats_a = _get_text_stats(work_dir / "doc_a.txt")
        txt_stats_b = _get_text_stats(work_dir / "doc_b.txt")

        # initial_matchingからマッチング統計を取得
        initial_matching_path = work_dir / "initial_matching.json"
        matching_stats = {}
        if initial_matching_path.exists():
            try:
                matching_data = json.loads(initial_matching_path.read_text(encoding="utf-8"))
                groups = matching_data.get("groups", [])
                unmatched_b = matching_data.get("unmatched_b", [])
                
                all_scores = []
                for g in groups:
                    for m in g.get("matches", []):
                        if isinstance(m, dict):
                            all_scores.append(m.get("score", 0))
                
                matching_stats = {
                    "total_groups": len(groups),
                    "unmatched_b_count": len(unmatched_b),
                    "avg_score": round(sum(all_scores) / len(all_scores), 3) if all_scores else 0,
                    "high_similarity_count": sum(1 for s in all_scores if s >= 0.8),
                    "low_similarity_count": sum(1 for s in all_scores if s < 0.5),
                }
            except Exception:
                pass

        # LLM判断用の統計情報をプロンプトに含める
        stats_lines = [
            "## ドキュメント統計情報（軽量判定の参考）",
            "",
            "**docA:**",
            f"- 原文文字数: {txt_stats_a['total_chars']:,}",
            f"- 原文行数: {txt_stats_a['total_lines']:,}",
            "",
            "**docB:**",
            f"- 原文文字数: {txt_stats_b['total_chars']:,}",
            f"- 原文行数: {txt_stats_b['total_lines']:,}",
            "",
        ]
        
        if matching_stats:
            stats_lines.extend([
                "**マッチング統計（ASTベース/参考）:**",
                f"- チャンクグループ数: {matching_stats.get('total_groups', 0)}",
                f"- 未マッチBチャンク: {matching_stats.get('unmatched_b_count', 0)}",
                f"- 平均類似度スコア: {matching_stats.get('avg_score', 0)}",
                f"- 高類似度(>=0.8): {matching_stats.get('high_similarity_count', 0)}件",
                f"- 低類似度(<0.5): {matching_stats.get('low_similarity_count', 0)}件",
                "",
            ])

        query_lines = [
            "2つのドキュメントdocAとdocBの関係性を分析してください。",
            "原文テキスト（doc_a.txt / doc_b.txt）を主な比較対象として扱ってください。",
            "AST（ast_a.ast.json / ast_b.ast.json）は参考情報であり、階層のズレが起こり得ます。",
            "",
            "- docA 原文: doc_a.txt",
            "- docB 原文: doc_b.txt",
            "- docA AST: ast_a.ast.json",
            "- docB AST: ast_b.ast.json",
            "※ASTのcontentは本文の内容で、content_summaryは分析作業の補助のために事前に付与した要約です。",
            "",
            *stats_lines,
            "**重点比較観点**",
            *focus_lines,
        ]
        if focus_file_name:
            query_lines.extend(
                [
                    "",
                    "**重点比較観点ファイル**",
                    "- /comparison_focus.txt を必要に応じて参照してください。",
                    f"- 元ファイル名: {focus_file_name}",
                ]
            )
        query_lines.extend(
            [
            "",
            "**前提事項**",
            "- それぞれのASTファイルは独立して解析し作成されたものです。そのため同じ構成でも階層分けが異なる場合があります。",
            "- chunk間の類似度は参考程度にしてプランを立ててください。同じcontentでもchunkの分割の違いで類似度が低くなっている場合があります。",
            "",
            "# 指示",
            "",
            "## Step 1: relation判定",
            "relation ∈ {Fix, Revision, Derivative, Heterogeneous, Subset}",
            "",
            "## Step 2: is_complete判定（論理式）",
            "",
            "```",
            "total_chars = docA.total_chars + docB.total_chars",
            f"total_chars = {txt_stats_a['total_chars']} + {txt_stats_b['total_chars']} = {txt_stats_a['total_chars'] + txt_stats_b['total_chars']}",
            "",
            "is_lightweight = (total_chars <= 10000) OR (relation == 'Fix')",
            "",
            "IF is_lightweight:",
            "    is_complete = true",
            "    filled_report = <Markdown形式の分析レポート>",
            "ELSE:",
            "    is_complete = false",
            "    filled_report = ''",
            "```",
            "",
            "## Step 3: 出力",
            "",
            "is_lightweight == true の場合: filled_reportに完成したレポートを出力",
            "is_lightweight == false の場合: planに分析手順、templateに空欄テンプレートを出力",
            ]
        )

        query = "\n".join(query_lines)

        result = _run_with_cancellation(
            ctx,
            label="pre_analysis_agent",
            func=lambda: agent.invoke({"messages": [{"role": "user", "content": query}], "files": files}),
        )
        structured = result.get("structured_response")
        if structured is None:
            raise RuntimeError("pre_analysis_agent returned no structured_response")

        # deep_agentの仮想FSから template_draft.md を取り出す（無ければフォールバック生成）
        template_text: Optional[str] = None
        vfiles = result.get("files") or {}
        for key in ["/template_draft.md", "template_draft.md", "/out/template_draft.md", "/out/template_draft.md"]:
            fd = vfiles.get(key)
            if isinstance(fd, dict) and "content" in fd:
                c = fd["content"]
                if isinstance(c, list):
                    template_text = "\n".join(c)
                else:
                    template_text = str(c)
                break

        if not template_text:
            # 最低限、structured.plan からテンプレを生成
            plan_lines = []
            for i, s in enumerate(getattr(structured, "plan", []) or [], 1):
                plan_lines.append(f"- [ ] {i}. {s}")
            template_text = "\n".join(
                [
                    "# 比較レポート（テンプレ）",
                    "",
                    "## 関係性（relation）",
                    f"- relation: {getattr(structured, 'relation', '')}",
                    f"- reason: {getattr(structured, 'reason', '')}",
                    "",
                    "## 分析プラン（plan）",
                    *(plan_lines or ["- [ ] （プラン未生成）"]),
                    "",
                    "## 差分まとめ",
                    "",
                    "(ここに段階的に追記されます)",
                    "",
                ]
            )

        # is_completeフラグを取得（軽量ファイルの場合にpre_analysisで完結）
        is_complete = getattr(structured, "is_complete", False)
        filled_report = getattr(structured, "filled_report", "") or ""

        # is_complete=Trueの場合、filled_reportを最終成果物として保存（Draftは作成しない）
        if is_complete and filled_report:
            # 記入済みレポートをtemplate_filled.mdとして保存（compare_analysisをスキップ）
            out_dir = Path(ctx.paths["out_dir"])
            out_dir.mkdir(parents=True, exist_ok=True)
            filled_path = out_dir / "template_filled.md"
            filled_path.write_text(filled_report, encoding="utf-8")
            ctx.events.emit(
                ctx.run_id,
                "artifact_updated",
                {"ts": _utcnow_iso(), "kind": "template_filled", "path": filled_path.relative_to(run_dir).as_posix()},
            )
            # is_complete=Trueの場合はtemplate_draft.mdを作成しない（軽量データでは空欄テンプレートの概念がない）
        else:
            out_draft.write_text(template_text, encoding="utf-8")

        out_meta.write_text(
            json.dumps(
                {
                    "relation": getattr(structured, "relation", None),
                    "reason": getattr(structured, "reason", None),
                    "plan": getattr(structured, "plan", None),
                    "template_name": getattr(structured, "template", None),
                    "is_complete": is_complete,
                    # LLM判断用に提供した統計情報
                    "doc_stats": {
                        "docA": stats_a,
                        "docB": stats_b,
                        "matching": matching_stats,
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        ctx.events.emit(
            ctx.run_id,
            "artifact_updated",
            {"ts": _utcnow_iso(), "kind": "template_draft", "path": out_draft.relative_to(run_dir).as_posix()},
        )
        ctx.events.emit(
            ctx.run_id,
            "artifact_updated",
            {"ts": _utcnow_iso(), "kind": "pre_analysis", "path": out_meta.relative_to(run_dir).as_posix()},
        )

        # is_complete=Trueの場合、後続のcompare_analysisをスキップするフラグを設定
        if is_complete:
            ctx.events.emit(
                ctx.run_id,
                "pre_analysis_complete",
                {"ts": _utcnow_iso(), "is_complete": True, "message": "Pre-analysis completed. Skipping compare_analysis."},
            )
            # skip_compare_analysisフラグをwork_dirに保存（後続ステップが参照）
            skip_flag_path = work_dir / ".skip_compare_analysis"
            skip_flag_path.write_text("true", encoding="utf-8")


@dataclass
class CompareAnalysisStep:
    """Pre-Analysisで作ったテンプレを埋めて、out/template_filled.md を作る。"""

    name: str = "compare_analysis"

    def should_run(self, ctx: RunContext) -> bool:
        work_dir = Path(ctx.paths["work_dir"])
        out_dir = Path(ctx.paths["out_dir"])
        filled = out_dir / "template_filled.md"
        force = bool(ctx.params.get("force", False))

        # pre_analysisで完結した場合はスキップ
        skip_flag_path = work_dir / ".skip_compare_analysis"
        if skip_flag_path.exists():
            ctx.events.emit(
                ctx.run_id,
                "step_skipped_reason",
                {"ts": _utcnow_iso(), "step": self.name, "reason": "Pre-analysis marked as complete (lightweight documents)"},
            )
            return False

        # pre_analysis.jsonからis_completeを確認
        pre_analysis_path = work_dir / "pre_analysis.json"
        if pre_analysis_path.exists():
            try:
                pre_analysis = json.loads(pre_analysis_path.read_text(encoding="utf-8"))
                if pre_analysis.get("is_complete", False):
                    ctx.events.emit(
                        ctx.run_id,
                        "step_skipped_reason",
                        {"ts": _utcnow_iso(), "step": self.name, "reason": "Pre-analysis is_complete=true"},
                    )
                    return False
            except Exception:
                pass

        return force or (not filled.exists())

    def run(self, ctx: RunContext) -> None:
        work_dir = Path(ctx.paths["work_dir"])
        out_dir = Path(ctx.paths["out_dir"])
        out_dir.mkdir(parents=True, exist_ok=True)
        run_dir = Path(ctx.paths["run_dir"])

        draft_path = work_dir / "template_draft.md"
        filled_path = out_dir / "template_filled.md"

        if not draft_path.exists():
            raise FileNotFoundError(str(draft_path))

        # COMPARE_STATEが空の場合は復元（compare_setupステップがスキップされた場合など）
        _restore_compare_state_if_needed(ctx)

        if not _have_llm_key():
            # フォールバック: そのままコピー＋注記
            base = draft_path.read_text(encoding="utf-8", errors="replace")
            filled_path.write_text(base + "\n\n> LLMキー未設定のため、Compare-Analysisは未実行です。\n", encoding="utf-8")
            ctx.events.emit(
                ctx.run_id,
                "artifact_updated",
                {"ts": _utcnow_iso(), "kind": "template_filled", "path": filled_path.relative_to(run_dir).as_posix()},
            )
            return

        from deepagents import create_deep_agent
        from deepagents.backends.utils import create_file_data

        from src.prompt import (
            compare_parent_agent_prompt,
            compare_sub_agent1,
            compare_sub_agent2,
            compare_sub_agent3,
            compare_sub_agent_general,
            compare_sub_agent_report,
        )
        from src.tools import (
            compare_get_chunk,
            compare_get_grouping,
            compare_search_by_keyphrase,
            compare_specified_chunks_diff,
            compare_specified_chunks_llm,
            extract_regex_matches,
            get_file_length,
            read_ast,
            read_text_file,
            read_text_segment,
        )
        from src.utils import build_llm

        from compare_app.agents.middleware import CompareRestoreConfig, EventSinkMiddleware

        llm = build_llm()
        llm_complex = build_llm(model=str(ctx.params.get("llm_complex_model", "gpt-5-mini")))

        # run入力（data/runs/{run_id}/input）を読む analyze_visual_contents を提供（data/input固定依存を回避）
        llm_visual = build_llm(
            model=str(ctx.params.get("llm_visual_model", ctx.params.get("llm_complex_model", "gpt-5-mini")))
        )
        from langchain_core.messages import HumanMessage
        from langchain_core.tools import tool
        import base64
        import fitz  # PyMuPDF

        input_dir = Path(ctx.paths["input_dir"])

        def _get_pdf_page_as_image(pdf_path: Path, page_numbers: list[int], dpi: int = 150) -> list[dict]:
            doc = fitz.open(str(pdf_path))
            try:
                out = []
                for page_number in page_numbers:
                    if page_number < 1 or page_number > len(doc):
                        raise ValueError(f"invalid page {page_number} (total {len(doc)})")
                    page = doc[page_number - 1]
                    mat = fitz.Matrix(dpi / 72, dpi / 72)
                    pix = page.get_pixmap(matrix=mat)
                    img_bytes = pix.tobytes("png")
                    b64_data = base64.b64encode(img_bytes).decode("utf-8")
                    out.append({"page_number": page_number, "base64_data": b64_data})
                return out
            finally:
                doc.close()

        @tool
        def analyze_visual_contents(document_name: str, page_numbers: list[int], prompt: str) -> str:
            """
            Run入力のPDF（data/runs/{run_id}/input）を対象に、指定ページを画像として分析して返す。
            document_name が .txt/.ast.json の場合は .pdf に置換して探索する。
            """
            name = str(document_name or "").strip()
            if name.endswith(".ast.json"):
                name = name.replace(".ast.json", ".pdf")
            elif name.endswith(".txt"):
                name = name.replace(".txt", ".pdf")
            p = Path(name)
            if p.is_absolute() and p.exists():
                pdf_path = p
            else:
                cand = input_dir / name
                if cand.exists():
                    pdf_path = cand
                else:
                    for fallback_name in ["doc_a.pdf", "doc_b.pdf"]:
                        fb = input_dir / fallback_name
                        if fb.exists():
                            pdf_path = fb
                            break
                    else:
                        return f"[analyze_visual_contents] pdf not found for: {document_name} (run has no PDF inputs)."

            images = _get_pdf_page_as_image(pdf_path, page_numbers)
            b64_data_list = [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{item['base64_data']}"}}
                for item in images
            ]
            message = HumanMessage(content=[{"type": "text", "text": prompt}, *b64_data_list])
            response = llm_visual.invoke([message])
            return response.content

        tools_compare = [
            read_ast,
            read_text_file,
            read_text_segment,
            extract_regex_matches,
            get_file_length,
            compare_get_grouping,
            compare_search_by_keyphrase,
            compare_get_chunk,
            compare_specified_chunks_diff,
            compare_specified_chunks_llm,
            analyze_visual_contents,
        ]

        # ThreadLocal な COMPARE_STATE 対策:
        # subagent/tool 実行が別スレッドになっても、compare_* 呼び出し直前に復元できるように設定を渡す
        ast_a_path = work_dir / "ast_a.ast.json"
        ast_b_path = work_dir / "ast_b.ast.json"
        cache_path = Path(ctx.paths["cache_dir"]) / "embedding_cache.json"
        initial_matching_path = work_dir / "initial_matching.json"
        restore_cfg = CompareRestoreConfig(
            ast_a_path=str(ast_a_path),
            ast_b_path=str(ast_b_path),
            txt_a_path=str(work_dir / "doc_a.txt"),
            txt_b_path=str(work_dir / "doc_b.txt"),
            cache_path=str(cache_path),
            initial_matching_path=str(initial_matching_path) if initial_matching_path.exists() else "",
            embedding_model=ctx.params.get("embedding_model"),
            embedding_batch_size=int(ctx.params.get("embedding_batch_size", 64)),
            warmup_matching=not initial_matching_path.exists(),
            match_top_k=int(ctx.params.get("match_top_k", 3)),
            match_alpha=float(ctx.params.get("match_alpha", 0.3)),
            match_beta=float(ctx.params.get("match_beta", 0.4)),
            match_min_score=float(ctx.params.get("match_min_score", 0.25)),
        )

        parent_mw = EventSinkMiddleware(
            run_id=ctx.run_id,
            events=ctx.events,
            cancellation=ctx.cancellation,
            agent_name="compare_parent",
            compare_restore=restore_cfg,
        )

        agent = create_deep_agent(
            model=llm_complex,
            system_prompt=compare_parent_agent_prompt,
            middleware=[parent_mw],
            subagents=[
                {
                    "name": "compare_general_purpose_agent",
                    "description": "ドキュメント比較に必要な準備作業や事前確認を汎用的に行うサブエージェントです。",
                    "system_prompt": compare_sub_agent_general,
                    "tools": tools_compare,
                    "middleware": [
                        EventSinkMiddleware(
                            run_id=ctx.run_id,
                            events=ctx.events,
                            cancellation=ctx.cancellation,
                            agent_name="compare_general",
                            is_subagent=True,
                            forced_parent_invocation_id=parent_mw.invocation_id,
                            forced_parent_agent_name=parent_mw.agent_name,
                            compare_restore=restore_cfg,
                        )
                    ],
                    "model": llm,
                },
                {
                    "name": "compare_agent",
                    "description": "与えられた特定の観点でドキュメント間の比較を行います。",
                    "system_prompt": compare_sub_agent1,
                    "tools": tools_compare,
                    "middleware": [
                        EventSinkMiddleware(
                            run_id=ctx.run_id,
                            events=ctx.events,
                            cancellation=ctx.cancellation,
                            agent_name="compare_agent",
                            is_subagent=True,
                            forced_parent_invocation_id=parent_mw.invocation_id,
                            forced_parent_agent_name=parent_mw.agent_name,
                            compare_restore=restore_cfg,
                        )
                    ],
                    "model": llm,
                },
                {
                    "name": "deep_research_agent",
                    "description": "compare_agentの結果に対して、より具体的な分析観点や論点について深掘りを行うサブエージェントです。",
                    "system_prompt": compare_sub_agent2,
                    "tools": tools_compare,
                    "middleware": [
                        EventSinkMiddleware(
                            run_id=ctx.run_id,
                            events=ctx.events,
                            cancellation=ctx.cancellation,
                            agent_name="deep_research_agent",
                            is_subagent=True,
                            forced_parent_invocation_id=parent_mw.invocation_id,
                            forced_parent_agent_name=parent_mw.agent_name,
                            compare_restore=restore_cfg,
                        )
                    ],
                    "model": llm,
                },
                {
                    "name": "validate_agent",
                    "description": "分析結果の妥当性を検証するサブエージェントです。",
                    "system_prompt": compare_sub_agent3,
                    "tools": tools_compare,
                    "middleware": [
                        EventSinkMiddleware(
                            run_id=ctx.run_id,
                            events=ctx.events,
                            cancellation=ctx.cancellation,
                            agent_name="validate_agent",
                            is_subagent=True,
                            forced_parent_invocation_id=parent_mw.invocation_id,
                            forced_parent_agent_name=parent_mw.agent_name,
                            compare_restore=restore_cfg,
                        )
                    ],
                    "model": llm,
                },
                {
                    "name": "report_agent",
                    "description": "分析結果をまとめて報告するサブエージェントです。",
                    "system_prompt": compare_sub_agent_report,
                    "model": llm,
                    "middleware": [
                        EventSinkMiddleware(
                            run_id=ctx.run_id,
                            events=ctx.events,
                            cancellation=ctx.cancellation,
                            agent_name="report_agent",
                            is_subagent=True,
                            forced_parent_invocation_id=parent_mw.invocation_id,
                            forced_parent_agent_name=parent_mw.agent_name,
                            compare_restore=restore_cfg,
                        )
                    ],
                },
            ],
            debug=False,
        )

        # 仮想FSへ投入（前のステップで作成された成果物を全てマッピング）
        ast_a_text = (work_dir / "ast_a.ast.json").read_text(encoding="utf-8", errors="replace")
        ast_b_text = (work_dir / "ast_b.ast.json").read_text(encoding="utf-8", errors="replace")
        cache_path = Path(ctx.paths["cache_dir"]) / "embedding_cache.json"
        cache_text = cache_path.read_text(encoding="utf-8", errors="replace") if cache_path.exists() else "{}"
        template_text = draft_path.read_text(encoding="utf-8", errors="replace")

        files = {
            # AST
            "/ast_a.ast.json": create_file_data(ast_a_text),
            "/ast_b.ast.json": create_file_data(ast_b_text),
            # キャッシュ
            "/.embedding_cache.json": create_file_data(cache_text),
            # テンプレート
            "/template_draft.md": create_file_data(template_text),
        }

        # pre_analysis.json を追加（存在すれば）
        pre_analysis_path = work_dir / "pre_analysis.json"
        if pre_analysis_path.exists():
            files["/pre_analysis.json"] = create_file_data(
                pre_analysis_path.read_text(encoding="utf-8", errors="replace")
            )

        # initial_matching.json を追加（存在すれば）
        initial_matching_path = work_dir / "initial_matching.json"
        if initial_matching_path.exists():
            files["/initial_matching.json"] = create_file_data(
                initial_matching_path.read_text(encoding="utf-8", errors="replace")
            )

        # blueprint を追加（存在すれば）
        for bp_name in ["blueprint_a.json", "blueprint_b.json"]:
            bp_path = work_dir / bp_name
            if bp_path.exists():
                files[f"/{bp_name}"] = create_file_data(
                    bp_path.read_text(encoding="utf-8", errors="replace")
                )

        # 入力テキスト（doc_a.txt, doc_b.txt）を追加（存在すれば）
        for doc_name in ["doc_a.txt", "doc_b.txt"]:
            # work_dirにコピーされている場合
            doc_path = work_dir / doc_name
            if doc_path.exists():
                files[f"/{doc_name}"] = create_file_data(
                    doc_path.read_text(encoding="utf-8", errors="replace")
                )
            else:
                # input_dirにある場合
                input_doc_path = input_dir / doc_name
                if input_doc_path.exists():
                    files[f"/{doc_name}"] = create_file_data(
                        input_doc_path.read_text(encoding="utf-8", errors="replace")
                    )

        query = "\n".join(
            [
                "次の2つの文書（原文テキスト）について分析し、日本語で報告してください。",
                "- docA: doc_a.txt（原文）",
                "- docB: doc_b.txt（原文）",
                "",
                "補助情報として ast_a.ast.json / ast_b.ast.json を参照できますが、",
                "ASTは各ドキュメントを独立に構造化した参考情報であり、",
                "同一構成でも階層のズレが起こり得ます。ASTは補助として扱い、",
                "根拠は原文テキスト（doc_a.txt / doc_b.txt）を優先してください。",
                "",
                "# 分析観点",
                "以下に記載されているテンプレートファイル `template_draft.md` を埋めてください。",
                "テンプレートは最後に一括で更新せず、段階的に編集してください。",
                "最後にチェックリストもあるので漏れなく確認して埋めてください。",
                "チェックがつけられない場合は、なぜチェックできないか根拠を記載したうえで、代替となる観点でチェックを行ってください。",
                "",
                "最終成果物は `template_filled.md` として出力してください。",
            ]
        )

        result = _run_with_cancellation(
            ctx,
            label="compare_analysis_agent",
            func=lambda: agent.invoke({"messages": [{"role": "user", "content": query}], "files": files}),
        )

        # 仮想FSから filled を抽出（無ければ draft を採用）
        vfiles = result.get("files") or {}
        content: Optional[str] = None
        for key in ["/template_filled.md", "template_filled.md", "/out/template_filled.md"]:
            fd = vfiles.get(key)
            if isinstance(fd, dict) and "content" in fd:
                c = fd["content"]
                content = "\n".join(c) if isinstance(c, list) else str(c)
                break
        if content is None:
            fd = vfiles.get("/template_draft.md")
            if isinstance(fd, dict) and "content" in fd:
                c = fd["content"]
                content = "\n".join(c) if isinstance(c, list) else str(c)

        if not content:
            # 最低限フォールバック
            content = template_text + "\n\n> Compare-Analysisの出力を取得できませんでした。\n"

        filled_path.write_text(content, encoding="utf-8")
        ctx.events.emit(
            ctx.run_id,
            "artifact_updated",
            {"ts": _utcnow_iso(), "kind": "template_filled", "path": filled_path.relative_to(run_dir).as_posix()},
        )
