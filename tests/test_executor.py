"""Tests for test execution and triage."""

import tempfile
import unittest
from pathlib import Path

from servo_fuzzing.executor import TestStatus, run_single_test, run_suite
from servo_fuzzing.signatures import CrashSignature


class TestExecutor(unittest.TestCase):
    def setUp(self):
        self.signatures: list[CrashSignature] = [
            {
                "id": "known-panic-1",
                "message": "assertion failed: dummy_assertion",
                "source_file": "components/dummy.rs",
                "stack_frames": ["dummy_frame"],
                "issue": "https://github.com/servo/servo/issues/999",
            }
        ]

        # Create a mock python script that behaves like servoshell
        self.mock_script = tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False)
        self.mock_script.write(
            """import sys, time

args = sys.argv[1:]
if "pass.html" in str(args):
    print("Page loaded successfully")
    sys.exit(0)
elif "known.html" in str(args):
    print("assertion failed: dummy_assertion (thread Script#1, at components/dummy.rs:100)")
    print("   0: dummy_frame")
    sys.exit(101)
elif "new.html" in str(args):
    print("assertion failed: unexpected_panic (thread Script#1, at components/unexpected.rs:50)")
    print("   0: unexpected_frame")
    sys.exit(101)
elif "timeout.html" in str(args):
    time.sleep(10)
    sys.exit(0)
elif "about:blank" in str(args):
    print("about:blank ok")
    sys.exit(0)
else:
    print("unknown argument")
    sys.exit(1)
"""
        )
        self.mock_script.close()

    def tearDown(self):
        Path(self.mock_script.name).unlink()

    def test_run_passing_test(self):
        res = run_single_test(
            binary=f"python3 {self.mock_script.name}",
            testcase=Path("pass.html"),
            servo_args=[],
            timeout=5.0,
            signatures=self.signatures,
        )
        self.assertEqual(res.status, TestStatus.PASS)
        self.assertEqual(res.exit_code, 0)

    def test_run_known_crash(self):
        res = run_single_test(
            binary=f"python3 {self.mock_script.name}",
            testcase=Path("known.html"),
            servo_args=[],
            timeout=5.0,
            signatures=self.signatures,
        )
        self.assertEqual(res.status, TestStatus.KNOWN_CRASH)
        self.assertEqual(res.matched_signature_id, "known-panic-1")

    def test_run_new_crash(self):
        res = run_single_test(
            binary=f"python3 {self.mock_script.name}",
            testcase=Path("new.html"),
            servo_args=[],
            timeout=5.0,
            signatures=self.signatures,
        )
        self.assertEqual(res.status, TestStatus.NEW_CRASH)
        self.assertIsNone(res.matched_signature_id)

    def test_run_timeout(self):
        res = run_single_test(
            binary=f"python3 {self.mock_script.name}",
            testcase=Path("timeout.html"),
            servo_args=[],
            timeout=0.5,
            signatures=self.signatures,
        )
        self.assertEqual(res.status, TestStatus.TIMEOUT)

    def test_run_suite_summary(self):
        cases = [Path("pass.html"), Path("known.html")]
        summary = run_suite(
            binary=f"python3 {self.mock_script.name}",
            testcases=cases,
            servo_args=[],
            timeout=5.0,
            concurrency=2,
            signatures=self.signatures,
        )
        self.assertEqual(summary.total, 2)
        self.assertEqual(summary.passed, 1)
        self.assertEqual(summary.known_crashes, 1)
        self.assertEqual(summary.new_crashes, 0)
        self.assertTrue(summary.is_success)


if __name__ == "__main__":
    unittest.main()
