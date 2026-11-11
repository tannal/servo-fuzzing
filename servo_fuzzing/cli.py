from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

from .bundle import download_bundle, pack_bundle, unpack_bundle
from .executor import run_single_test
from .generators import GeneratorManager
from .github_sync import (
    check_and_prune_closed_issues,
    sync_closed_history_from_github,
    sync_signatures_from_github,
)
from .reducer import reduce_delta_lines, reduce_with_lithium
from .reporter import make_github_issue_draft
from .reproduce import estimate_reproducibility, parallelize_test, repeat_test
from .runner import get_servo_version, main as runner_main
from .signatures import load_signatures, save_signatures, sync_draft_signatures


def cmd_run(args: argparse.Namespace) -> int:
    runner_args = [
        "--binary",
        args.binary,
        "--signatures",
        args.signatures,
        "--output",
        args.output,
    ]
    if args.config:
        runner_args.extend(["--config", args.config])
    if args.testcases:
        runner_args.append("--testcases")
        runner_args.extend(args.testcases)
    if args.bundle:
        runner_args.extend(["--bundle", args.bundle])
    if getattr(args, "force_download", False):
        runner_args.append("--force-download")
    if args.jobs:
        runner_args.extend(["-j", str(args.jobs)])
    if args.timeout:
        runner_args.extend(["--timeout", str(args.timeout)])
    if args.generate:
        runner_args.append("--generate")
    if hasattr(args, "generate_count") and args.generate_count:
        runner_args.extend(["--generate-count", str(args.generate_count)])
    if args.dry_run:
        runner_args.append("--dry-run")
    return runner_main(runner_args)


def cmd_setup_generators(args: argparse.Namespace) -> int:
    manager = GeneratorManager()
    print("DOM Fuzzing Generators Status:")
    for name, info in manager.get_status().items():
        status = "Installed" if info["installed"] else "Not found"
        print(f"  * {name:8s}: {status} ({info['path']})")

    if getattr(args, "check", False):
        return 0

    print("\nEnsuring all generators are installed...")
    results = manager.clone_all()
    print("Setup completed.")
    return 0 if all(results.values()) else 1


def cmd_generate(args: argparse.Namespace) -> int:
    manager = GeneratorManager()
    output_dir = Path(args.output)

    if getattr(args, "status", False):
        print("DOM Fuzzing Generators Status:")
        for name, info in manager.get_status().items():
            mark = "+" if info["installed"] else "-"
            print(f"  [{mark}] {name:8s} -> {info['path']}")
        return 0

    if args.clone:
        manager.clone_all()

    if args.tool:
        tool_name = args.tool.lower()
        if tool_name not in manager.generators:
            print(f"Unknown generator tool: {tool_name}. Choose from: domato, freedom, minerva")
            return 1
        gen = manager.generators[tool_name]
        if not gen.is_installed:
            print(f"Generator '{tool_name}' is not installed at {gen.local_path}. Use --clone to download it.")
            return 1
        cases = gen.generate(output_dir=output_dir / tool_name, count=args.count)
        print(f"Generated {len(cases)} testcases in {output_dir / tool_name}.")
    else:
        results = manager.generate_all(
            output_dir=output_dir,
            count_per_generator=args.count,
            auto_clone=args.clone,
        )
        print(f"Generated total of {sum(len(c) for c in results.values())} testcases across {len(results)} tools.")
    return 0


def cmd_reproduce(args: argparse.Namespace) -> int:
    binary = Path(args.binary)
    testcase = Path(args.testcase)

    if args.mode == "repeat":
        repeat_test(binary, testcase, count=args.count, timeout=args.timeout)
    elif args.mode == "parallel":
        parallelize_test(binary, testcase, count=args.count, timeout=args.timeout)
    else:
        if not args.pattern:
            print("Error: --pattern is required for reproduce mode (e.g. --pattern 'assertion failed')")
            return 1
        estimate_reproducibility(
            binary=binary,
            testcase=testcase,
            pattern=args.pattern,
            repeat_count=args.repeat,
            parallel_count=args.parallel,
            timeout=args.timeout,
        )
    return 0


def cmd_reduce(args: argparse.Namespace) -> int:
    testcase = Path(args.testcase).resolve()
    if not testcase.exists():
        print(f"Error: Testcase not found: {testcase}")
        return 1

    binary = Path(args.binary).resolve()
    if not binary.exists():
        print(f"Error: Binary not found: {binary}")
        return 1

    target_sig = None
    if getattr(args, "signature", None):
        sig_path = Path(args.signature)
        if sig_path.exists():
            try:
                target_sig = json.loads(sig_path.read_text(encoding="utf-8"))
            except Exception as e:
                print(f"Warning: Failed to parse signature from {sig_path}: {e}")

    pattern = args.pattern if args.pattern else None

    if args.method == "lithium":
        success = reduce_with_lithium(
            binary=binary,
            testcase=testcase,
            target_pattern=pattern or "assertion failed",
            timeout=int(args.timeout),
        )
        return 0 if success else 1

    out = Path(args.output).resolve() if args.output else None
    try:
        res_path = reduce_delta_lines(
            binary=binary,
            testcase=testcase,
            target_signature=target_sig,
            target_message=pattern,
            output_path=out,
            timeout=args.timeout,
            attempts=args.attempts,
        )
    except ValueError as exc:
        print(f"Error: {exc}")
        return 1
    print(f"Reduced reproducer written to: {res_path}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    binary = Path(args.binary)
    testcase = Path(args.testcase)

    print(f"Running {testcase} on {binary}...")
    res = run_single_test(binary=binary, testcase=testcase, timeout=args.timeout)
    draft = make_github_issue_draft(res, servo_version=get_servo_version(binary))
    print("\n" + "=" * 60)
    print(draft)
    print("=" * 60)

    if args.output:
        Path(args.output).write_text(draft + "\n", encoding="utf-8")
        print(f"Draft report saved to {args.output}")
    return 0


def cmd_add_signature(args: argparse.Namespace) -> int:
    sig_path = Path(args.signatures)
    sigs = load_signatures(sig_path) if sig_path.exists() else []

    entry: dict[str, Any] = {}
    if args.from_draft:
        draft_path = Path(args.from_draft)
        if not draft_path.exists():
            print(f"Error: Draft file not found: {draft_path}")
            return 1
        entry = json.loads(draft_path.read_text(encoding="utf-8"))
        if args.issue:
            entry["issue"] = args.issue
        if args.id:
            entry["id"] = args.id
    else:
        if not (args.id and args.message and args.source_file and args.issue):
            print("Error: Missing required fields. Provide --from-draft or (--id, --message, --source-file, --issue)")
            return 1
        entry = {
            "id": args.id,
            "message": args.message,
            "source_file": args.source_file,
            "stack_frames": args.stack_frame or ["main"],
            "issue": args.issue,
        }

    if any(s["id"] == entry["id"] for s in sigs):
        print(f"Error: Signature ID '{entry['id']}' already exists.")
        return 1

    sigs.append(entry)
    save_signatures(sig_path, sigs)
    print(f"Added signature '{entry['id']}' to {sig_path}")
    return 0


def cmd_sync_issues(args: argparse.Namespace) -> int:
    added, existing = sync_signatures_from_github(
        signatures_path=Path(args.signatures),
        repo=args.repo,
        token=args.token,
        dry_run=args.dry_run,
    )
    print(f"\nSummary (open): {added} added, {existing} already present.")
    if getattr(args, "include_closed", False):
        hist_added, hist_existing = sync_closed_history_from_github(
            archive_path=Path(args.history),
            repo=args.repo,
            token=args.token,
            dry_run=args.dry_run,
        )
        print(f"Summary (closed history): {hist_added} added, {hist_existing} already present.")
    return 0


def cmd_sync_drafts(args: argparse.Namespace) -> int:
    added = sync_draft_signatures(
        triage_dir=Path(args.triage_dir),
        signatures_path=Path(args.signatures),
        corpus_out_dir=Path(args.corpus) if args.corpus else None,
        reduce_binary=args.reduce_binary if getattr(args, "reduce_binary", None) else None,
        reduce_timeout=args.reduce_timeout if getattr(args, "reduce_timeout", None) else 2.0,
        lookup_github=getattr(args, "lookup_github", False),
    )
    print(f"Added {len(added)} unique panic signature(s) to {args.signatures}")
    return 0


def cmd_prune_closed(args: argparse.Namespace) -> int:
    pruned = check_and_prune_closed_issues(
        signatures_path=Path(args.signatures),
        archive_path=Path(args.archive),
        token=args.token,
        dry_run=args.dry_run,
    )
    print(f"\nSummary: {len(pruned)} closed/fixed issues pruned from active panic signatures.")
    return 0


def cmd_bundle(args: argparse.Namespace) -> int:
    if args.action == "pack":
        binary = Path(args.binary) if args.binary else None
        pack_bundle(
            source_dir=Path(args.source),
            output_tar=Path(args.output),
            max_tests=args.max_tests,
            filter_timeouts_with_binary=binary,
            timeout=args.timeout,
            from_triage=Path(args.from_triage) if getattr(args, "from_triage", None) else None,
            jobs=getattr(args, "jobs", 4),
        )
    elif args.action == "unpack":
        unpack_bundle(
            bundle_tar=Path(args.bundle),
            target_dir=Path(args.target),
            clean_target=args.clean,
        )
    elif args.action == "download":
        download_bundle(
            url=args.url,
            output_path=Path(args.output),
            token=args.token,
        )
    return 0


def cmd_validate_signatures(args: argparse.Namespace) -> int:
    sig_path = Path(args.signatures)
    try:
        sigs = load_signatures(sig_path)
        print(f"Signatures database {sig_path} is valid.")
        print(f"Total signatures: {len(sigs)}")
        for s in sigs:
            print(f"  * {s['id']}: {s['message'][:50]}... ({s['source_file']})")
        return 0
    except Exception as exc:
        print(f"Validation failed for {sig_path}: {exc}")
        return 1


def cmd_import(args: argparse.Namespace) -> int:
    from .importer import import_crashes

    result = import_crashes(
        source=Path(args.source),
        binary=Path(args.binary),
        signatures_path=Path(args.signatures),
        corpus_dir=Path(args.corpus) if args.corpus else None,
        output_dir=Path(args.output) if args.output else None,
        timeout=args.timeout,
        concurrency=args.jobs,
        max_tests=args.max_tests,
        reduce=not getattr(args, "no_reduce", False),
        reduce_timeout=args.reduce_timeout,
        lookup_github=getattr(args, "lookup_github", False),
    )
    if result.get("fatal_error"):
        return 2
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="servo-fuzzing",
        description="Servo Fuzzing Command Line Interface",
    )
    subparsers = parser.add_subparsers(dest="subcommand", help="Available subcommands")

    p_run = subparsers.add_parser("run", help="Run fuzzing test suite")
    p_run.add_argument("--binary", required=True, help="Path to servoshell binary")
    p_run.add_argument("--config", default=None, help="Path to config file")
    p_run.add_argument("--signatures", default="signatures/rust-panic.json", help="Path to signatures file")
    p_run.add_argument("--output", default="target/fuzzing", help="Artifacts directory")
    p_run.add_argument("--testcases", nargs="*", default=None, help="Direct testcase directories or HTML files to replay")
    p_run.add_argument("--bundle", default=None, help="Path or URL to testcase bundle (.tar.gz)")
    p_run.add_argument("--force-download", action="store_true", help="Force re-downloading bundle")
    p_run.add_argument("-j", "--jobs", type=int, default=None, help="Parallel worker threads")
    p_run.add_argument("--timeout", type=float, default=None, help="Per-test timeout in seconds")
    p_run.add_argument("--generate", action="store_true", help="Generate fresh tests before running")
    p_run.add_argument("--generate-count", type=int, default=100, help="Test cases per tool if --generate is enabled")
    p_run.add_argument("--dry-run", action="store_true", help="Validate config without running tests")
    p_run.set_defaults(func=cmd_run)

    p_setup = subparsers.add_parser("setup-generators", help="Clone or check DOM fuzzing generators")
    p_setup.add_argument("--check", action="store_true", help="Check status without cloning")
    p_setup.set_defaults(func=cmd_setup_generators)

    p_gen = subparsers.add_parser("generate", help="Generate test cases")
    p_gen.add_argument("--tool", choices=["domato", "freedom", "minerva"], help="Specific generator tool")
    p_gen.add_argument("-n", "--count", type=int, default=100, help="Test cases per tool (default: 100)")
    p_gen.add_argument("-o", "--output", default="testcases", help="Output directory")
    p_gen.add_argument("--clone", action="store_true", help="Clone generator repos if not present")
    p_gen.add_argument("--status", action="store_true", help="Check installation status of generators")
    p_gen.set_defaults(func=cmd_generate)

    p_repro = subparsers.add_parser("reproduce", help="Reproduce crashes and estimate flakiness")
    p_repro.add_argument("testcase", help="Path to testcase HTML")
    p_repro.add_argument("--binary", required=True, help="Path to servoshell binary")
    p_repro.add_argument("--pattern", help="Regexp pattern to match")
    p_repro.add_argument("--mode", choices=["estimate", "repeat", "parallel"], default="estimate")
    p_repro.add_argument("--count", type=int, default=10, help="Count for repeat or parallel mode")
    p_repro.add_argument("--repeat", type=int, default=10, help="Number of repetition rounds")
    p_repro.add_argument("--parallel", type=int, default=20, help="Parallel instances per round")
    p_repro.add_argument("--timeout", type=float, default=2.0, help="Instance timeout in seconds")
    p_repro.set_defaults(func=cmd_reproduce)

    p_reduce = subparsers.add_parser("reduce", help="Minimize a crashing testcase")
    p_reduce.add_argument("testcase", help="Path to testcase HTML")
    p_reduce.add_argument("--binary", required=True, help="Path to servoshell binary")
    p_reduce.add_argument("--pattern", default=None, help="Target panic message or pattern")
    p_reduce.add_argument("--signature", help="Path to signature JSON to preserve specific crash")
    p_reduce.add_argument("--method", choices=["delta", "lithium"], default="delta", help="Reduction algorithm")
    p_reduce.add_argument("-o", "--output", help="Output path for reduced testcase")
    p_reduce.add_argument("--timeout", type=float, default=3.0, help="Per-run timeout in seconds")
    p_reduce.add_argument("--attempts", type=int, default=1, help="Runs per candidate for intermittent panics")
    p_reduce.set_defaults(func=cmd_reduce)

    p_rep = subparsers.add_parser("report", help="Generate GitHub issue draft for a testcase")
    p_rep.add_argument("testcase", help="Path to testcase HTML")
    p_rep.add_argument("--binary", required=True, help="Path to servoshell binary")
    p_rep.add_argument("-o", "--output", help="Save report to file")
    p_rep.add_argument("--timeout", type=float, default=5.0, help="Timeout in seconds")
    p_rep.set_defaults(func=cmd_report)

    p_add = subparsers.add_parser("add-signature", help="Add a signature to rust-panic.json")
    p_add.add_argument("--signatures", default="signatures/rust-panic.json", help="Path to signatures file")
    p_add.add_argument("--from-draft", help="Path to signature_draft.json")
    p_add.add_argument("--issue", help="GitHub issue URL")
    p_add.add_argument("--id", help="Signature unique ID")
    p_add.add_argument("--message", help="Panic message")
    p_add.add_argument("--source-file", help="Source file path")
    p_add.add_argument("--stack-frame", action="append", help="Stack backtrace frame")
    p_add.set_defaults(func=cmd_add_signature)

    p_sync = subparsers.add_parser("sync-issues", help="Sync B-fuzzing issues from GitHub")
    p_sync.add_argument("--signatures", default="signatures/rust-panic.json", help="Path to rust-panic.json")
    p_sync.add_argument("--history", default="signatures/fixed-history.json", help="Path to fixed-history.json")
    p_sync.add_argument("--include-closed", action="store_true", help="Also sync closed B-fuzzing issues into fixed-history.json")
    p_sync.add_argument("--repo", default="servo/servo", help="Target GitHub repo")
    p_sync.add_argument("--token", help="GitHub Personal Access Token")
    p_sync.add_argument("--dry-run", action="store_true", help="Print changes without modifying files")
    p_sync.set_defaults(func=cmd_sync_issues)

    p_prune = subparsers.add_parser("prune-closed", help="Prune fixed/closed issues from rust-panic.json")
    p_prune.add_argument("--signatures", default="signatures/rust-panic.json", help="Path to signatures file")
    p_prune.add_argument("--archive", default="signatures/fixed-history.json", help="Path to fixed archive")
    p_prune.add_argument("--token", help="GitHub Personal Access Token")
    p_prune.add_argument("--dry-run", action="store_true", help="Print changes without modifying files")
    p_prune.set_defaults(func=cmd_prune_closed)

    p_sync_drafts = subparsers.add_parser("sync-drafts", help="Deduplicate and append new draft signatures from triage")
    p_sync_drafts.add_argument("--triage-dir", required=True, help="Path to triage directory (e.g. target/triage)")
    p_sync_drafts.add_argument("--signatures", default="signatures/rust-panic.json", help="Path to signatures file")
    p_sync_drafts.add_argument("--corpus", default=None, help="Optional directory to save deduplicated reproducer HTMLs (e.g. testcases/corpus)")
    p_sync_drafts.add_argument("--reduce-binary", default=None, help="Optional binary to auto-reduce reproducers")
    p_sync_drafts.add_argument("--reduce-timeout", type=float, default=2.0, help="Per-run timeout during reduction")
    p_sync_drafts.add_argument("--lookup-github", action="store_true", help="Search servo/servo open and closed issues to link existing issues")
    p_sync_drafts.set_defaults(func=cmd_sync_drafts)

    p_bundle = subparsers.add_parser("bundle", help="Pack or unpack testcase bundles")
    p_bundle_sub = p_bundle.add_subparsers(dest="action", help="Bundle action (pack, unpack, download)")

    p_b_pack = p_bundle_sub.add_parser("pack", help="Pack directory into bundle tarball")
    p_b_pack.add_argument("--source", default="testcases", help="Source directory containing HTML testcases")
    p_b_pack.add_argument("--output", default="target/fuzz-bundle-latest.tar.gz", help="Output tarball path")
    p_b_pack.add_argument("--max-tests", type=int, default=300, help="Maximum tests to bundle")
    p_b_pack.add_argument("--binary", help="Binary to filter out hanging/timing-out cases")
    p_b_pack.add_argument("--timeout", type=float, default=2.0, help="Timeout threshold to discard hanging tests")
    p_b_pack.add_argument("--from-triage", help="Triage directory to reuse existing results")
    p_b_pack.add_argument("--jobs", "-j", type=int, default=4, help="Concurrency for filtering timeouts")

    p_b_unpack = p_bundle_sub.add_parser("unpack", help="Unpack bundle tarball into directory")
    p_b_unpack.add_argument("--bundle", required=True, help="Path to .tar.gz bundle")
    p_b_unpack.add_argument("--target", default="testcases/bundle", help="Target directory")
    p_b_unpack.add_argument("--clean", action="store_true", help="Clean target directory before unpacking")

    p_b_dl = p_bundle_sub.add_parser("download", help="Download bundle tarball from URL")
    p_b_dl.add_argument("--url", required=True, help="URL to download bundle from")
    p_b_dl.add_argument("--output", default="target/fuzz-bundle-latest.tar.gz", help="Output path")
    p_b_dl.add_argument("--token", help="GitHub Personal Access Token")

    p_bundle.set_defaults(func=cmd_bundle)

    p_val = subparsers.add_parser("validate-signatures", help="Validate rust-panic.json schema")
    p_val.add_argument("--signatures", default="signatures/rust-panic.json", help="Path to signatures file")
    p_val.set_defaults(func=cmd_validate_signatures)

    p_import = subparsers.add_parser("import", help="Import, triage, deduplicate, and minimize raw fuzzer crashes")
    p_import.add_argument("source", help="Directory or archive (.tar.gz, .zip) containing crash suspects")
    p_import.add_argument("--binary", default="./servo/servoshell", help="Path to servoshell binary (default: ./servo/servoshell)")
    p_import.add_argument("--signatures", default="signatures/rust-panic.json", help="Path to signatures file")
    p_import.add_argument("--corpus", default="testcases/corpus", help="Output directory for deduplicated minimized reproducers")
    p_import.add_argument("--output", default="target/triage", help="Triage output directory")
    p_import.add_argument("-j", "--jobs", type=int, default=4, help="Parallel worker threads")
    p_import.add_argument("--timeout", type=float, default=2.0, help="Per-test timeout in seconds")
    p_import.add_argument("--max-tests", type=int, default=None, help="Maximum number of testcases to process")
    p_import.add_argument("--reduce", action="store_true", default=True, help="Auto-minimize unique crashing reproducers (default: True)")
    p_import.add_argument("--no-reduce", action="store_true", help="Disable reproducer minimization")
    p_import.add_argument("--reduce-timeout", type=float, default=2.0, help="Per-run timeout during minimization")
    p_import.add_argument("--lookup-github", action="store_true", help="Search servo/servo open and closed issues to deduplicate and detect regressions")
    p_import.set_defaults(func=cmd_import)

    parsed = parser.parse_args(argv)
    if not hasattr(parsed, "func") or not parsed.func:
        parser.print_help()
        return 1

    return parsed.func(parsed)


if __name__ == "__main__":
    sys.exit(main())
