from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

from .base import BaseGenerator
from .domato import DomatoGenerator
from .freedom import FreedomGenerator
from .minerva import MinervaGenerator


class GeneratorManager:
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
        return {
            name: (True if gen.is_installed else gen.clone())
            for name, gen in self.generators.items()
        }

    def get_status(self) -> dict[str, dict[str, str | bool]]:
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
        max_workers: Optional[int] = None,
    ) -> dict[str, list[Path]]:
        output_dir = Path(output_dir).resolve()

        if auto_clone:
            self.clone_all()

        active: list[tuple[str, BaseGenerator, Path]] = []
        for name, gen in self.generators.items():
            if not gen.is_installed:
                print(f"Notice: Generator '{name}' is not installed at {gen.local_path}. Skipping.", flush=True)
                continue
            active.append((name, gen, output_dir / name))

        if not active:
            return {}

        workers = len(active) if max_workers is None or max_workers <= 0 else min(len(active), max_workers)

        def _run_one(name: str, gen: BaseGenerator, gen_out: Path) -> tuple[str, list[Path], float]:
            print(f"Running {name} to generate {count_per_generator} testcases in {gen_out}...", flush=True)
            t0 = time.perf_counter()
            cases = gen.generate(output_dir=gen_out, count=count_per_generator)
            return name, cases, time.perf_counter() - t0

        collected: dict[str, list[Path]] = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_run_one, name, gen, gen_out): name
                for name, gen, gen_out in active
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    _, cases, elapsed = future.result()
                    collected[name] = cases
                    print(f"Generated {len(cases)} testcases for {name} in {elapsed:.2f}s.", flush=True)
                except Exception as exc:
                    print(f"Error running generator '{name}': {exc}", flush=True)

        return {name: collected[name] for name, _, _ in active if name in collected}


__all__ = [
    "BaseGenerator",
    "DomatoGenerator",
    "FreedomGenerator",
    "MinervaGenerator",
    "GeneratorManager",
]
