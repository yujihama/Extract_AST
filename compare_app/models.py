from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Optional


RunStatus = Literal["queued", "running", "succeeded", "failed", "cancelled", "waiting_user"]


ArtifactKind = Literal[
    "input_doc_a",
    "input_doc_b",
    "txt_a",
    "txt_b",
    "blueprint_a",
    "blueprint_b",
    "ast_a",
    "ast_b",
    "template_draft",
    "template_filled",
    "log_jsonl",
    "events_log_jsonl",
]


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

