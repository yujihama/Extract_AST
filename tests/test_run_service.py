from __future__ import annotations

from pathlib import Path

from document_process_app.bootstrap import build_default_executor
from document_process_app.service import RunService


def _set_test_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DOCUMENT_PROCESS_APP_DB_PATH", str(tmp_path / "test_document_process_app.db"))
    monkeypatch.setenv("DOCUMENT_PROCESS_APP_RUNS_ROOT", str(tmp_path / "runs"))
    monkeypatch.setenv("DOCUMENT_PROCESS_APP_LOAD_DOTENV", "0")


def test_run_service_cli_api_parity(monkeypatch, tmp_path: Path, sample_doc_a: Path, sample_doc_b: Path):
    _set_test_env(monkeypatch, tmp_path)
    executor, _, _, _ = build_default_executor()
    service = RunService(executor=executor)

    cli_result = service.create_from_cli(
        doc_paths=[str(sample_doc_a), str(sample_doc_b)],
        doc_hashes=[],
        request_text="parity check",
        hil_enabled=False,
        mode="dummy",
        params_raw={},
    )

    api_result = service.create_from_payload(
        {
            "documents": [
                {"path": str(sample_doc_a)},
                {"path": str(sample_doc_b)},
            ],
            "mode": "dummy",
            "request_text": "parity check",
            "hil_enabled": False,
        }
    )

    assert cli_result.run.params == api_result.run.params
    assert cli_result.run.params.get("request_text") == "parity check"
    assert cli_result.run.params.get("hil_enabled") is False
