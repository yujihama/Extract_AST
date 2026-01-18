from __future__ import annotations

import contextlib
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

from langchain_core.tools import tool

# NOTE:
# - venvによっては langgraph.checkpoint.sqlite が同梱されていない（memoryのみ）ことがある。
# - その場合は MemorySaver にフォールバックする（同一プロセス内のHITLに必要十分）。
try:
    from langgraph.checkpoint.sqlite import SqliteSaver  # type: ignore
except Exception:  # pragma: no cover
    SqliteSaver = None  # type: ignore

from langgraph.checkpoint.memory import MemorySaver


@dataclass(frozen=True)
class HitlPaths:
    """Run(work_dir)配下で使うHITL用ファイル。"""

    interrupt_file: str = "hitl_interrupt.json"
    resume_file: str = "hitl_resume.json"


def _repo_root() -> Path:
    # document_process_app/agents/hitl.py -> document_process_app -> repo root
    return Path(__file__).resolve().parents[2]


def get_hitl_paths(work_dir: Path) -> tuple[Path, Path]:
    hp = HitlPaths()
    return (work_dir / hp.interrupt_file), (work_dir / hp.resume_file)


def load_json_if_exists(path: Path) -> Optional[Any]:
    try:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def remove_if_exists(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except Exception:
        pass


@contextlib.contextmanager
def open_checkpointer(*, db_path: Optional[str] = None) -> Iterator[Any]:
    """deepagents(HITL)用のcheckpointerを提供する。

    - 可能ならSQLiteで永続化（langgraph.checkpoint.sqlite がある場合）
    - 無ければ MemorySaver にフォールバック（同一プロセス内のresumeは可能）

    env:
    - DOCUMENT_PROCESS_APP_CHECKPOINT_DB_PATH: SQLiteの保存先（SQLite利用時のみ）
    """
    if SqliteSaver is None:
        # SQLite saverが無い環境（.venvで確認済み）: in-memoryで継続
        yield MemorySaver()
        return

    # SQLite saverが使える環境
    if db_path is None:
        env = os.getenv("DOCUMENT_PROCESS_APP_CHECKPOINT_DB_PATH")
        if env and str(env).strip():
            db_path = str(env).strip()
        else:
            db_path = str((_repo_root() / "data" / "document_process_app_checkpoints.sqlite3").resolve())

    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), check_same_thread=False)
    try:
        saver = SqliteSaver(conn)  # type: ignore[misc]

        # langgraphのバージョン差分により、SqliteSaver が `jsonplus_serde.dumps/loads` を期待するが、
        # JsonPlusSerializer が `dumps_typed/loads_typed` のみ提供するケースがある場合だけshimする。
        try:
            from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer  # type: ignore

            class _JsonPlusCompat:
                def __init__(self) -> None:
                    self._inner = JsonPlusSerializer()

                def dumps(self, obj: Any) -> bytes:  # type: ignore[override]
                    _type, payload = self._inner.dumps_typed(obj)
                    return payload

                def loads(self, blob: bytes) -> Any:  # type: ignore[override]
                    return self._inner.loads_typed(("msgpack", blob))

            saver.jsonplus_serde = _JsonPlusCompat()  # type: ignore[attr-defined]
        except Exception:
            pass

        yield saver
    finally:
        try:
            conn.close()
        except Exception:
            pass


@tool
def human_input(question: str, answer: str = "") -> str:
    """人間に質問して回答を受け取るためのツール。

    想定:
    - deepagents の interrupt_config により、このツール呼び出しは実行前に中断される
    - UIが人間の回答を取得し、resume時に args.answer を edit して続行する
    """
    q = str(question or "").strip()
    a = str(answer or "").strip()
    if not q:
        raise ValueError("human_input.question is required")
    if not a:
        # interrupt_config が未設定/効いていない場合に silent に進まないよう明示
        raise RuntimeError("human_input called without answer (interrupt/resume misconfigured)")
    return a

