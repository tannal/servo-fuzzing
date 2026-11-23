from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from .executor import DEFAULT_SERVO_ARGS, TestResult, TestStatus, run_single_test, run_suite
from .reporter import print_cli_summary, write_artifacts
from .signatures import default_fixed_history_path, load_signatures


def load_config(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    text = config_path.read_text(encoding="utf-8")
    if config_path.suffix.lower() == ".json":
        return json.loads(text)

    try:
        import tomllib
        return tomllib.loads(text)
    except ImportError:
        pass

    try:
        import tomli
        return tomli.loads(text)
    except ImportError:
        pass

    try:
        return json.loads(text)
    except Exception:
        raise ValueError(f"Config file {config_path} could not be parsed as JSON or TOML.")


def collect_testcases(test_dirs: list[str | Path], extensions: list[str]) -> list[Path]:
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
    parser.add_argument("--signatures", default=None, help="Path to rust-panic.json signatures file")
    parser.add_argument("--output", default="target/fuzzing", help="Artifacts directory (default: target/fuzzing)")
    parser.add_argument("--testcases", nargs="*", default=None, help="Direct testcase directories or HTML files")
    parser.add_argument("--bundle", default=None, help="Path or URL to testcase bundle (.tar.gz)")
    parser.add_argument("--force-download", action="store_true", help="Force re-downloading bundle")
    parser.add_argument(
        "--timeouts",
        choices=["include", "exclude"],
        default="include",
        help="Include or exclude testcases marked as timeouts in a bundle manifest (default: include)",
    )
    parser.add_argument("-j", "--jobs", type=int, default=None, help="Number of parallel workers")
    parser.add_argument("--timeout", type=float, default=None, help="Timeout in seconds per test")
    parser.add_argument("--generate", action="store_true", help="Generate fresh testcases before running")
    parser.add_argument("--generate-count", type=int, default=100, help="Testcase count per generator")
    parser.add_argument("--auto-clone", action="store_true", default=None, help="Auto clone missing generators")
    parser.add_argument("--no-auto-clone", action="store_false", dest="auto_clone", help="Disable auto cloning")
    parser.add_argument("--dry-run", action="store_true", help="Validate configuration without running")
    parser.add_argument("positional_testcases", nargs="*", default=[], help="Optional test files or directories")

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
        repo_root = Path(__file__).resolve().parent.parent
        for cand in [
            Path.cwd() / "tests" / "fuzzing" / "signatures" / "rust-panic.json",
            Path.cwd() / "signatures" / "rust-panic.json",
            repo_root / "signatures" / "rust-panic.json",
        ]:
            if cand.exists():
                signatures_path = cand.resolve()
                break

    output_dir = Path(args.output).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    config: dict[str, Any] = {}
    if args.config:
        try:
            config = load_config(Path(args.config).resolve())
        except Exception as exc:
            print(f"Error loading configuration: {exc}")
            return 2

    signatures = []
    fixed_signatures = []
    if signatures_path and signatures_path.exists():
        try:
            signatures = load_signatures(signatures_path)
            print(f"Loaded {len(signatures)} known crash signatures from {signatures_path}.")
            history_path = default_fixed_history_path(signatures_path)
            if history_path.exists():
                fixed_signatures = load_signatures(history_path)
                print(f"Loaded {len(fixed_signatures)} historical fixed signatures from {history_path}.")
        except Exception as exc:
            print(f"Error loading crash signatures: {exc}")
            return 2
    else:
        print("Notice: No signatures file found. All crashes will be treated as unexpected.")

    timeout = args.timeout if args.timeout is not None else float(config.get("timeout_seconds", 5.0))
    concurrency = args.jobs if args.jobs is not None else int(config.get("concurrency", 4))
    servo_args = config.get("servo_args", list(DEFAULT_SERVO_ARGS))

    test_dirs: list[str | Path] = []
    if args.positional_testcases:
        test_dirs.extend(args.positional_testcases)
    if args.testcases:
        test_dirs.extend(args.testcases)

    gen_config = config.get("generators", {})
    generate = bool(args.generate or gen_config.get("enabled", False))

    bundle_spec = args.bundle
    if not test_dirs and not bundle_spec:
        test_dirs.extend(config.get("test_dirs", []))

    excluded_timeout_paths: set[Path] = set()
    # Without explicit testcases, replay a bundle; a generating run only does so when asked.
    if not test_dirs and not bundle_spec and not generate:
        print("No testcases specified. Resolving the latest bundle from GitHub Releases...")
        bundle_spec = "latest"
    if not test_dirs and bundle_spec:
        from .bundle import obtain_bundle, read_bundle_manifest, unpack_bundle

        bundle_tar = obtain_bundle(bundle_spec=bundle_spec, force_download=args.force_download)
        if not bundle_tar:
            print(f"Error: could not obtain bundle '{bundle_spec}'. Pass --testcases or --bundle.")
            return 2
        bundle_target = output_dir / "bundle"
        unpack_bundle(bundle_tar=bundle_tar, target_dir=bundle_target, clean_target=True)
        test_dirs.append(bundle_target)
        manifest = read_bundle_manifest(bundle_target)
        if manifest:
            print(
                f"Bundle manifest: version={manifest.get('version', 'unknown')} "
                f"timeouts={manifest.get('timeouts', 'unknown')} "
                f"total={manifest.get('total_tests', 'unknown')}"
            )
            if args.timeouts == "exclude":
                timeout_entries = manifest.get("timeout_tests") or []
                excluded_timeout_paths = {(bundle_target / rel).resolve() for rel in timeout_entries}

    extensions = config.get("test_extensions", [".html", ".htm"])

    if generate:
        from .generators import GeneratorManager

        count = args.generate_count or gen_config.get("count_per_generator", 100)
        auto_clone = args.auto_clone if args.auto_clone is not None else gen_config.get("auto_clone", True)
        gen_out = output_dir / "generated"
        print(f"\n--- Generating test cases ({count} per tool) ---")
        gen_mgr = GeneratorManager(
            domato_path=Path(gen_config.get("domato_path")) if gen_config.get("domato_path") else None,
            freedom_path=Path(gen_config.get("freedom_path")) if gen_config.get("freedom_path") else None,
            minerva_path=Path(gen_config.get("minerva_path")) if gen_config.get("minerva_path") else None,
            minerva_mem_dep=Path(gen_config.get("minerva_mem_dep")) if gen_config.get("minerva_mem_dep") else None,
        )
        results = gen_mgr.generate_all(output_dir=gen_out, count_per_generator=count, auto_clone=auto_clone)
        print(f"Total generated: {sum(len(c) for c in results.values())} test cases across {len(results)} generators.\n")
        test_dirs.append(gen_out)

    testcases = collect_testcases(test_dirs, extensions)
    if excluded_timeout_paths:
        before_filter = len(testcases)
        testcases = [tc for tc in testcases if tc not in excluded_timeout_paths]
        skipped = before_filter - len(testcases)
        if skipped:
            print(f"Excluded {skipped} bundled timeout testcase(s) via --timeouts exclude.")
    print(f"Found {len(testcases)} test cases to execute.")

    if not testcases:
        print(f"Warning: No testcases found in directories: {test_dirs}")
        return 0

    if args.dry_run:
        print("Dry run requested. Configuration and signatures are valid.")
        return 0

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
            print(f"{prefix} PASS        : {name} ({res.duration:.2f}s)")
        elif res.status == TestStatus.KNOWN_CRASH:
            print(f"{prefix} KNOWN CRASH: {name} -> {res.matched_signature_id} ({res.duration:.2f}s)")
        elif res.status == TestStatus.REGRESSION:
            print(f"{prefix} REGRESSION : {name} -> {res.matched_signature_id} ({res.matched_issue}) ({res.duration:.2f}s)")
        elif res.status == TestStatus.NEW_CRASH:
            msg = res.crash_info.message if res.crash_info else "Unexpected exit"
            print(f"{prefix} NEW CRASH  : {name} -> {msg} ({res.duration:.2f}s)")
        elif res.status == TestStatus.TIMEOUT:
            print(f"{prefix} TIMEOUT    : {name} (> {timeout}s)")
        else:
            print(f"{prefix} ERROR      : {name} ({res.error_message})")

    summary = run_suite(
        binary=binary_path,
        testcases=testcases,
        servo_args=servo_args,
        timeout=timeout,
        concurrency=concurrency,
        signatures=signatures,
        fixed_signatures=fixed_signatures,
        on_result=on_result,
    )

    write_artifacts(summary, output_dir=output_dir, servo_version=servo_version)

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
