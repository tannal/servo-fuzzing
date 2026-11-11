"""Generator management for Domato, Freedom, and Minerva."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .base import BaseGenerator
from .domato import DomatoGenerator
from .freedom import FreedomGenerator
from .minerva import MinervaGenerator


class GeneratorManager:
    """Orchestrates testcase generators."""

    def __init__(
        self,
        generators_dir: Optional[Path] = None,
        domato_path: Optional[Path] = None,
        freedom_path: Optional[Path] = None,
        minerva_path: Optional[Path] = None,
        minerva_mem_dep: Optional[Path] = None,
    ):
        base = generators_dir or (Path(__file__).resolve().parent.parent.parent / "generators")
        self.domato = DomatoGenerator(local_path=domato_path or (base / "domato"))
        self.freedom = FreedomGenerator(local_path=freedom_path or (base / "freedom"))
        self.minerva = MinervaGenerator(
            local_path=minerva_path or (base / "minerva"),
            mem_dep_json_path=minerva_mem_dep,
        )
        self.generators: dict[str, BaseGenerator] = {
            "domato": self.domato,
            "freedom": self.freedom,
            "minerva": self.minerva,
        }

    def clone_all(self) -> dict[str, bool]:
        """Clone all generators that are not present."""
        status = {}
        for name, gen in self.generators.items():
            if not gen.is_installed:
                ok = gen.clone()
                status[name] = ok
            else:
                status[name] = True
        return status

    def get_status(self) -> dict[str, dict[str, str | bool]]:
        """Return status for all generators."""
        return {
            name: {
                "installed": gen.is_installed,
                "path": str(gen.local_path),
                "repo_url": gen.repo_url,
            }
            for name, gen in self.generators.items()
        }

    def generate_all(
        self,
        output_dir: Path,
        count_per_generator: int = 100,
        auto_clone: bool = False,
    ) -> dict[str, list[Path]]:
        """Run all generators to produce testcases (e.g. 100 x 3 = 300 testcases)."""
        output_dir = Path(output_dir).resolve()
        results: dict[str, list[Path]] = {}

        if auto_clone:
            self.clone_all()

        for name, gen in self.generators.items():
            if not gen.is_installed:
                print(f"Notice: Generator '{name}' is not installed at {gen.local_path}. Skipping.")
                continue
            gen_out = output_dir / name
            print(f"Running {name} to generate {count_per_generator} testcases in {gen_out}...")
            try:
                cases = gen.generate(output_dir=gen_out, count=count_per_generator)
                results[name] = cases
                print(f"Generated {len(cases)} testcases for {name}.")
            except Exception as exc:
                print(f"Error running generator '{name}': {exc}")

        return results


__all__ = [
    "BaseGenerator",
    "DomatoGenerator",
    "FreedomGenerator",
    "MinervaGenerator",
    "GeneratorManager",
]
