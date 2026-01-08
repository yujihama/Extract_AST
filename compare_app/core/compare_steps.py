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
            read_ast,
        )
        from src.utils import build_llm

        from compare_app.agents.middleware import EventSinkMiddleware

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
            compare_get_grouping,
            compare_search_by_keyphrase,
            compare_get_chunk,
            compare_specified_chunks_diff,
            compare_specified_chunks_llm,
            analyze_visual_contents,
        ]

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
                )
            ],
            debug=False,
        )

        # deep_agentのfileツールも使えるように、AST/キャッシュを仮想FSにも投入しておく（任意）
        cache_path = Path(ctx.paths["cache_dir"]) / "embedding_cache.json"
        cache_text = cache_path.read_text(encoding="utf-8", errors="replace") if cache_path.exists() else "{}"
        files = {
            "/ast_a.ast.json": create_file_data(ast_a.read_text(encoding="utf-8", errors="replace")),
            "/ast_b.ast.json": create_file_data(ast_b.read_text(encoding="utf-8", errors="replace")),
            "/.embedding_cache.json": create_file_data(cache_text),
        }

        query = "\n".join(
            [
                "次の2つのAST文書（*.ast.json）の関係性を分析してください。また重点比較観点を分析するための具体的なプランを策定してください。",
                "",
                "- docA: ast_a.ast.json",
                "- docB: ast_b.ast.json",
                "",
                "**重点比較観点**",
                "- 変更箇所とその影響の特定",
                "",
                "プランは概要レベルではなく、具体的かつ単純なタスクまで細分化・具体化して回答してください。",
                "また網羅的に比較ができたことを確認するための検証ステップもプランには含めてください。",
                "",
                "さらに、上記プランに基づいて結果を記入するためのMarkdownテンプレートも作成してください。",
                "テンプレートは `template_draft.md` として出力してください。",
                "（可能なら段階的に編集しながら作成してください）",
            ]
        )

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

        out_draft.write_text(template_text, encoding="utf-8")
        out_meta.write_text(
            json.dumps(
                {
                    "relation": getattr(structured, "relation", None),
                    "reason": getattr(structured, "reason", None),
                    "plan": getattr(structured, "plan", None),
                    "template_name": getattr(structured, "template", None),
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


@dataclass
class CompareAnalysisStep:
    """Pre-Analysisで作ったテンプレを埋めて、out/template_filled.md を作る。"""

    name: str = "compare_analysis"

    def should_run(self, ctx: RunContext) -> bool:
        out_dir = Path(ctx.paths["out_dir"])
        filled = out_dir / "template_filled.md"
        force = bool(ctx.params.get("force", False))
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
            read_ast,
        )
        from src.utils import build_llm

        from compare_app.agents.middleware import EventSinkMiddleware

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
            compare_get_grouping,
            compare_search_by_keyphrase,
            compare_get_chunk,
            compare_specified_chunks_diff,
            compare_specified_chunks_llm,
            analyze_visual_contents,
        ]

        agent = create_deep_agent(
            model=llm_complex,
            system_prompt=compare_parent_agent_prompt,
            middleware=[
                EventSinkMiddleware(
                    run_id=ctx.run_id,
                    events=ctx.events,
                    cancellation=ctx.cancellation,
                    agent_name="compare_parent",
                )
            ],
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
                        )
                    ],
                },
            ],
            debug=False,
        )

        # 仮想FSへ投入
        ast_a = (work_dir / "ast_a.ast.json").read_text(encoding="utf-8", errors="replace")
        ast_b = (work_dir / "ast_b.ast.json").read_text(encoding="utf-8", errors="replace")
        cache_path = Path(ctx.paths["cache_dir"]) / "embedding_cache.json"
        cache_text = cache_path.read_text(encoding="utf-8", errors="replace") if cache_path.exists() else "{}"
        template_text = draft_path.read_text(encoding="utf-8", errors="replace")

        files = {
            "/ast_a.ast.json": create_file_data(ast_a),
            "/ast_b.ast.json": create_file_data(ast_b),
            "/.embedding_cache.json": create_file_data(cache_text),
            "/template_draft.md": create_file_data(template_text),
        }

        query = "\n".join(
            [
                "次の2つの文書（*.ast.json）について分析し、日本語で報告してください。",
                "- docA: ast_a.ast.json",
                "- docB: ast_b.ast.json",
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
