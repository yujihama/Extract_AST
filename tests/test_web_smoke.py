from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from document_process_app.web.app import create_app


def _set_test_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DOCUMENT_PROCESS_APP_DB_PATH", str(tmp_path / "test_document_process_app.db"))
    monkeypatch.setenv("DOCUMENT_PROCESS_APP_RUNS_ROOT", str(tmp_path / "runs"))
    monkeypatch.setenv("DOCUMENT_PROCESS_APP_LOAD_DOTENV", "0")


def test_admin_pages_smoke(monkeypatch, tmp_path: Path):
    _set_test_env(monkeypatch, tmp_path)
    app = create_app()
    client = TestClient(app)

    resp_index = client.get("/admin")
    assert resp_index.status_code == 200

    resp_new = client.get("/admin/runs/new")
    assert resp_new.status_code == 200


def test_api_create_run_smoke(monkeypatch, tmp_path: Path):
    _set_test_env(monkeypatch, tmp_path)
    app = create_app()
    client = TestClient(app)

    payload = {
        "documents": [
            {"text": "doc a"},
            {"text": "doc b"},
        ],
        "mode": "dummy",
        "request_text": "api smoke",
        "hil_enabled": False,
    }
    resp = client.post("/api/runs", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert "run_id" in body
    assert body.get("status") == "queued"
