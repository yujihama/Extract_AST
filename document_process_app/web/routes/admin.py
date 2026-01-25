from __future__ import annotations

import asyncio
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator, Optional

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from document_process_app.service import RunServiceError
from document_process_app.web.routes._helpers import get_run_base_dir, resolve_artifact_path

router = APIRouter(prefix="/admin")

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Jinja2 の tojson フィルタが \uXXXX エスケープで出力しないようにする（UIで日本語が読めるように）
try:
    templates.env.policies.setdefault("json.dumps_kwargs", {})
    templates.env.policies["json.dumps_kwargs"].update({"ensure_ascii": False})
except Exception:
    # テンプレ初期化に失敗してもアプリ起動は継続（best effort）
    pass


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    repo = request.app.state.repo
    runs = repo.list_runs(limit=50)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "runs": runs,
            "db_path": getattr(repo, "db_path", None),
        },
    )


@router.get("/documents", response_class=HTMLResponse)
def documents_list(request: Request):
    """ドキュメント一覧画面"""
    doc_repo = request.app.state.executor.artifacts.doc_repo
    docs = doc_repo.list_all()
    return templates.TemplateResponse(
        "documents.html",
        {"request": request, "documents": docs},
    )


@router.delete("/documents/{doc_hash}", response_class=HTMLResponse)
def delete_document_ui(request: Request, doc_hash: str):
    """ドキュメント削除（UI用・HTMXから呼び出し）"""
    doc_repo = request.app.state.executor.artifacts.doc_repo
    deleted = doc_repo.delete(doc_hash)
    if not deleted:
        return HTMLResponse("<div class='alert alert-error'>ドキュメントが見つかりません</div>", status_code=404)
    # 削除成功時は空のレスポンス（行が消える）
    return HTMLResponse("")


@router.get("/runs/new", response_class=HTMLResponse)
def runs_new(request: Request):
    # ドキュメント中心アーキテクチャ: 既存ドキュメント一覧を取得
    doc_repo = request.app.state.executor.artifacts.doc_repo
    documents = doc_repo.list_all()

    return templates.TemplateResponse(
        "run_new.html",
        {
            "request": request,
            "documents": documents,
        },
    )


@router.post("/runs")
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
    run_service = request.app.state.run_service
    try:
        result = run_service.create_from_form(
            doc_hashes=doc_hashes,
            upload_docs=docs,
            request_text=request_text,
            template_file=template_file,
            hil_enabled=hil_enabled,
            mode=mode,
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
            start_now=start_now,
        )
    except RunServiceError as exc:
        return HTMLResponse(str(exc), status_code=400)

    return RedirectResponse(url=f"/admin/runs/{result.run.run_id}", status_code=303)


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def runs_detail(request: Request, run_id: str):
    repo = request.app.state.repo
    events = request.app.state.events
    run = repo.get_run(run_id)
    initial_events = events.list(run_id, limit=200)
    return templates.TemplateResponse(
        "run_detail.html",
        {"request": request, "run": run, "initial_events": initial_events},
    )


@router.delete("/runs/{run_id}", response_class=HTMLResponse)
def runs_delete_ui(request: Request, run_id: str):
    """Run削除（UI用・HTMXから呼び出し）。成功時は空で行を消す。"""
    repo = request.app.state.repo
    executor = request.app.state.executor
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


@router.post("/runs/{run_id}/start", response_class=HTMLResponse)
def runs_start(request: Request, run_id: str):
    run_service = request.app.state.run_service
    run_service.start_run(run_id)
    repo = request.app.state.repo
    run = repo.get_run(run_id)
    return templates.TemplateResponse("partials/status.html", {"request": request, "run": run})


@router.post("/runs/{run_id}/cancel", response_class=HTMLResponse)
def runs_cancel(request: Request, run_id: str):
    run_service = request.app.state.run_service
    run_service.cancel_run(run_id)
    repo = request.app.state.repo
    run = repo.get_run(run_id)
    return templates.TemplateResponse("partials/status.html", {"request": request, "run": run})


@router.get("/runs/{run_id}/partials/status", response_class=HTMLResponse)
def runs_status_partial(request: Request, run_id: str):
    repo = request.app.state.repo
    events = request.app.state.events
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


@router.get("/runs/{run_id}/partials/hitl", response_class=HTMLResponse)
def runs_hitl_partial(request: Request, run_id: str):
    """HITL（deepagents interrupt）で止まっている場合の質問表示。"""
    repo = request.app.state.repo
    run = repo.get_run(run_id)
    base_dir = get_run_base_dir(repo, run_id)
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


@router.post("/runs/{run_id}/hitl/respond", response_class=HTMLResponse)
async def runs_hitl_respond(request: Request, run_id: str, answer: str = Form("")):
    """HITL質問に回答し、同じrunを再開する（work/hitl_resume.json を書いて start）。"""
    repo = request.app.state.repo
    run_service = request.app.state.run_service
    run = repo.get_run(run_id)
    base_dir = get_run_base_dir(repo, run_id)
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

    run_service.start_run(run_id)
    run2 = repo.get_run(run_id)
    return templates.TemplateResponse(
        "partials/hitl.html",
        {"request": request, "run": run2, "hitl": None, "flash_message": "回答を受け付けました。再開しました。"},
    )


@router.get("/runs/{run_id}/partials/artifacts", response_class=HTMLResponse)
def runs_artifacts_partial(request: Request, run_id: str):
    artifacts_repo = request.app.state.artifacts_repo
    events = request.app.state.events
    base_dir = get_run_base_dir(request.app.state.repo, run_id)

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


@router.get("/runs/{run_id}/artifacts/view/{rel_path:path}", response_class=HTMLResponse)
def runs_artifact_view(request: Request, run_id: str, rel_path: str):
    try:
        p = resolve_artifact_path(request.app.state.repo, run_id, rel_path)
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
    return RedirectResponse(url=f"/admin/runs/{run_id}/artifacts/download/{rel_path}", status_code=302)


@router.get("/runs/{run_id}/artifacts/download/{rel_path:path}")
def runs_artifact_download(request: Request, run_id: str, rel_path: str):
    try:
        p = resolve_artifact_path(request.app.state.repo, run_id, rel_path)
    except Exception:
        return HTMLResponse("invalid path", status_code=400)
    if not p.exists() or not p.is_file():
        return HTMLResponse("not found", status_code=404)
    return FileResponse(path=str(p), filename=p.name)


@router.get("/runs/{run_id}/template/{kind}", response_class=HTMLResponse)
def runs_template_preview(request: Request, run_id: str, kind: str):
    repo = request.app.state.repo
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


@router.get("/runs/{run_id}/partials/template", response_class=HTMLResponse)
def runs_template_partial(request: Request, run_id: str, kind: str = "draft"):
    # 互換: query param で kind を受ける（design docのI/F）
    return runs_template_preview(request=request, run_id=run_id, kind=kind)


@router.get("/runs/{run_id}/events")
async def runs_events(request: Request, run_id: str):
    events = request.app.state.events
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
