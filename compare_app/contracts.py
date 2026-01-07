from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Optional, Protocol, Sequence


@dataclass(frozen=True)
class RunEvent:
    event_id: int
    run_id: str
    ts: datetime
    event_type: str
    payload: dict[str, Any]


class EventSink(Protocol):
    """イベント保存/取得の抽象。UIのSSEとデバッグ可視化の根幹。"""

    def emit(self, run_id: str, event_type: str, payload: Mapping[str, Any]) -> None: ...

    def list(
        self,
        run_id: str,
        *,
        after_event_id: Optional[int] = None,
        limit: int = 200,
    ) -> Sequence[RunEvent]: ...


class CancellationToken(Protocol):
    """協調的キャンセル用。長時間ループ/step境界で参照する。"""

    def is_cancelled(self) -> bool: ...


class CancellationRegistry(Protocol):
    """run_idごとにキャンセル状態（token）を管理する。"""

    def get(self, run_id: str) -> CancellationToken: ...

    def request_cancel(self, run_id: str) -> None: ...


class JobQueue(Protocol):
    """バックグラウンド実行基盤の抽象（MVPはin-process、将来Celeryへ）。"""

    def enqueue(self, run_id: str, job_type: str, payload: Mapping[str, Any]) -> str: ...

    def cancel(self, run_id: str) -> bool: ...

