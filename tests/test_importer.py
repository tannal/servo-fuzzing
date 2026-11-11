from __future__ import annotations

import json
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from servo_fuzzing.cli import main as cli_main
from servo_fuzzing.executor import CrashInfo, SuiteSummary, TestResult, TestStatus
from servo_fuzzing.importer import (
    find_import_testcases,
    import_crashes,
    is_archive,
    unpack_archive_if_needed,
)


class TestImporter(unittest.TestCase):
    def test_is_archive(self):
        self.assertTrue(is_archive(Path("crashes.tar.gz")))
        self.assertTrue(is_archive(Path("crashes.zip")))
        self.assertTrue(is_archive(Path("archive.tar.bz2")))
        self.assertFalse(is_archive(Path("test.html")))
        self.assertFalse(is_archive(Path("fuzz.html.html")))

    def test_unpack_archive_if_needed_tar(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            tar_file = tmp_path / "crashes.tar.gz"
            src_file = tmp_path / "dummy.html"
            src_file.write_text("<html>hello</html>", encoding="utf-8")

            with tarfile.open(tar_file, "w:gz") as tar:
                tar.add(src_file, arcname="dummy.html")

            unpack_dir = tmp_path / "unpacked"
            search_paths = unpack_archive_if_needed(tar_file, unpack_dir)
            self.assertEqual(len(search_paths), 1)
            self.assertTrue((unpack_dir / "dummy.html").exists())

    def test_unpack_archive_if_needed_zip(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            zip_file = tmp_path / "crashes.zip"
            unpack_dir = tmp_path / "unpacked_zip"

            with zipfile.ZipFile(zip_file, "w") as z:
                z.writestr("test1.html", "<body>content</body>")

            search_paths = unpack_archive_if_needed(zip_file, unpack_dir)
            self.assertEqual(len(search_paths), 1)
            self.assertTrue((unpack_dir / "test1.html").exists())

    def test_find_import_testcases(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            f1 = tmp_path / "normal.html"
            f1.write_text("a", encoding="utf-8")
            f2 = tmp_path / "double_ext.html.html"
            f2.write_text("b", encoding="utf-8")
            f3 = tmp_path / "ignored.txt"
            f3.write_text("c", encoding="utf-8")

            cases = find_import_testcases([tmp_path])
            case_names = [c.name for c in cases]
            self.assertIn("normal.html", case_names)
            self.assertIn("double_ext.html.html", case_names)
            self.assertNotIn("ignored.txt", case_names)

            limited = find_import_testcases([tmp_path], max_tests=1)
            self.assertEqual(len(limited), 1)

    @patch("servo_fuzzing.importer.run_single_test")
    @patch("servo_fuzzing.importer.run_suite")
    def test_import_crashes_deduplication(self, mock_run_suite, mock_run_single):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            crashes_dir = tmp_path / "raw_crashes"
            crashes_dir.mkdir()
            tc1 = crashes_dir / "crash_01.html"
            tc2 = crashes_dir / "crash_02.html"
            tc1.write_text("<body>crash 1</body>", encoding="utf-8")
            tc2.write_text("<body>crash 2</body>", encoding="utf-8")

            sig_path = tmp_path / "signatures.json"
            sig_path.write_text('{"schema_version": 1, "signatures": []}', encoding="utf-8")
            corpus_dir = tmp_path / "corpus"
            output_dir = tmp_path / "triage"

            fake_bin = tmp_path / "servoshell"
            fake_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_bin.chmod(0o755)

            # Health check pass
            mock_run_single.return_value = TestResult(
                testcase=Path("about:blank"),
                status=TestStatus.PASS,
                duration=0.1,
            )

            # 2 crashes with IDENTICAL panic message and source file
            r1 = TestResult(
                testcase=tc1,
                status=TestStatus.NEW_CRASH,
                duration=0.1,
                combined_output="panicked at 'assertion failed: foo', src/bar.rs:10\nstack backtrace:\n0: main",
                crash_info=CrashInfo(
                    message="assertion failed: foo",
                    source_file="components/bar.rs:10",
                    stack_frames=["main"],
                ),
            )
            r2 = TestResult(
                testcase=tc2,
                status=TestStatus.NEW_CRASH,
                duration=0.1,
                combined_output="panicked at 'assertion failed: foo', src/bar.rs:10\nstack backtrace:\n0: main",
                crash_info=CrashInfo(
                    message="assertion failed: foo",
                    source_file="components/bar.rs:10",
                    stack_frames=["main"],
                ),
            )
            mock_run_suite.return_value = SuiteSummary(
                total=2,
                passed=0,
                known_crashes=0,
                new_crashes=2,
                timeouts=0,
                errors=0,
                results=[r1, r2],
            )

            summary = import_crashes(
                source=crashes_dir,
                binary=fake_bin,
                signatures_path=sig_path,
                corpus_dir=corpus_dir,
                output_dir=output_dir,
                reduce=False,
            )

            self.assertEqual(summary["total_scanned"], 2)
            self.assertEqual(summary["new_crashes"], 2)
            # Only 1 unique signature should be added due to deduplication
            self.assertEqual(summary["signatures_added"], 1)

            saved_sigs = json.loads(sig_path.read_text(encoding="utf-8"))["signatures"]
            self.assertEqual(len(saved_sigs), 1)
            sig_id = saved_sigs[0]["id"]
            self.assertTrue((corpus_dir / f"{sig_id}.html").exists())

            # Report should only have 1 unique crash row, and unpacked_raw should not pollute output_dir
            report_text = (output_dir / "report.md").read_text(encoding="utf-8")
            self.assertIn("**Unique New Panics**: 1", report_text)
            self.assertFalse((output_dir / "unpacked_raw").exists())
            new_crash_dirs = list((output_dir / "new_crashes").iterdir())
            self.assertEqual(len(new_crash_dirs), 1)

    def test_cli_import_help_and_execution(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            crashes_dir = tmp_path / "crashes"
            crashes_dir.mkdir()
            fake_bin = tmp_path / "servoshell"
            fake_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_bin.chmod(0o755)

            with patch("servo_fuzzing.importer.run_single_test") as mock_single:
                mock_single.return_value = TestResult(
                    testcase=Path("about:blank"),
                    status=TestStatus.PASS,
                    duration=0.1,
                )
                code = cli_main([
                    "import",
                    str(crashes_dir),
                    "--binary", str(fake_bin),
                    "--output", str(tmp_path / "triage"),
                ])
                self.assertEqual(code, 0)
