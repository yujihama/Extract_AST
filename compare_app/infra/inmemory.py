from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional, Sequence

from compare_app.contracts import CancellationRegistry, CancellationToken, EventSink, JobQueue, RunEvent
from compare_app.models import RunRecord, RunStatus


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class InMemoryEventSink(EventSink):
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list[RunEvent] = []
        self._seq = 0

    def emit(self, run_id: str, event_type: str, payload: Mapping[str, Any]) -> None:
        with self._lock:
            self._seq += 1
            self._events.append(
                RunEvent(
                    event_id=self._seq,
                    run_id=run_id,
                    ts=_utcnow(),
                    event_type=str(event_type),
                    payload=dict(payload),
                )
            )

    def list(self, run_id: str, *, after_event_id: Optional[int] = None, limit: int = 200) -> Sequence[RunEvent]:
        with self._lock:
            evs = [e for e in self._events if e.run_id == run_id]
            if after_event_id is not None:
                evs = [e for e in evs if e.event_id > int(after_event_id)]
            return evs[: max(1, int(limit))]


class InMemoryCancellationToken(CancellationToken):
    def __init__(self) -> None:
        self._flag = threading.Event()

    def cancel(self) -> None:
        self._flag.set()

    def is_cancelled(self) -> bool:
        return self._flag.is_set()


class InMemoryCancellationRegistry(CancellationRegistry):
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tokens: dict[str, InMemoryCancellationToken] = {}

    def get(self, run_id: str) -> CancellationToken:
        with self._lock:
            tok = self._tokens.get(run_id)
            if tok is None:
                tok = InMemoryCancellationToken()
                self._tokens[run_id] = tok
            return tok

    def request_cancel(self, run_id: str) -> None:
        tok = self.get(run_id)
        if isinstance(tok, InMemoryCancellationToken):
            tok.cancel()


class InMemoryRunRepository:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: dict[str, RunRecord] = {}

    def create_run(self, run: RunRecord) -> None:
        with self._lock:
            self._runs[run.run_id] = run

    def get_run(self, run_id: str) -> RunRecord:
        with self._lock:
            return self._runs[run_id]

    def update_status(self, run_id: str, status: RunStatus, *, message: str | None = None) -> None:
        with self._lock:
            cur = self._runs[run_id]
            self._runs[run_id] = RunRecord(
                run_id=cur.run_id,
                status=status,
                created_at=cur.created_at,
                started_at=cur.started_at if status != "running" else (cur.started_at or _utcnow()),
                finished_at=cur.finished_at if status not in {"succeeded", "failed", "cancelled"} else (cur.finished_at or _utcnow()),
                params=cur.params,
                error_message=cur.error_message,
                workdir=cur.workdir,
            )

    def set_error(self, run_id: str, error_message: str) -> None:
        with self._lock:
            cur = self._runs[run_id]
            self._runs[run_id] = RunRecord(
                run_id=cur.run_id,
                status=cur.status,
                created_at=cur.created_at,
                started_at=cur.started_at,
                finished_at=cur.finished_at,
                params=cur.params,
                error_message=error_message,
                workdir=cur.workdir,
            )

    def list_runs(self, *, limit: int = 50, offset: int = 0) -> list[RunRecord]:
        with self._lock:
            runs = list(self._runs.values())
        runs.sort(key=lambda r: r.created_at, reverse=True)
        off = max(0, int(offset))
        lim = max(1, int(limit))
        return runs[off : off + lim]


@dataclass
class InProcessJobQueue(JobQueue):
    """MVP用の簡易ジョブキュー（別スレッドで関数を走らせる）。

    注意: 今はインターフェースと“形”の提供が目的なので、
    実行関数の登録/dispatchは後続フェーズで実装する。
    """

    dispatcher: Callable[[str, Mapping[str, Any]], None]
    cancellations: CancellationRegistry

    def enqueue(self, run_id: str, job_type: str, payload: Mapping[str, Any]) -> str:
        job_id = f"inprocess:{run_id}:{job_type}:{uuid.uuid4().hex}"  # type: ignore[name-defined]

        def _run():
            self.dispatcher(job_type, payload)

        t = threading.Thread(target=_run, name=job_id, daemon=True)
        t.start()
        return job_id

    def cancel(self, run_id: str) -> bool:
        self.cancellations.request_cancel(run_id)
        return True

