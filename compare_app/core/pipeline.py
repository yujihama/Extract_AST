from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Protocol, Sequence

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

    def _normalize_steps_include(self, raw: Any) -> list[str] | None:
        if raw is None:
            return None
        if isinstance(raw, str):
            return [raw]
        if isinstance(raw, Iterable):
            items: list[str] = []
            for item in raw:
                if item is None:
                    continue
                items.append(str(item))
            return items or None
        return [str(raw)]

    def _resolve_step_range(self, name_to_index: dict[str, int], step_from: Any, step_to: Any) -> tuple[int, int] | None:
        start = 0
        end = len(name_to_index) - 1
        if step_from is not None:
            step_from = str(step_from)
            if step_from not in name_to_index:
                return None
            start = name_to_index[step_from]
        if step_to is not None:
            step_to = str(step_to)
            if step_to not in name_to_index:
                return None
            end = name_to_index[step_to]
        if start > end:
            return None
        return start, end

    def _resolve_allowed_steps(self, step_names: list[str], params: dict[str, Any]) -> set[str] | None:
        steps_include = self._normalize_steps_include(params.get("steps_include"))
        step_from = params.get("step_from")
        step_to = params.get("step_to")

        if steps_include is None and step_from is None and step_to is None:
            return None

        allowed = set(step_names)
        if steps_include is not None:
            allowed &= set(steps_include)

        if step_from is not None or step_to is not None:
            name_to_index = {name: idx for idx, name in enumerate(step_names)}
            step_range = self._resolve_step_range(name_to_index, step_from, step_to)
            if step_range is None:
                return set()
            range_names = set(step_names[step_range[0] : step_range[1] + 1])
            allowed &= range_names

        return allowed

    def run(self, ctx: RunContext) -> None:
        step_names = [step.name for step in self._steps]
        allowed_steps = self._resolve_allowed_steps(step_names, ctx.params)

        for step in self._steps:
            if ctx.cancellation.is_cancelled():
                ctx.events.emit(
                    ctx.run_id,
                    "run_cancelled",
                    {"ts": _utcnow().isoformat(), "where": "before_step", "step": step.name},
                )
                raise CancelledError(f"cancelled before step: {step.name}")

            if allowed_steps is not None and step.name not in allowed_steps:
                ctx.events.emit(
                    ctx.run_id,
                    "step_skipped",
                    {"ts": _utcnow().isoformat(), "step": step.name},
                )
                continue

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
