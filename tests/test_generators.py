"""Tests for generator management and orchestration."""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

from servo_fuzzing.generators import BaseGenerator, GeneratorManager


class TestGenerators(unittest.TestCase):
    def test_generator_manager_status(self):
        manager = GeneratorManager()
        status = manager.get_status()
        self.assertIn("domato", status)
        self.assertIn("freedom", status)
        self.assertIn("minerva", status)

    @patch.object(BaseGenerator, "is_installed", new_callable=PropertyMock)
    def test_generate_all_mocked(self, mock_installed):
        mock_installed.return_value = True
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            manager = GeneratorManager(generators_dir=out_dir / "gen_src")

            for name, gen in manager.generators.items():
                gen.generate = MagicMock(return_value=[out_dir / f"{name}_1.html"])  # type: ignore

            results = manager.generate_all(output_dir=out_dir, count_per_generator=100)
            self.assertEqual(len(results), 3)
            self.assertIn("domato", results)
            self.assertIn("freedom", results)
            self.assertIn("minerva", results)

    @patch.object(BaseGenerator, "is_installed", new_callable=PropertyMock)
    def test_generate_all_runs_generators_in_parallel(self, mock_installed):
        mock_installed.return_value = True
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            manager = GeneratorManager(generators_dir=out_dir / "gen_src")
            barrier = threading.Barrier(3, timeout=2.0)

            for name, gen in manager.generators.items():
                def make_run(gen_name):
                    def _run(output_dir, count=100):
                        barrier.wait()
                        return [output_dir / f"{gen_name}.html"]
                    return _run

                gen.generate = MagicMock(side_effect=make_run(name))  # type: ignore

            results = manager.generate_all(output_dir=out_dir, count_per_generator=5)
            self.assertEqual(list(results.keys()), ["domato", "freedom", "minerva"])

    @patch.object(BaseGenerator, "is_installed", new_callable=PropertyMock)
    def test_generate_all_isolates_generator_failure(self, mock_installed):
        mock_installed.return_value = True
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            manager = GeneratorManager(generators_dir=out_dir / "gen_src")
            manager.domato.generate = MagicMock(return_value=[out_dir / "domato" / "0.html"])  # type: ignore
            manager.freedom.generate = MagicMock(side_effect=RuntimeError("boom"))  # type: ignore
            manager.minerva.generate = MagicMock(return_value=[out_dir / "minerva" / "0.html"])  # type: ignore

            results = manager.generate_all(output_dir=out_dir, count_per_generator=5)
            self.assertEqual(list(results.keys()), ["domato", "minerva"])


if __name__ == "__main__":
    unittest.main()
