"""Reproduction and flakiness estimation utilities.

Modern replacement for repeat.sh, parallelize.sh, and reproduce.sh.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class ReproduceStats:
    """Statistics from reproducibility testing."""

    total_runs: int
    matched_runs: int
    reproducibility_rate: float
    duration: float

    @property
    def percentage(self) -> str:
        return f"{self.reproducibility_rate * 100:.1f}%"


def run_single_instance(
    cmd: list[str],
    timeout: float = 2.0,
    env: Optional[dict[str, str]] = None,
) -> tuple[int, str]:
    """Execute a single instance and return (exit_code, combined_output)."""
    run_env = os.environ.copy()
    run_env["RUST_BACKTRACE"] = "1"
    if env:
        run_env.update(env)

    try:
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
            return proc.returncode, (out or "") + "\n" + (err or "")
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            out, err = proc.communicate()
            return -signal.SIGKILL, (out or "") + "\n" + (err or "")
    except Exception as exc:
        return -1, str(exc)


def repeat_test(
    binary: str | Path,
    testcase: str | Path,
    count: int = 10,
    servo_args: Optional[list[str]] = None,
    timeout: float = 5.0,
) -> list[tuple[int, str]]:
    """Execute testcase sequentially `count` times."""
    args = servo_args if servo_args is not None else ["-xfz"]
    cmd = [str(binary)] + args + [str(testcase)]
    results = []

    for i in range(1, count + 1):
        print(f"[{i}/{count}] Running {testcase}...")
        code, out = run_single_instance(cmd, timeout=timeout)
        results.append((code, out))

    return results


def parallelize_test(
    binary: str | Path,
    testcase: str | Path,
    count: int = 10,
    timeout: float = 2.0,
    servo_args: Optional[list[str]] = None,
) -> list[tuple[int, str]]:
    """Execute testcase `count` times in parallel."""
    args = servo_args if servo_args is not None else ["-xfz"]
    cmd = [str(binary)] + args + [str(testcase)]

    print(f"Executing {count} instances in parallel (timeout {timeout}s)...")
    with ThreadPoolExecutor(max_workers=count) as pool:
        futures = [pool.submit(run_single_instance, cmd, timeout=timeout) for _ in range(count)]
        results = [f.result() for f in futures]

    return results


def estimate_reproducibility(
    binary: str | Path,
    testcase: str | Path,
    pattern: str,
    repeat_count: int = 10,
    parallel_count: int = 20,
    timeout: float = 2.0,
    servo_args: Optional[list[str]] = None,
) -> ReproduceStats:
    """Run `repeat_count` rounds of `parallel_count` parallel instances to estimate crash rate."""
    args = servo_args if servo_args is not None else ["-xfz"]
    cmd = [str(binary)] + args + [str(testcase)]
    regex = re.compile(pattern)

    start_time = time.perf_counter()
    total_runs = repeat_count * parallel_count
    matched_runs = 0

    print(f"Testing reproducibility of pattern '{pattern}' on {testcase}...")
    print(f"Running {repeat_count} rounds of {parallel_count} parallel instances (Total {total_runs} runs)...")

    for round_idx in range(1, repeat_count + 1):
        print(f"Round {round_idx}/{repeat_count}: running {parallel_count} instances in parallel...")
        with ThreadPoolExecutor(max_workers=parallel_count) as pool:
            futures = [pool.submit(run_single_instance, cmd, timeout=timeout) for _ in range(parallel_count)]
            for f in futures:
                code, out = f.result()
                if regex.search(out):
                    matched_runs += 1

    duration = time.perf_counter() - start_time
    rate = matched_runs / total_runs if total_runs > 0 else 0.0

    stats = ReproduceStats(
        total_runs=total_runs,
        matched_runs=matched_runs,
        reproducibility_rate=rate,
        duration=duration,
    )

    print(f"\nReproducibility: {matched_runs}/{total_runs} ({stats.percentage}) in {duration:.2f}s")
    return stats
