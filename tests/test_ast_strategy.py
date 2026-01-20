from __future__ import annotations

import json
from pathlib import Path

from document_process_app.core.pipeline import RunContext
from document_process_app.core.real_steps import _select_ast_builder_strategy


class _DummyEvents:
    def emit(self, run_id: str, event_type: str, payload: dict) -> None:
        return None

    def list(self, run_id: str, *, after_event_id: int | None = None, limit: int = 200):
        return []


class _DummyCancel:
    def is_cancelled(self) -> bool:
        return False


def _make_ctx(tmp_path: Path, docs: list[dict], params: dict) -> RunContext:
    run_dir = tmp_path / "run"
    work_dir = run_dir / "work"
    input_dir = run_dir / "input"
    out_dir = run_dir / "out"
    log_dir = run_dir / "log"
    cache_dir = run_dir / "cache"
    for d in [work_dir, input_dir, out_dir, log_dir, cache_dir]:
        d.mkdir(parents=True, exist_ok=True)

    (run_dir / "config.json").write_text(json.dumps({"documents": docs}, ensure_ascii=False, indent=2), encoding="utf-8")
    return RunContext(
        run_id="test_run",
        params=params,
        events=_DummyEvents(),
        cancellation=_DummyCancel(),
        paths={
            "run_dir": str(run_dir),
            "work_dir": str(work_dir),
            "input_dir": str(input_dir),
            "out_dir": str(out_dir),
            "log_dir": str(log_dir),
            "cache_dir": str(cache_dir),
        },
    )


def test_strategy_auto_markdown(tmp_path: Path) -> None:
    docs = [{"doc_id": "d1", "filename": "sample.md"}]
    ctx = _make_ctx(tmp_path, docs, params={})
    txt_path = Path(ctx.paths["work_dir"]) / "doc_d1.txt"
    txt_path.write_text("# Title\nbody", encoding="utf-8")

    strategy, info = _select_ast_builder_strategy(ctx, "d1", text_path=txt_path)
    assert strategy == "markdown"
    assert info.get("filename") == "sample.md"


def test_strategy_auto_short_text(tmp_path: Path) -> None:
    docs = [{"doc_id": "d1", "filename": "sample.txt"}]
    ctx = _make_ctx(tmp_path, docs, params={"ast_bypass_max_chars": 5000})
    txt_path = Path(ctx.paths["work_dir"]) / "doc_d1.txt"
    txt_path.write_text("short text", encoding="utf-8")

    strategy, info = _select_ast_builder_strategy(ctx, "d1", text_path=txt_path)
    assert strategy == "llm_direct"
    assert info.get("char_count") is not None
