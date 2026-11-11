"""Tests for runner and reporter."""

import json
import tempfile
import unittest
from pathlib import Path

from servo_fuzzing.executor import SuiteSummary, TestResult, TestStatus
from servo_fuzzing.parser import CrashInfo
from servo_fuzzing.reporter import make_signature_draft, write_artifacts
from servo_fuzzing.runner import collect_testcases, load_config


class TestRunnerAndReporter(unittest.TestCase):
    def test_collect_testcases(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            f1 = d / "test1.html"
            f2 = d / "test2.htm"
            f3 = d / "ignore.txt"
            f1.write_text("html1")
            f2.write_text("html2")
            f3.write_text("txt")

            cases = collect_testcases([d], [".html", ".htm"])
            self.assertEqual(len(cases), 2)
            self.assertIn(f1.resolve(), cases)
            self.assertIn(f2.resolve(), cases)

    def test_load_json_config(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"timeout_seconds": 10.0, "concurrency": 8}, f)
            path = Path(f.name)

        cfg = load_config(path)
        self.assertEqual(cfg["timeout_seconds"], 10.0)
        self.assertEqual(cfg["concurrency"], 8)
        path.unlink()

    def test_write_artifacts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            tc = out_dir / "crash.html"
            tc.write_text("<body>crash</body>")

            crash_info = CrashInfo(
                message="assertion failed: test",
                source_file="components/test.rs",
                stack_frames=["frame1", "frame2"],
            )

            res = TestResult(
                testcase=tc,
                status=TestStatus.NEW_CRASH,
                exit_code=101,
                duration=0.5,
                combined_output="panic output",
                crash_info=crash_info,
            )

            summary = SuiteSummary(
                total=1,
                new_crashes=1,
                duration=0.5,
                results=[res],
            )

            write_artifacts(summary, output_dir=out_dir)

            self.assertTrue((out_dir / "summary.json").exists())
            self.assertTrue((out_dir / "report.md").exists())
            self.assertTrue((out_dir / "new_crashes" / "crash" / "signature_draft.json").exists())
            self.assertTrue((out_dir / "new_crashes" / "crash" / "github_issue_draft.md").exists())

            summary_json = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
            self.assertFalse(summary_json["success"])
            self.assertEqual(summary_json["new_crashes"], 1)

    def test_make_signature_draft(self):
        crash_info = CrashInfo(
            message="assertion failed: test_val.is_some()",
            source_file="components/script/dom/test.rs",
            stack_frames=["<script::dom::test>::run", "main"],
        )
        draft = make_signature_draft(crash_info)
        self.assertEqual(draft["message"], "assertion failed: test_val.is_some()")
        self.assertEqual(draft["source_file"], "components/script/dom/test.rs")
        self.assertIn("<script::dom::test>::run", draft["stack_frames"])
        self.assertTrue(draft["issue"].startswith("http"))


if __name__ == "__main__":
    unittest.main()
