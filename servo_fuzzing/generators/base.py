"""Base interface for testcase generators."""

from __future__ import annotations

import subprocess
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional


class BaseGenerator(ABC):
    """Abstract base class for DOM fuzzing generators."""

    def __init__(self, name: str, repo_url: str, local_path: Optional[Path] = None):
        self.name = name
        self.repo_url = repo_url
        self.repo_root = Path(__file__).resolve().parent.parent.parent

        if local_path is not None:
            self.local_path = Path(local_path).resolve()
        else:
            # Check candidate locations: generators/<name>, ./<name>, ./<Name>
            candidates = [
                self.repo_root / "generators" / name,
                self.repo_root / name,
                self.repo_root / name.capitalize(),
                Path.cwd() / "generators" / name,
                Path.cwd() / name,
            ]
            found = None
            for cand in candidates:
                if cand.exists() and cand.is_dir():
                    found = cand.resolve()
                    break
            self.local_path = found if found is not None else (self.repo_root / "generators" / name)

    @property
    def is_installed(self) -> bool:
        """Check if generator source directory is present and contains entry scripts."""
        return self.local_path.exists() and self.local_path.is_dir()

    def clone(self, target_dir: Optional[Path] = None) -> bool:
        """Clone the generator repository if not present."""
        dest = Path(target_dir).resolve() if target_dir else self.local_path
        if dest.exists() and any(dest.iterdir()):
            return True
        dest.parent.mkdir(parents=True, exist_ok=True)
        print(f"Cloning {self.name} from {self.repo_url} into {dest}...")
        try:
            subprocess.run(
                ["git", "clone", "--depth", "1", self.repo_url, str(dest)],
                check=True,
            )
            self.local_path = dest
            return True
        except Exception as exc:
            print(f"Warning: Failed to clone {self.name} from {self.repo_url}: {exc}")
            return False

    @abstractmethod
    def generate(self, output_dir: Path, count: int = 100) -> list[Path]:
        """Generate test cases into output_dir and return list of generated HTML paths."""
        pass
