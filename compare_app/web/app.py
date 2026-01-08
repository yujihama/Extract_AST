from __future__ import annotations

import json
import asyncio
import os
import tempfile
from pathlib import Path
from typing import Any, AsyncGenerator, Optional

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from compare_app.bootstrap import build_default_executor


TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def create_app() -> FastAPI:
    app = FastAPI(title="compare_agent (MVP)")

    executor, repo, events, artifacts_repo = build_default_executor()
    app.state.executor = executor
    app.state.repo = repo
    app.state.events = events
    app.state.artifacts_repo = artifacts_repo

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

    @app.get("/runs/new", response_class=HTMLResponse)
    def runs_new(request: Request):
        return templates.TemplateResponse("run_new.html", {"request": request})

    def _save_upload_to_temp(upload: UploadFile) -> str:
        # Windowsでも扱いやすいように NamedTemporaryFile(delete=False)
        suffix = Path(upload.filename or "").suffix
        fd, tmp_path = tempfile.mkstemp(prefix="compare_app_upload_", suffix=suffix)
        os.close(fd)
        with open(tmp_path, "wb") as f:
            while True:
                chunk = upload.file.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
        return tmp_path

    def _write_text_to_temp(text: str, *, suffix: str = ".txt") -> str:
        fd, tmp_path = tempfile.mkstemp(prefix="compare_app_text_", suffix=suffix)
        os.close(fd)
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        return tmp_path

    @app.post("/api/runs/text")
    async def api_runs_create_from_text(payload: dict[str, Any] = Body(...)):
        doc_a_text = payload.get("doc_a_text")
        doc_b_text = payload.get("doc_b_text")
        if not isinstance(doc_a_text, str) or not isinstance(doc_b_text, str):
            raise HTTPException(status_code=400, detail="doc_a_text/doc_b_text must be provided as string")

        params = payload.get("params") or {}
        if not isinstance(params, dict):
            params = {"params": params}

        # mode をpayload直下でも受ける
        if "mode" in payload and "mode" not in params:
            params["mode"] = payload.get("mode")

        start_now = bool(payload.get("start_now", False))

        tmp_a = _write_text_to_temp(doc_a_text, suffix=".txt")
        tmp_b = _write_text_to_temp(doc_b_text, suffix=".txt")
        try:
            run = executor.create_run(doc_a_path=tmp_a, doc_b_path=tmp_b, params=params)
        finally:
            try:
                os.remove(tmp_a)
            except Exception:
                pass
            try:
                os.remove(tmp_b)
            except Exception:
                pass

        if start_now:
            executor.start(run.run_id)

        return JSONResponse({"run_id": run.run_id, "status": run.status})

    @app.post("/runs")
    async def runs_create(
        request: Request,
        doc_a: UploadFile = File(...),
        doc_b: UploadFile = File(...),
        mode: str = Form("dummy"),
        pdf_mode_a: str = Form("fast"),
        pdf_mode_b: str = Form("fast"),
        pdf_start_page: str = Form("1"),
        pdf_end_page: str = Form(""),
        pdf_batch_size: str = Form("5"),
        pdf_use_image: Optional[str] = Form(None),
        summarize_ast: Optional[str] = Form(None),
        ast_summary_model: str = Form("gpt-5-mini"),
        llm_complex_model: str = Form(""),
        start_now: Optional[str] = Form("on"),
    ):
        tmp_a = _save_upload_to_temp(doc_a)
        tmp_b = _save_upload_to_temp(doc_b)
        try:
            m = str(mode).lower().strip()
            if m not in {"dummy", "real"}:
                m = "dummy"
            params: dict[str, Any] = {"mode": m}

            # PDF→TXT（PDF入力のときだけ使用される）
            pma = str(pdf_mode_a).lower().strip()
            pmb = str(pdf_mode_b).lower().strip()
            if pma in {"fast", "llm"}:
                params["pdf_mode_a"] = pma
            if pmb in {"fast", "llm"}:
                params["pdf_mode_b"] = pmb
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
            params["summarize_ast"] = bool(summarize_ast)
            if str(ast_summary_model).strip():
                params["ast_summary_model"] = str(ast_summary_model).strip()

            # 任意: 複雑系モデル
            if str(llm_complex_model).strip():
                params["llm_complex_model"] = str(llm_complex_model).strip()

            run = executor.create_run(doc_a_path=tmp_a, doc_b_path=tmp_b, params=params)
        finally:
            # run配下へコピー後なので削除
            try:
                os.remove(tmp_a)
            except Exception:
                pass
            try:
                os.remove(tmp_b)
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
        return templates.TemplateResponse("partials/status.html", {"request": request, "run": run})

    def _get_run_base_dir(run_id: str) -> Path:
        run = repo.get_run(run_id)
        return Path(run.workdir) if run.workdir else (Path("data") / "runs" / run_id)

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
                items.append({"rel": rel, "kind": None, "size": st.st_size, "updated_at": None})
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
            content = "(not generated yet)"
        return templates.TemplateResponse("partials/template.html", {"request": request, "content": content})

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

