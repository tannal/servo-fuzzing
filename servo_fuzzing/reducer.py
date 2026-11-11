from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from .executor import run_single_test
from .signatures import CrashSignature, match


def reduce_with_lithium(
    binary: str | Path,
    testcase: Path,
    target_pattern: Optional[str] = None,
    servo_args: Optional[list[str]] = None,
    timeout: int = 1,
) -> bool:
    args = servo_args if servo_args is not None else ["-xzf"]
    cmd = [sys.executable, "-m", "lithium"]
    if target_pattern:
        cmd.extend(["outputs", f"-t{timeout}", "-s", target_pattern])
    else:
        cmd.extend(["crashes", f"-t{timeout}"])

    cmd.extend([str(binary), *args, str(testcase)])
    try:
        subprocess.run(cmd, check=True)
        return True
    except FileNotFoundError:
        print("Error: lithium module not installed. Install via: pip install lithium-reducer")
        return False
    except subprocess.CalledProcessError as exc:
        print(f"Lithium reduction failed: {exc}")
        return False


def reduce_delta_lines(
    binary: str | Path,
    testcase: Path,
    target_signature: Optional[CrashSignature] = None,
    target_message: Optional[str] = None,
    output_path: Optional[Path] = None,
    timeout: float = 3.0,
    servo_args: Optional[list[str]] = None,
    attempts: int = 1,
) -> Path:
    if attempts < 1 or timeout <= 0:
        raise ValueError("attempts and timeout must be positive")

    lines = testcase.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    out_file = output_path or testcase.with_name(f"{testcase.stem}-reduced.html")
    out_file.parent.mkdir(parents=True, exist_ok=True)
    runs = 0

    with tempfile.NamedTemporaryFile(
        prefix=f".{out_file.stem}-",
        suffix=out_file.suffix or ".html",
        dir=out_file.parent,
        delete=False,
    ) as trial:
        trial_path = Path(trial.name)

    def test_lines(candidate_lines: list[str]) -> bool:
        nonlocal runs
        trial_path.write_text("".join(candidate_lines), encoding="utf-8")
        for _ in range(attempts):
            runs += 1
            res = run_single_test(
                binary=binary,
                testcase=trial_path,
                servo_args=servo_args,
                timeout=timeout,
            )
            if res.crash_info is None:
                continue
            if target_message and target_message not in res.crash_info.message:
                continue
            if target_signature and match(res.crash_info, [target_signature]) != target_signature["id"]:
                continue
            return True
        return False

    try:
        if not test_lines(lines):
            raise ValueError(f"Baseline testcase does not reproduce target panic on {binary}")

        out_file.write_text("".join(lines), encoding="utf-8")
        print(f"Starting chunk reduction for {testcase.name} ({len(lines)} lines)...", flush=True)

        chunk = 1 << max(0, (len(lines) - 1).bit_length() - 1)
        while True:
            changed = False
            idx = 0
            while idx < len(lines):
                candidate = lines[:idx] + lines[idx + chunk:]
                if test_lines(candidate):
                    lines = candidate
                    changed = True
                    out_file.write_text("".join(lines), encoding="utf-8")
                    print(f"Reduced to {len(lines)} lines (chunk {chunk}, {runs} runs)", flush=True)
                else:
                    idx += chunk
            if chunk == 1:
                if not changed:
                    break
            else:
                chunk //= 2

        print(f"Reduction complete: {out_file} ({len(lines)} lines, {runs} runs)")
        return out_file
    finally:
        trial_path.unlink(missing_ok=True)
