from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Union

from document_process_app.contracts import CancellationRegistry, EventSink, JobQueue
from document_process_app.core.pipeline import CancelledError, Pipeline, RunContext, WaitingUserError
from document_process_app.infra.document_store import Document
from document_process_app.models import RunRecord, RunStatus


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_doc_id(raw: Any, fallback: str) -> str:
    text = str(raw).strip() if raw is not None else ""
    if not text:
        return fallback
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", text)
    return safe or fallback


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
        documents: Optional[list[Mapping[str, Any]]] = None,
        request_text: Optional[str] = None,
        hil_enabled: Optional[bool] = None,
        template_file_path: Optional[str] = None,
        params: Mapping[str, Any],
    ) -> RunRecord:
        """Runを作成する。
        
        入力ドキュメントは documents により指定する。
        """
        run_id = uuid.uuid4().hex
        run_dir_paths = self.artifacts.ensure_run_dirs(run_id)

        docs: list[Document] = []
        doc_ids: list[str] = []

        if not documents or len(documents) < 1:
            raise ValueError("At least 1 document must be provided")
        for idx, spec in enumerate(documents, 1):
            if not isinstance(spec, Mapping):
                raise ValueError("documents must be a list of mappings")
            doc_id = _normalize_doc_id(spec.get("doc_id"), f"d{idx}")
            # パス or ハッシュのいずれか
            spec_hash = spec.get("doc_hash") or spec.get("hash")
            spec_path = spec.get("path")
            if spec_hash:
                doc = self.artifacts.add_input_by_hash(run_id, which=doc_id, doc_hash=str(spec_hash))
            elif spec_path:
                doc = self.artifacts.add_input(run_id, which=doc_id, src_path=str(spec_path))
            else:
                raise ValueError("Each document must include 'path' or 'doc_hash'")
            docs.append(doc)
            doc_ids.append(doc_id)

        # 既存のペア成果物（matching, embedding_cache）があればwork_dir/cache_dirにコピー
        if len(docs) >= 2:
            self._copy_pair_artifacts_if_exist(
                run_id, docs[0].doc_hash, docs[1].doc_hash, run_dir_paths
            )

        params_dict = dict(params)

        rec = RunRecord(
            run_id=run_id,
            status="queued",
            created_at=_utcnow(),
            params=params_dict,
            workdir=run_dir_paths.get("run_dir"),
        )
        self.repo.create_run(rec)
        self.events.emit(run_id, "run_created", {"ts": _utcnow().isoformat()})

        self.events.emit(
            run_id,
            "documents_linked",
            {
                "ts": _utcnow().isoformat(),
                "documents": [
                    {
                        "doc_id": doc_ids[i] if i < len(doc_ids) else f"d{i+1}",
                        "doc_hash": d.doc_hash,
                        "filename": d.original_filename,
                    }
                    for i, d in enumerate(docs)
                ],
            },
        )

        # configにrequest/hilを保存（後続ステップで参照）
        try:
            cfg = self.artifacts.get_run_config(run_id)
            if request_text is not None:
                cfg["request_text"] = request_text
            if hil_enabled is not None:
                cfg["hil_enabled"] = bool(hil_enabled)

            # テンプレートもドキュメントとして永続化（doc_repo）し、Runに紐付ける。
            # - documents 配列には入れない（分析対象に混ざるため）
            # - Run配下には input/template.<ext> としてコピーし、pre_analysis の seed として利用する
            ingest_src = None
            if template_file_path and str(template_file_path).strip():
                ingest_src = str(template_file_path).strip()

            if ingest_src:
                src = Path(str(ingest_src))
                if not src.exists() or not src.is_file():
                    raise FileNotFoundError(str(src))

                doc_repo = getattr(self.artifacts, "doc_repo", None)
                if doc_repo is None:
                    raise RuntimeError("artifact store has no doc_repo (cannot ingest template)")

                template_doc = doc_repo.add_from_path(str(src), src.name)
                cfg["template_doc_hash"] = template_doc.doc_hash
                cfg["template_filename"] = template_doc.original_filename

                # Run入力へコピー（pre_analysis の seed）
                input_dir = Path(run_dir_paths["input_dir"])
                ext = Path(template_doc.original_filename or "").suffix or src.suffix or ".md"
                if ext.lower() not in {".md", ".txt"}:
                    # テンプレ用途はテキスト前提。未知拡張でも保存はするが .md 扱いに寄せる。
                    ext = ".md"
                seed_path = input_dir / f"template_seed{ext}"
                raw_path = doc_repo.get_raw_path(template_doc.doc_hash)
                if raw_path and raw_path.exists():
                    seed_path.write_bytes(raw_path.read_bytes())
                else:
                    # 念のためフォールバック（通常ここには来ない）
                    seed_path.write_bytes(src.read_bytes())

                # Run内で参照できるようにパスも保存（相対）
                run_dir = Path(run_dir_paths["run_dir"])
                cfg["template_seed_path"] = seed_path.relative_to(run_dir).as_posix()
                self.events.emit(
                    run_id,
                    "artifact_updated",
                    {"ts": _utcnow().isoformat(), "kind": "template_seed", "path": cfg["template_seed_path"]},
                )

            # documentsはadd_inputで更新済みだが、念のため doc_id順に並び替える
            if isinstance(cfg.get("documents"), list) and doc_ids:
                order = {doc_id: i for i, doc_id in enumerate(doc_ids)}
                cfg["documents"].sort(key=lambda d: order.get(str(d.get("doc_id")), 9999))
            self.artifacts.save_run_config(run_id, cfg)
        except Exception:
            # config更新失敗は致命にしない（run自体は作成済み）
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
        - work/ast_<doc_id>.ast.json
        - work/blueprint_<doc_id>.json

        Returns:
            コピーしたファイルのリスト
        """
        import shutil
        from pathlib import Path

        copied = []
        source_run_dir = Path("data") / "runs" / source_run_id
        target_work = Path(paths["work_dir"])
        # work配下のファイル（doc_idごと）
        config_path = source_run_dir / "config.json"
        doc_ids: list[str] = []
        if config_path.exists():
            try:
                cfg = json.loads(config_path.read_text(encoding="utf-8"))
                docs = cfg.get("documents")
                if isinstance(docs, list):
                    for i, d in enumerate(docs, 1):
                        if isinstance(d, dict):
                            doc_id = _normalize_doc_id(d.get("doc_id"), f"d{i}")
                            doc_ids.append(doc_id)
            except Exception:
                doc_ids = []

        for doc_id in doc_ids:
            for prefix, suffix in [("ast", "ast.json"), ("blueprint", "json")]:
                fname = f"{prefix}_{doc_id}.{suffix}"
                src = source_run_dir / "work" / fname
                if src.exists():
                    shutil.copy(src, target_work / fname)
                    copied.append(f"work/{fname}")

        return copied

    def execute(self, run_id: str, *, params_override: Optional[Mapping[str, Any]] = None) -> None:
        """CLI/テスト向け: 同期実行する（例外はそのまま上げる）。"""
        run = self.repo.get_run(run_id)
        params = dict(run.params or {})
        if params_override:
            params.update(dict(params_override))

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
        except WaitingUserError:
            self.repo.update_status(run_id, "waiting_user")
            self.events.emit(run_id, "run_status_changed", {"ts": _utcnow().isoformat(), "status": "waiting_user"})
            # ログ出力（waiting_userでも残す）
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

