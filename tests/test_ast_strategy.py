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


# ========================================
# Scenario 11: AST Branch Routing Tests
# ========================================


def test_strategy_auto_long_text_blueprint(tmp_path: Path) -> None:
    """5000文字超のテキストでblueprint戦略が選択されることを確認する（scenario11）。"""
    docs = [{"doc_id": "d1", "filename": "long_document.txt"}]
    ctx = _make_ctx(tmp_path, docs, params={"ast_bypass_max_chars": 5000})
    txt_path = Path(ctx.paths["work_dir"]) / "doc_d1.txt"
    # 5000文字超のテキストを生成
    txt_path.write_text("x" * 5001, encoding="utf-8")

    strategy, info = _select_ast_builder_strategy(ctx, "d1", text_path=txt_path)
    assert strategy == "blueprint", f"Expected 'blueprint' but got '{strategy}'"
    assert info.get("char_count") == 5001
    assert info.get("reason") == "default"


def test_strategy_force_blueprint(tmp_path: Path) -> None:
    """ast_force_blueprint_doc_idsで強制的にblueprint戦略が選択されることを確認する（scenario11）。"""
    docs = [{"doc_id": "d1", "filename": "short.txt"}]
    ctx = _make_ctx(tmp_path, docs, params={
        "ast_bypass_max_chars": 5000,
        "ast_force_blueprint_doc_ids": ["d1"]
    })
    txt_path = Path(ctx.paths["work_dir"]) / "doc_d1.txt"
    # 短いテキスト（通常はllm_direct）
    txt_path.write_text("short", encoding="utf-8")

    strategy, info = _select_ast_builder_strategy(ctx, "d1", text_path=txt_path)
    assert strategy == "blueprint", f"Expected 'blueprint' but got '{strategy}'"
    assert info.get("reason") == "force_blueprint"


def test_strategy_policy_override_markdown(tmp_path: Path) -> None:
    """ast_builder_policy='markdown'で強制的にmarkdown戦略が選択されることを確認する（scenario11）。"""
    docs = [{"doc_id": "d1", "filename": "plain.txt"}]  # .txt拡張子
    ctx = _make_ctx(tmp_path, docs, params={"ast_builder_policy": "markdown"})
    txt_path = Path(ctx.paths["work_dir"]) / "doc_d1.txt"
    txt_path.write_text("plain text content", encoding="utf-8")

    strategy, info = _select_ast_builder_strategy(ctx, "d1", text_path=txt_path)
    assert strategy == "markdown", f"Expected 'markdown' but got '{strategy}'"
    assert info.get("reason") == "policy:markdown"


def test_strategy_policy_override_llm_direct(tmp_path: Path) -> None:
    """ast_builder_policy='llm_direct'で強制的にllm_direct戦略が選択されることを確認する（scenario11）。"""
    docs = [{"doc_id": "d1", "filename": "long.txt"}]
    ctx = _make_ctx(tmp_path, docs, params={"ast_builder_policy": "llm_direct"})
    txt_path = Path(ctx.paths["work_dir"]) / "doc_d1.txt"
    # 長いテキスト（通常はblueprint）
    txt_path.write_text("x" * 10000, encoding="utf-8")

    strategy, info = _select_ast_builder_strategy(ctx, "d1", text_path=txt_path)
    assert strategy == "llm_direct", f"Expected 'llm_direct' but got '{strategy}'"
    assert info.get("reason") == "policy:llm_direct"


def test_strategy_policy_override_blueprint(tmp_path: Path) -> None:
    """ast_builder_policy='blueprint'で強制的にblueprint戦略が選択されることを確認する（scenario11）。"""
    docs = [{"doc_id": "d1", "filename": "doc.md"}]  # .md拡張子（通常はmarkdown）
    ctx = _make_ctx(tmp_path, docs, params={"ast_builder_policy": "blueprint"})
    txt_path = Path(ctx.paths["work_dir"]) / "doc_d1.txt"
    txt_path.write_text("# Title\nContent", encoding="utf-8")

    strategy, info = _select_ast_builder_strategy(ctx, "d1", text_path=txt_path)
    assert strategy == "blueprint", f"Expected 'blueprint' but got '{strategy}'"
    assert info.get("reason") == "policy:blueprint"


def test_strategy_with_branch_test_files() -> None:
    """実際のbranch_*テストファイルで分岐が正しく動作することを確認する（scenario11）。

    - branch_short_under_5k.txt → llm_direct
    - branch_long_over_5k.txt → blueprint
    - branch_markdown.md → markdown
    """
    import tempfile
    from pathlib import Path as P

    project_root = P(__file__).resolve().parent.parent
    data_input = project_root / "data" / "input"

    test_cases = [
        ("branch_short_under_5k.txt", "llm_direct", "short"),
        ("branch_long_over_5k.txt", "blueprint", "long"),
        ("branch_markdown.md", "markdown", "markdown"),
    ]

    for filename, expected_strategy, case_name in test_cases:
        file_path = data_input / filename
        if not file_path.exists():
            continue  # ファイルがない場合はスキップ

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = P(tmp)
            docs = [{"doc_id": "d1", "filename": filename}]
            ctx = _make_ctx(tmp_path, docs, params={"ast_bypass_max_chars": 5000})

            # ファイルをwork_dirにコピー
            txt_path = P(ctx.paths["work_dir"]) / "doc_d1.txt"
            txt_path.write_text(file_path.read_text(encoding="utf-8"), encoding="utf-8")

            strategy, info = _select_ast_builder_strategy(ctx, "d1", text_path=txt_path)
            assert strategy == expected_strategy, (
                f"[{case_name}] Expected '{expected_strategy}' but got '{strategy}' "
                f"(file: {filename}, char_count: {info.get('char_count')})"
            )
