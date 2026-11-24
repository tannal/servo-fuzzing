"""Tests for test execution and triage."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from servo_fuzzing.executor import (
    TestStatus,
    _is_url,
    _split_binary_command,
    run_single_test,
    run_suite,
)
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

    def test_run_regression_from_fixed_history(self):
        fixed_sigs: list[CrashSignature] = [
            {
                "id": "fixed-unexpected-panic",
                "message": "assertion failed: unexpected_panic",
                "source_file": "components/unexpected.rs",
                "stack_frames": ["unexpected_frame"],
                "issue": "https://github.com/servo/servo/issues/46109",
            }
        ]
        summary = run_suite(
            binary=f"python3 {self.mock_script.name}",
            testcases=[Path("new.html")],
            servo_args=[],
            timeout=5.0,
            concurrency=1,
            signatures=self.signatures,
            fixed_signatures=fixed_sigs,
        )
        self.assertEqual(summary.regressions, 1)
        self.assertEqual(summary.new_crashes, 0)
        self.assertFalse(summary.is_success)
        self.assertTrue(summary.has_new_crashes)
        self.assertEqual(summary.results[0].status, TestStatus.REGRESSION)
        self.assertEqual(summary.results[0].matched_issue, "https://github.com/servo/servo/issues/46109")

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

    def test_split_binary_command_preserves_windows_paths_and_quotes(self):
        self.assertEqual(
            _split_binary_command(Path(r"C:\Users\dev\servo\servoshell.exe")),
            [r"C:\Users\dev\servo\servoshell.exe"],
        )
        self.assertEqual(
            _split_binary_command(Path("/tmp/my dir/servoshell")),
            ["/tmp/my dir/servoshell"],
        )
        self.assertEqual(
            _split_binary_command(r"C:\Users\dev\servoshell.exe", is_windows=True),
            [r"C:\Users\dev\servoshell.exe"],
        )
        self.assertEqual(
            _split_binary_command(r'"C:\Program Files\python.exe" C:\tmp\script.py', is_windows=True),
            [r"C:\Program Files\python.exe", r"C:\tmp\script.py"],
        )
        self.assertEqual(
            _split_binary_command("python3 /tmp/mock.py", is_windows=False),
            ["python3", "/tmp/mock.py"],
        )

    def test_is_url_distinguishes_urls_from_windows_drive_paths(self):
        self.assertTrue(_is_url("about:blank"))
        self.assertTrue(_is_url("https://example.com"))
        self.assertTrue(_is_url("file:///C:/a.html"))
        self.assertFalse(_is_url(r"C:\servo\test.html"))
        self.assertFalse(_is_url("C:/servo/test.html"))
        self.assertFalse(_is_url("pass.html"))

    def test_about_blank_is_not_resolved_as_filesystem_path(self):
        proc = MagicMock()
        proc.communicate.return_value = ("ok", "")
        proc.returncode = 0
        with patch("servo_fuzzing.executor.subprocess.Popen", return_value=proc) as popen:
            res = run_single_test(
                binary=Path(r"C:\servo\servoshell.exe"),
                testcase=Path("about:blank"),
                servo_args=["-xfz"],
                timeout=5.0,
            )
        self.assertEqual(res.status, TestStatus.PASS)
        self.assertEqual(popen.call_args.args[0], [r"C:\servo\servoshell.exe", "-xfz", "about:blank"])

    def test_windows_timeout_uses_process_group_and_taskkill(self):
        proc = MagicMock()
        proc.pid = 4321
        proc.returncode = None
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd=["servoshell.exe"], timeout=1.0),
            ("", ""),
        ]
        with (
            patch("servo_fuzzing.executor._is_windows", return_value=True),
            patch("servo_fuzzing.executor.subprocess.CREATE_NEW_PROCESS_GROUP", 0x200, create=True),
            patch("servo_fuzzing.executor.subprocess.Popen", return_value=proc) as popen,
            patch("servo_fuzzing.executor.subprocess.run") as mock_run,
        ):
            res = run_single_test(
                binary=Path(r"C:\servo\servoshell.exe"),
                testcase=Path("about:blank"),
                servo_args=["-xfz"],
                timeout=1.0,
            )
        self.assertEqual(res.status, TestStatus.TIMEOUT)
        popen_kwargs = popen.call_args.kwargs
        self.assertNotIn("start_new_session", popen_kwargs)
        self.assertEqual(popen_kwargs.get("creationflags"), 0x200)
        mock_run.assert_called_once_with(
            ["taskkill", "/F", "/T", "/PID", "4321"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        proc.kill.assert_called_once()


if __name__ == "__main__":
    unittest.main()
