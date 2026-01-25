from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from document_process_app.service import RunServiceError
from document_process_app.web.recipes import get_recipe, list_recipes
from document_process_app.web.routes._helpers import get_run_base_dir, resolve_user_artifact_path
from document_process_app.web.sse import stream_run_events

router = APIRouter(prefix="/u")

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Jinja2 の tojson フィルタが \uXXXX エスケープで出力しないようにする
try:
    templates.env.policies.setdefault("json.dumps_kwargs", {})
    templates.env.policies["json.dumps_kwargs"].update({"ensure_ascii": False})
except Exception:
    pass


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _recipe_or_404(usecase: str) -> Optional[dict[str, Any]]:
    recipe = get_recipe(usecase)
    if not recipe:
        return None
    return {
        "recipe": recipe,
        "usecase": recipe.recipe_id,
    }


def _run_for_usecase(repo, run_id: str, usecase: str):
    run = repo.get_run(run_id)
    params = run.params or {}
    rid = str(params.get("recipe_id") or "").strip()
    if rid != str(usecase):
        return None
    return run


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def usecase_index(request: Request):
    return templates.TemplateResponse(
        "user/usecase_index.html",
        {"request": request, "recipes": list_recipes()},
    )


@router.get("/{usecase}", response_class=HTMLResponse)
def usecase_home(request: Request, usecase: str):
    ctx = _recipe_or_404(usecase)
    if ctx is None:
        return HTMLResponse("usecase not found", status_code=404)
    repo = request.app.state.repo
    runs = repo.list_runs(limit=200)
    recent_runs: list[dict[str, Any]] = []
    for run in runs:
        params = run.params or {}
        rid = str(params.get("recipe_id") or "").strip()
        if rid != str(usecase):
            continue
        base_dir = Path(run.workdir) if run.workdir else (Path("data") / "runs" / run.run_id)
        base_dir = base_dir.resolve()
        has_output = (base_dir / "out" / "template_filled.md").exists()
        recent_runs.append(
            {
                "run_id": run.run_id,
                "run_id_short": run.run_id[:8],
                "status": run.status,
                "created_at": run.created_at,
                "finished_at": run.finished_at,
                "has_output": has_output,
            }
        )
        if len(recent_runs) >= 20:
            break
    return templates.TemplateResponse(
        "user/usecase_home.html",
        {
            "request": request,
            "recent_runs": recent_runs,
            **ctx,
        },
    )


@router.get("/{usecase}/runs/new", response_class=HTMLResponse)
def runs_new(request: Request, usecase: str):
    ctx = _recipe_or_404(usecase)
    if ctx is None:
        return HTMLResponse("usecase not found", status_code=404)
    doc_repo = request.app.state.executor.artifacts.doc_repo
    documents = doc_repo.list_all()
    return templates.TemplateResponse(
        "user/run_new.html",
        {
            "request": request,
            "documents": documents,
            **ctx,
        },
    )


@router.post("/{usecase}/runs")
async def runs_create(
    request: Request,
    usecase: str,
    docs: Optional[list[UploadFile]] = File(None),
    doc_hashes: Optional[list[str]] = Form(None),
    request_text: str = Form(""),
    template_file: Optional[UploadFile] = File(None),
    # hidden + checkbox (0/1)
    hil_enabled: str = Form("0"),
    # user向けはreal固定
    mode: str = Form("real"),
    pdf_mode: str = Form("fast"),
    pdf_start_page: str = Form("1"),
    pdf_end_page: str = Form(""),
    pdf_batch_size: str = Form("5"),
    pdf_use_image: Optional[str] = Form(None),
    summarize_ast: str = Form("1"),
    ast_summary_model: str = Form(""),
    llm_complex_model: str = Form(""),
    ast_builder_policy: str = Form("auto"),
    ast_bypass_max_chars: str = Form("5000"),
    start_now: Optional[str] = Form("on"),
):
    recipe = get_recipe(usecase)
    if not recipe:
        return HTMLResponse("usecase not found", status_code=404)

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
            start_now=start_now,
            recipe_id=recipe.recipe_id,
            recipe_defaults=recipe.normalize_defaults(),
        )
    except RunServiceError as exc:
        return HTMLResponse(str(exc), status_code=400)

    return RedirectResponse(url=f"/u/{recipe.recipe_id}/runs/{result.run.run_id}", status_code=303)


@router.get("/{usecase}/runs/{run_id}", response_class=HTMLResponse)
def runs_detail(request: Request, usecase: str, run_id: str):
    ctx = _recipe_or_404(usecase)
    if ctx is None:
        return HTMLResponse("usecase not found", status_code=404)
    repo = request.app.state.repo
    run = _run_for_usecase(repo, run_id, usecase)
    if run is None:
        return HTMLResponse("run not found", status_code=404)
    return templates.TemplateResponse(
        "user/run_detail.html",
        {"request": request, "run": run, **ctx},
    )


@router.get("/{usecase}/runs/{run_id}/partials/status", response_class=HTMLResponse)
def runs_status_partial(request: Request, usecase: str, run_id: str):
    ctx = _recipe_or_404(usecase)
    if ctx is None:
        return HTMLResponse("usecase not found", status_code=404)
    repo = request.app.state.repo
    run = _run_for_usecase(repo, run_id, usecase)
    if run is None:
        return HTMLResponse("run not found", status_code=404)
    return templates.TemplateResponse(
        "user/partials/status.html",
        {"request": request, "run": run, **ctx},
    )


@router.post("/{usecase}/runs/{run_id}/start", response_class=HTMLResponse)
def runs_start(request: Request, usecase: str, run_id: str):
    ctx = _recipe_or_404(usecase)
    if ctx is None:
        return HTMLResponse("usecase not found", status_code=404)
    run_service = request.app.state.run_service
    repo = request.app.state.repo
    run = _run_for_usecase(repo, run_id, usecase)
    if run is None:
        return HTMLResponse("run not found", status_code=404)
    run_service.start_run(run_id)
    run = _run_for_usecase(repo, run_id, usecase) or run
    return templates.TemplateResponse(
        "partials/status.html",
        {"request": request, "run": run, **ctx},
    )


@router.get("/{usecase}/runs/{run_id}/partials/output", response_class=HTMLResponse)
def runs_output_partial(request: Request, usecase: str, run_id: str):
    ctx = _recipe_or_404(usecase)
    if ctx is None:
        return HTMLResponse("usecase not found", status_code=404)
    repo = request.app.state.repo
    run = _run_for_usecase(repo, run_id, usecase)
    if run is None:
        return HTMLResponse("run not found", status_code=404)
    base_dir = get_run_base_dir(repo, run_id)
    filled_path = (base_dir / "out" / "template_filled.md").resolve()
    draft_path = (base_dir / "work" / "template_draft.md").resolve()

    filled = _read_text(filled_path) if filled_path.exists() else ""
    draft = _read_text(draft_path) if draft_path.exists() else ""

    return templates.TemplateResponse(
        "user/partials/output.html",
        {
            "request": request,
            "run": run,
            "filled_content": filled,
            "draft_content": draft,
            "filled_path": "out/template_filled.md",
            "draft_path": "work/template_draft.md",
            **ctx,
        },
    )


@router.get("/{usecase}/runs/{run_id}/events")
async def runs_events(request: Request, usecase: str, run_id: str):
    ctx = _recipe_or_404(usecase)
    if ctx is None:
        return HTMLResponse("usecase not found", status_code=404)
    repo = request.app.state.repo
    run = _run_for_usecase(repo, run_id, usecase)
    if run is None:
        return HTMLResponse("run not found", status_code=404)
    allowed = {
        "run_status_changed",
        "step_started",
        "step_finished",
        "step_failed",
        "step_skipped",
        "agent_start",
        "agent_end",
    }

    def sanitize_payload(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        if event_type.startswith("step_"):
            return {"step": payload.get("step")}
        if event_type.startswith("agent_"):
            return {
                "agent_name": payload.get("agent_name"),
                "is_subagent": bool(payload.get("is_subagent")),
                "invocation_id": payload.get("invocation_id"),
                "parent_invocation_id": payload.get("parent_invocation_id"),
                "depth": payload.get("depth"),
            }
        if event_type == "run_status_changed":
            return {"status": payload.get("status")}
        return {}
    return stream_run_events(
        request,
        request.app.state.events,
        run_id,
        allowed_types=allowed,
        sanitize=sanitize_payload,
    )


@router.get("/{usecase}/runs/{run_id}/partials/artifacts", response_class=HTMLResponse)
def runs_artifacts_partial(request: Request, usecase: str, run_id: str):
    ctx = _recipe_or_404(usecase)
    if ctx is None:
        return HTMLResponse("usecase not found", status_code=404)
    repo = request.app.state.repo
    run = _run_for_usecase(repo, run_id, usecase)
    if run is None:
        return HTMLResponse("run not found", status_code=404)
    artifacts_repo = request.app.state.artifacts_repo
    recipe = ctx["recipe"]

    try:
        rows = artifacts_repo.list_artifacts(run_id)
    except Exception:
        rows = []

    items: list[dict[str, Any]] = []
    for r in rows:
        rel = str(r.get("path") or "")
        if not rel:
            continue
        if not _is_path_allowed(rel, recipe.allowed_artifact_prefixes):
            continue
        items.append(
            {
                "rel": rel,
                "kind": r.get("kind"),
                "updated_at": r.get("updated_at"),
                "size": None,
            }
        )

    return templates.TemplateResponse(
        "user/partials/artifacts.html",
        {"request": request, "run_id": run_id, "items": items, **ctx},
    )


@router.get("/{usecase}/runs/{run_id}/artifacts/view/{rel_path:path}", response_class=HTMLResponse)
def runs_artifact_view(request: Request, usecase: str, run_id: str, rel_path: str):
    recipe = get_recipe(usecase)
    if not recipe:
        return HTMLResponse("usecase not found", status_code=404)
    repo = request.app.state.repo
    if _run_for_usecase(repo, run_id, usecase) is None:
        return HTMLResponse("run not found", status_code=404)
    try:
        p = resolve_user_artifact_path(
            repo,
            run_id,
            rel_path,
            allowed_prefixes=recipe.allowed_artifact_prefixes,
        )
    except Exception:
        return HTMLResponse("invalid path", status_code=400)

    if not p.exists() or not p.is_file():
        return HTMLResponse("not found", status_code=404)

    ext = p.suffix.lower()
    if ext in {".txt", ".md", ".json", ".jsonl", ".log"}:
        content = p.read_text(encoding="utf-8", errors="replace")
        return templates.TemplateResponse(
            "user/artifact_view.html",
            {"request": request, "run_id": run_id, "rel": rel_path, "content": content, "usecase": usecase},
        )
    return RedirectResponse(url=f"/u/{usecase}/runs/{run_id}/artifacts/download/{rel_path}", status_code=302)


@router.get("/{usecase}/runs/{run_id}/artifacts/download/{rel_path:path}")
def runs_artifact_download(request: Request, usecase: str, run_id: str, rel_path: str):
    recipe = get_recipe(usecase)
    if not recipe:
        return HTMLResponse("usecase not found", status_code=404)
    repo = request.app.state.repo
    if _run_for_usecase(repo, run_id, usecase) is None:
        return HTMLResponse("run not found", status_code=404)
    try:
        p = resolve_user_artifact_path(
            repo,
            run_id,
            rel_path,
            allowed_prefixes=recipe.allowed_artifact_prefixes,
        )
    except Exception:
        return HTMLResponse("invalid path", status_code=400)
    if not p.exists() or not p.is_file():
        return HTMLResponse("not found", status_code=404)
    return FileResponse(path=str(p), filename=p.name)


@router.get("/{usecase}/runs/{run_id}/partials/hitl", response_class=HTMLResponse)
def runs_hitl_partial(request: Request, usecase: str, run_id: str):
    ctx = _recipe_or_404(usecase)
    if ctx is None:
        return HTMLResponse("usecase not found", status_code=404)
    repo = request.app.state.repo
    run = _run_for_usecase(repo, run_id, usecase)
    if run is None:
        return HTMLResponse("run not found", status_code=404)
    base_dir = get_run_base_dir(repo, run_id)
    hitl = None
    try:
        p = (base_dir / "work" / "hitl_interrupt.json")
        if p.exists():
            hitl = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        hitl = None
    return templates.TemplateResponse(
        "user/partials/hitl.html",
        {"request": request, "run": run, "hitl": hitl, **ctx},
    )


@router.post("/{usecase}/runs/{run_id}/hitl/respond", response_class=HTMLResponse)
async def runs_hitl_respond(request: Request, usecase: str, run_id: str, answer: str = Form("")):
    ctx = _recipe_or_404(usecase)
    if ctx is None:
        return HTMLResponse("usecase not found", status_code=404)
    repo = request.app.state.repo
    run_service = request.app.state.run_service
    run = _run_for_usecase(repo, run_id, usecase)
    if run is None:
        return HTMLResponse("run not found", status_code=404)
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
            "user/partials/hitl.html",
            {
                "request": request,
                "run": run,
                "hitl": hitl if isinstance(hitl, dict) else None,
                "flash_message": "回答が空です。回答を入力してください。",
                **ctx,
            },
        )

    actions = hitl.get("action_requests") if isinstance(hitl, dict) else None
    if not isinstance(actions, list) or not actions:
        return templates.TemplateResponse(
            "user/partials/hitl.html",
            {"request": request, "run": run, "hitl": None, "flash_message": "HITLの保留内容が見つかりません。", **ctx},
        )
    if len(actions) != 1:
        return templates.TemplateResponse(
            "user/partials/hitl.html",
            {
                "request": request,
                "run": run,
                "hitl": hitl if isinstance(hitl, dict) else None,
                "flash_message": f"複数の承認待ち({len(actions)})は未対応です（現状は1件のみ対応）。",
                **ctx,
            },
        )

    a0 = actions[0] if isinstance(actions[0], dict) else {}
    name = str(a0.get("name") or "")
    args = a0.get("args") if isinstance(a0.get("args"), dict) else {}

    if name != "human_input":
        return templates.TemplateResponse(
            "user/partials/hitl.html",
            {
                "request": request,
                "run": run,
                "hitl": hitl if isinstance(hitl, dict) else None,
                "flash_message": f"未対応のHITLツールです: {name}",
                **ctx,
            },
        )

    edited_args = dict(args or {})
    edited_args["answer"] = ans
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
    try:
        if interrupt_path.exists():
            interrupt_path.unlink()
    except Exception:
        pass

    run_service.start_run(run_id)
    run2 = repo.get_run(run_id)
    return templates.TemplateResponse(
        "user/partials/hitl.html",
        {"request": request, "run": run2, "hitl": None, "flash_message": "回答を受け付けました。再開しました。", **ctx},
    )


def _is_path_allowed(rel_path: str, allowed_prefixes: list[str]) -> bool:
    rel = str(rel_path or "").replace("\\", "/")
    for prefix in allowed_prefixes or []:
        pref = str(prefix or "").replace("\\", "/")
        if not pref:
            continue
        if rel == pref or rel.startswith(pref):
            return True
    return False
