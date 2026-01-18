from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Optional


RunStatus = Literal["queued", "running", "succeeded", "failed", "cancelled", "waiting_user"]


ArtifactKind = str


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    status: RunStatus
    created_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    params: dict[str, Any] | None = None
    error_message: Optional[str] = None
    workdir: Optional[str] = None


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    run_id: str
    kind: ArtifactKind
    path: str
    created_at: datetime
    updated_at: Optional[datetime] = None
    meta: dict[str, Any] | None = None

