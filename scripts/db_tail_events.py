from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("run_id")
    p.add_argument("--db", default="data/document_process_app.db")
    p.add_argument("--limit", type=int, default=50)
    args = p.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        raise FileNotFoundError(str(db_path))

    con = sqlite3.connect(str(db_path))
    try:
        cur = con.cursor()
        # schema: payload_json
        rows = cur.execute(
            "SELECT event_id, ts, event_type, payload_json FROM run_events WHERE run_id = ? ORDER BY event_id DESC LIMIT ?",
            (args.run_id, int(args.limit)),
        ).fetchall()
    finally:
        con.close()

    rows = list(reversed(rows))
    for event_id, ts, event_type, payload in rows:
        # payload is JSON text
        payload_preview = ""
        try:
            obj = json.loads(payload) if isinstance(payload, str) else payload
            payload_preview = json.dumps(obj, ensure_ascii=False)[:400]
        except Exception:
            payload_preview = str(payload)[:400]
        print(f"[{event_id}] {ts} {event_type} {payload_preview}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

