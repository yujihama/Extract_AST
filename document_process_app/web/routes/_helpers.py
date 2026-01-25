from __future__ import annotations

from pathlib import Path
from typing import Optional


def get_run_base_dir(repo, run_id: str) -> Path:
    run = repo.get_run(run_id)
    base_dir = Path(run.workdir) if run.workdir else (Path("data") / "runs" / run_id)
    return base_dir.resolve()


def resolve_artifact_path(repo, run_id: str, rel_path: str) -> Path:
    base_dir = get_run_base_dir(repo, run_id)
    candidate = (base_dir / rel_path).resolve()
    if base_dir not in candidate.parents and candidate != base_dir:
        raise ValueError("invalid path")
    return candidate


def get_work_file(repo, run_id: str, name: str) -> Path:
    base_dir = get_run_base_dir(repo, run_id)
    p = (base_dir / "work" / name).resolve()
    if base_dir not in p.parents and p != base_dir:
        raise ValueError("invalid path")
    return p


def parse_node_path(s: Optional[str]) -> Optional[list[int]]:
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    try:
        return [int(x) for x in s.split(",") if str(x).strip() != ""]
    except Exception:
        return None
