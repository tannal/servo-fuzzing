"""Execution engine for running test cases against servoshell.

Manages process lifecycles, timeouts, backtrace capture, and status triage.
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from .parser import CrashInfo, parse_crash
from .signatures import CrashSignature, match


class TestStatus(str, Enum):
    PASS = "PASS"
    KNOWN_CRASH = "KNOWN_CRASH"
    NEW_CRASH = "NEW_CRASH"
    TIMEOUT = "TIMEOUT"
    ERROR = "ERROR"


@dataclass
class TestResult:
    """Result of running a single test case."""

    testcase: Path
    status: TestStatus
    exit_code: Optional[int] = None
    duration: float = 0.0
    stdout: str = ""
    stderr: str = ""
    combined_output: str = ""
    crash_info: Optional[CrashInfo] = None
    matched_signature_id: Optional[str] = None
    matched_issue: Optional[str] = None
    error_message: Optional[str] = None


@dataclass
class SuiteSummary:
    """Aggregated summary of a test suite execution."""

    total: int = 0
    passed: int = 0
    known_crashes: int = 0
    new_crashes: int = 0
    timeouts: int = 0
    errors: int = 0
    duration: float = 0.0
    results: list[TestResult] = field(default_factory=list)

    @property
    def has_new_crashes(self) -> bool:
        return self.new_crashes > 0

    @property
    def is_success(self) -> bool:
        """Returns True if no new crashes were encountered (known crashes are ignored)."""
        return self.new_crashes == 0 and self.errors == 0


def run_single_test(
    binary: str | Path,
    testcase: Path,
    servo_args: Optional[list[str]] = None,
    timeout: float = 5.0,
    signatures: Optional[list[CrashSignature]] = None,
    env: Optional[dict[str, str]] = None,
) -> TestResult:
    """Execute a single test case with servoshell and triage results."""
    testcase_path = Path(testcase).resolve()
    if servo_args is None:
        servo_args = ["-xfz"]

    if isinstance(binary, (list, tuple)):
        cmd = list(binary) + list(servo_args) + [str(testcase_path)]
    else:
        cmd = shlex.split(str(binary).strip()) + list(servo_args) + [str(testcase_path)]

    run_env = os.environ.copy()
    run_env["RUST_BACKTRACE"] = "1"
    if env:
        run_env.update(env)

    start_time = time.perf_counter()
    proc = None
    stdout_data = ""
    stderr_data = ""
    combined = ""
    exit_code = None
    timed_out = False

    try:
        # Use start_new_session=True so we can kill entire process group on timeout
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            env=run_env,
        )
        try:
            out, err = proc.communicate(timeout=timeout)
            stdout_data = out or ""
            stderr_data = err or ""
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            out, err = proc.communicate()
            stdout_data = out or ""
            stderr_data = err or ""
            exit_code = -signal.SIGKILL
    except Exception as exc:
        duration = time.perf_counter() - start_time
        return TestResult(
            testcase=testcase_path,
            status=TestStatus.ERROR,
            duration=duration,
            error_message=str(exc),
        )

    duration = time.perf_counter() - start_time
    combined = (stdout_data + "\n" + stderr_data).strip()

    # Parse for any crash / panic in output
    crash_info = parse_crash(combined)

    if crash_info is not None:
        sig_id = match(crash_info, signatures or [])
        if sig_id:
            # Matched a known issue
            issue_url = None
            if signatures:
                for s in signatures:
                    if s["id"] == sig_id:
                        issue_url = s["issue"]
                        break
            return TestResult(
                testcase=testcase_path,
                status=TestStatus.KNOWN_CRASH,
                exit_code=exit_code,
                duration=duration,
                stdout=stdout_data,
                stderr=stderr_data,
                combined_output=combined,
                crash_info=crash_info,
                matched_signature_id=sig_id,
                matched_issue=issue_url,
            )
        else:
            # New, unclassified crash
            return TestResult(
                testcase=testcase_path,
                status=TestStatus.NEW_CRASH,
                exit_code=exit_code,
                duration=duration,
                stdout=stdout_data,
                stderr=stderr_data,
                combined_output=combined,
                crash_info=crash_info,
            )

    if timed_out:
        return TestResult(
            testcase=testcase_path,
            status=TestStatus.TIMEOUT,
            exit_code=exit_code,
            duration=duration,
            stdout=stdout_data,
            stderr=stderr_data,
            combined_output=combined,
        )

    # Process exited without panic or crash
    # If exit code was non-zero and no panic was detected, check for segfault or fatal code
    if exit_code not in (0, None):
        return TestResult(
            testcase=testcase_path,
            status=TestStatus.NEW_CRASH,
            exit_code=exit_code,
            duration=duration,
            stdout=stdout_data,
            stderr=stderr_data,
            combined_output=combined,
            error_message=f"Process exited with unexpected code {exit_code}",
        )

    return TestResult(
        testcase=testcase_path,
        status=TestStatus.PASS,
        exit_code=exit_code,
        duration=duration,
        stdout=stdout_data,
        stderr=stderr_data,
        combined_output=combined,
    )


def run_suite(
    binary: str | Path,
    testcases: list[Path],
    servo_args: Optional[list[str]] = None,
    timeout: float = 5.0,
    concurrency: int = 4,
    signatures: Optional[list[CrashSignature]] = None,
    on_result: Optional[Callable[[TestResult, int, int], None]] = None,
) -> SuiteSummary:
    """Run a suite of testcases concurrently against servoshell."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    start_time = time.perf_counter()
    summary = SuiteSummary(total=len(testcases))

    if concurrency <= 0:
        concurrency = os.cpu_count() or 4

    completed_count = 0

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        future_to_case = {
            executor.submit(
                run_single_test,
                binary=binary,
                testcase=tc,
                servo_args=servo_args,
                timeout=timeout,
                signatures=signatures,
            ): tc
            for tc in testcases
        }

        for future in as_completed(future_to_case):
            result = future.result()
            completed_count += 1
            summary.results.append(result)

            if result.status == TestStatus.PASS:
                summary.passed += 1
            elif result.status == TestStatus.KNOWN_CRASH:
                summary.known_crashes += 1
            elif result.status == TestStatus.NEW_CRASH:
                summary.new_crashes += 1
            elif result.status == TestStatus.TIMEOUT:
                summary.timeouts += 1
            elif result.status == TestStatus.ERROR:
                summary.errors += 1

            if on_result:
                on_result(result, completed_count, len(testcases))

    summary.duration = time.perf_counter() - start_time
    return summary
