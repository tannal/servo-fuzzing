from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Optional

from .base import BaseGenerator


class DomatoGenerator(BaseGenerator):
    def __init__(self, local_path: Optional[Path] = None):
        super().__init__(
            name="domato",
            repo_url="https://github.com/googleprojectzero/domato.git",
            local_path=local_path,
        )

    def generate(self, output_dir: Path, count: int = 100) -> list[Path]:
        output_dir = Path(output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

        generator_script = self.local_path / "generator.py"
        if not generator_script.exists():
            raise FileNotFoundError(f"Domato generator not found at {generator_script}")

        cmd = [
            sys.executable,
            str(generator_script.resolve()),
            "-o",
            str(output_dir),
            "-n",
            str(count),
        ]
        subprocess.run(cmd, check=True, cwd=str(self.local_path), capture_output=True)
        return sorted(list(output_dir.glob("*.html")))
