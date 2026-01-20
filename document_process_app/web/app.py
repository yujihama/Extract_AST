from __future__ import annotations

import json
import asyncio
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator, Optional

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from document_process_app.bootstrap import build_default_executor


TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
# Jinja2 の tojson フィルタが \uXXXX エスケープで出力しないようにする（UIで日本語が読めるように）
try:
    templates.env.policies.setdefault("json.dumps_kwargs", {})
    templates.env.policies["json.dumps_kwargs"].update({"ensure_ascii": False})
except Exception:
    # テンプレ初期化に失敗してもアプリ起動は継続（best effort）
    pass


def create_app() -> FastAPI:
    app = FastAPI(title="document_process_agent (MVP)")

    executor, repo, events, artifacts_repo = build_default_executor()
    app.state.executor = executor
    app.state.repo = repo
    app.state.events = events
    app.state.artifacts_repo = artifacts_repo
    
    # ドキュメント中心アーキテクチャ: FileArtifactStoreからdoc_repoを取得
    doc_repo = executor.artifacts.doc_repo

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        runs = repo.list_runs(limit=50)
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "runs": runs,
                "db_path": getattr(repo, "db_path", None),
            },
        )


    # ----------------------------
    # ドキュメント管理（ドキュメント中心アーキテクチャ）
    # ----------------------------

    @app.get("/documents", response_class=HTMLResponse)
    def documents_list(request: Request):
        """ドキュメント一覧画面"""
        docs = doc_repo.list_all()
        return templates.TemplateResponse(
            "documents.html",
            {"request": request, "documents": docs},
        )

    @app.get("/api/documents")
    def api_list_documents():
        """ドキュメント一覧API"""
        docs = doc_repo.list_all()
        return JSONResponse([d.to_dict() for d in docs])

    @app.get("/api/documents/{doc_hash}")
    def api_get_document(doc_hash: str):
        """ドキュメント詳細API"""
        doc = doc_repo.get(doc_hash)
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")
        return JSONResponse(doc.to_dict())

    @app.post("/api/documents")
    async def api_upload_document(file: UploadFile = File(...)):
        """ドキュメントアップロードAPI"""
        content = await file.read()
        doc = doc_repo.add(content, file.filename or "uploaded.txt")
        return JSONResponse(doc.to_dict())

    @app.delete("/api/documents/{doc_hash}")
    def api_delete_document(doc_hash: str):
        """ドキュメント削除API"""
        deleted = doc_repo.delete(doc_hash)
        if not deleted:
            raise HTTPException(status_code=404, detail="Document not found")
        return JSONResponse({"deleted": True, "doc_hash": doc_hash})

    @app.delete("/documents/{doc_hash}", response_class=HTMLResponse)
    def delete_document_ui(request: Request, doc_hash: str):
        """ドキュメント削除（UI用・HTMXから呼び出し）"""
        deleted = doc_repo.delete(doc_hash)
        if not deleted:
            return HTMLResponse("<div class='alert alert-error'>ドキュメントが見つかりません</div>", status_code=404)
        # 削除成功時は空のレスポンス（行が消える）
        return HTMLResponse("")

    # ----------------------------
    # JSON API（UI未実装でも使えるI/F）
    # ----------------------------

    @app.get("/api/runs")
    def api_list_runs(limit: int = 50):
        runs = repo.list_runs(limit=max(1, int(limit)))
        return JSONResponse(
            [
                {
                    "run_id": r.run_id,
                    "status": r.status,
                    "created_at": r.created_at.isoformat(),
                    "started_at": r.started_at.isoformat() if r.started_at else None,
                    "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                    "params": r.params,
                    "workdir": r.workdir,
                }
                for r in runs
            ]
        )

    @app.get("/api/runs/{run_id}")
    def api_get_run(run_id: str):
        r = repo.get_run(run_id)
        return JSONResponse(
            {
                "run_id": r.run_id,
                "status": r.status,
                "created_at": r.created_at.isoformat(),
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "params": r.params,
                "error_message": r.error_message,
                "workdir": r.workdir,
            }
        )

    @app.delete("/api/runs/{run_id}")
    def api_delete_run(run_id: str):
        """Run削除API（DB上のrun/events/artifacts + FSのrun dir）。"""
        try:
            run = repo.get_run(run_id)
        except Exception:
            raise HTTPException(status_code=404, detail="run not found")

        # 実行中なら協調キャンセルを要求（best effort）
        try:
            if str(getattr(run, "status", "")).lower() == "running":
                executor.request_cancel(run_id)
        except Exception:
            pass

        deleted = False
        if hasattr(repo, "delete_run"):
            deleted = bool(repo.delete_run(run_id))  # type: ignore[attr-defined]
        else:
            raise HTTPException(status_code=500, detail="repo does not support delete_run")

        # FS掃除（best effort）
        try:
            if run.workdir:
                shutil.rmtree(run.workdir, ignore_errors=True)
        except Exception:
            pass

        if not deleted:
            raise HTTPException(status_code=404, detail="run not found")
        return JSONResponse({"deleted": True, "run_id": run_id})

    @app.get("/runs/new", response_class=HTMLResponse)
    def runs_new(request: Request):
        # ドキュメント中心アーキテクチャ: 既存ドキュメント一覧を取得
        documents = doc_repo.list_all()
        
        return templates.TemplateResponse("run_new.html", {
            "request": request, 
            "documents": documents,
        })

    def _save_upload_to_temp(upload: UploadFile) -> str:
        # Windowsでも扱いやすいように NamedTemporaryFile(delete=False)
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

    def _write_text_to_temp(text: str, *, suffix: str = ".txt") -> str:
        fd, tmp_path = tempfile.mkstemp(prefix="document_process_app_text_", suffix=suffix)
        os.close(fd)
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        return tmp_path

    @app.post("/api/runs/multi")
    async def api_runs_create_multi(payload: dict[str, Any] = Body(...)):
        documents = payload.get("documents")
        if not isinstance(documents, list) or len(documents) < 1:
            raise HTTPException(status_code=400, detail="documents must be a list (len>=1)")

        params = payload.get("params") or {}
        if not isinstance(params, dict):
            params = {"params": params}

        if "mode" in payload and "mode" not in params:
            params["mode"] = payload.get("mode")
        params.setdefault("summarize_ast", True)

        request_text = payload.get("request_text") or ""
        hil_enabled = bool(payload.get("hil_enabled", False))
        start_now = bool(payload.get("start_now", False))

        tmp_paths: list[str] = []
        docs_for_run: list[dict[str, Any]] = []
        for i, spec in enumerate(documents, 1):
            if not isinstance(spec, dict):
                raise HTTPException(status_code=400, detail="documents must be list of objects")
            doc_id = str(spec.get("doc_id") or f"d{i}")
            if spec.get("doc_hash"):
                docs_for_run.append({"doc_id": doc_id, "doc_hash": str(spec.get("doc_hash"))})
                continue
            if spec.get("path"):
                docs_for_run.append({"doc_id": doc_id, "path": str(spec.get("path"))})
                continue
            if spec.get("text"):
                tmp_path = _write_text_to_temp(str(spec.get("text")), suffix=".txt")
                tmp_paths.append(tmp_path)
                docs_for_run.append({"doc_id": doc_id, "path": tmp_path})
                continue
            raise HTTPException(status_code=400, detail="each document needs doc_hash, path, or text")

        try:
            run = executor.create_run(
                documents=docs_for_run,
                request_text=str(request_text) if request_text is not None else "",
                hil_enabled=hil_enabled,
                params=params,
            )
        finally:
            for p in tmp_paths:
                try:
                    os.remove(p)
                except Exception:
                    pass

        if start_now:
            executor.start(run.run_id)

        return JSONResponse({"run_id": run.run_id, "status": run.status})

    @app.post("/runs")
    async def runs_create(
        request: Request,
        docs: Optional[list[UploadFile]] = File(None),
        doc_hashes: Optional[list[str]] = Form(None),
        request_text: str = Form(""),
        template_file: Optional[UploadFile] = File(None),
        # hidden + checkbox (0/1)
        hil_enabled: str = Form("0"),
        mode: str = Form("dummy"),
        pdf_mode: str = Form("fast"),
        pdf_start_page: str = Form("1"),
        pdf_end_page: str = Form(""),
        pdf_batch_size: str = Form("5"),
        pdf_use_image: Optional[str] = Form(None),
        # run_new.html 側で hidden(0) + checkbox(1) を送るため、常に値が来る想定
        summarize_ast: str = Form("1"),
        ast_summary_model: str = Form(""),
        llm_complex_model: str = Form(""),
        ast_builder_policy: str = Form("auto"),
        ast_bypass_max_chars: str = Form("5000"),
        step_from: str = Form(""),
        step_to: str = Form(""),
        start_now: Optional[str] = Form("on"),
    ):
        use_doc_hashes = [h.strip() for h in (doc_hashes or []) if str(h).strip()]
        upload_docs = [d for d in (docs or []) if d and d.filename]

        tmp_files: list[str] = []
        documents: list[dict[str, Any]] = []

        # 既存ドキュメント
        for h in use_doc_hashes:
            if not doc_repo.exists(h):
                from starlette.responses import HTMLResponse
                return HTMLResponse(f"ドキュメント ({h}) が見つかりません", status_code=400)
            documents.append({"doc_hash": h})

        # 新規アップロード
        for up in upload_docs:
            tmp_path = _save_upload_to_temp(up)
            tmp_files.append(tmp_path)
            documents.append({"path": tmp_path})

        if len(documents) < 1:
            from starlette.responses import HTMLResponse
            return HTMLResponse("ドキュメントは1件以上必要です（既存選択またはアップロード）", status_code=400)
        try:
            m = str(mode).lower().strip()
            if m not in {"dummy", "real"}:
                m = "dummy"
            params: dict[str, Any] = {"mode": m}

            # PDF→TXT（PDF入力のときだけ使用される）
            pm = str(pdf_mode).lower().strip()
            if pm in {"fast", "llm"}:
                params["pdf_mode"] = pm
            try:
                params["pdf_start_page"] = max(1, int(pdf_start_page or "1"))
            except Exception:
                params["pdf_start_page"] = 1
            try:
                params["pdf_end_page"] = int(pdf_end_page) if str(pdf_end_page).strip() else None
            except Exception:
                params["pdf_end_page"] = None
            try:
                params["pdf_batch_size"] = max(1, int(pdf_batch_size or "5"))
            except Exception:
                params["pdf_batch_size"] = 5
            params["pdf_use_image"] = bool(pdf_use_image)

            # AST枝サマリ（realのみ）
            sa = str(summarize_ast or "").strip().lower()
            params["summarize_ast"] = sa in {"1", "true", "on", "yes", "y"}
            if str(ast_summary_model).strip():
                params["ast_summary_model"] = str(ast_summary_model).strip()

            # AST生成ポリシー（auto/blueprint/markdown/llm_direct）
            pol = str(ast_builder_policy or "").strip().lower()
            if pol in {"auto", "blueprint", "markdown", "llm_direct"}:
                params["ast_builder_policy"] = pol
            try:
                max_chars = int(ast_bypass_max_chars or "0")
                if max_chars > 0:
                    params["ast_bypass_max_chars"] = max_chars
            except Exception:
                pass

            # 任意: 複雑系モデル
            if str(llm_complex_model).strip():
                params["llm_complex_model"] = str(llm_complex_model).strip()

            # ステップ選択（step filtering）
            if str(step_from).strip():
                params["step_from"] = str(step_from).strip()
            if str(step_to).strip():
                params["step_to"] = str(step_to).strip()

            # Run作成（ドキュメントハッシュまたはパスを指定）
            hil_flag = str(hil_enabled or "").strip().lower() in {"1", "true", "on", "yes", "y"}
            template_file_path: Optional[str] = None

            # テンプレートのアップロード（任意 / .md, .txt）
            if template_file and template_file.filename:
                fname = str(template_file.filename)
                ext = Path(fname).suffix.lower()
                if ext not in {".md", ".txt"}:
                    from starlette.responses import HTMLResponse
                    return HTMLResponse("テンプレートは .md / .txt のみ対応しています", status_code=400)
                tmp_template = _save_upload_to_temp(template_file)
                tmp_files.append(tmp_template)
                template_file_path = tmp_template

            run = executor.create_run(
                documents=documents,
                request_text=request_text,
                hil_enabled=hil_flag,
                template_file_path=template_file_path,
                params=params,
            )
        finally:
            # アップロードした一時ファイルを削除
            for tmp_path in tmp_files:
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

        if start_now:
            executor.start(run.run_id)

        return RedirectResponse(url=f"/runs/{run.run_id}", status_code=303)

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def runs_detail(request: Request, run_id: str):
        run = repo.get_run(run_id)
        initial_events = events.list(run_id, limit=200)
        return templates.TemplateResponse(
            "run_detail.html",
            {"request": request, "run": run, "initial_events": initial_events},
        )

    @app.delete("/runs/{run_id}", response_class=HTMLResponse)
    def runs_delete_ui(request: Request, run_id: str):
        """Run削除（UI用・HTMXから呼び出し）。成功時は空で行を消す。"""
        try:
            run = repo.get_run(run_id)
        except Exception:
            # 行置換を想定: エラー表示の<tr>を返す
            return HTMLResponse(
                "<tr><td colspan='6'><div class='alert alert-error'>Runが見つかりません</div></td></tr>",
                status_code=200,
            )

        # 実行中なら協調キャンセルを要求（best effort）。その上で削除を続行する。
        try:
            if str(getattr(run, "status", "")).lower() == "running":
                executor.request_cancel(run_id)
        except Exception:
            pass

        if not hasattr(repo, "delete_run"):
            return HTMLResponse(
                "<tr><td colspan='6'><div class='alert alert-error'>この環境ではRun削除に対応していません</div></td></tr>",
                status_code=200,
            )

        deleted = bool(repo.delete_run(run_id))  # type: ignore[attr-defined]
        if not deleted:
            return HTMLResponse(
                "<tr><td colspan='6'><div class='alert alert-error'>Runが見つかりません</div></td></tr>",
                status_code=200,
            )

        # FS掃除（best effort）
        try:
            if run.workdir:
                shutil.rmtree(run.workdir, ignore_errors=True)
        except Exception:
            pass

        return HTMLResponse("")

    @app.post("/runs/{run_id}/start", response_class=HTMLResponse)
    def runs_start(request: Request, run_id: str):
        executor.start(run_id)
        run = repo.get_run(run_id)
        return templates.TemplateResponse("partials/status.html", {"request": request, "run": run})

    @app.post("/runs/{run_id}/cancel", response_class=HTMLResponse)
    def runs_cancel(request: Request, run_id: str):
        executor.request_cancel(run_id)
        run = repo.get_run(run_id)
        return templates.TemplateResponse("partials/status.html", {"request": request, "run": run})

    @app.get("/runs/{run_id}/partials/status", response_class=HTMLResponse)
    def runs_status_partial(request: Request, run_id: str):
        run = repo.get_run(run_id)
        # ステータス欄に「直近のエージェント最終回答」を表示する
        latest_agent = None
        latest_any = None
        try:
            evs = events.list(run_id, limit=500)
            for ev in reversed(list(evs)):
                if str(getattr(ev, "event_type", "")) != "agent_end":
                    continue
                payload = getattr(ev, "payload", {}) or {}
                if not isinstance(payload, dict):
                    payload = {"payload": payload}
                rec = {
                    "ts": getattr(ev, "ts", None),
                    "agent_name": payload.get("agent_name"),
                    "is_subagent": bool(payload.get("is_subagent", False)),
                    "final_response": payload.get("final_response") or "",
                    "final_response_preview": payload.get("final_response_preview") or "",
                }
                if latest_any is None:
                    latest_any = rec
                if not rec["is_subagent"] and latest_agent is None:
                    latest_agent = rec
                if latest_agent is not None and latest_any is not None:
                    break
        except Exception:
            latest_agent = None
            latest_any = None

        agent_answer_obj = latest_agent or latest_any
        agent_answer_text = ""
        agent_answer_name = None
        if isinstance(agent_answer_obj, dict):
            agent_answer_name = agent_answer_obj.get("agent_name")
            agent_answer_text = (
                str(agent_answer_obj.get("final_response") or "").strip()
                or str(agent_answer_obj.get("final_response_preview") or "").strip()
            )

        return templates.TemplateResponse(
            "partials/status.html",
            {"request": request, "run": run, "agent_answer_text": agent_answer_text, "agent_answer_name": agent_answer_name},
        )

    def _get_run_base_dir(run_id: str) -> Path:
        run = repo.get_run(run_id)
        return Path(run.workdir) if run.workdir else (Path("data") / "runs" / run_id)

    @app.get("/runs/{run_id}/partials/hitl", response_class=HTMLResponse)
    def runs_hitl_partial(request: Request, run_id: str):
        """HITL（deepagents interrupt）で止まっている場合の質問表示。"""
        run = repo.get_run(run_id)
        base_dir = _get_run_base_dir(run_id).resolve()
        hitl = None
        try:
            p = (base_dir / "work" / "hitl_interrupt.json")
            if p.exists():
                hitl = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            hitl = None
        return templates.TemplateResponse(
            "partials/hitl.html",
            {"request": request, "run": run, "hitl": hitl},
        )

    @app.post("/runs/{run_id}/hitl/respond", response_class=HTMLResponse)
    async def runs_hitl_respond(request: Request, run_id: str, answer: str = Form("")):
        """HITL質問に回答し、同じrunを再開する（work/hitl_resume.json を書いて start）。"""
        run = repo.get_run(run_id)
        base_dir = _get_run_base_dir(run_id).resolve()
        work_dir = (base_dir / "work")
        interrupt_path = (work_dir / "hitl_interrupt.json")
        resume_path = (work_dir / "hitl_resume.json")

        try:
            hitl = json.loads(interrupt_path.read_text(encoding="utf-8")) if interrupt_path.exists() else None
        except Exception:
            hitl = None

        ans = str(answer or "").strip()
        if not ans:
            return templates.TemplateResponse(
                "partials/hitl.html",
                {
                    "request": request,
                    "run": run,
                    "hitl": hitl if isinstance(hitl, dict) else None,
                    "flash_message": "回答が空です。回答を入力してください。",
                },
            )

        actions = hitl.get("action_requests") if isinstance(hitl, dict) else None
        if not isinstance(actions, list) or not actions:
            return templates.TemplateResponse(
                "partials/hitl.html",
                {"request": request, "run": run, "hitl": None, "flash_message": "HITLの保留内容が見つかりません。"},
            )
        if len(actions) != 1:
            return templates.TemplateResponse(
                "partials/hitl.html",
                {
                    "request": request,
                    "run": run,
                    "hitl": hitl if isinstance(hitl, dict) else None,
                    "flash_message": f"複数の承認待ち({len(actions)})は未対応です（現状は1件のみ対応）。",
                },
            )

        a0 = actions[0] if isinstance(actions[0], dict) else {}
        name = str(a0.get("name") or "")
        args = a0.get("args") if isinstance(a0.get("args"), dict) else {}

        if name != "human_input":
            return templates.TemplateResponse(
                "partials/hitl.html",
                {
                    "request": request,
                    "run": run,
                    "hitl": hitl if isinstance(hitl, dict) else None,
                    "flash_message": f"未対応のHITLツールです: {name}",
                },
            )

        edited_args = dict(args or {})
        edited_args["answer"] = ans
        # deepagents docs: decisions must match action_requests order
        decisions = [
            {
                "type": "edit",
                "edited_action": {
                    "name": name,
                    "args": edited_args,
                },
            }
        ]

        work_dir.mkdir(parents=True, exist_ok=True)
        resume_path.write_text(json.dumps({"decisions": decisions}, ensure_ascii=False, indent=2), encoding="utf-8")
        # いったんinterruptは消す（再停止したらstep側で再生成される）
        try:
            if interrupt_path.exists():
                interrupt_path.unlink()
        except Exception:
            pass

        executor.start(run_id)
        run2 = repo.get_run(run_id)
        return templates.TemplateResponse(
            "partials/hitl.html",
            {"request": request, "run": run2, "hitl": None, "flash_message": "回答を受け付けました。再開しました。"},
        )

    @app.get("/runs/{run_id}/partials/artifacts", response_class=HTMLResponse)
    def runs_artifacts_partial(request: Request, run_id: str):
        base_dir = _get_run_base_dir(run_id)

        # DB（artifacts）優先。未記録のファイルがある場合はFSスキャンで補完する。
        items: list[dict[str, Any]] = []
        seen: set[str] = set()

        try:
            rows = artifacts_repo.list_artifacts(run_id)
        except Exception:
            rows = []

        for r in rows:
            rel = str(r.get("path") or "")
            if not rel:
                continue
            seen.add(rel)
            p = (base_dir / rel)
            size = p.stat().st_size if p.exists() else None
            items.append(
                {
                    "rel": rel,
                    "kind": r.get("kind"),
                    "size": size,
                    "updated_at": r.get("updated_at"),
                }
            )

        # FS補完（DBに無いファイルも見せる）
        for sub in ["input", "work", "out", "log", "cache"]:
            d = base_dir / sub
            if not d.exists():
                continue
            for p in sorted(d.rglob("*")):
                if not p.is_file():
                    continue
                rel = p.relative_to(base_dir).as_posix()
                if rel in seen:
                    continue
                st = p.stat()
                ts = datetime.now(timezone.utc).isoformat()
                try:
                    events.emit(run_id, "artifact_updated", {"ts": ts, "kind": "file", "path": rel, "size": st.st_size})
                except Exception:
                    pass
                items.append({"rel": rel, "kind": "file", "size": st.st_size, "updated_at": None})
                seen.add(rel)

        # sort: kindありを上、updated_at新しい順（ざっくり）
        def _sort_key(it: dict[str, Any]):
            return (0 if it.get("kind") else 1, it.get("updated_at") or "", it.get("rel") or "")

        items.sort(key=_sort_key)
        return templates.TemplateResponse(
            "partials/artifacts.html",
            {"request": request, "run_id": run_id, "items": items},
        )

    def _resolve_artifact_path(run_id: str, rel_path: str) -> Path:
        base_dir = _get_run_base_dir(run_id).resolve()
        candidate = (base_dir / rel_path).resolve()
        # パストラバーサル防止
        if base_dir not in candidate.parents and candidate != base_dir:
            raise ValueError("invalid path")
        return candidate

    def _get_work_file(run_id: str, name: str) -> Path:
        base_dir = _get_run_base_dir(run_id).resolve()
        p = (base_dir / "work" / name).resolve()
        if base_dir not in p.parents and p != base_dir:
            raise ValueError("invalid path")
        return p

    @app.get("/api/runs/{run_id}/blueprint/{which}")
    def api_get_blueprint(run_id: str, which: str):
        w = str(which).lower()
        if w not in {"a", "b"}:
            raise HTTPException(status_code=400, detail="which must be 'a' or 'b'")
        bp = _get_work_file(run_id, f"blueprint_{w}.json")
        if not bp.exists():
            raise HTTPException(status_code=404, detail="blueprint not found")
        try:
            return JSONResponse(json.loads(bp.read_text(encoding="utf-8")))
        except Exception:
            return JSONResponse({"ok": False, "error": "failed to read blueprint"}, status_code=500)

    @app.put("/api/runs/{run_id}/blueprint/{which}")
    async def api_put_blueprint(run_id: str, which: str, payload: dict[str, Any] = Body(...)):
        w = str(which).lower()
        if w not in {"a", "b"}:
            raise HTTPException(status_code=400, detail="which must be 'a' or 'b'")
        bp = _get_work_file(run_id, f"blueprint_{w}.json")
        bp.parent.mkdir(parents=True, exist_ok=True)
        bp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        # artifact更新イベント
        try:
            base_dir = _get_run_base_dir(run_id).resolve()
            rel = bp.relative_to(base_dir).as_posix()
            events.emit(run_id, "artifact_updated", {"ts": "", "kind": f"blueprint_{w}", "path": rel})
        except Exception:
            pass
        return JSONResponse({"ok": True})

    @app.get("/api/runs/{run_id}/blueprint/{which}/preview")
    def api_preview_blueprint(run_id: str, which: str):
        w = str(which).lower()
        if w not in {"a", "b"}:
            raise HTTPException(status_code=400, detail="which must be 'a' or 'b'")
        bp = _get_work_file(run_id, f"blueprint_{w}.json")
        txt = _get_work_file(run_id, f"doc_{w}.txt")
        if not bp.exists() or not txt.exists():
            raise HTTPException(status_code=404, detail="required files not found")
        from src.tools import preview_blueprint_headings

        out = preview_blueprint_headings.invoke({"blueprint_path": str(bp), "text_path": str(txt)})
        try:
            return JSONResponse(json.loads(out))
        except Exception:
            return JSONResponse({"ok": False, "raw": out})

    @app.get("/api/runs/{run_id}/blueprint/{which}/validate")
    def api_validate_blueprint(
        run_id: str,
        which: str,
        mode: str = "gaps",
        gap_threshold: int = 100,
        max_level: int = 3,
    ):
        w = str(which).lower()
        if w not in {"a", "b"}:
            raise HTTPException(status_code=400, detail="which must be 'a' or 'b'")
        bp = _get_work_file(run_id, f"blueprint_{w}.json")
        txt = _get_work_file(run_id, f"doc_{w}.txt")
        if not bp.exists() or not txt.exists():
            raise HTTPException(status_code=404, detail="required files not found")
        from src.tools import validate_blueprint

        out = validate_blueprint.invoke(
            {
                "blueprint_path": str(bp),
                "text_path": str(txt),
                "mode": str(mode),
                "gap_threshold": int(gap_threshold),
                "max_level": int(max_level),
            }
        )
        try:
            return JSONResponse(json.loads(out))
        except Exception:
            return JSONResponse({"ok": False, "raw": out})

    def _parse_node_path(s: Optional[str]) -> Optional[list[int]]:
        if s is None:
            return None
        s = str(s).strip()
        if not s:
            return None
        try:
            return [int(x) for x in s.split(",") if str(x).strip() != ""]
        except Exception:
            return None

    @app.get("/api/runs/{run_id}/ast/{which}")
    def api_read_ast(
        run_id: str,
        which: str,
        mode: str = "summary",
        node_path: Optional[str] = None,
        title_query: Optional[str] = None,
        max_depth: int = 3,
        include_content: bool = False,
        max_content_chars: int = 500,
    ):
        w = str(which).lower()
        if w not in {"a", "b"}:
            raise HTTPException(status_code=400, detail="which must be 'a' or 'b'")
        ast_path = _get_work_file(run_id, f"ast_{w}.ast.json")
        if not ast_path.exists():
            raise HTTPException(status_code=404, detail="ast not found")
        from src.tools import read_ast

        out = read_ast.invoke(
            {
                "file_path": str(ast_path),
                "mode": str(mode),
                "node_path": _parse_node_path(node_path),
                "title_query": title_query,
                "max_depth": int(max_depth),
                "include_content": bool(include_content),
                "max_content_chars": int(max_content_chars),
            }
        )
        try:
            return JSONResponse(json.loads(out))
        except Exception:
            return JSONResponse({"ok": False, "raw": out})

    @app.get("/api/runs/{run_id}/compare/initial_matching")
    def api_get_initial_matching(run_id: str):
        p = _get_work_file(run_id, "initial_matching.json")
        if not p.exists():
            raise HTTPException(status_code=404, detail="initial_matching not found")
        try:
            return JSONResponse(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            return JSONResponse({"ok": False, "error": "failed to read initial_matching"}, status_code=500)

    @app.get("/api/runs/{run_id}/events")
    def api_list_events(run_id: str, after_event_id: Optional[int] = None, limit: int = 200):
        evs = events.list(run_id, after_event_id=after_event_id, limit=max(1, int(limit)))
        return JSONResponse(
            [
                {
                    "event_id": e.event_id,
                    "run_id": e.run_id,
                    "ts": e.ts.isoformat(),
                    "event_type": e.event_type,
                    "payload": e.payload,
                }
                for e in evs
            ]
        )

    @app.get("/runs/{run_id}/artifacts/view/{rel_path:path}", response_class=HTMLResponse)
    def runs_artifact_view(request: Request, run_id: str, rel_path: str):
        try:
            p = _resolve_artifact_path(run_id, rel_path)
        except Exception:
            return HTMLResponse("invalid path", status_code=400)
        if not p.exists() or not p.is_file():
            return HTMLResponse("not found", status_code=404)

        # テキスト系はプレビュー
        ext = p.suffix.lower()
        if ext in {".txt", ".md", ".json", ".jsonl", ".log"}:
            content = p.read_text(encoding="utf-8", errors="replace")
            return templates.TemplateResponse(
                "artifact_view.html",
                {"request": request, "run_id": run_id, "rel": rel_path, "content": content},
            )
        # それ以外はダウンロードへ誘導
        return RedirectResponse(url=f"/runs/{run_id}/artifacts/download/{rel_path}", status_code=302)

    @app.get("/runs/{run_id}/artifacts/download/{rel_path:path}")
    def runs_artifact_download(run_id: str, rel_path: str):
        try:
            p = _resolve_artifact_path(run_id, rel_path)
        except Exception:
            return HTMLResponse("invalid path", status_code=400)
        if not p.exists() or not p.is_file():
            return HTMLResponse("not found", status_code=404)
        return FileResponse(path=str(p), filename=p.name)

    @app.get("/runs/{run_id}/template/{kind}", response_class=HTMLResponse)
    def runs_template_preview(request: Request, run_id: str, kind: str):
        run = repo.get_run(run_id)
        # workdirはrun作成時に保存されている想定だが、無い場合はrun_idから組み立て
        base_dir = Path(run.workdir) if run.workdir else Path("data") / "runs" / run_id
        if kind == "draft":
            p = base_dir / "work" / "template_draft.md"
        elif kind == "filled":
            p = base_dir / "out" / "template_filled.md"
        else:
            return HTMLResponse("invalid kind", status_code=400)

        if p.exists():
            content = p.read_text(encoding="utf-8", errors="replace")
        else:
            content = ""  # ファイルが存在しない場合は空（UIで「-」表示）
        return templates.TemplateResponse("partials/template.html", {"request": request, "content": content, "kind": kind})

    @app.get("/runs/{run_id}/partials/template", response_class=HTMLResponse)
    def runs_template_partial(request: Request, run_id: str, kind: str = "draft"):
        # 互換: query param で kind を受ける（design docのI/F）
        return runs_template_preview(request=request, run_id=run_id, kind=kind)

    @app.get("/runs/{run_id}/events")
    async def runs_events(request: Request, run_id: str):
        last_id_hdr = request.headers.get("Last-Event-ID")
        try:
            last_id = int(last_id_hdr) if last_id_hdr else 0
        except Exception:
            last_id = 0

        async def gen() -> AsyncGenerator[bytes, None]:
            nonlocal last_id
            # 最初のkeep-alive
            yield b": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break

                new_events = events.list(run_id, after_event_id=(last_id or None), limit=200)
                if not new_events:
                    yield b": keepalive\n\n"
                    await asyncio.sleep(0.5)
                    continue

                for ev in new_events:
                    last_id = ev.event_id
                    payload = {
                        "event_id": ev.event_id,
                        "ts": ev.ts.isoformat(),
                        "event_type": ev.event_type,
                        "payload": ev.payload,
                    }
                    data = json.dumps(payload, ensure_ascii=False)
                    msg = f"id: {ev.event_id}\nevent: {ev.event_type}\ndata: {data}\n\n"
                    yield msg.encode("utf-8")

        return StreamingResponse(gen(), media_type="text/event-stream")

    return app


app = create_app()
