from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Dict


@dataclass
class FileArtifactStore:
    """run_id 配下に成果物を配置する最小FS実装（MVP用）。

    既存コードは `data/` 配下を前提としているため、まずは `data/runs/{run_id}/...` を正にする。
    """

    runs_root: Path = Path("data") / "runs"

    def ensure_run_dirs(self, run_id: str) -> Dict[str, str]:
        run_dir = self.runs_root / run_id
        paths = {
            "run_dir": str(run_dir),
            "input_dir": str(run_dir / "input"),
            "work_dir": str(run_dir / "work"),
            "out_dir": str(run_dir / "out"),
            "log_dir": str(run_dir / "log"),
            "cache_dir": str(run_dir / "cache"),
        }
        for p in paths.values():
            os.makedirs(p, exist_ok=True)
        return paths

    def add_input(self, run_id: str, *, which: str, src_path: str) -> str:
        paths = self.ensure_run_dirs(run_id)
        src = Path(src_path)
        if not src.exists():
            raise FileNotFoundError(str(src))
        # which: "a" or "b"
        dest_name = f"doc_{which}{src.suffix}"
        dest = Path(paths["input_dir"]) / dest_name
        shutil.copy2(src, dest)
        return str(dest)

