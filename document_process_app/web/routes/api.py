from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import APIRouter, Body, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from document_process_app.service import RunServiceError
from document_process_app.web.routes._helpers import get_work_file, parse_node_path, get_run_base_dir

router = APIRouter(prefix="/api")


@router.get("/documents")
def api_list_documents(request):
    """ドキュメント一覧API"""
    doc_repo = request.app.state.executor.artifacts.doc_repo
    docs = doc_repo.list_all()
    return JSONResponse([d.to_dict() for d in docs])


@router.get("/documents/{doc_hash}")
def api_get_document(request, doc_hash: str):
    """ドキュメント詳細API"""
    doc_repo = request.app.state.executor.artifacts.doc_repo
    doc = doc_repo.get(doc_hash)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    return JSONResponse(doc.to_dict())


@router.post("/documents")
async def api_upload_document(request, file: UploadFile = File(...)):
    """ドキュメントアップロードAPI"""
    doc_repo = request.app.state.executor.artifacts.doc_repo
    content = await file.read()
    doc = doc_repo.add(content, file.filename or "uploaded.txt")
    return JSONResponse(doc.to_dict())


@router.delete("/documents/{doc_hash}")
def api_delete_document(request, doc_hash: str):
    """ドキュメント削除API"""
    doc_repo = request.app.state.executor.artifacts.doc_repo
    deleted = doc_repo.delete(doc_hash)
    if not deleted:
        raise HTTPException(status_code=404, detail="Document not found")
    return JSONResponse({"deleted": True, "doc_hash": doc_hash})


@router.get("/runs")
def api_list_runs(request, limit: int = 50):
    repo = request.app.state.repo
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


@router.post("/runs")
async def api_runs_create(request, payload: dict[str, Any] = Body(...)):
    run_service = request.app.state.run_service
    try:
        result = run_service.create_from_payload(payload)
    except RunServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    run = result.run
    return JSONResponse(
        {
            "run_id": run.run_id,
            "status": run.status,
            "job_id": result.job_id,
        }
    )


@router.get("/runs/{run_id}")
def api_get_run(request, run_id: str):
    repo = request.app.state.repo
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


@router.delete("/runs/{run_id}")
def api_delete_run(request, run_id: str):
    """Run削除API（DB上のrun/events/artifacts + FSのrun dir）。"""
    repo = request.app.state.repo
    executor = request.app.state.executor
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
            import shutil

            shutil.rmtree(run.workdir, ignore_errors=True)
    except Exception:
        pass

    if not deleted:
        raise HTTPException(status_code=404, detail="run not found")
    return JSONResponse({"deleted": True, "run_id": run_id})


@router.post("/runs/{run_id}/start")
def api_start_run(request, run_id: str):
    run_service = request.app.state.run_service
    job_id = run_service.start_run(run_id)
    return JSONResponse({"ok": True, "run_id": run_id, "job_id": job_id})


@router.post("/runs/{run_id}/cancel")
def api_cancel_run(request, run_id: str):
    run_service = request.app.state.run_service
    run_service.cancel_run(run_id)
    return JSONResponse({"ok": True, "run_id": run_id})


@router.get("/runs/{run_id}/blueprint/{which}")
def api_get_blueprint(request, run_id: str, which: str):
    repo = request.app.state.repo
    w = str(which).lower()
    if w not in {"a", "b"}:
        raise HTTPException(status_code=400, detail="which must be 'a' or 'b'")
    bp = get_work_file(repo, run_id, f"blueprint_{w}.json")
    if not bp.exists():
        raise HTTPException(status_code=404, detail="blueprint not found")
    try:
        return JSONResponse(json.loads(bp.read_text(encoding="utf-8")))
    except Exception:
        return JSONResponse({"ok": False, "error": "failed to read blueprint"}, status_code=500)


@router.put("/runs/{run_id}/blueprint/{which}")
async def api_put_blueprint(request, run_id: str, which: str, payload: dict[str, Any] = Body(...)):
    repo = request.app.state.repo
    events = request.app.state.events
    w = str(which).lower()
    if w not in {"a", "b"}:
        raise HTTPException(status_code=400, detail="which must be 'a' or 'b'")
    bp = get_work_file(repo, run_id, f"blueprint_{w}.json")
    bp.parent.mkdir(parents=True, exist_ok=True)
    bp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    # artifact更新イベント
    try:
        base_dir = get_run_base_dir(repo, run_id)
        rel = bp.relative_to(base_dir).as_posix()
        events.emit(run_id, "artifact_updated", {"ts": "", "kind": f"blueprint_{w}", "path": rel})
    except Exception:
        pass
    return JSONResponse({"ok": True})


@router.get("/runs/{run_id}/blueprint/{which}/preview")
def api_preview_blueprint(request, run_id: str, which: str):
    repo = request.app.state.repo
    w = str(which).lower()
    if w not in {"a", "b"}:
        raise HTTPException(status_code=400, detail="which must be 'a' or 'b'")
    bp = get_work_file(repo, run_id, f"blueprint_{w}.json")
    txt = get_work_file(repo, run_id, f"doc_{w}.txt")
    if not bp.exists() or not txt.exists():
        raise HTTPException(status_code=404, detail="required files not found")
    from src.tools import preview_blueprint_headings

    out = preview_blueprint_headings.invoke({"blueprint_path": str(bp), "text_path": str(txt)})
    try:
        return JSONResponse(json.loads(out))
    except Exception:
        return JSONResponse({"ok": False, "raw": out})


@router.get("/runs/{run_id}/blueprint/{which}/validate")
def api_validate_blueprint(
    request,
    run_id: str,
    which: str,
    mode: str = "gaps",
    gap_threshold: int = 100,
    max_level: int = 3,
):
    repo = request.app.state.repo
    w = str(which).lower()
    if w not in {"a", "b"}:
        raise HTTPException(status_code=400, detail="which must be 'a' or 'b'")
    bp = get_work_file(repo, run_id, f"blueprint_{w}.json")
    txt = get_work_file(repo, run_id, f"doc_{w}.txt")
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


@router.get("/runs/{run_id}/ast/{which}")
def api_read_ast(
    request,
    run_id: str,
    which: str,
    mode: str = "summary",
    node_path: Optional[str] = None,
    title_query: Optional[str] = None,
    max_depth: int = 3,
    include_content: bool = False,
    max_content_chars: int = 500,
):
    repo = request.app.state.repo
    w = str(which).lower()
    if w not in {"a", "b"}:
        raise HTTPException(status_code=400, detail="which must be 'a' or 'b'")
    ast_path = get_work_file(repo, run_id, f"ast_{w}.ast.json")
    if not ast_path.exists():
        raise HTTPException(status_code=404, detail="ast not found")
    from src.tools import read_ast

    out = read_ast.invoke(
        {
            "file_path": str(ast_path),
            "mode": str(mode),
            "node_path": parse_node_path(node_path),
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


@router.get("/runs/{run_id}/compare/initial_matching")
def api_get_initial_matching(request, run_id: str):
    repo = request.app.state.repo
    p = get_work_file(repo, run_id, "initial_matching.json")
    if not p.exists():
        raise HTTPException(status_code=404, detail="initial_matching not found")
    try:
        return JSONResponse(json.loads(p.read_text(encoding="utf-8")))
    except Exception:
        return JSONResponse({"ok": False, "error": "failed to read initial_matching"}, status_code=500)


@router.get("/runs/{run_id}/events")
def api_list_events(request, run_id: str, after_event_id: Optional[int] = None, limit: int = 200):
    events = request.app.state.events
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
