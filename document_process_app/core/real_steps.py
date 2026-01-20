from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Tuple

from document_process_app.core.pipeline import RunContext


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _extract_last_ai_text(result: Any) -> str:
    """deepagents/langgraphのstate(dict)から最終AI出力テキストを取り出す。"""
    if isinstance(result, dict):
        msgs = result.get("messages") or []
        for m in reversed(list(msgs)):
            if hasattr(m, "__class__") and m.__class__.__name__ == "AIMessage":
                content = getattr(m, "content", None)
                if content:
                    if isinstance(content, str):
                        return content
                    if isinstance(content, dict):
                        text = content.get("text")
                        return str(text) if text else str(content)
                    if isinstance(content, list):
                        texts = []
                        for item in content:
                            if isinstance(item, dict):
                                if item.get("text"):
                                    texts.append(str(item.get("text")))
                            elif isinstance(item, str):
                                texts.append(item)
                        if texts:
                            return "\n".join(texts)
                    return str(content)
            if isinstance(m, dict) and (m.get("role") in {"assistant", "ai"}):
                c = m.get("content")
                if c:
                    if isinstance(c, str):
                        return c
                    if isinstance(c, dict):
                        text = c.get("text")
                        return str(text) if text else str(c)
                    if isinstance(c, list):
                        texts = []
                        for item in c:
                            if isinstance(item, dict):
                                if item.get("text"):
                                    texts.append(str(item.get("text")))
                            elif isinstance(item, str):
                                texts.append(item)
                        if texts:
                            return "\n".join(texts)
                    return str(c)
    return ""


def _extract_json_from_text(text: str) -> dict:
    """LLM出力からJSON部分を抽出してdictにする（前後に説明が混ざっても許容）。"""
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        # JSONDecodeError を上位へそのまま投げるとステップ失敗理由が曖昧になるので、
        # ここでも明示的にメッセージ化して返す。
        try:
            return json.loads(text[start : end + 1])
        except Exception as e:
            raise ValueError(f"Failed to parse JSON from extracted substring: {e}") from e
    raise ValueError("Failed to parse JSON from LLM response")


def _load_run_config(ctx: RunContext) -> dict[str, Any]:
    run_dir = Path(ctx.paths["run_dir"])
    config_path = run_dir / "config.json"
    if not config_path.exists():
        return {}
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _normalize_doc_id(raw: Any, fallback: str) -> str:
    text = str(raw).strip() if raw is not None else ""
    if not text:
        return fallback
    # ファイル名として安全な範囲に限定
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", text)
    return safe or fallback


def _normalize_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [str(value).strip()] if str(value).strip() else []


def _get_doc_filename(ctx: RunContext, doc_id: str) -> Optional[str]:
    for d in _get_documents(ctx):
        if str(d.get("doc_id")) == str(doc_id):
            return str(d.get("filename") or d.get("original_filename") or "") or None
    return None


def _select_ast_builder_strategy(
    ctx: RunContext,
    doc_id: str,
    *,
    text_path: Optional[Path] = None,
) -> Tuple[str, dict[str, Any]]:
    policy = str(ctx.params.get("ast_builder_policy", "auto") or "auto").lower().strip()
    max_chars = int(ctx.params.get("ast_bypass_max_chars", 5000) or 5000)
    force_bp = set(_normalize_string_list(ctx.params.get("ast_force_blueprint_doc_ids")))

    filename = _get_doc_filename(ctx, doc_id)
    ext = Path(filename).suffix.lower() if filename else ""
    char_count = None

    if doc_id in force_bp:
        return "blueprint", {"reason": "force_blueprint", "policy": policy, "filename": filename, "char_count": char_count}

    if policy in {"blueprint", "markdown", "llm_direct"}:
        return policy, {"reason": f"policy:{policy}", "policy": policy, "filename": filename, "char_count": char_count}

    if ext in {".md", ".markdown"}:
        return "markdown", {"reason": "ext_markdown", "policy": policy, "filename": filename, "char_count": char_count}

    if text_path and text_path.exists():
        try:
            char_count = len(_safe_read_text(text_path))
        except Exception:
            char_count = None

    if char_count is not None and char_count <= max_chars:
        return "llm_direct", {
            "reason": f"short_text<={max_chars}",
            "policy": policy,
            "filename": filename,
            "char_count": char_count,
        }

    return "blueprint", {"reason": "default", "policy": policy, "filename": filename, "char_count": char_count}


def _get_documents(ctx: RunContext) -> list[dict[str, Any]]:
    config = _load_run_config(ctx)
    docs = config.get("documents")
    if isinstance(docs, list) and docs:
        out: list[dict[str, Any]] = []
        for i, d in enumerate(docs, 1):
            if not isinstance(d, dict):
                continue
            doc_id = _normalize_doc_id(d.get("doc_id"), f"d{i}")
            item = {**d, "doc_id": doc_id}
            out.append(item)
        return out
    return []


def _get_doc_hash(ctx: RunContext, doc_id: str) -> Optional[str]:
    config = _load_run_config(ctx)
    docs = config.get("documents")
    if isinstance(docs, list):
        for d in docs:
            if isinstance(d, dict) and str(d.get("doc_id")) == doc_id:
                h = d.get("doc_hash")
                return str(h) if h else None
    # fallback: params
    key = f"doc_{doc_id}_hash"
    if ctx.params.get(key):
        return str(ctx.params.get(key))
    return None


@dataclass
class EnsureTextStep:
    """ドキュメント入力を txt に正規化して work_dir に配置する。

    - 入力がTXT: work/doc_{doc_id}.txt にコピー
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

        # テキスト系拡張子（例: .md）にも対応（テンプレ/FAQなど）
        # - pipelineの後続（blueprint/AST）は .txt 前提のため、work配下へ .txt として正規化する
        for ext in [".md", ".markdown", ".csv", ".json"]:
            cand = input_dir / f"doc_{which}{ext}"
            if cand.exists():
                shutil.copy2(cand, out_txt)
                rel = out_txt.relative_to(run_dir).as_posix()
                ctx.events.emit(
                    ctx.run_id,
                    "artifact_updated",
                    {"ts": _utcnow_iso(), "kind": f"txt_{which}", "path": rel},
                )
                return

        if not src_pdf.exists():
            raise FileNotFoundError(f"input not found for doc_{which}: {src_txt} / {src_pdf}")

        def _get_param(base_key: str, default: Any = None) -> Any:
            key = f"{base_key}_{which}"
            if key in ctx.params:
                return ctx.params.get(key)
            return ctx.params.get(base_key, default)

        pdf_mode = str(_get_param("pdf_mode", "fast")).lower()
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

        start_page = int(_get_param("pdf_start_page", 1) or 1)
        end_page = _get_param("pdf_end_page")
        end_page_i: Optional[int] = int(end_page) if end_page not in (None, "", 0) else None
        batch_size = int(_get_param("pdf_batch_size", 5) or 5)
        use_image = bool(_get_param("pdf_use_image", False) or False)

        model_override = str(_get_param("pdf_llm_model") or ctx.params.get("llm_complex_model") or "").strip()
        asyncio.run(
            convert_pdf_with_llm(
                pdf_path=str(src_pdf),
                output_path=str(out_txt),
                start_page=start_page,
                end_page=end_page_i,
                batch_size=batch_size,
                use_image=use_image,
                model=model_override or None,
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
class EnsureTextAllDocsStep:
    """全ドキュメントの入力を txt に正規化する。"""

    name: str = "ensure_text_all"

    def should_run(self, ctx: RunContext) -> bool:
        force = bool(ctx.params.get("force", False))
        if force:
            return True
        work_dir = Path(ctx.paths["work_dir"])
        for d in _get_documents(ctx):
            doc_id = _normalize_doc_id(d.get("doc_id"), "d1")
            out_txt = work_dir / f"doc_{doc_id}.txt"
            if not out_txt.exists():
                return True
        return False

    def run(self, ctx: RunContext) -> None:
        docs = _get_documents(ctx)
        if not docs:
            raise ValueError("No documents found in run config")
        for d in docs:
            doc_id = _normalize_doc_id(d.get("doc_id"), "d1")
            step = EnsureTextStep(name=f"ensure_text_{doc_id}", which=doc_id)
            if step.should_run(ctx):
                step.run(ctx)

@dataclass
class BuildBlueprintStep:
    """doc_{doc_id}.txt から blueprint_{doc_id}.json を生成する（LLM）。"""

    name: str
    which: str  # "a" or "b"

    def should_run(self, ctx: RunContext) -> bool:
        work_dir = Path(ctx.paths["work_dir"])
        out_path = work_dir / f"blueprint_{self.which.lower()}.json"
        force = bool(ctx.params.get("force", False))
        if not force:
            txt_path = work_dir / f"doc_{self.which.lower()}.txt"
            strategy, _ = _select_ast_builder_strategy(ctx, self.which.lower(), text_path=txt_path)
            if strategy != "blueprint":
                return False
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
        if not (os.getenv("OPENAI_API_KEY") or os.getenv("AZURE_OPENAI_API_KEY") or os.getenv("GOOGLE_API_KEY")):
            raise RuntimeError("LLM API key not set (OPENAI_API_KEY or AZURE_OPENAI_API_KEY or GOOGLE_API_KEY)")

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

        # UI向けイベント（tool_call/agentログ）
        try:
            from document_process_app.agents.middleware import EventSinkMiddleware  # type: ignore
        except Exception:
            EventSinkMiddleware = None  # type: ignore

        llm = build_llm()
        llm_complex_model = str(ctx.params.get("llm_complex_model") or "").strip()
        llm_complex = build_llm(model=llm_complex_model) if llm_complex_model else build_llm()

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

        middleware = []
        validate_mw = None
        if EventSinkMiddleware is not None:
            parent_mw = EventSinkMiddleware(
                run_id=ctx.run_id,
                events=ctx.events,
                cancellation=ctx.cancellation,
                agent_name=f"blueprint_builder_{which}",
            )
            middleware = [parent_mw]
            # subagent側も親子関係を保ってログできるようにする
            validate_mw = EventSinkMiddleware(
                run_id=ctx.run_id,
                events=ctx.events,
                cancellation=ctx.cancellation,
                agent_name=f"validate_blueprint_agent_{which}",
                is_subagent=True,
                forced_parent_invocation_id=parent_mw.invocation_id,
                forced_parent_agent_name=parent_mw.agent_name,
            )

        agent = create_deep_agent(
            model=llm_complex,
            tools=tools_blueprint_builder,
            system_prompt=blueprint_ast_builder_prompt,
            # 可能なら structured_response を使って JSON抽出の脆さを回避する
            response_format=DocumentStructureBlueprint,
            middleware=middleware,
            subagents=[
                {
                    "name": "validate_blueprint_agent",
                    "description": "blueprintを複数の観点で検証して必要に応じて修正します。blueprintのパスを指示してください。",
                    "system_prompt": blueprint_validate_prompt,
                    "tools": tools_blueprint_validator,
                    **({"middleware": [validate_mw]} if validate_mw is not None else {}),
                    "model": llm,
                }
            ],
            debug=False,
        )

        txt_name = f"doc_{which}.txt"
        files = {f"/{txt_name}": create_file_data(_safe_read_text(txt_path))}
        query = f"ファイル '{txt_name}' を解析してください。文書内のすべての見出しパターンを自律的に検出して、抽出するためのblueprintを生成してください。"
        recursion_limit = int(ctx.params.get("recursion_limit", 120) or 120)
        config_lg = {"configurable": {"thread_id": f"{ctx.run_id}:{which}:blueprint"}, "recursion_limit": recursion_limit}
        result = agent.invoke({"messages": [{"role": "user", "content": query}], "files": files}, config=config_lg)
        # structured output を唯一のソースにする（テキストからJSON抽出しない）
        if not isinstance(result, dict):
            raise RuntimeError("blueprint agent returned non-dict result (structured_response unavailable)")
        sr = result.get("structured_response")
        if isinstance(sr, DocumentStructureBlueprint):
            blueprint = sr
        elif isinstance(sr, dict):
            blueprint = DocumentStructureBlueprint(**sr)
        else:
            # 型安全を優先するため、ここで明示的に失敗させる
            raise RuntimeError(
                "blueprint agent returned no structured_response. "
                "Ensure response_format is supported and the model/toolchain is configured correctly."
            )

        # 永続化（Runのworkディレクトリ）
        blueprint_data = blueprint.model_dump()
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(blueprint_data, f, ensure_ascii=False, indent=2)

        rel = out_path.relative_to(run_dir).as_posix()
        ctx.events.emit(ctx.run_id, "artifact_updated", {"ts": _utcnow_iso(), "kind": f"blueprint_{which}", "path": rel})
        
        # ドキュメントリポジトリにも保存（has_blueprintフラグを更新）
        doc_hash = ctx.params.get(f"doc_{which}_hash") or _get_doc_hash(ctx, which)
        if doc_hash:
            from document_process_app.infra.document_store import DocumentRepository
            doc_repo = DocumentRepository()
            doc_repo.save_blueprint(doc_hash, blueprint_data)


@dataclass
class BuildBlueprintAllDocsStep:
    """全ドキュメントから blueprint を生成する（LLM）。"""

    name: str = "build_blueprint_all"

    def should_run(self, ctx: RunContext) -> bool:
        force = bool(ctx.params.get("force", False))
        if force:
            return True
        work_dir = Path(ctx.paths["work_dir"])
        for d in _get_documents(ctx):
            doc_id = _normalize_doc_id(d.get("doc_id"), "d1")
            out_path = work_dir / f"blueprint_{doc_id}.json"
            if not out_path.exists():
                return True
        return False

    def run(self, ctx: RunContext) -> None:
        docs = _get_documents(ctx)
        if not docs:
            raise ValueError("No documents found in run config")
        for d in docs:
            doc_id = _normalize_doc_id(d.get("doc_id"), "d1")
            step = BuildBlueprintStep(name=f"build_blueprint_{doc_id}", which=doc_id)
            if step.should_run(ctx):
                step.run(ctx)

@dataclass
class BuildAstStep:
    """blueprint_{doc_id}.json と doc_{doc_id}.txt から AST を生成する（非LLM）。"""

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

        out_ast = work_dir / f"ast_{which}.ast.json"
        strategy, info = _select_ast_builder_strategy(ctx, which, text_path=txt_path)
        ctx.events.emit(
            ctx.run_id,
            "ast_builder_selected",
            {
                "ts": _utcnow_iso(),
                "which": which,
                "strategy": strategy,
                "reason": info.get("reason"),
                "policy": info.get("policy"),
                "filename": info.get("filename"),
                "char_count": info.get("char_count"),
            },
        )

        if strategy == "markdown":
            from src import markdown_ast_builder

            markdown_ast_builder.build_ast_from_markdown(
                text_path=str(txt_path),
                out_ast_path=str(out_ast),
                root_title=f"doc_{which}",
                max_content_chars_per_node=int(ctx.params.get("max_content_chars_per_node", 2000)),
            )
        elif strategy == "llm_direct":
            if not (os.getenv("OPENAI_API_KEY") or os.getenv("AZURE_OPENAI_API_KEY") or os.getenv("GOOGLE_API_KEY")):
                raise RuntimeError("LLM API key not set (OPENAI_API_KEY or AZURE_OPENAI_API_KEY or GOOGLE_API_KEY)")

            from deepagents import create_deep_agent
            from deepagents.backends.utils import create_file_data

            from src.prompt import direct_ast_builder_prompt
            from src.schema import DirectDocumentAST
            from src.utils import build_llm

            # UI向けイベント（tool_call/agentログ）
            try:
                from document_process_app.agents.middleware import EventSinkMiddleware  # type: ignore
            except Exception:
                EventSinkMiddleware = None  # type: ignore

            model_name = str(ctx.params.get("ast_llm_direct_model") or ctx.params.get("llm_complex_model") or "").strip()
            llm = build_llm(model=model_name) if model_name else build_llm()

            middleware = []
            if EventSinkMiddleware is not None:
                middleware = [
                    EventSinkMiddleware(
                        run_id=ctx.run_id,
                        events=ctx.events,
                        cancellation=ctx.cancellation,
                        agent_name=f"direct_ast_builder_{which}",
                    )
                ]

            agent = create_deep_agent(
                model=llm,
                tools=[],
                system_prompt=direct_ast_builder_prompt,
                response_format=DirectDocumentAST,
                middleware=middleware,
                debug=False,
            )

            txt_name = f"doc_{which}.txt"
            files = {f"/{txt_name}": create_file_data(_safe_read_text(txt_path))}
            query = f"ファイル '{txt_name}' を解析してASTを構築してください。"
            config_lg = {"configurable": {"thread_id": f"{ctx.run_id}:{which}:direct_ast"}}
            result = agent.invoke({"messages": [{"role": "user", "content": query}], "files": files}, config=config_lg)
            if not isinstance(result, dict):
                raise RuntimeError("direct AST agent returned non-dict result (structured_response unavailable)")
            sr = result.get("structured_response")
            if isinstance(sr, DirectDocumentAST):
                ast_obj = sr
            elif isinstance(sr, dict):
                ast_obj = DirectDocumentAST(**sr)
            else:
                raise RuntimeError("direct AST agent returned no structured_response")

            ast_data = ast_obj.model_dump()
            if not ast_data.get("file_name"):
                ast_data["file_name"] = os.path.basename(str(txt_path))
            root = ast_data.get("root") or {}
            if isinstance(root, dict) and not root.get("section_title"):
                root["section_title"] = f"doc_{which}"
                ast_data["root"] = root

            ast_data["__meta__"] = {
                "rev": 0,
                "updated_at": _utcnow_iso(),
                "generated_from": "llm_direct",
                "model": model_name,
            }

            out_ast.write_text(json.dumps(ast_data, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            if not bp_path.exists():
                raise FileNotFoundError(str(bp_path))

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
        
        # ドキュメントリポジトリにも保存（has_astフラグを更新）
        doc_hash = ctx.params.get(f"doc_{which}_hash") or _get_doc_hash(ctx, which)
        if doc_hash:
            from document_process_app.infra.document_store import DocumentRepository
            ast_data = json.loads(out_ast.read_text(encoding="utf-8"))
            doc_repo = DocumentRepository()
            doc_repo.save_ast(doc_hash, ast_data)


@dataclass
class BuildAstAllDocsStep:
    """全ドキュメントの AST を生成する（非LLM）。"""

    name: str = "build_ast_all"

    def should_run(self, ctx: RunContext) -> bool:
        force = bool(ctx.params.get("force", False))
        if force:
            return True
        work_dir = Path(ctx.paths["work_dir"])
        for d in _get_documents(ctx):
            doc_id = _normalize_doc_id(d.get("doc_id"), "d1")
            out_ast = work_dir / f"ast_{doc_id}.ast.json"
            if not out_ast.exists():
                return True
        return False

    def run(self, ctx: RunContext) -> None:
        docs = _get_documents(ctx)
        if not docs:
            raise ValueError("No documents found in run config")
        for d in docs:
            doc_id = _normalize_doc_id(d.get("doc_id"), "d1")
            step = BuildAstStep(name=f"build_ast_{doc_id}", which=doc_id)
            if step.should_run(ctx):
                step.run(ctx)

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

        model = str(ctx.params.get("ast_summary_model") or ctx.params.get("llm_complex_model") or "").strip()
        overwrite = bool(ctx.params.get("ast_summary_overwrite", False) or ctx.params.get("force", False))
        opts = SummarizeOptions(model=model or "", skip_if_summary_exists=not overwrite)
        try:
            summarize_ast_inplace(ast_path=str(ast_path), options=opts, is_cancelled=ctx.cancellation.is_cancelled)
        except SummarizationCancelled:
            from document_process_app.core.pipeline import CancelledError

            raise CancelledError("cancelled during ast summarization")

        # ドキュメントリポジトリにも保存（次のrunで要約済みASTを再利用できるようにする）
        doc_hash = ctx.params.get(f"doc_{which}_hash") or _get_doc_hash(ctx, which)
        if doc_hash:
            try:
                from document_process_app.infra.document_store import DocumentRepository

                ast_data = json.loads(ast_path.read_text(encoding="utf-8"))
                DocumentRepository().save_ast(doc_hash, ast_data)
            except Exception:
                # 要約自体は完了しているので、永続化失敗は致命にしない
                pass

        rel = ast_path.relative_to(run_dir).as_posix()
        ctx.events.emit(ctx.run_id, "artifact_updated", {"ts": _utcnow_iso(), "kind": f"ast_{which}", "path": rel})
        ctx.events.emit(ctx.run_id, "ast_summarized", {"ts": _utcnow_iso(), "which": which, "model": model})


@dataclass
class SummarizeAstAllDocsStep:
    """全ドキュメントの AST 要約を実行する（LLM）。"""

    name: str = "summarize_ast_all"

    def should_run(self, ctx: RunContext) -> bool:
        return bool(ctx.params.get("summarize_ast", False))

    def run(self, ctx: RunContext) -> None:
        if not bool(ctx.params.get("summarize_ast", False)):
            return
        docs = _get_documents(ctx)
        if not docs:
            raise ValueError("No documents found in run config")
        for d in docs:
            doc_id = _normalize_doc_id(d.get("doc_id"), "d1")
            step = SummarizeAstStep(name=f"summarize_ast_{doc_id}", which=doc_id)
            step.run(ctx)

