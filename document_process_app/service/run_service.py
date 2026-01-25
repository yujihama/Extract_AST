from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from fastapi import UploadFile

from document_process_app.core.run_executor import RunExecutor
from document_process_app.infra.document_store import DocumentRepository
from document_process_app.models import RunRecord


class RunServiceError(ValueError):
    """Run作成/起動の入力不備を示すエラー。"""


@dataclass
class RunCreationResult:
    run: RunRecord
    job_id: Optional[str] = None


@dataclass
class RunService:
    executor: RunExecutor
    doc_repo: Optional[DocumentRepository] = None

    def __post_init__(self) -> None:
        if self.doc_repo is None:
            self.doc_repo = getattr(self.executor.artifacts, "doc_repo", None)

    def create_from_form(
        self,
        *,
        doc_hashes: Optional[Sequence[str]] = None,
        upload_docs: Optional[Sequence[UploadFile]] = None,
        request_text: str = "",
        template_file: Optional[UploadFile] = None,
        hil_enabled: Optional[str] = None,
        mode: Optional[str] = None,
        pdf_mode: Optional[str] = None,
        pdf_start_page: Optional[str] = None,
        pdf_end_page: Optional[str] = None,
        pdf_batch_size: Optional[str] = None,
        pdf_use_image: Optional[str] = None,
        summarize_ast: Optional[str] = None,
        ast_summary_model: Optional[str] = None,
        llm_complex_model: Optional[str] = None,
        ast_builder_policy: Optional[str] = None,
        ast_bypass_max_chars: Optional[str] = None,
        step_from: Optional[str] = None,
        step_to: Optional[str] = None,
        start_now: Optional[str] = None,
    ) -> RunCreationResult:
        use_doc_hashes = [h.strip() for h in (doc_hashes or []) if str(h).strip()]
        upload_files = [d for d in (upload_docs or []) if d and d.filename]

        if not use_doc_hashes and not upload_files:
            raise RunServiceError("ドキュメントは1件以上必要です（既存選択またはアップロード）")

        if self.doc_repo:
            for h in use_doc_hashes:
                if not self.doc_repo.exists(h):
                    raise RunServiceError(f"ドキュメント ({h}) が見つかりません")

        tmp_files: list[str] = []
        documents: list[dict[str, Any]] = []

        for h in use_doc_hashes:
            documents.append({"doc_hash": h})

        for up in upload_files:
            tmp_path = self._save_upload_to_temp(up)
            tmp_files.append(tmp_path)
            documents.append({"path": tmp_path})

        template_file_path: Optional[str] = None
        if template_file and template_file.filename:
            fname = str(template_file.filename)
            ext = Path(fname).suffix.lower()
            if ext not in {".md", ".txt"}:
                raise RunServiceError("テンプレートは .md / .txt のみ対応しています")
            tmp_template = self._save_upload_to_temp(template_file)
            tmp_files.append(tmp_template)
            template_file_path = tmp_template

        params = self._normalize_params(
            base_params={},
            mode=mode,
            default_mode="dummy",
            pdf_mode=pdf_mode,
            pdf_start_page=pdf_start_page,
            pdf_end_page=pdf_end_page,
            pdf_batch_size=pdf_batch_size,
            pdf_use_image=pdf_use_image,
            summarize_ast=summarize_ast,
            ast_summary_model=ast_summary_model,
            llm_complex_model=llm_complex_model,
            ast_builder_policy=ast_builder_policy,
            ast_bypass_max_chars=ast_bypass_max_chars,
            step_from=step_from,
            step_to=step_to,
            default_summarize_ast=True,
        )

        hil_flag = self._parse_bool(hil_enabled, default=False)
        start_flag = self._parse_bool(start_now, default=True)
        self._inject_request_hil(params, request_text=request_text, hil_enabled=hil_flag)

        try:
            run = self.executor.create_run(
                documents=documents,
                request_text=request_text,
                hil_enabled=hil_flag,
                template_file_path=template_file_path,
                params=params,
            )
            job_id = self.executor.start(run.run_id) if start_flag else None
            return RunCreationResult(run=run, job_id=job_id)
        finally:
            for tmp_path in tmp_files:
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

    def create_from_payload(self, payload: Mapping[str, Any]) -> RunCreationResult:
        documents_raw = payload.get("documents")
        if not isinstance(documents_raw, list) or len(documents_raw) < 1:
            raise RunServiceError("documents must be a list (len>=1)")

        params = payload.get("params") or {}
        if not isinstance(params, dict):
            params = {"params": params}

        mode = payload.get("mode")
        params = self._normalize_params(
            base_params=params,
            mode=mode,
            default_mode=None,
            default_summarize_ast=True,
        )

        request_text = payload.get("request_text") or ""
        hil_enabled = self._parse_bool(payload.get("hil_enabled"), default=False)
        start_now = self._parse_bool(payload.get("start_now"), default=False)
        self._inject_request_hil(params, request_text=request_text, hil_enabled=hil_enabled)

        tmp_paths: list[str] = []
        docs_for_run: list[dict[str, Any]] = []
        for i, spec in enumerate(documents_raw, 1):
            if not isinstance(spec, Mapping):
                raise RunServiceError("documents must be list of objects")
            doc_id = str(spec.get("doc_id") or f"d{i}")
            if spec.get("doc_hash"):
                doc_hash = str(spec.get("doc_hash"))
                if self.doc_repo and not self.doc_repo.exists(doc_hash):
                    raise RunServiceError(f"document not found: {doc_hash}")
                docs_for_run.append({"doc_id": doc_id, "doc_hash": doc_hash})
                continue
            if spec.get("path"):
                docs_for_run.append({"doc_id": doc_id, "path": str(spec.get("path"))})
                continue
            if spec.get("text"):
                tmp_path = self._write_text_to_temp(str(spec.get("text")), suffix=".txt")
                tmp_paths.append(tmp_path)
                docs_for_run.append({"doc_id": doc_id, "path": tmp_path})
                continue
            raise RunServiceError("each document needs doc_hash, path, or text")

        try:
            run = self.executor.create_run(
                documents=docs_for_run,
                request_text=str(request_text) if request_text is not None else "",
                hil_enabled=hil_enabled,
                params=params,
            )
            job_id = self.executor.start(run.run_id) if start_now else None
            return RunCreationResult(run=run, job_id=job_id)
        finally:
            for p in tmp_paths:
                try:
                    os.remove(p)
                except Exception:
                    pass

    def create_from_cli(
        self,
        *,
        doc_paths: Sequence[str],
        doc_hashes: Sequence[str],
        request_text: str,
        hil_enabled: bool,
        mode: str,
        params_raw: Mapping[str, Any],
    ) -> RunCreationResult:
        params = params_raw
        if not isinstance(params, dict):
            params = {"params": params}

        params = self._normalize_params(
            base_params=params,
            mode=mode,
            default_mode="dummy",
            default_summarize_ast=True,
        )
        self._inject_request_hil(params, request_text=request_text, hil_enabled=bool(hil_enabled))

        documents: list[dict[str, str]] = []
        for p in doc_paths:
            documents.append({"path": str(p)})
        for h in doc_hashes:
            if self.doc_repo and not self.doc_repo.exists(str(h)):
                raise RunServiceError(f"document not found: {h}")
            documents.append({"doc_hash": str(h)})

        if len(documents) < 1:
            raise RunServiceError("documents must be >= 1 (use --doc/--doc-hash)")

        run = self.executor.create_run(
            documents=documents,
            request_text=str(request_text or ""),
            hil_enabled=bool(hil_enabled),
            params=params,
        )
        return RunCreationResult(run=run, job_id=None)

    def start_run(self, run_id: str) -> str:
        return self.executor.start(run_id)

    def cancel_run(self, run_id: str) -> None:
        self.executor.request_cancel(run_id)

    def _normalize_params(
        self,
        *,
        base_params: Mapping[str, Any],
        mode: Optional[str],
        default_mode: Optional[str],
        pdf_mode: Optional[str] = None,
        pdf_start_page: Optional[str] = None,
        pdf_end_page: Optional[str] = None,
        pdf_batch_size: Optional[str] = None,
        pdf_use_image: Optional[str] = None,
        summarize_ast: Optional[str] = None,
        ast_summary_model: Optional[str] = None,
        llm_complex_model: Optional[str] = None,
        ast_builder_policy: Optional[str] = None,
        ast_bypass_max_chars: Optional[str] = None,
        step_from: Optional[str] = None,
        step_to: Optional[str] = None,
        default_summarize_ast: Optional[bool] = None,
    ) -> dict[str, Any]:
        params = dict(base_params or {})

        if "mode" not in params:
            if mode is not None:
                params["mode"] = self._normalize_mode(mode, default_mode or "dummy")
            elif default_mode is not None:
                params["mode"] = self._normalize_mode(default_mode, default_mode)

        if pdf_mode:
            pm = str(pdf_mode).lower().strip()
            if pm in {"fast", "llm"}:
                params["pdf_mode"] = pm

        if pdf_start_page is not None:
            params["pdf_start_page"] = self._parse_int(pdf_start_page, default=1, minimum=1)
        if pdf_end_page is not None:
            params["pdf_end_page"] = self._parse_int(pdf_end_page, default=None, minimum=None)
        if pdf_batch_size is not None:
            params["pdf_batch_size"] = self._parse_int(pdf_batch_size, default=5, minimum=1)
        if pdf_use_image is not None or pdf_mode is not None:
            params["pdf_use_image"] = bool(self._parse_bool(pdf_use_image, default=False))

        if summarize_ast is not None:
            params["summarize_ast"] = bool(self._parse_bool(summarize_ast, default=False))
        elif default_summarize_ast is not None and "summarize_ast" not in params:
            params["summarize_ast"] = bool(default_summarize_ast)

        if ast_summary_model and str(ast_summary_model).strip():
            params["ast_summary_model"] = str(ast_summary_model).strip()

        if llm_complex_model and str(llm_complex_model).strip():
            params["llm_complex_model"] = str(llm_complex_model).strip()

        if ast_builder_policy:
            pol = str(ast_builder_policy).strip().lower()
            if pol in {"auto", "blueprint", "markdown", "llm_direct"}:
                params["ast_builder_policy"] = pol

        if ast_bypass_max_chars is not None:
            max_chars = self._parse_int(ast_bypass_max_chars, default=None, minimum=1)
            if max_chars:
                params["ast_bypass_max_chars"] = max_chars

        if step_from and str(step_from).strip():
            params["step_from"] = str(step_from).strip()
        if step_to and str(step_to).strip():
            params["step_to"] = str(step_to).strip()

        return params

    def _inject_request_hil(self, params: dict[str, Any], *, request_text: str, hil_enabled: bool) -> None:
        if "request_text" not in params and request_text is not None:
            params["request_text"] = str(request_text)
        if "hil_enabled" not in params:
            params["hil_enabled"] = bool(hil_enabled)

    def _normalize_mode(self, mode: str, default: str) -> str:
        m = str(mode or default).lower().strip()
        if m not in {"dummy", "real"}:
            return str(default).lower().strip() or "dummy"
        return m

    def _parse_bool(self, value: Any, *, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if not text:
            return default
        return text in {"1", "true", "yes", "y", "on"}

    def _parse_int(self, value: Any, *, default: Optional[int], minimum: Optional[int]) -> Optional[int]:
        if value is None:
            return default
        text = str(value).strip()
        if not text:
            return default
        try:
            num = int(text)
        except Exception:
            return default
        if minimum is not None and num < minimum:
            return minimum
        return num

    def _save_upload_to_temp(self, upload: UploadFile) -> str:
        suffix = Path(upload.filename or "").suffix
        fd, tmp_path = tempfile.mkstemp(prefix="document_process_app_upload_", suffix=suffix)
        os.close(fd)
        with open(tmp_path, "wb") as f:
            while True:
                chunk = upload.file.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
        return tmp_path

    def _write_text_to_temp(self, text: str, *, suffix: str = ".txt") -> str:
        fd, tmp_path = tempfile.mkstemp(prefix="document_process_app_text_", suffix=suffix)
        os.close(fd)
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        return tmp_path
