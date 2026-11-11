from __future__ import annotations

import argparse
import math
import os
import signal
import tempfile
import threading
from pathlib import Path
from typing import Optional

from .collect import collect_panics
from .executor import TestStatus, run_single_test, run_suite
from .generators import GeneratorManager
from .reporter import print_cli_summary, write_artifacts
from .runner import get_servo_version
from .signatures import load_signatures


def run_campaign(args: argparse.Namespace, stop: threading.Event) -> int:
    binary = Path(args.binary).resolve()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ValueError(f"Servo binary is not executable: {binary}")
    if args.count <= 0 or args.jobs <= 0 or args.iterations < 0:
        raise ValueError("count/jobs must be positive; iterations must be zero or positive")
    if not math.isfinite(args.timeout) or args.timeout <= 0 or not math.isfinite(args.pause) or args.pause < 0:
        raise ValueError("timeout must be positive and pause nonnegative, both finite")

    signatures = load_signatures(Path(args.signatures))
    servo_args = args.servo_arg or ["-xfz", "--enable-experimental-web-platform-features"]
    health = run_single_test(binary=binary, testcase=Path("about:blank"), servo_args=servo_args, timeout=10)
    if health.status != TestStatus.PASS:
        raise ValueError(f"Servo health check failed: {health.combined_output or health.error_message}")

    manager = GeneratorManager(
        generators_dir=Path(args.generators_dir).resolve() if args.generators_dir else None,
        domato_path=Path(args.domato_path).resolve() if args.domato_path else None,
        freedom_path=Path(args.freedom_path).resolve() if args.freedom_path else None,
        minerva_path=Path(args.minerva_path).resolve() if args.minerva_path else None,
    )
    if args.clone:
        manager.clone_all()
    if not any(gen.is_installed for gen in manager.generators.values()):
        raise ValueError("No generators installed; run setup-generators or supply generator paths")

    output, corpus = Path(args.output).resolve(), Path(args.corpus).resolve()
    output.mkdir(parents=True, exist_ok=True)
    version = get_servo_version(binary)
    found_crash = False
    iteration = 0

    while not stop.is_set() and (args.iterations == 0 or iteration < args.iterations):
        iteration += 1
        report = Path(tempfile.mkdtemp(prefix=f"round-{iteration:06d}-", dir=output))
        print(f"\n=== Campaign round {iteration}: {report} ===", flush=True)

        with tempfile.TemporaryDirectory(prefix="generated-", dir=report) as directory:
            generated = manager.generate_all(Path(directory), count_per_generator=args.count, auto_clone=False)
            cases = []
            for tool, batch in generated.items():
                for case in batch:
                    case = Path(case).resolve()
                    named = case.with_name(f"{tool}-{case.name}")
                    case.rename(named)
                    cases.append(named)
            cases = sorted(set(cases))
            if stop.is_set():
                break
            if not cases:
                raise ValueError("Generators produced no testcases; stopping.")

            summary = run_suite(
                binary=binary,
                testcases=cases,
                servo_args=servo_args,
                timeout=args.timeout,
                concurrency=args.jobs,
                signatures=signatures,
            )
            write_artifacts(summary, report, servo_version=version)
            copied = collect_panics(report, corpus)
            print_cli_summary(summary)
            print(f"Preserved {copied} new panic input(s) in {corpus}", flush=True)
            found_crash |= summary.has_new_crashes
            if summary.errors:
                raise ValueError(f"Replay encountered {summary.errors} error(s); see {report}")

        if args.iterations == 0 or iteration < args.iterations:
            stop.wait(args.pause)

    return 1 if found_crash else 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Continuous fuzzing campaign")
    parser.add_argument("--binary", default="./servo/servoshell")
    parser.add_argument(
        "--signatures",
        default=str(Path(__file__).resolve().parent.parent / "signatures/rust-panic.json"),
    )
    parser.add_argument("--output", default="target/campaign", help="Output directory for reports")
    parser.add_argument("--corpus", default="testcases/corpus", help="Directory for new panic inputs")
    parser.add_argument("--count", type=int, default=30, help="Inputs per installed generator per round")
    parser.add_argument("--iterations", type=int, default=0, help="Rounds to run (0 for continuous)")
    parser.add_argument("--jobs", "-j", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--pause", type=float, default=1.0)
    parser.add_argument("--servo-arg", action="append", help="Servo arguments")
    parser.add_argument("--generators-dir")
    for name in ("domato", "freedom", "minerva"):
        parser.add_argument(f"--{name}-path")
    parser.add_argument("--clone", action="store_true", help="Clone missing generators before starting")
    args = parser.parse_args(argv)

    stop = threading.Event()
    received = []

    def request_stop(number: int, frame: object) -> None:
        if not received:
            print("\nStopping after the current batch.", flush=True)
        received.append(number)
        stop.set()

    previous = {number: signal.signal(number, request_stop) for number in (signal.SIGINT, signal.SIGTERM)}
    try:
        result = run_campaign(args, stop)
        return 128 + received[0] if received else result
    except (OSError, ValueError) as error:
        print(f"Campaign failed: {error}", flush=True)
        return 2
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


if __name__ == "__main__":
    raise SystemExit(main())
