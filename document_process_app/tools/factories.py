from __future__ import annotations

import base64
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from document_process_app.core.pipeline import RunContext


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _have_llm_key() -> bool:
    return bool(os.getenv("OPENAI_API_KEY") or os.getenv("AZURE_OPENAI_API_KEY") or os.getenv("GOOGLE_API_KEY"))


def _normalize_doc_id(raw: Any, fallback: str) -> str:
    text = str(raw).strip() if raw is not None else ""
    if not text:
        return fallback
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", text)
    return safe or fallback


def _resolve_doc_path(
    doc_id: str,
    key: str,
    docs_by_id: dict[str, dict[str, Any]],
    run_dir: Path,
    work_dir: Path,
) -> Optional[Path]:
    doc = docs_by_id.get(doc_id)
    if not doc:
        return None
    paths = doc.get("paths") or {}
    raw = paths.get(key)
    if raw:
        p = Path(str(raw))
        return p if p.is_absolute() else (run_dir / p)
    fallback_map = {
        "txt": f"doc_{doc_id}.txt",
        "ast": f"ast_{doc_id}.ast.json",
        "blueprint": f"blueprint_{doc_id}.json",
    }
    fallback = fallback_map.get(key)
    return work_dir / fallback if fallback else None


def _safe_pair_key(a_id: str, b_id: str) -> str:
    safe_a = _normalize_doc_id(a_id, a_id)
    safe_b = _normalize_doc_id(b_id, b_id)
    return f"{safe_a}_{safe_b}"


def _ensure_compare_state_for_pair(
    ctx: RunContext,
    *,
    docs_by_id: dict[str, dict[str, Any]],
    run_dir: Path,
    work_dir: Path,
    doc_a_id: str,
    doc_b_id: str,
) -> tuple[str, Path]:
    """
    指定ペアの比較状態が現在スレッドで初期化されていることを保証する。

    方針（案A）:
    - ペアは work/pairs/<a>_<b> に一意に解決する（dirを引数で渡さない）
    - compare_* はグローバル状態（COMPARE_STATE）に依存しているため、
      必要ならこのスレッドで compare_setup を再実行して復元する。
    - initial_matching は pair_dir/initial_matching.json を優先して読み込む。
    """
    a_id = str(doc_a_id or "").strip()
    b_id = str(doc_b_id or "").strip()
    if not a_id or not b_id or a_id == b_id:
        raise ValueError("doc_a_id and doc_b_id must be different and non-empty")
    if a_id not in docs_by_id or b_id not in docs_by_id:
        raise ValueError("unknown doc_id for compare tools")

    pair_dir = work_dir / "pairs" / _safe_pair_key(a_id, b_id)
    if not pair_dir.exists():
        # pair_compare_setup が未実行（または失敗）とみなす
        raise FileNotFoundError(f"pair_dir not found. run pair_compare_setup first: {pair_dir}")

    cache_path = pair_dir / "embedding_cache.json"
    initial_matching_path = pair_dir / "initial_matching.json"

    ast_a_path = _resolve_doc_path(a_id, "ast", docs_by_id, run_dir, work_dir)
    ast_b_path = _resolve_doc_path(b_id, "ast", docs_by_id, run_dir, work_dir)
    doc_a_txt = _resolve_doc_path(a_id, "txt", docs_by_id, run_dir, work_dir)
    doc_b_txt = _resolve_doc_path(b_id, "txt", docs_by_id, run_dir, work_dir)

    if not ast_a_path or not ast_a_path.exists():
        raise FileNotFoundError(str(ast_a_path) if ast_a_path else f"ast not found: {a_id}")
    if not ast_b_path or not ast_b_path.exists():
        raise FileNotFoundError(str(ast_b_path) if ast_b_path else f"ast not found: {b_id}")

    # compare_* tools は src.tools.COMPARE_STATE（スレッドローカル）に依存する。
    # tool が別スレッドで実行される場合があるため、各 tool call の中で必ず復元できるようにする。
    from src.tools import COMPARE_STATE, compare_setup as compare_setup_tool

    wanted_marker = f"{ctx.run_id}:{pair_dir.resolve()}"
    current_marker = str(COMPARE_STATE.get("_pair_marker") or "")

    if current_marker != wanted_marker or COMPARE_STATE.get("ast_a") is None:
        # このスレッドで compare_setup を実行して状態を初期化（cacheがあれば高速）
        # NOTE: compare_setup は内部で COMPARE_STATE.clear() を行う
        compare_setup_tool.invoke(
            {
                "docA": str(ast_a_path),
                "docB": str(ast_b_path),
                "docA_txt": str(doc_a_txt) if doc_a_txt and doc_a_txt.exists() else None,
                "docB_txt": str(doc_b_txt) if doc_b_txt and doc_b_txt.exists() else None,
                "embedding_model": ctx.params.get("embedding_model"),
                "cache_path": str(cache_path),
                "batch_size": int(ctx.params.get("embedding_batch_size", 64)),
            }
        )
        COMPARE_STATE["_run_id"] = ctx.run_id
        COMPARE_STATE["_pair_marker"] = wanted_marker

    # initial_matching をペア成果物から復元（存在すれば）
    if initial_matching_path.exists():
        try:
            initial_text = initial_matching_path.read_text(encoding="utf-8", errors="replace")
            initial_data = json.loads(initial_text)
            if isinstance(initial_data, dict):
                COMPARE_STATE["initial_matching"] = initial_data
        except Exception:
            pass

    return wanted_marker, pair_dir


def _get_pdf_page_as_image(pdf_path: Path, page_numbers: list[int], dpi: int = 150) -> list[dict]:
    import fitz  # PyMuPDF

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


def make_analyze_visual_contents_tool(
    base_dir: Path,
    llm,
    *,
    allow_any_pdf_fallback: bool = False,
    fallback_pdf_name: Optional[str] = None,
    error_on_missing: bool = True,
    message_mode: str = "multi",
    include_page_labels: bool = True,
):
    from langchain_core.messages import HumanMessage
    from langchain_core.tools import tool

    base_dir = Path(base_dir)
    mode = str(message_mode or "").strip().lower()
    use_single_message = mode == "single"

    def _resolve_pdf_path(document_name: str) -> Optional[Path]:
        name = str(document_name or "").strip()
        if name.endswith(".ast.json"):
            name = name.replace(".ast.json", ".pdf")
        elif name.endswith(".txt"):
            name = name.replace(".txt", ".pdf")
        if name:
            p = Path(name)
            if p.is_absolute() and p.exists():
                return p
            cand = base_dir / name
            if cand.exists():
                return cand
        if fallback_pdf_name:
            fallback = base_dir / fallback_pdf_name
            if fallback.exists():
                return fallback
        if allow_any_pdf_fallback:
            pdf_candidates = sorted(base_dir.glob("*.pdf"))
            if pdf_candidates:
                return pdf_candidates[0]
        return None

    @tool
    def analyze_visual_contents(document_name: str, page_numbers: list[int], prompt: str) -> str:
        """
        Run入力のPDF（data/runs/{run_id}/input）を対象に、指定ページを画像として分析して返す。
        document_name が .txt/.ast.json の場合は .pdf に置換して探索する。
        """
        pdf_path = _resolve_pdf_path(document_name)
        if not pdf_path:
            if error_on_missing:
                raise FileNotFoundError(f"pdf not found: {document_name}")
            return f"[analyze_visual_contents] pdf not found for: {document_name} (run has no PDF inputs)."

        pages = _get_pdf_page_as_image(pdf_path, page_numbers, dpi=150)
        if not pages:
            if error_on_missing:
                raise RuntimeError("failed to render pdf pages")
            return "[analyze_visual_contents] failed to render pdf pages"

        prompt_text = prompt or "PDFの視覚情報を分析し、表や図の内容を説明してください。"
        if use_single_message:
            content = [{"type": "text", "text": prompt_text}]
            for page in pages:
                content.append(
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{page['base64_data']}"}}
                )
                if include_page_labels:
                    content.append({"type": "text", "text": f"page {page['page_number']}"})
            resp = llm.invoke([HumanMessage(content=content)])
        else:
            messages = [HumanMessage(content=prompt_text)]
            for page in pages:
                page_content = [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{page['base64_data']}"}},
                ]
                if include_page_labels:
                    page_content.append({"type": "text", "text": f"page {page['page_number']}"})
                messages.append(HumanMessage(content=page_content))
            resp = llm.invoke(messages)

        return getattr(resp, "content", None) or str(resp)

    return analyze_visual_contents


def _build_pair_compare_setup_tool(
    ctx: RunContext,
    docs_by_id: dict[str, dict[str, Any]],
    run_dir: Path,
    work_dir: Path,
):
    from langchain_core.tools import tool

    @tool
    def pair_compare_setup(doc_a_id: str, doc_b_id: str, purpose: str = "") -> str:
        """指定ペアの比較準備（compare_setup＋初期マッチング保存）"""
        a_id = str(doc_a_id or "").strip()
        b_id = str(doc_b_id or "").strip()
        if not a_id or not b_id or a_id == b_id:
            raise ValueError("doc_a_id and doc_b_id must be different and non-empty")
        if a_id not in docs_by_id or b_id not in docs_by_id:
            raise ValueError("unknown doc_id for pair_compare_setup")

        if not _have_llm_key():
            raise RuntimeError("Embedding/LLM API key not set (OPENAI_API_KEY or AZURE_OPENAI_API_KEY or GOOGLE_API_KEY)")

        safe_a = _normalize_doc_id(a_id, a_id)
        safe_b = _normalize_doc_id(b_id, b_id)

        pairs_dir = work_dir / "pairs" / f"{safe_a}_{safe_b}"
        pairs_dir.mkdir(parents=True, exist_ok=True)

        # NOTE:
        # Windows + tool並列実行で work_dir/doc_<doc_id>.txt 等の上書きが WinError 32 を起こしやすい。
        # ここでは shared な一時ファイルは作らず、元の doc_{id} / ast_{id} を直接 compare_setup に渡す。
        # キャッシュもペアごとに分離してファイルロック競合を避ける。

        doc_a_txt = _resolve_doc_path(a_id, "txt", docs_by_id, run_dir, work_dir)
        doc_b_txt = _resolve_doc_path(b_id, "txt", docs_by_id, run_dir, work_dir)
        ast_a_path = _resolve_doc_path(a_id, "ast", docs_by_id, run_dir, work_dir)
        ast_b_path = _resolve_doc_path(b_id, "ast", docs_by_id, run_dir, work_dir)
        if not doc_a_txt or not doc_a_txt.exists():
            raise FileNotFoundError(str(doc_a_txt) if doc_a_txt else f"doc txt not found: {a_id}")
        if not doc_b_txt or not doc_b_txt.exists():
            raise FileNotFoundError(str(doc_b_txt) if doc_b_txt else f"doc txt not found: {b_id}")
        if not ast_a_path or not ast_a_path.exists():
            raise FileNotFoundError(str(ast_a_path) if ast_a_path else f"ast not found: {a_id}")
        if not ast_b_path or not ast_b_path.exists():
            raise FileNotFoundError(str(ast_b_path) if ast_b_path else f"ast not found: {b_id}")

        cache_path = pairs_dir / "embedding_cache.json"

        from src.tools import COMPARE_STATE, compare_all_chunk_similarity_matching, compare_setup

        try:
            COMPARE_STATE.clear()
        except Exception:
            pass

        setup_json = compare_setup.invoke(
            {
                "docA": str(ast_a_path),
                "docB": str(ast_b_path),
                "docA_txt": str(doc_a_txt),
                "docB_txt": str(doc_b_txt),
                "embedding_model": ctx.params.get("embedding_model"),
                "cache_path": str(cache_path),
                "batch_size": int(ctx.params.get("embedding_batch_size", 64)),
            }
        )

        initial_matching_json = compare_all_chunk_similarity_matching.invoke(
            {
                "top_k": int(ctx.params.get("match_top_k", 3)),
                "alpha": float(ctx.params.get("match_alpha", 0.3)),
                "beta": float(ctx.params.get("match_beta", 0.4)),
                "min_score": float(ctx.params.get("match_min_score", 0.25)),
            }
        )

        initial_matching_path = pairs_dir / "initial_matching.json"
        initial_matching_path.write_text(str(initial_matching_json), encoding="utf-8")

        try:
            ctx.events.emit(
                ctx.run_id,
                "pair_setup_done",
                {
                    "ts": _utcnow_iso(),
                    "a": a_id,
                    "b": b_id,
                    "purpose": str(purpose or ""),
                    "pair_dir": pairs_dir.relative_to(run_dir).as_posix(),
                },
            )
        except Exception:
            pass

        try:
            ctx.events.emit(
                ctx.run_id,
                "artifact_updated",
                {
                    "ts": _utcnow_iso(),
                    "kind": "pair_initial_matching",
                    "path": initial_matching_path.relative_to(run_dir).as_posix(),
                },
            )
        except Exception:
            pass

        if cache_path.exists():
            try:
                ctx.events.emit(
                    ctx.run_id,
                    "artifact_updated",
                    {"ts": _utcnow_iso(), "kind": "embedding_cache", "path": cache_path.relative_to(run_dir).as_posix()},
                )
            except Exception:
                pass

        # ドキュメントペア成果物として永続化（次runで自動再利用できるようにする）
        doc_a_hash = docs_by_id.get(a_id, {}).get("doc_hash")
        doc_b_hash = docs_by_id.get(b_id, {}).get("doc_hash")
        if doc_a_hash and doc_b_hash:
            try:
                from document_process_app.infra.document_store import DocumentPairRepository

                pair_repo = DocumentPairRepository()

                matching_data = None
                try:
                    matching_data = json.loads(initial_matching_json)
                except Exception:
                    if isinstance(COMPARE_STATE.get("initial_matching"), dict):
                        matching_data = COMPARE_STATE.get("initial_matching")
                if isinstance(matching_data, dict):
                    pair_repo.save_matching(str(doc_a_hash), str(doc_b_hash), matching_data)

                if cache_path.exists():
                    try:
                        cache_data = json.loads(cache_path.read_text(encoding="utf-8"))
                        if isinstance(cache_data, dict):
                            pair_repo.save_embedding_cache(str(doc_a_hash), str(doc_b_hash), cache_data)
                    except Exception:
                        pass
            except Exception:
                # pair_compare_setup 自体は成功しているので、永続化失敗は致命にしない
                pass

        return json.dumps(
            {
                "ok": True,
                "a": a_id,
                "b": b_id,
                "purpose": str(purpose or ""),
                "pair_dir": pairs_dir.relative_to(run_dir).as_posix(),
                "initial_matching_path": initial_matching_path.relative_to(run_dir).as_posix(),
                "cache_path": cache_path.relative_to(run_dir).as_posix() if cache_path.exists() else "",
                "setup_json_preview": str(setup_json)[:300],
            },
            ensure_ascii=False,
        )

    return pair_compare_setup


def _build_pair_scoped_compare_tools(
    ctx: RunContext,
    docs_by_id: dict[str, dict[str, Any]],
    run_dir: Path,
    work_dir: Path,
):
    """
    compare_* をペア指定（doc_a_id/doc_b_id）で必ず動くようにするツール群を返す。

    - 呼び出し側は dir を渡さず、doc_id のペアだけ渡す
    - 内部でペアdirを一意に解決し、必要なら compare_setup を復元する
    """
    from langchain_core.tools import tool

    from src.tools import (
        compare_all_chunk_similarity_matching as _match_all,
        compare_get_chunk as _get_chunk,
        compare_get_grouping as _get_grouping,
        compare_specified_chunks_diff as _specified_diff,
        compare_specified_chunks_llm as _specified_llm,
        COMPARE_STATE,
    )

    def _invoke_tool(tool_func, **kwargs):
        if hasattr(tool_func, "invoke"):
            return tool_func.invoke(kwargs)
        return tool_func(**kwargs)

    @tool
    def compare_all_chunk_similarity_matching(
        doc_a_id: str,
        doc_b_id: str,
        top_k: int = 3,
        alpha: float = 0.3,
        beta: float = 0.4,
        min_score: float = 0.25,
    ) -> str:
        """指定ペアで類似度マッチングを実行（必要なら compare_setup を復元）。"""
        try:
            _, pair_dir = _ensure_compare_state_for_pair(
                ctx,
                docs_by_id=docs_by_id,
                run_dir=run_dir,
                work_dir=work_dir,
                doc_a_id=doc_a_id,
                doc_b_id=doc_b_id,
            )
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False, indent=2)
        res = _invoke_tool(_match_all, top_k=top_k, alpha=alpha, beta=beta, min_score=min_score)
        # last_matching をペアdirへ永続化（スレッドをまたいでも参照できるように）
        try:
            last = COMPARE_STATE.get("last_matching")
            if isinstance(last, dict):
                (pair_dir / "last_matching.json").write_text(
                    json.dumps(last, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
        except Exception:
            pass
        return res

    @tool
    def compare_get_grouping(
        doc_a_id: str,
        doc_b_id: str,
        which: str,
        mode: str = "summary",
        chunk_ids: Optional[list[str]] = None,
        min_similarity: Optional[float] = None,
        max_groups: int = 50,
        max_matches: int = 1,
    ) -> str:
        """指定ペアのグルーピング結果を取得（必要なら compare_setup を復元）。"""
        try:
            _, pair_dir = _ensure_compare_state_for_pair(
                ctx,
                docs_by_id=docs_by_id,
                run_dir=run_dir,
                work_dir=work_dir,
                doc_a_id=doc_a_id,
                doc_b_id=doc_b_id,
            )
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False, indent=2)
        # which=last の場合、まずペアdirの永続化を優先して復元（存在すれば）
        if str(which).lower().strip() == "last":
            last_path = pair_dir / "last_matching.json"
            if last_path.exists():
                try:
                    last = json.loads(last_path.read_text(encoding="utf-8", errors="replace"))
                    if isinstance(last, dict):
                        COMPARE_STATE["last_matching"] = last
                except Exception:
                    pass
        return _invoke_tool(
            _get_grouping,
            which=which,
            mode=mode,
            chunk_ids=chunk_ids,
            min_similarity=min_similarity,
            max_groups=max_groups,
            max_matches=max_matches,
        )

    @tool
    def compare_get_chunk(doc_a_id: str, doc_b_id: str, which: str, chunk_id: str) -> str:
        """指定ペアでチャンク内容を取得（必要なら compare_setup を復元）。"""
        try:
            _ensure_compare_state_for_pair(
                ctx,
                docs_by_id=docs_by_id,
                run_dir=run_dir,
                work_dir=work_dir,
                doc_a_id=doc_a_id,
                doc_b_id=doc_b_id,
            )
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False, indent=2)
        return _invoke_tool(_get_chunk, chunk_ids=[chunk_id], intent="compare_get_chunk", in_doc=which)

    @tool
    def compare_specified_chunks_diff(
        doc_a_id: str,
        doc_b_id: str,
        chunk_id_a: str,
        chunk_id_b: str,
        include_metadata: bool = True,
    ) -> str:
        """指定ペアで2チャンクの差分を取得（必要なら compare_setup を復元）。"""
        try:
            _ensure_compare_state_for_pair(
                ctx,
                docs_by_id=docs_by_id,
                run_dir=run_dir,
                work_dir=work_dir,
                doc_a_id=doc_a_id,
                doc_b_id=doc_b_id,
            )
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False, indent=2)
        return _invoke_tool(
            _specified_diff,
            chunk_id_a=chunk_id_a,
            chunk_id_b=chunk_id_b,
            include_metadata=include_metadata,
        )

    @tool
    def compare_specified_chunks_llm(
        doc_a_id: str,
        doc_b_id: str,
        chunk_id_a: str,
        chunk_id_b: str,
        focus: str = "",
    ) -> str:
        """指定ペアで2チャンクをLLMで比較（必要なら compare_setup を復元）。"""
        try:
            _ensure_compare_state_for_pair(
                ctx,
                docs_by_id=docs_by_id,
                run_dir=run_dir,
                work_dir=work_dir,
                doc_a_id=doc_a_id,
                doc_b_id=doc_b_id,
            )
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False, indent=2)
        return _invoke_tool(_specified_llm, chunk_id_a=chunk_id_a, chunk_id_b=chunk_id_b, focus=focus)

    return [
        compare_all_chunk_similarity_matching,
        compare_get_grouping,
        compare_get_chunk,
        compare_specified_chunks_diff,
        compare_specified_chunks_llm,
    ]

