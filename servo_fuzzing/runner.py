"""Mach runner entrypoint for Servo fuzzing.

Invoked via `./mach test-fuzzing` or `python -m servo_fuzzing.runner`.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from .executor import TestResult, TestStatus, run_single_test, run_suite
from .reporter import print_cli_summary, write_artifacts
from .signatures import load_signatures


def load_config(config_path: Path) -> dict[str, Any]:
    """Load configuration from JSON or TOML."""
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    text = config_path.read_text(encoding="utf-8")
    if config_path.suffix.lower() == ".json":
        return json.loads(text)

    # If python 3.11+ tomllib is available
    try:
        import tomllib  # type: ignore

        return tomllib.loads(text)
    except ImportError:
        pass

    try:
        import tomli  # type: ignore

        return tomli.loads(text)
    except ImportError:
        pass

    # Fallback to json if json content in toml or raise informative error
    try:
        return json.loads(text)
    except Exception:
        raise ValueError(
            f"Config file {config_path} is TOML but neither tomllib nor tomli is installed. "
            "Please use a JSON config (e.g. config.json) or install tomli."
        )


def collect_testcases(test_dirs: list[str | Path], extensions: list[str]) -> list[Path]:
    """Find all matching test case files inside given directories."""
    found: list[Path] = []
    seen = set()

    for d in test_dirs:
        dir_path = Path(d).resolve()
        if not dir_path.exists():
            continue
        if dir_path.is_file():
            if dir_path.suffix.lower() in extensions and dir_path not in seen:
                seen.add(dir_path)
                found.append(dir_path)
            continue

        for ext in extensions:
            for file_path in dir_path.rglob(f"*{ext}"):
                resolved = file_path.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    found.append(resolved)

    return sorted(found)


def get_servo_version(binary: Path) -> str:
    """Query servoshell version string."""
    try:
        res = subprocess.run([str(binary), "--version"], capture_output=True, text=True, timeout=5)
        return res.stdout.strip() or res.stderr.strip() or "unknown"
    except Exception:
        return "unknown"


def parse_args(args: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="servo-fuzzing",
        description="Run Servo fuzzing test suite with crash signature matching",
    )
    parser.add_argument("--binary", required=True, help="Path to servoshell binary")
    parser.add_argument("--config", default=None, help="Path to config file (.json or .toml)")
    parser.add_argument("--signatures", default=None, help="Path to rust-panic.json signatures file (auto-detected if omitted)")
    parser.add_argument("--output", default="target/fuzzing", help="Artifacts directory (default: target/fuzzing)")
    parser.add_argument("--testcases", nargs="*", default=None, help="Direct testcase directories or HTML files to replay")
    parser.add_argument("--bundle", default=None, help="Path or URL to testcase bundle (.tar.gz)")
    parser.add_argument("--force-download", action="store_true", help="Force re-downloading the latest bundle ignoring cache")
    parser.add_argument("-j", "--jobs", type=int, default=None, help="Number of parallel workers (overrides config)")
    parser.add_argument("--timeout", type=float, default=None, help="Timeout in seconds per test (overrides config)")
    parser.add_argument("--generate", action="store_true", help="Generate fresh testcases before running (discovery mode)")
    parser.add_argument("--generate-count", type=int, default=100, help="Testcase count per generator (default: 100)")
    parser.add_argument("--auto-clone", action="store_true", default=None, help="Automatically clone missing generators")
    parser.add_argument("--no-auto-clone", action="store_false", dest="auto_clone", help="Disable automatic generator cloning")
    parser.add_argument("--dry-run", action="store_true", help="Load configuration and validate without running tests")
    parser.add_argument("positional_testcases", nargs="*", default=[], help="Optional positional test files or directories")

    return parser.parse_args(args)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)

    binary_path = Path(args.binary).resolve()
    if not binary_path.exists() or not os.access(binary_path, os.X_OK):
        print(f"Error: servoshell binary not found or not executable: {binary_path}")
        return 2

    signatures_path: Optional[Path] = None
    if args.signatures:
        signatures_path = Path(args.signatures).resolve()
    else:
        # Search candidate locations
        repo_root = Path(__file__).resolve().parent.parent
        candidates = [
            Path.cwd() / "tests" / "fuzzing" / "signatures" / "rust-panic.json",
            Path.cwd() / "signatures" / "rust-panic.json",
            repo_root / "signatures" / "rust-panic.json",
        ]
        for cand in candidates:
            if cand.exists():
                signatures_path = cand.resolve()
                break

    output_dir = Path(args.output).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    config: dict[str, Any] = {}
    if args.config:
        config_path = Path(args.config).resolve()
        try:
            config = load_config(config_path)
        except Exception as exc:
            print(f"Error loading configuration: {exc}")
            return 2

    signatures = []
    if signatures_path and signatures_path.exists():
        try:
            signatures = load_signatures(signatures_path)
            print(f"Loaded {len(signatures)} known crash signatures from {signatures_path}.")
        except Exception as exc:
            print(f"Error loading crash signatures: {exc}")
            return 2
    else:
        print("Notice: No signatures file found. All crashes will be treated as unexpected.")

    timeout = args.timeout if args.timeout is not None else float(config.get("timeout_seconds", 5.0))
    concurrency = args.jobs if args.jobs is not None else int(config.get("concurrency", 4))
    servo_args = config.get("servo_args", ["-xfz", "--enable-experimental-web-platform-features"])

    test_dirs: list[str | Path] = []
    if args.positional_testcases:
        test_dirs.extend(args.positional_testcases)
    if args.testcases:
        test_dirs.extend(args.testcases)

    if not test_dirs and args.bundle:
        from .bundle import obtain_bundle, unpack_bundle

        bundle_tar = obtain_bundle(bundle_spec=args.bundle, force_download=args.force_download)
        if bundle_tar:
            bundle_target = output_dir / "bundle"
            unpack_bundle(bundle_tar=bundle_tar, target_dir=bundle_target, clean_target=True)
            test_dirs.append(bundle_target)
        else:
            print("Warning: Could not obtain specified bundle. Falling back to default testcases.")
            test_dirs.append("testcases")
    elif not test_dirs and "test_dirs" in config:
        test_dirs.extend(config["test_dirs"])
    elif not test_dirs:
        # Default behavior: automatically resolve and use the latest release bundle
        from .bundle import obtain_bundle, unpack_bundle

        print("No testcases specified. Automatically resolving latest bundle from GitHub Releases...")
        bundle_tar = obtain_bundle(bundle_spec="latest", force_download=args.force_download)
        if bundle_tar:
            bundle_target = output_dir / "bundle"
            unpack_bundle(bundle_tar=bundle_tar, target_dir=bundle_target, clean_target=True)
            test_dirs.append(bundle_target)
        else:
            repo_root = Path(__file__).resolve().parent.parent
            local_corpus = repo_root / "testcases" / "corpus"
            if local_corpus.exists():
                print(f"Using local regression corpus: {local_corpus}")
                test_dirs.append(local_corpus)
            else:
                test_dirs.append("testcases")

    extensions = config.get("test_extensions", [".html", ".htm"])

    # Optional test generation step (Discovery mode only)
    gen_config = config.get("generators", {})
    should_generate = args.generate or gen_config.get("enabled", False)
    if should_generate:
        from .generators import GeneratorManager

        count = args.generate_count or gen_config.get("count_per_generator", 100)
        auto_clone = (
            args.auto_clone
            if args.auto_clone is not None
            else gen_config.get("auto_clone", True)
        )
        gen_out = output_dir / "generated"
        print(f"\n--- Generating test cases ({count} per tool) ---")
        gen_mgr = GeneratorManager(
            domato_path=Path(gen_config.get("domato_path")) if gen_config.get("domato_path") else None,
            freedom_path=Path(gen_config.get("freedom_path")) if gen_config.get("freedom_path") else None,
            minerva_path=Path(gen_config.get("minerva_path")) if gen_config.get("minerva_path") else None,
            minerva_mem_dep=Path(gen_config.get("minerva_mem_dep")) if gen_config.get("minerva_mem_dep") else None,
        )
        results = gen_mgr.generate_all(output_dir=gen_out, count_per_generator=count, auto_clone=auto_clone)
        total_gen = sum(len(c) for c in results.values())
        print(f"Total generated: {total_gen} test cases across {len(results)} generators.\n")
        test_dirs.append(gen_out)

    testcases = collect_testcases(test_dirs, extensions)
    print(f"Found {len(testcases)} test cases to execute.")

    if not testcases:
        print(f"Warning: No testcases found in directories: {test_dirs}")
        return 0

    if args.dry_run:
        print("Dry run requested. Configuration and signatures are valid.")
        return 0

    # 1. Health check: verify servoshell can start about:blank
    print(f"\nPerforming health check: {binary_path} {servo_args} about:blank ...")
    health_result = run_single_test(
        binary=binary_path,
        testcase=Path("about:blank"),
        servo_args=servo_args,
        timeout=10.0,
        signatures=signatures,
    )
    if health_result.status in (TestStatus.NEW_CRASH, TestStatus.ERROR):
        print("FATAL: servoshell failed health check on about:blank!")
        print(health_result.combined_output)
        return 2
    print("Health check passed.")

    servo_version = get_servo_version(binary_path)
    print(f"Target Servo: {servo_version}")
    print(f"Running suite with concurrency={concurrency}, timeout={timeout}s...\n")

    def on_result(res: TestResult, current: int, total: int) -> None:
        prefix = f"[{current:4d}/{total:4d}]"
        name = res.testcase.name
        if res.status == TestStatus.PASS:
            print(f"{prefix} ✅ PASS        : {name} ({res.duration:.2f}s)")
        elif res.status == TestStatus.KNOWN_CRASH:
            print(f"{prefix} ⚠️  KNOWN CRASH: {name} -> {res.matched_signature_id} ({res.duration:.2f}s)")
        elif res.status == TestStatus.NEW_CRASH:
            msg = res.crash_info.message if res.crash_info else "Unexpected exit"
            print(f"{prefix} ❌ NEW CRASH  : {name} -> {msg} ({res.duration:.2f}s)")
        elif res.status == TestStatus.TIMEOUT:
            print(f"{prefix} ⏱️  TIMEOUT    : {name} (> {timeout}s)")
        else:
            print(f"{prefix} ⚠️  ERROR      : {name} ({res.error_message})")

    summary = run_suite(
        binary=binary_path,
        testcases=testcases,
        servo_args=servo_args,
        timeout=timeout,
        concurrency=concurrency,
        signatures=signatures,
        on_result=on_result,
    )

    # Write output artifacts
    write_artifacts(summary, output_dir=output_dir, servo_version=servo_version)
    # Preserve new panic inputs with the same collector used for CI artifacts.
    from .collect import collect_panics
    collected = collect_panics(output_dir, output_dir / "corpus")
    if collected:
        print(f"Saved {collected} new panic testcase(s) to {output_dir / 'corpus'}")
    print_cli_summary(summary)

    if summary.has_new_crashes:
        return 1
    if summary.errors > 0:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
