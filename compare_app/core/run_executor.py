from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Union

from compare_app.contracts import CancellationRegistry, EventSink, JobQueue
from compare_app.core.pipeline import CancelledError, Pipeline, RunContext
from compare_app.infra.document_store import Document
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
    """FS成果物の配置（ドキュメント中心アーキテクチャ対応）。"""

    def ensure_run_dirs(self, run_id: str) -> dict[str, str]: ...

    def add_input(self, run_id: str, *, which: str, src_path: str) -> Document: ...
    
    def add_input_by_hash(self, run_id: str, *, which: str, doc_hash: str) -> Document: ...
    
    def get_run_config(self, run_id: str) -> dict[str, Any]: ...
    
    def save_run_config(self, run_id: str, config: dict[str, Any]) -> None: ...


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

    def _sync_artifacts_from_fs(self, run_id: str, paths: Mapping[str, str]) -> None:
        """FS上の成果物を artifacts に反映する（登録漏れ対策 / kindは基本 'file'）。"""
        try:
            from pathlib import Path

            run_dir = Path(paths.get("run_dir") or (Path("data") / "runs" / run_id))
            if not run_dir.exists():
                return

            for sub in ["input", "work", "out", "log", "cache"]:
                d = run_dir / sub
                if not d.exists():
                    continue
                for p in d.rglob("*"):
                    if not p.is_file():
                        continue
                    rel = p.relative_to(run_dir).as_posix()
                    try:
                        st = p.stat()
                        size = int(st.st_size)
                    except Exception:
                        size = None
                    self.events.emit(
                        run_id,
                        "artifact_updated",
                        {"ts": _utcnow().isoformat(), "kind": "file", "path": rel, "size": size},
                    )
        except Exception:
            return

    def create_run(
        self,
        *,
        doc_a_path: Optional[str] = None,
        doc_b_path: Optional[str] = None,
        doc_a_hash: Optional[str] = None,
        doc_b_hash: Optional[str] = None,
        params: Mapping[str, Any],
    ) -> RunRecord:
        """Runを作成する。
        
        入力ドキュメントは以下のいずれかの方法で指定:
        - doc_a_path/doc_b_path: ファイルパスから新規登録
        - doc_a_hash/doc_b_hash: 既存ドキュメントのハッシュで参照
        """
        run_id = uuid.uuid4().hex
        run_dir_paths = self.artifacts.ensure_run_dirs(run_id)

        # ドキュメントを解決
        if doc_a_hash:
            doc_a = self.artifacts.add_input_by_hash(run_id, which="a", doc_hash=doc_a_hash)
        elif doc_a_path:
            doc_a = self.artifacts.add_input(run_id, which="a", src_path=doc_a_path)
        else:
            raise ValueError("Either doc_a_path or doc_a_hash must be provided")
        
        if doc_b_hash:
            doc_b = self.artifacts.add_input_by_hash(run_id, which="b", doc_hash=doc_b_hash)
        elif doc_b_path:
            doc_b = self.artifacts.add_input(run_id, which="b", src_path=doc_b_path)
        else:
            raise ValueError("Either doc_b_path or doc_b_hash must be provided")

        # 既存のペア成果物（matching, embedding_cache）があればwork_dir/cache_dirにコピー
        self._copy_pair_artifacts_if_exist(
            run_id, doc_a.doc_hash, doc_b.doc_hash, run_dir_paths
        )

        rec = RunRecord(
            run_id=run_id,
            status="queued",
            created_at=_utcnow(),
            doc_a_hash=doc_a.doc_hash,
            doc_b_hash=doc_b.doc_hash,
            params=dict(params),
            workdir=run_dir_paths.get("run_dir"),
        )
        self.repo.create_run(rec)
        self.events.emit(run_id, "run_created", {"ts": _utcnow().isoformat()})

        # ドキュメント情報をイベントとして記録
        self.events.emit(
            run_id,
            "document_linked",
            {
                "ts": _utcnow().isoformat(),
                "doc_a_hash": doc_a.doc_hash,
                "doc_a_filename": doc_a.original_filename,
                "doc_b_hash": doc_b.doc_hash,
                "doc_b_filename": doc_b.original_filename,
            },
        )
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

    def _copy_pair_artifacts_if_exist(
        self, run_id: str, doc_a_hash: str, doc_b_hash: str, paths: Mapping[str, str]
    ) -> list[str]:
        """既存のペア成果物をwork_dir/cache_dirにコピーする。
        
        コピー対象:
        - initial_matching.json → work_dir
        - embedding_cache.json → cache_dir
        
        Returns:
            コピーしたファイルのリスト
        """
        import shutil
        from pathlib import Path
        
        copied = []
        work_dir = Path(paths["work_dir"])
        cache_dir = Path(paths["cache_dir"])
        
        # ペアディレクトリを取得
        pair_hash = self.artifacts.pair_repo.compute_pair_hash(doc_a_hash, doc_b_hash)
        pair_dir = self.artifacts.pair_repo.base_dir / pair_hash
        
        if not pair_dir.exists():
            return copied
        
        # initial_matching.json
        matching_src = pair_dir / "initial_matching.json"
        matching_dest = work_dir / "initial_matching.json"
        if matching_src.exists() and not matching_dest.exists():
            shutil.copy2(matching_src, matching_dest)
            copied.append("work/initial_matching.json")
        
        # embedding_cache.json
        cache_src = pair_dir / "embedding_cache.json"
        cache_dest = cache_dir / "embedding_cache.json"
        if cache_src.exists() and not cache_dest.exists():
            shutil.copy2(cache_src, cache_dest)
            copied.append("cache/embedding_cache.json")
        
        if copied:
            self.events.emit(
                run_id,
                "pair_artifacts_reused",
                {"ts": _utcnow().isoformat(), "doc_a_hash": doc_a_hash, "doc_b_hash": doc_b_hash, "files": copied},
            )
        
        return copied

    def _copy_artifacts_from_run(self, source_run_id: str, target_run_id: str, paths: Mapping[str, str]) -> list[str]:
        """別runから成果物をコピーする。

        コピー対象:
        - work/ast_a.ast.json, ast_b.ast.json
        - work/initial_matching.json
        - work/blueprint_a.json, blueprint_b.json
        - cache/embedding_cache.json

        Returns:
            コピーしたファイルのリスト
        """
        import shutil
        from pathlib import Path

        copied = []
        source_run_dir = Path("data") / "runs" / source_run_id
        target_work = Path(paths["work_dir"])
        target_cache = Path(paths["cache_dir"])

        # work配下のファイル
        work_files = [
            "ast_a.ast.json",
            "ast_b.ast.json",
            "initial_matching.json",
            "blueprint_a.json",
            "blueprint_b.json",
        ]
        for fname in work_files:
            src = source_run_dir / "work" / fname
            if src.exists():
                shutil.copy(src, target_work / fname)
                copied.append(f"work/{fname}")

        # cache配下のファイル
        cache_src = source_run_dir / "cache" / "embedding_cache.json"
        if cache_src.exists():
            shutil.copy(cache_src, target_cache / "embedding_cache.json")
            copied.append("cache/embedding_cache.json")

        return copied

    def execute(self, run_id: str, *, params_override: Optional[Mapping[str, Any]] = None) -> None:
        """CLI/テスト向け: 同期実行する（例外はそのまま上げる）。"""
        run = self.repo.get_run(run_id)
        params = dict(run.params or {})
        if params_override:
            params.update(dict(params_override))
        
        # ドキュメントハッシュをparamsに追加（ステップで利用可能にする）
        if run.doc_a_hash:
            params["doc_a_hash"] = run.doc_a_hash
        if run.doc_b_hash:
            params["doc_b_hash"] = run.doc_b_hash

        paths = self.artifacts.ensure_run_dirs(run_id)

        # reuse_artifacts_from: 別runから成果物をコピー
        reuse_from = params.get("reuse_artifacts_from")
        if reuse_from:
            try:
                copied = self._copy_artifacts_from_run(reuse_from, run_id, paths)
                if copied:
                    self.events.emit(
                        run_id,
                        "artifacts_reused",
                        {"ts": _utcnow().isoformat(), "source_run_id": reuse_from, "files": copied},
                    )
            except Exception as e:
                self.events.emit(
                    run_id,
                    "artifacts_reuse_failed",
                    {"ts": _utcnow().isoformat(), "source_run_id": reuse_from, "error": str(e)},
                )
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
            self._sync_artifacts_from_fs(run_id, paths)
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
            self._sync_artifacts_from_fs(run_id, paths)
            raise

        self.repo.update_status(run_id, "succeeded")
        self.events.emit(run_id, "run_status_changed", {"ts": _utcnow().isoformat(), "status": "succeeded"})
        # ログ出力（成功でも残す）
        self._export_events_jsonl(run_id, paths)
        self._sync_artifacts_from_fs(run_id, paths)

