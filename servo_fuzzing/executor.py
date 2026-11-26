from __future__ import annotations

import os
import re
import shlex
import signal
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from .parser import CrashInfo, parse_crash
from .signatures import CrashSignature, match

DEFAULT_SERVO_ARGS: list[str] = ["-xfz", "--enable-experimental-web-platform-features"]


class TestStatus(str, Enum):
    PASS = "PASS"
    KNOWN_CRASH = "KNOWN_CRASH"
    REGRESSION = "REGRESSION"
    NEW_CRASH = "NEW_CRASH"
    TIMEOUT = "TIMEOUT"
    ERROR = "ERROR"


@dataclass
class TestResult:
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
    total: int = 0
    passed: int = 0
    known_crashes: int = 0
    regressions: int = 0
    new_crashes: int = 0
    timeouts: int = 0
    errors: int = 0
    duration: float = 0.0
    results: list[TestResult] = field(default_factory=list)

    @property
    def has_new_crashes(self) -> bool:
        return self.new_crashes > 0 or self.regressions > 0

    @property
    def is_success(self) -> bool:
        return self.new_crashes == 0 and self.regressions == 0 and self.errors == 0


def _is_windows() -> bool:
    return os.name == "nt"


def _is_url(target: str) -> bool:
    """Return True for URL schemes (e.g., about:blank, http://) without matching Windows drive paths."""
    return bool(re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]+:", target))


def _split_binary_command(
    binary: str | Path | list[str] | tuple[str, ...],
    *,
    is_windows: Optional[bool] = None,
) -> list[str]:
    """Split a binary specification into argv parts without stripping Windows backslashes."""
    if isinstance(binary, (list, tuple)):
        return [str(part) for part in binary]
    if isinstance(binary, Path):
        return [str(binary)]

    raw = str(binary).strip()
    if raw and Path(raw).exists():
        return [raw]

    win = _is_windows() if is_windows is None else is_windows
    if win:
        parts = shlex.split(raw, posix=False)
        return [p[1:-1] if len(p) >= 2 and p[0] == p[-1] == '"' else p for p in parts]
    return shlex.split(raw, posix=True)


def _popen_kwargs(*, is_windows: Optional[bool] = None) -> dict[str, object]:
    """Return platform-appropriate process-group creation kwargs for Popen."""
    win = _is_windows() if is_windows is None else is_windows
    if win:
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return {"creationflags": flags} if flags else {}
    return {"start_new_session": True}


def _terminate_process_tree(proc: subprocess.Popen, *, is_windows: Optional[bool] = None) -> int:
    """Force-terminate a timed-out subprocess and its child process tree."""
    win = _is_windows() if is_windows is None else is_windows
    if win or not hasattr(os, "killpg") or not hasattr(signal, "SIGKILL"):
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            proc.kill()
        except OSError:
            pass
        return proc.returncode if proc.returncode is not None else -9

    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass
    return -signal.SIGKILL


def run_single_test(
    binary: str | Path | list[str] | tuple[str, ...],
    testcase: str | Path,
    servo_args: Optional[list[str]] = None,
    timeout: float = 5.0,
    signatures: Optional[list[CrashSignature]] = None,
    fixed_signatures: Optional[list[CrashSignature]] = None,
    env: Optional[dict[str, str]] = None,
) -> TestResult:
    raw_target = str(testcase)
    if _is_url(raw_target):
        testcase_arg = raw_target
        testcase_path = testcase if isinstance(testcase, Path) else Path(raw_target)
    else:
        testcase_path = Path(testcase).resolve()
        testcase_arg = str(testcase_path)

    args = servo_args if servo_args is not None else ["-xfz"]
    cmd = _split_binary_command(binary) + list(args) + [testcase_arg]

    run_env = os.environ.copy()
    run_env["RUST_BACKTRACE"] = "1"
    if env:
        run_env.update(env)

    start_time = time.perf_counter()
    stdout_data = ""
    stderr_data = ""
    exit_code = None
    timed_out = False

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=run_env,
            **_popen_kwargs(),
        )
        try:
            out, err = proc.communicate(timeout=timeout)
            stdout_data = out or ""
            stderr_data = err or ""
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            fallback_exit_code = _terminate_process_tree(proc)
            try:
                out, err = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass
                out, err = "", ""
            stdout_data = out or ""
            stderr_data = err or ""
            exit_code = (
                fallback_exit_code
                if not _is_windows()
                else (proc.returncode if proc.returncode is not None else fallback_exit_code)
            )
    except Exception as exc:
        return TestResult(
            testcase=testcase_path,
            status=TestStatus.ERROR,
            duration=time.perf_counter() - start_time,
            error_message=str(exc),
        )

    duration = time.perf_counter() - start_time
    combined = (stdout_data + "\n" + stderr_data).strip()
    crash_info = parse_crash(combined)

    if crash_info:
        sig_id = match(crash_info, signatures or [])
        if sig_id:
            issue_url = next((s["issue"] for s in (signatures or []) if s["id"] == sig_id), None)
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
        reg_id = match(crash_info, fixed_signatures or [])
        if reg_id:
            issue_url = next((s["issue"] for s in (fixed_signatures or []) if s["id"] == reg_id), None)
            return TestResult(
                testcase=testcase_path,
                status=TestStatus.REGRESSION,
                exit_code=exit_code,
                duration=duration,
                stdout=stdout_data,
                stderr=stderr_data,
                combined_output=combined,
                crash_info=crash_info,
                matched_signature_id=reg_id,
                matched_issue=issue_url,
            )
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


def _run_test_with_retries(
    binary: str | Path,
    testcase: Path,
    servo_args: Optional[list[str]] = None,
    timeout: float = 5.0,
    signatures: Optional[list[CrashSignature]] = None,
    fixed_signatures: Optional[list[CrashSignature]] = None,
    attempts: int = 1,
) -> TestResult:
    tries = max(1, attempts)
    last_result: Optional[TestResult] = None
    for _ in range(tries):
        res = run_single_test(
            binary=binary,
            testcase=testcase,
            servo_args=servo_args,
            timeout=timeout,
            signatures=signatures,
            fixed_signatures=fixed_signatures,
        )
        last_result = res
        if res.status not in (TestStatus.PASS, TestStatus.TIMEOUT):
            return res
    assert last_result is not None
    return last_result


def run_suite(
    binary: str | Path,
    testcases: list[Path],
    servo_args: Optional[list[str]] = None,
    timeout: float = 5.0,
    concurrency: int = 4,
    signatures: Optional[list[CrashSignature]] = None,
    fixed_signatures: Optional[list[CrashSignature]] = None,
    on_result: Optional[Callable[[TestResult, int, int], None]] = None,
    attempts: int = 1,
) -> SuiteSummary:
    start_time = time.perf_counter()
    summary = SuiteSummary(total=len(testcases))
    workers = concurrency if concurrency > 0 else (os.cpu_count() or 4)
    completed_count = 0

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_case = {
            executor.submit(
                _run_test_with_retries,
                binary=binary,
                testcase=tc,
                servo_args=servo_args,
                timeout=timeout,
                signatures=signatures,
                fixed_signatures=fixed_signatures,
                attempts=attempts,
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
            elif result.status == TestStatus.REGRESSION:
                summary.regressions += 1
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
