from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Optional

from .base import BaseGenerator


class FreedomGenerator(BaseGenerator):
    def __init__(self, local_path: Optional[Path] = None):
        super().__init__(
            name="freedom",
            repo_url="https://github.com/sslab-gatech/freedom.git",
            local_path=local_path,
        )

    def generate(self, output_dir: Path, count: int = 100) -> list[Path]:
        output_dir = Path(output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

        main_script = self.local_path / "main.py"
        if not main_script.exists():
            raise FileNotFoundError(f"Freedom main.py not found at {main_script}")

        cmd = [
            sys.executable,
            str(main_script.resolve()),
            "-i",
            "1",
            "-m",
            "generate",
            "-n",
            str(count),
            "-o",
            str(output_dir),
        ]
        subprocess.run(cmd, check=True, cwd=str(self.local_path), capture_output=True)
        return sorted(list(output_dir.glob("*.html")))
