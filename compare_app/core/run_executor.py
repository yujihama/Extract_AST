from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Protocol

from compare_app.contracts import CancellationRegistry, EventSink, JobQueue
from compare_app.core.pipeline import CancelledError, Pipeline, RunContext
from compare_app.models import RunRecord, RunStatus


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RunRepository(Protocol):
    """Runメタ情報の永続化（SQLite想定）。"""

    def create_run(self, run: RunRecord) -> None: ...

    def get_run(self, run_id: str) -> RunRecord: ...

    def update_status(self, run_id: str, status: RunStatus, *, message: Optional[str] = None) -> None: ...

    def set_error(self, run_id: str, error_message: str) -> None: ...

    def list_runs(self, *, limit: int = 50, offset: int = 0) -> list[RunRecord]: ...


class ArtifactStore(Protocol):
    """FS成果物の配置（run_dir作成・入力コピー等）。"""

    def ensure_run_dirs(self, run_id: str) -> dict[str, str]: ...

    def add_input(self, run_id: str, *, which: str, src_path: str) -> str: ...


@dataclass
class RunExecutor:
    """UI/CLI共通の実行入口。

    - UI: start() を呼び、JobQueue（in-process→Celery）に投入
    - CLI/テスト: execute() を呼び、同期実行
    """

    pipeline: Pipeline
    repo: RunRepository
    artifacts: ArtifactStore
    events: EventSink
    job_queue: JobQueue
    cancellations: CancellationRegistry

    def _export_events_jsonl(self, run_id: str, paths: Mapping[str, str]) -> Optional[str]:
        """run_events を run_dir/log/events.jsonl にエクスポートする（DBが真でもフォールバック可能）。"""
        try:
            import json
            from pathlib import Path

            run_dir = Path(paths.get("run_dir") or (Path("data") / "runs" / run_id))
            log_dir = Path(paths.get("log_dir") or (run_dir / "log"))
            log_dir.mkdir(parents=True, exist_ok=True)
            out_path = log_dir / "events.jsonl"

            last_id: Optional[int] = None
            with open(out_path, "w", encoding="utf-8", newline="\n") as f:
                while True:
                    batch = self.events.list(run_id, after_event_id=last_id, limit=500)
                    if not batch:
                        break
                    for ev in batch:
                        last_id = ev.event_id
                        rec = {
                            "event_id": ev.event_id,
                            "run_id": ev.run_id,
                            "ts": ev.ts.isoformat(),
                            "event_type": ev.event_type,
                            "payload": ev.payload,
                        }
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

            rel = out_path.relative_to(run_dir).as_posix()
            # ログ自体もartifactとして登録
            self.events.emit(run_id, "artifact_updated", {"ts": _utcnow().isoformat(), "kind": "events_log_jsonl", "path": rel})
            return str(out_path)
        except Exception:
            return None

    def create_run(self, *, doc_a_path: str, doc_b_path: str, params: Mapping[str, Any]) -> RunRecord:
        run_id = uuid.uuid4().hex
        run_dir_paths = self.artifacts.ensure_run_dirs(run_id)

        # 入力をrun配下に確保（UIアップロード・CLIパスの両方で同じ流れにする）
        dst_a = self.artifacts.add_input(run_id, which="a", src_path=doc_a_path)
        dst_b = self.artifacts.add_input(run_id, which="b", src_path=doc_b_path)

        rec = RunRecord(
            run_id=run_id,
            status="queued",
            created_at=_utcnow(),
            params=dict(params),
            workdir=run_dir_paths.get("run_dir"),
        )
        self.repo.create_run(rec)
        self.events.emit(run_id, "run_created", {"ts": _utcnow().isoformat()})

        # 入力ファイルもartifactとして記録（一覧/監査のため）
        try:
            from pathlib import Path

            run_dir = Path(run_dir_paths.get("run_dir") or (Path("data") / "runs" / run_id))
            rel_a = Path(dst_a).relative_to(run_dir).as_posix()
            rel_b = Path(dst_b).relative_to(run_dir).as_posix()
            self.events.emit(run_id, "artifact_created", {"ts": _utcnow().isoformat(), "kind": "input_doc_a", "path": rel_a})
            self.events.emit(run_id, "artifact_created", {"ts": _utcnow().isoformat(), "kind": "input_doc_b", "path": rel_b})
        except Exception:
            # 失敗してもrun作成自体は継続
            pass
        return rec

    def start(self, run_id: str) -> str:
        """UI向け: バックグラウンド実行を起動する。"""
        self.repo.update_status(run_id, "queued")
        self.events.emit(run_id, "run_status_changed", {"ts": _utcnow().isoformat(), "status": "queued"})
        return self.job_queue.enqueue(run_id, "run_pipeline", {"run_id": run_id})

    def request_cancel(self, run_id: str) -> None:
        """協調的キャンセル要求（即時停止はしない）。"""
        self.cancellations.request_cancel(run_id)
        self.events.emit(run_id, "cancel_requested", {"ts": _utcnow().isoformat()})

    def execute(self, run_id: str, *, params_override: Optional[Mapping[str, Any]] = None) -> None:
        """CLI/テスト向け: 同期実行する（例外はそのまま上げる）。"""
        run = self.repo.get_run(run_id)
        params = dict(run.params or {})
        if params_override:
            params.update(dict(params_override))

        paths = self.artifacts.ensure_run_dirs(run_id)
        token = self.cancellations.get(run_id)

        self.repo.update_status(run_id, "running")
        self.events.emit(run_id, "run_status_changed", {"ts": _utcnow().isoformat(), "status": "running"})

        ctx = RunContext(
            run_id=run_id,
            params=params,
            events=self.events,
            cancellation=token,
            paths=paths,
        )

        try:
            self.pipeline.run(ctx)
        except CancelledError:
            self.repo.update_status(run_id, "cancelled")
            self.events.emit(run_id, "run_status_changed", {"ts": _utcnow().isoformat(), "status": "cancelled"})
            # ログ出力（キャンセルでも残す）
            self._export_events_jsonl(run_id, paths)
            return
        except Exception as e:
            self.repo.update_status(run_id, "failed")
            self.repo.set_error(run_id, str(e))
            self.events.emit(
                run_id,
                "run_status_changed",
                {"ts": _utcnow().isoformat(), "status": "failed", "error": str(e), "error_type": type(e).__name__},
            )
            # ログ出力（失敗でも残す）
            self._export_events_jsonl(run_id, paths)
            raise

        self.repo.update_status(run_id, "succeeded")
        self.events.emit(run_id, "run_status_changed", {"ts": _utcnow().isoformat(), "status": "succeeded"})
        # ログ出力（成功でも残す）
        self._export_events_jsonl(run_id, paths)

