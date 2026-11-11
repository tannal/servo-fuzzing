from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .base import BaseGenerator


class MinervaGenerator(BaseGenerator):
    def __init__(self, local_path: Optional[Path] = None, mem_dep_json_path: Optional[Path] = None):
        super().__init__(
            name="minerva",
            repo_url="https://github.com/ChijinZ/Minerva.git",
            local_path=local_path,
        )
        self.mem_dep_json_path = mem_dep_json_path

    def generate(self, output_dir: Path, count: int = 100) -> list[Path]:
        output_dir = Path(output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

        generator_script = self.local_path / "generator.py"
        if not generator_script.exists():
            raise FileNotFoundError(f"Minerva generator.py not found at {generator_script}")

        env = os.environ.copy()
        if self.mem_dep_json_path:
            env["MEM_DEP_JSON_PATH"] = str(Path(self.mem_dep_json_path).resolve())
        else:
            default_mem_dep = self.local_path / "mod_ref_helper" / "mem_dep.json"
            if default_mem_dep.exists():
                env["MEM_DEP_JSON_PATH"] = str(default_mem_dep.resolve())

        cmd = [
            sys.executable,
            str(generator_script.resolve()),
            "--output_dir",
            str(output_dir),
            "--no_of_files",
            str(count),
        ]

        try:
            subprocess.run(cmd, check=True, cwd=str(self.local_path), env=env, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            err = exc.stderr.strip() if exc.stderr else str(exc)
            if "No module named 'cffi'" in err:
                print("Error: Minerva requires the 'cffi' package. Install it via: pip install cffi")
            else:
                print(f"Error running Minerva generator:\n{err}")
            raise

        return sorted(list(output_dir.glob("*.html")))
