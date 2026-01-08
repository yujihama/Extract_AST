from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Protocol, Sequence

from compare_app.contracts import CancellationToken, EventSink


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class RunContext:
    """stepが受け取る共通コンテキスト（UI/CLI共通）。"""

    run_id: str
    params: dict[str, Any]
    events: EventSink
    cancellation: CancellationToken
    paths: dict[str, str]  # run_dir/input/work/out/log/cache 等（MVPは辞書で開始）


class Step(Protocol):
    """Pipelineの最小単位。"""

    name: str

    def run(self, ctx: RunContext) -> None: ...


class SkippableStep(Step, Protocol):
    def should_run(self, ctx: RunContext) -> bool: ...


@dataclass
class ConditionalStep:
    step: Step
    when: Callable[[RunContext], bool]

    @property
    def name(self) -> str:
        return self.step.name

    def should_run(self, ctx: RunContext) -> bool:
        if not bool(self.when(ctx)):
            return False
        # 内包stepが should_run を持つ場合はそれも尊重（= mode条件 AND idempotency条件）
        if hasattr(self.step, "should_run"):
            return bool(getattr(self.step, "should_run")(ctx))  # type: ignore[misc]
        return True

    def run(self, ctx: RunContext) -> None:
        return self.step.run(ctx)


class CancelledError(RuntimeError):
    pass


class Pipeline:
    """step列を順に実行し、必ず step_* イベントをemitする。"""

    def __init__(self, steps: Sequence[Step]) -> None:
        self._steps = list(steps)

    def run(self, ctx: RunContext) -> None:
        for step in self._steps:
            if ctx.cancellation.is_cancelled():
                ctx.events.emit(
                    ctx.run_id,
                    "run_cancelled",
                    {"ts": _utcnow().isoformat(), "where": "before_step", "step": step.name},
                )
                raise CancelledError(f"cancelled before step: {step.name}")

            # 条件付きスキップ（MVP: ここで“走らない”を明示して次へ）
            if hasattr(step, "should_run"):
                try:
                    if not bool(getattr(step, "should_run")(ctx)):  # type: ignore[misc]
                        ctx.events.emit(
                            ctx.run_id,
                            "step_skipped",
                            {"ts": _utcnow().isoformat(), "step": step.name},
                        )
                        continue
                except Exception as e:
                    ctx.events.emit(
                        ctx.run_id,
                        "step_failed",
                        {"ts": _utcnow().isoformat(), "step": step.name, "error": str(e), "error_type": type(e).__name__},
                    )
                    raise

            ctx.events.emit(
                ctx.run_id,
                "step_started",
                {"ts": _utcnow().isoformat(), "step": step.name},
            )

            try:
                step.run(ctx)
            except CancelledError:
                ctx.events.emit(
                    ctx.run_id,
                    "run_cancelled",
                    {"ts": _utcnow().isoformat(), "where": "in_step", "step": step.name},
                )
                raise
            except Exception as e:
                ctx.events.emit(
                    ctx.run_id,
                    "step_failed",
                    {"ts": _utcnow().isoformat(), "step": step.name, "error": str(e), "error_type": type(e).__name__},
                )
                raise

            ctx.events.emit(
                ctx.run_id,
                "step_finished",
                {"ts": _utcnow().isoformat(), "step": step.name},
            )

