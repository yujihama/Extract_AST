from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from compare_app.core.pipeline import RunContext


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


@dataclass
class EnsureTextStep:
    """docA/docB の入力を txt に正規化して work_dir に配置する。

    - 入力がTXT: work/doc_{a|b}.txt にコピー
    - 入力がPDF: paramsで指定された方式でPDF→TXTし、workへ配置
    """

    name: str
    which: str  # "a" or "b"

    def should_run(self, ctx: RunContext) -> bool:
        # 既に work/doc_{a|b}.txt があればスキップ（forceで上書き）
        work_dir = Path(ctx.paths["work_dir"])
        out_txt = work_dir / f"doc_{self.which.lower()}.txt"
        force = bool(ctx.params.get("force", False))
        return force or (not out_txt.exists())

    def run(self, ctx: RunContext) -> None:
        which = self.which.lower()
        run_dir = Path(ctx.paths["run_dir"])
        input_dir = Path(ctx.paths["input_dir"])
        work_dir = Path(ctx.paths["work_dir"])
        work_dir.mkdir(parents=True, exist_ok=True)

        # add_input() が doc_{a|b}.{ext} を作る前提
        src_txt = input_dir / f"doc_{which}.txt"
        src_pdf = input_dir / f"doc_{which}.pdf"
        out_txt = work_dir / f"doc_{which}.txt"

        if src_txt.exists():
            shutil.copy2(src_txt, out_txt)
            rel = out_txt.relative_to(run_dir).as_posix()
            ctx.events.emit(ctx.run_id, "artifact_updated", {"ts": _utcnow_iso(), "kind": f"txt_{which}", "path": rel})
            return

        if not src_pdf.exists():
            raise FileNotFoundError(f"input not found for doc_{which}: {src_txt} / {src_pdf}")

        pdf_mode = str(ctx.params.get(f"pdf_mode_{which}") or ctx.params.get("pdf_mode") or "fast").lower()
        if pdf_mode not in {"fast", "llm"}:
            pdf_mode = "fast"

        if pdf_mode == "fast":
            # 既存 convert_pdf_to_txt は input_dir + target_file を前提としている
            from src.utils import convert_pdf_to_txt

            result = convert_pdf_to_txt(target_file=src_pdf.name, input_dir=str(input_dir))
            produced_path = Path(result.get("output_path") or "")
            if not produced_path.exists():
                raise FileNotFoundError(f"convert_pdf_to_txt did not produce: {produced_path}")
            shutil.copy2(produced_path, out_txt)
            ctx.events.emit(
                ctx.run_id,
                "pdf_converted",
                {"ts": _utcnow_iso(), "which": which, "mode": "fast", "output_path": str(out_txt)},
            )
            rel = out_txt.relative_to(run_dir).as_posix()
            ctx.events.emit(ctx.run_id, "artifact_updated", {"ts": _utcnow_iso(), "kind": f"txt_{which}", "path": rel})
            return

        # LLMモード
        from src.pdf_to_text_llm import convert_pdf_with_llm

        start_page = int(ctx.params.get(f"pdf_{which}_start_page") or ctx.params.get("pdf_start_page") or 1)
        end_page = ctx.params.get(f"pdf_{which}_end_page") or ctx.params.get("pdf_end_page")
        end_page_i: Optional[int] = int(end_page) if end_page not in (None, "", 0) else None
        batch_size = int(ctx.params.get(f"pdf_{which}_batch_size") or ctx.params.get("pdf_batch_size") or 5)
        use_image = bool(ctx.params.get(f"pdf_{which}_use_image") or ctx.params.get("pdf_use_image") or False)

        model = str(
            ctx.params.get(f"pdf_llm_model_{which}")
            or ctx.params.get("pdf_llm_model")
            or ctx.params.get("llm_complex_model")
            or "gpt-5-mini"
        )
        asyncio.run(
            convert_pdf_with_llm(
                pdf_path=str(src_pdf),
                output_path=str(out_txt),
                start_page=start_page,
                end_page=end_page_i,
                batch_size=batch_size,
                use_image=use_image,
                model=model,
                verbose=False,
            )
        )
        if not out_txt.exists():
            raise FileNotFoundError(f"convert_pdf_with_llm did not produce: {out_txt}")
        ctx.events.emit(
            ctx.run_id,
            "pdf_converted",
            {"ts": _utcnow_iso(), "which": which, "mode": "llm", "output_path": str(out_txt)},
        )
        rel = out_txt.relative_to(run_dir).as_posix()
        ctx.events.emit(ctx.run_id, "artifact_updated", {"ts": _utcnow_iso(), "kind": f"txt_{which}", "path": rel})


@dataclass
class BuildBlueprintStep:
    """doc_{a|b}.txt から blueprint_{a|b}.json を生成する（LLM）。"""

    name: str
    which: str  # "a" or "b"

    def should_run(self, ctx: RunContext) -> bool:
        work_dir = Path(ctx.paths["work_dir"])
        out_path = work_dir / f"blueprint_{self.which.lower()}.json"
        force = bool(ctx.params.get("force", False))
        return force or (not out_path.exists())

    def run(self, ctx: RunContext) -> None:
        which = self.which.lower()
        run_dir = Path(ctx.paths["run_dir"])
        work_dir = Path(ctx.paths["work_dir"])
        txt_path = work_dir / f"doc_{which}.txt"
        if not txt_path.exists():
            raise FileNotFoundError(str(txt_path))

        out_path = work_dir / f"blueprint_{which}.json"

        # LLMキー未設定の場合は明示的に失敗（silent skipしない）
        if not (os.getenv("OPENAI_API_KEY") or os.getenv("AZURE_OPENAI_API_KEY")):
            raise RuntimeError("LLM API key not set (OPENAI_API_KEY or AZURE_OPENAI_API_KEY)")

        from deepagents import create_deep_agent
        from deepagents.backends.utils import create_file_data

        from src.schema import DocumentStructureBlueprint
        from src.prompt import blueprint_ast_builder_prompt, blueprint_validate_prompt
        from src.tools import (
            extract_regex_matches,
            get_file_length,
            preview_blueprint_headings,
            read_text_file,
            read_text_segment,
            validate_blueprint,
        )
        from src.utils import build_llm

        # アプリ側イベントミドルウェア
        from compare_app.agents.middleware import EventSinkMiddleware

        llm = build_llm()
        llm_complex = build_llm(model=str(ctx.params.get("llm_complex_model", "gpt-5-mini")))

        # data/input固定依存を避けるため、run入力（data/runs/{run_id}/input）を読む analyze_visual_contents を提供する
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
                    fallback = input_dir / f"doc_{which}.pdf"
                    if fallback.exists():
                        pdf_path = fallback
                    else:
                        return f"[analyze_visual_contents] pdf not found for: {document_name} (run has no PDF inputs)."

            images = _get_pdf_page_as_image(pdf_path, page_numbers)
            b64_data_list = [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{item['base64_data']}"}}
                for item in images
            ]
            message = HumanMessage(content=[{"type": "text", "text": prompt}, *b64_data_list])
            response = llm.invoke([message])
            return response.content

        tools_blueprint_builder = [read_text_segment, read_text_file, extract_regex_matches, get_file_length, analyze_visual_contents]
        tools_blueprint_validator = [
            read_text_segment,
            read_text_file,
            extract_regex_matches,
            get_file_length,
            preview_blueprint_headings,
            validate_blueprint,
            analyze_visual_contents,
        ]

        agent = create_deep_agent(
            model=llm_complex,
            tools=tools_blueprint_builder,
            system_prompt=blueprint_ast_builder_prompt,
            response_format=DocumentStructureBlueprint,
            middleware=[
                EventSinkMiddleware(
                    run_id=ctx.run_id,
                    events=ctx.events,
                    cancellation=ctx.cancellation,
                    agent_name=f"blueprint_builder_{which}",
                )
            ],
            subagents=[
                {
                    "name": "validate_blueprint_agent",
                    "description": "blueprintを複数の観点で検証して必要に応じて修正します。blueprintのパスを指示してください。",
                    "system_prompt": blueprint_validate_prompt,
                    "tools": tools_blueprint_validator,
                    "middleware": [
                        EventSinkMiddleware(
                            run_id=ctx.run_id,
                            events=ctx.events,
                            cancellation=ctx.cancellation,
                            agent_name=f"validate_blueprint_agent_{which}",
                            is_subagent=True,
                        )
                    ],
                    "model": llm,
                }
            ],
            debug=False,
        )

        txt_name = f"doc_{which}.txt"
        files = {f"/{txt_name}": create_file_data(_safe_read_text(txt_path))}
        query = f"ファイル '{txt_name}' を解析してください。文書内のすべての見出しパターンを自律的に検出して、抽出するためのblueprintを生成してください。"
        result = agent.invoke({"messages": [{"role": "user", "content": query}], "files": files})
        blueprint = result.get("structured_response")
        if blueprint is None:
            raise RuntimeError("blueprint agent returned no structured_response")

        # 永続化
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(blueprint.model_dump(), f, ensure_ascii=False, indent=2)

        rel = out_path.relative_to(run_dir).as_posix()
        ctx.events.emit(ctx.run_id, "artifact_updated", {"ts": _utcnow_iso(), "kind": f"blueprint_{which}", "path": rel})


@dataclass
class BuildAstStep:
    """blueprint_{a|b}.json と doc_{a|b}.txt から AST を生成する（非LLM）。"""

    name: str
    which: str  # "a" or "b"

    def should_run(self, ctx: RunContext) -> bool:
        work_dir = Path(ctx.paths["work_dir"])
        out_ast = work_dir / f"ast_{self.which.lower()}.ast.json"
        force = bool(ctx.params.get("force", False))
        return force or (not out_ast.exists())

    def run(self, ctx: RunContext) -> None:
        which = self.which.lower()
        run_dir = Path(ctx.paths["run_dir"])
        work_dir = Path(ctx.paths["work_dir"])
        txt_path = work_dir / f"doc_{which}.txt"
        bp_path = work_dir / f"blueprint_{which}.json"
        if not txt_path.exists():
            raise FileNotFoundError(str(txt_path))
        if not bp_path.exists():
            raise FileNotFoundError(str(bp_path))

        out_ast = work_dir / f"ast_{which}.ast.json"

        from src import blueprint_ast_builder

        blueprint_ast_builder.build_ast_from_blueprint(
            blueprint_path=str(bp_path),
            text_path=str(txt_path),
            out_ast_path=str(out_ast),
            root_title=f"doc_{which}",
            max_content_chars_per_node=int(ctx.params.get("max_content_chars_per_node", 2000)),
        )

        rel = out_ast.relative_to(run_dir).as_posix()
        ctx.events.emit(ctx.run_id, "artifact_updated", {"ts": _utcnow_iso(), "kind": f"ast_{which}", "path": rel})


@dataclass
class SummarizeAstStep:
    """ASTの非leafノードに content_summary を付与する（LLM）。"""

    name: str
    which: str  # "a" or "b"

    def should_run(self, ctx: RunContext) -> bool:
        if not bool(ctx.params.get("summarize_ast", False)):
            return False
        # 実装は in-place 更新だが、繰り返し実行しても skip_if_summary_exists で概ね冪等
        return True

    def run(self, ctx: RunContext) -> None:
        if not bool(ctx.params.get("summarize_ast", False)):
            return
        which = self.which.lower()
        run_dir = Path(ctx.paths["run_dir"])
        work_dir = Path(ctx.paths["work_dir"])
        ast_path = work_dir / f"ast_{which}.ast.json"
        if not ast_path.exists():
            raise FileNotFoundError(str(ast_path))

        from src.ast_llm_summarizer import SummarizeOptions, SummarizationCancelled, summarize_ast_inplace

        model = str(ctx.params.get("ast_summary_model") or ctx.params.get("llm_complex_model") or "gpt-5-mini")
        overwrite = bool(ctx.params.get("ast_summary_overwrite", False) or ctx.params.get("force", False))
        opts = SummarizeOptions(model=model, skip_if_summary_exists=not overwrite)
        try:
            summarize_ast_inplace(ast_path=str(ast_path), options=opts, is_cancelled=ctx.cancellation.is_cancelled)
        except SummarizationCancelled:
            from compare_app.core.pipeline import CancelledError

            raise CancelledError("cancelled during ast summarization")

        rel = ast_path.relative_to(run_dir).as_posix()
        ctx.events.emit(ctx.run_id, "artifact_updated", {"ts": _utcnow_iso(), "kind": f"ast_{which}", "path": rel})
        ctx.events.emit(ctx.run_id, "ast_summarized", {"ts": _utcnow_iso(), "which": which, "model": model})

