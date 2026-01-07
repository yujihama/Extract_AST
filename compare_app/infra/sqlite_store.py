from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

from compare_app.contracts import EventSink, RunEvent
from compare_app.models import RunRecord, RunStatus


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_parent(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def init_db(db_path: str) -> None:
    """最小スキーマを作成（MVP）。

    - 将来の拡張を考え、schema_versionを保持する
    """
    _ensure_parent(db_path)
    con = sqlite3.connect(db_path)
    try:
        cur = con.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_version (
              version INTEGER NOT NULL
            )
            """
        )
        # 初期化（空なら1行入れる）
        cur.execute("SELECT COUNT(*) FROM schema_version")
        if int(cur.fetchone()[0]) == 0:
            cur.execute("INSERT INTO schema_version(version) VALUES (1)")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
              run_id TEXT PRIMARY KEY,
              status TEXT NOT NULL,
              created_at TEXT NOT NULL,
              started_at TEXT,
              finished_at TEXT,
              params_json TEXT,
              error_message TEXT,
              workdir TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS run_events (
              event_id INTEGER PRIMARY KEY AUTOINCREMENT,
              run_id TEXT NOT NULL,
              ts TEXT NOT NULL,
              event_type TEXT NOT NULL,
              payload_json TEXT NOT NULL
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_run_events_run_id_event_id ON run_events(run_id, event_id)")

        # artifacts（run_id + path を一意キーにし、更新時は updated_at を更新）
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS artifacts (
              artifact_id INTEGER PRIMARY KEY AUTOINCREMENT,
              run_id TEXT NOT NULL,
              kind TEXT NOT NULL,
              path TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT,
              meta_json TEXT,
              UNIQUE(run_id, path)
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_artifacts_run_id ON artifacts(run_id)")
        con.commit()
    finally:
        con.close()


@dataclass
class SqliteEventSink(EventSink):
    db_path: str

    def emit(self, run_id: str, event_type: str, payload: Mapping[str, Any]) -> None:
        init_db(self.db_path)
        con = sqlite3.connect(self.db_path)
        try:
            cur = con.cursor()
            cur.execute(
                "INSERT INTO run_events(run_id, ts, event_type, payload_json) VALUES (?,?,?,?)",
                (
                    str(run_id),
                    _utcnow().isoformat(),
                    str(event_type),
                    json.dumps(dict(payload), ensure_ascii=False),
                ),
            )

            # artifact events は artifacts テーブルにも反映
            if str(event_type) in {"artifact_updated", "artifact_created"}:
                kind = None
                path = None
                meta = dict(payload) if isinstance(payload, Mapping) else {}
                if isinstance(meta, dict):
                    kind = meta.get("kind")
                    path = meta.get("path")
                if isinstance(kind, str) and isinstance(path, str) and kind.strip() and path.strip():
                    now = _utcnow().isoformat()
                    # upsert（run_id+path）
                    cur.execute(
                        """
                        INSERT INTO artifacts(run_id, kind, path, created_at, updated_at, meta_json)
                        VALUES (?,?,?,?,?,?)
                        ON CONFLICT(run_id, path) DO UPDATE SET
                          kind=excluded.kind,
                          updated_at=excluded.updated_at,
                          meta_json=excluded.meta_json
                        """,
                        (
                            str(run_id),
                            str(kind),
                            str(path),
                            now,
                            now,
                            json.dumps(meta, ensure_ascii=False),
                        ),
                    )
            con.commit()
        finally:
            con.close()

    def list(
        self,
        run_id: str,
        *,
        after_event_id: Optional[int] = None,
        limit: int = 200,
    ) -> Sequence[RunEvent]:
        init_db(self.db_path)
        con = sqlite3.connect(self.db_path)
        try:
            cur = con.cursor()
            lim = max(1, int(limit))
            if after_event_id is None:
                cur.execute(
                    "SELECT event_id, ts, event_type, payload_json FROM run_events WHERE run_id=? ORDER BY event_id ASC LIMIT ?",
                    (str(run_id), lim),
                )
            else:
                cur.execute(
                    "SELECT event_id, ts, event_type, payload_json FROM run_events WHERE run_id=? AND event_id>? ORDER BY event_id ASC LIMIT ?",
                    (str(run_id), int(after_event_id), lim),
                )
            rows = cur.fetchall()
        finally:
            con.close()

        out: list[RunEvent] = []
        for event_id, ts, event_type, payload_json in rows:
            try:
                payload = json.loads(payload_json) if payload_json else {}
            except Exception:
                payload = {"_raw": payload_json}
            try:
                ts_dt = datetime.fromisoformat(ts)
            except Exception:
                ts_dt = _utcnow()
            out.append(
                RunEvent(
                    event_id=int(event_id),
                    run_id=str(run_id),
                    ts=ts_dt,
                    event_type=str(event_type),
                    payload=payload if isinstance(payload, dict) else {"payload": payload},
                )
            )
        return out


@dataclass
class SqliteRunRepository:
    db_path: str

    def create_run(self, run: RunRecord) -> None:
        init_db(self.db_path)
        con = sqlite3.connect(self.db_path)
        try:
            cur = con.cursor()
            cur.execute(
                """
                INSERT INTO runs(run_id, status, created_at, started_at, finished_at, params_json, error_message, workdir)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    run.run_id,
                    run.status,
                    run.created_at.isoformat(),
                    run.started_at.isoformat() if run.started_at else None,
                    run.finished_at.isoformat() if run.finished_at else None,
                    json.dumps(run.params or {}, ensure_ascii=False) if run.params is not None else None,
                    run.error_message,
                    run.workdir,
                ),
            )
            con.commit()
        finally:
            con.close()

    def get_run(self, run_id: str) -> RunRecord:
        init_db(self.db_path)
        con = sqlite3.connect(self.db_path)
        try:
            cur = con.cursor()
            cur.execute(
                "SELECT run_id, status, created_at, started_at, finished_at, params_json, error_message, workdir FROM runs WHERE run_id=?",
                (str(run_id),),
            )
            row = cur.fetchone()
        finally:
            con.close()

        if row is None:
            raise KeyError(f"run not found: {run_id}")

        run_id_s, status, created_at, started_at, finished_at, params_json, error_message, workdir = row
        params: dict[str, Any] | None
        if params_json:
            try:
                pj = json.loads(params_json)
                params = pj if isinstance(pj, dict) else {"params": pj}
            except Exception:
                params = {"_raw": params_json}
        else:
            params = None

        def _parse_ts(v: Optional[str]) -> Optional[datetime]:
            if not v:
                return None
            try:
                return datetime.fromisoformat(v)
            except Exception:
                return None

        return RunRecord(
            run_id=str(run_id_s),
            status=str(status),  # type: ignore[assignment]
            created_at=datetime.fromisoformat(created_at),
            started_at=_parse_ts(started_at),
            finished_at=_parse_ts(finished_at),
            params=params,
            error_message=error_message,
            workdir=workdir,
        )

    def update_status(self, run_id: str, status: RunStatus, *, message: Optional[str] = None) -> None:
        init_db(self.db_path)
        now = _utcnow().isoformat()
        con = sqlite3.connect(self.db_path)
        try:
            cur = con.cursor()
            # started_at / finished_at は必要に応じて埋める
            if status == "running":
                cur.execute(
                    "UPDATE runs SET status=?, started_at=COALESCE(started_at, ?) WHERE run_id=?",
                    (status, now, str(run_id)),
                )
            elif status in {"succeeded", "failed", "cancelled"}:
                cur.execute(
                    "UPDATE runs SET status=?, finished_at=COALESCE(finished_at, ?) WHERE run_id=?",
                    (status, now, str(run_id)),
                )
            else:
                cur.execute("UPDATE runs SET status=? WHERE run_id=?", (status, str(run_id)))
            con.commit()
        finally:
            con.close()

    def set_error(self, run_id: str, error_message: str) -> None:
        init_db(self.db_path)
        con = sqlite3.connect(self.db_path)
        try:
            cur = con.cursor()
            cur.execute("UPDATE runs SET error_message=? WHERE run_id=?", (str(error_message), str(run_id)))
            con.commit()
        finally:
            con.close()

    def list_runs(self, *, limit: int = 50, offset: int = 0) -> list[RunRecord]:
        init_db(self.db_path)
        lim = max(1, int(limit))
        off = max(0, int(offset))
        con = sqlite3.connect(self.db_path)
        try:
            cur = con.cursor()
            cur.execute(
                """
                SELECT run_id, status, created_at, started_at, finished_at, params_json, error_message, workdir
                FROM runs
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                (lim, off),
            )
            rows = cur.fetchall()
        finally:
            con.close()

        out: list[RunRecord] = []
        for row in rows:
            rid, status, created_at, started_at, finished_at, params_json, error_message, workdir = row
            params: dict[str, Any] | None
            if params_json:
                try:
                    pj = json.loads(params_json)
                    params = pj if isinstance(pj, dict) else {"params": pj}
                except Exception:
                    params = {"_raw": params_json}
            else:
                params = None

            def _parse_ts(v: Optional[str]) -> Optional[datetime]:
                if not v:
                    return None
                try:
                    return datetime.fromisoformat(v)
                except Exception:
                    return None

            out.append(
                RunRecord(
                    run_id=str(rid),
                    status=str(status),  # type: ignore[assignment]
                    created_at=datetime.fromisoformat(created_at),
                    started_at=_parse_ts(started_at),
                    finished_at=_parse_ts(finished_at),
                    params=params,
                    error_message=error_message,
                    workdir=workdir,
                )
            )
        return out


@dataclass
class SqliteArtifactRepository:
    db_path: str

    def list_artifacts(self, run_id: str) -> list[dict[str, Any]]:
        init_db(self.db_path)
        con = sqlite3.connect(self.db_path)
        try:
            cur = con.cursor()
            cur.execute(
                """
                SELECT kind, path, created_at, updated_at, meta_json
                FROM artifacts
                WHERE run_id=?
                ORDER BY COALESCE(updated_at, created_at) DESC
                """,
                (str(run_id),),
            )
            rows = cur.fetchall()
        finally:
            con.close()

        out: list[dict[str, Any]] = []
        for kind, path, created_at, updated_at, meta_json in rows:
            try:
                meta = json.loads(meta_json) if meta_json else {}
            except Exception:
                meta = {"_raw": meta_json}
            out.append(
                {
                    "kind": str(kind),
                    "path": str(path),
                    "created_at": created_at,
                    "updated_at": updated_at,
                    "meta": meta if isinstance(meta, dict) else {"meta": meta},
                }
            )
        return out

