from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping, Tuple

from compare_app.core.dummy_steps import DummyAgentTraceStep, DummyFillTemplateStep, DummySleepStep, DummyWriteTemplateDraftStep
from compare_app.core.pipeline import ConditionalStep, Pipeline
from compare_app.core.compare_steps import CompareAnalysisStep, CompareSetupStep, PreAnalysisStep
from compare_app.core.real_steps import BuildAstStep, BuildBlueprintStep, EnsureTextStep, SummarizeAstStep
from compare_app.core.run_executor import RunExecutor
from compare_app.compat.patch_src_tools import patch_src_tools_compare_state
from compare_app.infra.fs_artifacts import FileArtifactStore
from compare_app.infra.inmemory import InMemoryCancellationRegistry, InProcessJobQueue
from compare_app.infra.sqlite_store import SqliteArtifactRepository, SqliteEventSink, SqliteRunRepository, init_db


def _env(name: str, default: str) -> str:
    v = os.getenv(name)
    return v if v is not None and str(v).strip() else default


def _resolve_path(repo_root: Path, value: str) -> str:
    """env/引数のパスを repo_root 基準で解決する（相対パスのみ）。"""
    try:
        p = Path(value)
        if p.is_absolute():
            return str(p)
        return str((repo_root / p).resolve())
    except Exception:
        return str(value)


def build_default_executor() -> Tuple[RunExecutor, SqliteRunRepository, SqliteEventSink, SqliteArtifactRepository]:
    """UI/CLI共通のデフォルト構成（MVP）を組み立てる。

    - 永続化: SQLite（runs, run_events）
    - 成果物: FS（data/runs/{run_id}/...）
    - キュー: in-process（別スレッド）
    """
    # .env を自動読み込み（UI/CLI共通）
    # - 環境変数が直接設定されるケースにも対応するため、override=False で「未設定のみ補完」する。
    # - `.env` を使わない運用では COMPARE_APP_LOAD_DOTENV=0 で無効化できる。
    repo_root = Path(__file__).resolve().parent.parent

    try:
        load_flag = (os.getenv("COMPARE_APP_LOAD_DOTENV") or "1").strip().lower()
        if load_flag not in {"0", "false", "no", "off"}:
            from dotenv import load_dotenv

            dotenv_path = os.getenv("COMPARE_APP_DOTENV_PATH")
            if dotenv_path and str(dotenv_path).strip():
                load_dotenv(Path(dotenv_path), override=False)
            else:
                load_dotenv(repo_root / ".env", override=False)
    except Exception:
        # dotenvが無い/読めない場合でも、環境変数が既に設定されていれば動く
        pass

    # DB/Run保存先は「起動ディレクトリ」に依存しないよう repo_root 基準で固定する。
    default_db_path = str((repo_root / "data" / "compare_app.db").resolve())
    raw_db_path = _env("COMPARE_APP_DB_PATH", default_db_path)
    db_path = _resolve_path(repo_root, raw_db_path)
    init_db(db_path)

    # PoC由来のグローバル状態（COMPARE_STATE）をスレッドローカルへ（将来の並列実行に備える）
    patch_src_tools_compare_state()

    repo = SqliteRunRepository(db_path=db_path)
    events = SqliteEventSink(db_path=db_path)
    artifacts_repo = SqliteArtifactRepository(db_path=db_path)
    default_runs_root = str((repo_root / "data" / "runs").resolve())
    raw_runs_root = _env("COMPARE_APP_RUNS_ROOT", default_runs_root)
    runs_root = Path(_resolve_path(repo_root, raw_runs_root))
    artifacts = FileArtifactStore(runs_root=runs_root)
    cancellations = InMemoryCancellationRegistry()

    def _is_dummy(ctx) -> bool:
        return str(ctx.params.get("mode", "dummy")).lower() == "dummy"

    def _is_real(ctx) -> bool:
        return str(ctx.params.get("mode", "dummy")).lower() == "real"

    pipeline = Pipeline(
        steps=[
            # dummy
            ConditionalStep(DummySleepStep(name="dummy_prepare", seconds=0.6, tick=0.2), when=_is_dummy),
            ConditionalStep(DummyAgentTraceStep(), when=_is_dummy),
            ConditionalStep(DummyWriteTemplateDraftStep(), when=_is_dummy),
            ConditionalStep(DummySleepStep(name="dummy_analyze", seconds=0.8, tick=0.2), when=_is_dummy),
            ConditionalStep(DummyFillTemplateStep(), when=_is_dummy),
            # real（フェーズ4: 入力→txt→blueprint→AST）
            ConditionalStep(EnsureTextStep(name="ensure_text_a", which="a"), when=_is_real),
            ConditionalStep(EnsureTextStep(name="ensure_text_b", which="b"), when=_is_real),
            ConditionalStep(BuildBlueprintStep(name="build_blueprint_a", which="a"), when=_is_real),
            ConditionalStep(BuildBlueprintStep(name="build_blueprint_b", which="b"), when=_is_real),
            ConditionalStep(BuildAstStep(name="build_ast_a", which="a"), when=_is_real),
            ConditionalStep(BuildAstStep(name="build_ast_b", which="b"), when=_is_real),
            # AST枝サマリ（任意: params.summarize_ast=true のとき）
            ConditionalStep(SummarizeAstStep(name="summarize_ast_a", which="a"), when=_is_real),
            ConditionalStep(SummarizeAstStep(name="summarize_ast_b", which="b"), when=_is_real),
            # フェーズ5/6: Pre-Analysis → Compare-Analysis（テンプレ生成→段階更新→filled）
            ConditionalStep(CompareSetupStep(), when=_is_real),
            ConditionalStep(PreAnalysisStep(), when=_is_real),
            ConditionalStep(CompareAnalysisStep(), when=_is_real),
        ]
    )

    executor: RunExecutor | None = None

    def dispatcher(job_type: str, payload: Mapping[str, Any]) -> None:
        assert executor is not None
        if job_type == "run_pipeline":
            executor.execute(str(payload.get("run_id")))
            return
        raise ValueError(f"unknown job_type: {job_type}")

    job_queue = InProcessJobQueue(dispatcher=dispatcher, cancellations=cancellations)
    executor = RunExecutor(
        pipeline=pipeline,
        repo=repo,
        artifacts=artifacts,
        events=events,
        job_queue=job_queue,
        cancellations=cancellations,
    )

    return executor, repo, events, artifacts_repo

