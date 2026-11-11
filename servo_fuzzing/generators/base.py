from __future__ import annotations

import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional


class BaseGenerator(ABC):
    def __init__(self, name: str, repo_url: str, local_path: Optional[Path] = None):
        self.name = name
        self.repo_url = repo_url
        self.repo_root = Path(__file__).resolve().parent.parent.parent

        if local_path is not None:
            self.local_path = Path(local_path).resolve()
        else:
            candidates = [
                self.repo_root / "generators" / name,
                self.repo_root / name,
                self.repo_root / name.capitalize(),
                Path.cwd() / "generators" / name,
                Path.cwd() / name,
            ]
            found = next((c.resolve() for c in candidates if c.is_dir()), None)
            self.local_path = found or (self.repo_root / "generators" / name)

    @property
    def is_installed(self) -> bool:
        return self.local_path.is_dir()

    def clone(self, target_dir: Optional[Path] = None) -> bool:
        dest = Path(target_dir).resolve() if target_dir else self.local_path
        if dest.is_dir() and any(dest.iterdir()):
            return True
        dest.parent.mkdir(parents=True, exist_ok=True)
        print(f"Cloning {self.name} from {self.repo_url} into {dest}...")
        try:
            subprocess.run(["git", "clone", "--depth", "1", self.repo_url, str(dest)], check=True)
            self.local_path = dest
            return True
        except Exception as exc:
            print(f"Warning: Failed to clone {self.name}: {exc}")
            return False

    @abstractmethod
    def generate(self, output_dir: Path, count: int = 100) -> list[Path]:
        pass
