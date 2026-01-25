from __future__ import annotations

from fastapi import FastAPI

from document_process_app.bootstrap import build_default_executor
from document_process_app.service import RunService
from document_process_app.web.routes import admin, api


def create_app() -> FastAPI:
    app = FastAPI(title="document_process_agent (MVP)")

    executor, repo, events, artifacts_repo = build_default_executor()
    app.state.executor = executor
    app.state.repo = repo
    app.state.events = events
    app.state.artifacts_repo = artifacts_repo
    app.state.run_service = RunService(executor=executor)

    app.include_router(admin.router)
    app.include_router(api.router)

    return app


app = create_app()
