from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any, Optional

from .bisect import DEFAULT_ASSET, DEFAULT_NIGHTLY_REPO, format_bisect_report, run_bisect
from .bundle import DEFAULT_MAX_TIMEOUT_TESTS, download_bundle, get_default_bundle_repo, pack_bundle, unpack_bundle
from .evidence import ANALYSIS_FILE, Evidence, gather_evidence, render_issue, write_evidence_pack
from .generators import GeneratorManager
from .github_sync import (
    sync_signatures_from_github,
)
from .proposals import Proposal, existing_proposal, propose_panics
from .reducer import reduce_delta_lines, reduce_with_lithium
from .reporter import make_signature_draft
from .reproduce import estimate_reproducibility, parallelize_test, repeat_test
from .runner import main as runner_main
from .signatures import (
    default_pending_dir,
    load_pending_signatures,
    load_signatures,
    save_signatures,
    sync_draft_signatures,
)


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


def cmd_bisect(args: argparse.Namespace) -> int:
    testcase = Path(args.testcase).resolve()
    if not testcase.exists():
        print(f"Error: Testcase not found: {testcase}")
        return 1
    try:
        result, commits = run_bisect(
            testcase=testcase,
            pattern=args.pattern,
            good=args.good,
            bad=args.bad,
            attempts=args.attempts,
            timeout=args.timeout,
            servo_args=shlex.split(args.servo_args),
            cache_dir=Path(args.cache_dir),
            repo=args.repo,
            asset_name=args.asset,
            token=args.token,
            keep_builds=args.keep_builds,
        )
    except ValueError as exc:
        print(f"Error: {exc}")
        return 1
    report = format_bisect_report(result, testcase, commits)
    print("\n" + report)
    if args.output:
        Path(args.output).write_text(report, encoding="utf-8")
        print(f"Bisect report saved to {args.output}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    binary = Path(args.binary)
    testcase = Path(args.testcase).resolve()
    if not testcase.exists():
        print(f"Error: Testcase not found: {testcase}")
        return 1
    evidence = gather_evidence(
        binary=binary,
        testcase=testcase,
        servo_args=shlex.split(args.servo_args),
        attempts=args.attempts,
        timeout=args.timeout,
        original=Path(args.original).resolve() if args.original else None,
        bisect=args.bisect,
        good=args.good,
        bad=args.bad,
        cache_dir=Path(args.cache_dir),
        token=args.token,
    )
    if evidence is None:
        print(f"Error: no crash in {args.attempts} run(s); nothing to report.")
        return 1
    crash = evidence.crash

    if args.pack:
        issue_path = write_evidence_pack(evidence, Path(args.pack), include_analysis=args.include_analysis)
        draft = issue_path.read_text(encoding="utf-8")
    else:
        draft = render_issue(evidence)
    print("\n" + "=" * 60)
    print(draft)
    print("=" * 60)
    if crash:
        print("Signature draft for signatures/rust-panic.json:")
        print(json.dumps(make_signature_draft(crash), indent=2))

    if args.output:
        Path(args.output).write_text(draft, encoding="utf-8")
        print(f"Draft report saved to {args.output}")
    if args.pack:
        print(f"Evidence pack written to {Path(args.pack).resolve()} (analysis goes in {ANALYSIS_FILE})")
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
    result = sync_signatures_from_github(
        signatures_path=Path(args.signatures),
        history_path=Path(args.history),
        repo=args.repo,
        token=args.token,
        dry_run=args.dry_run,
        pending_dir=Path(args.pending) if args.pending else default_pending_dir(Path(args.signatures)),
        reports_dir=Path(args.reports),
    )
    summary = result.to_markdown(args.repo)
    print("\n" + summary)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as summary_file:
            summary_file.write(summary)
    return 0


def cmd_sync_drafts(args: argparse.Namespace) -> int:
    added = sync_draft_signatures(
        triage_dir=Path(args.triage_dir),
        signatures_path=Path(args.signatures),
        corpus_out_dir=Path(args.corpus) if args.corpus else None,
        drafts_path=Path(args.drafts) if args.drafts else None,
        reduce_binary=args.reduce_binary if getattr(args, "reduce_binary", None) else None,
        reduce_timeout=args.reduce_timeout if getattr(args, "reduce_timeout", None) else 2.0,
        lookup_github=getattr(args, "lookup_github", False),
    )
    print(f"Found {len(added)} unique new panic signature(s) not in {args.signatures}")
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
            include_timeouts=args.timeouts == "include",
            max_timeout_tests=args.max_timeout_tests,
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


def cmd_propose(args: argparse.Namespace) -> int:
    binary = Path(args.binary).resolve()
    repo = args.repo or os.environ.get("GITHUB_REPOSITORY") or get_default_bundle_repo()

    def find_existing(signature_id: str) -> Optional[Proposal]:
        try:
            return existing_proposal(repo, signature_id, args.token)
        except Exception as exc:
            print(f"Warning: could not look up pull requests for {signature_id} in {repo}: {exc}")
            return None

    def gather(testcase: Path) -> Optional[Evidence]:
        return gather_evidence(
            binary=binary,
            testcase=testcase,
            servo_args=shlex.split(args.servo_args),
            attempts=args.attempts,
            timeout=args.timeout,
            bisect=args.bisect,
            cache_dir=Path(args.cache_dir),
            token=args.token,
        )

    proposed = propose_panics(
        drafts_path=Path(args.drafts),
        corpus_dir=Path(args.corpus),
        out_dir=Path(args.output),
        gather=gather,
        repo=repo,
        find_existing=find_existing if args.skip_proposed else None,
        limit=args.max_panics,
    )
    print(f"Proposed {len(proposed)} new panic(s) under {args.output}")
    return 0


def cmd_validate_signatures(args: argparse.Namespace) -> int:
    sig_path = Path(args.signatures)
    try:
        sigs = load_pending_signatures(sig_path) if sig_path.is_dir() else load_signatures(sig_path)
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
        drafts_path=Path(args.drafts) if args.drafts else None,
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
    p_gen.add_argument("-o", "--output", default="target/generated", help="Output directory")
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

    p_bisect = subparsers.add_parser("bisect", help="Bisect a crash across Servo nightly builds")
    p_bisect.add_argument("testcase", help="Path to testcase HTML")
    p_bisect.add_argument("--pattern", default=None, help="Regexp the output must match (default: any panic)")
    p_bisect.add_argument("--good", help="Known good nightly tag (YYYY-MM-DD); searched backwards if omitted")
    p_bisect.add_argument("--bad", help="Known bad nightly tag (YYYY-MM-DD); defaults to the latest nightly")
    p_bisect.add_argument("--attempts", type=int, default=3, help="Runs per nightly; any match counts as bad")
    p_bisect.add_argument("--timeout", type=float, default=10.0, help="Per-run timeout in seconds")
    p_bisect.add_argument("--servo-args", default="-xfz", help="Arguments passed to servoshell")
    p_bisect.add_argument("--cache-dir", default="target/bisect", help="Directory for downloaded nightlies")
    p_bisect.add_argument("--repo", default=DEFAULT_NIGHTLY_REPO, help="GitHub repo hosting nightly releases")
    p_bisect.add_argument("--asset", default=DEFAULT_ASSET, help="Release asset name for this platform")
    p_bisect.add_argument("--token", help="GitHub token (defaults to GITHUB_TOKEN / GH_TOKEN)")
    p_bisect.add_argument("--keep-builds", action="store_true", help="Keep extracted nightlies after testing")
    p_bisect.add_argument("-o", "--output", help="Save bisect report to file")
    p_bisect.set_defaults(func=cmd_bisect)

    p_rep = subparsers.add_parser("report", help="Generate GitHub issue draft for a testcase")
    p_rep.add_argument("testcase", help="Path to testcase HTML")
    p_rep.add_argument("--binary", required=True, help="Path to servoshell binary")
    p_rep.add_argument("-o", "--output", help="Save report to file")
    p_rep.add_argument("--timeout", type=float, default=5.0, help="Timeout in seconds")
    p_rep.add_argument("--attempts", type=int, default=3, help="Runs used for the reproducibility rate")
    p_rep.add_argument("--servo-args", default="-xfz", help="Arguments passed to servoshell")
    p_rep.add_argument(
        "--pack",
        metavar="DIR",
        help="Write an evidence pack: issue.md for people, context.json and AGENT.md for agents, analysis.md for both",
    )
    p_rep.add_argument("--original", help="Unreduced testcase to keep in the pack")
    p_rep.add_argument(
        "--include-analysis",
        action="store_true",
        help="Append the filled-in sections of the pack's analysis.md to issue.md",
    )
    p_rep.add_argument("--bisect", action="store_true", help="Bisect nightlies and add the regression range")
    p_rep.add_argument("--good", help="With --bisect: nightly tag known to be good")
    p_rep.add_argument("--bad", help="With --bisect: nightly tag known to be bad")
    p_rep.add_argument("--cache-dir", default="target/bisect", help="With --bisect: where nightlies are downloaded")
    p_rep.add_argument("--token", help="GitHub token (defaults to GITHUB_TOKEN / GH_TOKEN)")
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

    p_sync = subparsers.add_parser("sync-issues", help="Regenerate both signature files from upstream B-fuzzing issues")
    p_sync.add_argument("--signatures", default="signatures/rust-panic.json", help="Output for open issues")
    p_sync.add_argument("--history", default="signatures/fixed-history.json", help="Output for closed issues")
    p_sync.add_argument("--pending", help="Pending signatures to drop once filed upstream (default: pending/ beside --signatures)")
    p_sync.add_argument("--reports", default="reports", help="Reports of the pending signatures, dropped with them")
    p_sync.add_argument("--summary", help="Append a Markdown summary to this file (e.g. $GITHUB_STEP_SUMMARY)")
    p_sync.add_argument("--repo", default="servo/servo", help="Target GitHub repo")
    p_sync.add_argument("--token", help="GitHub Personal Access Token")
    p_sync.add_argument("--dry-run", action="store_true", help="Print changes without modifying files")
    p_sync.set_defaults(func=cmd_sync_issues)

    p_sync_drafts = subparsers.add_parser("sync-drafts", help="Deduplicate and append new draft signatures from triage")
    p_sync_drafts.add_argument("--triage-dir", required=True, help="Path to triage directory (e.g. target/triage)")
    p_sync_drafts.add_argument("--signatures", default="signatures/rust-panic.json", help="Path to signatures file")
    p_sync_drafts.add_argument("--drafts", default=None, help="Output file for the draft signatures (default: <triage-dir>/draft-signatures.json)")
    p_sync_drafts.add_argument("--corpus", default=None, help="Optional directory to save deduplicated reproducer HTMLs (e.g. target/panic-corpus)")
    p_sync_drafts.add_argument("--reduce-binary", default=None, help="Optional binary to auto-reduce reproducers")
    p_sync_drafts.add_argument("--reduce-timeout", type=float, default=2.0, help="Per-run timeout during reduction")
    p_sync_drafts.add_argument("--lookup-github", action="store_true", help="Search servo/servo open and closed issues to link existing issues")
    p_sync_drafts.set_defaults(func=cmd_sync_drafts)

    p_bundle = subparsers.add_parser("bundle", help="Pack or unpack testcase bundles")
    p_bundle_sub = p_bundle.add_subparsers(dest="action", help="Bundle action (pack, unpack, download)")

    p_b_pack = p_bundle_sub.add_parser("pack", help="Pack directory into bundle tarball")
    p_b_pack.add_argument("--source", default="target/generated", help="Source directory containing HTML testcases")
    p_b_pack.add_argument("--output", default="target/fuzz-bundle-latest.tar.gz", help="Output tarball path")
    p_b_pack.add_argument("--max-tests", type=int, default=300, help="Maximum tests to bundle")
    p_b_pack.add_argument("--binary", help="Binary to classify passing/timing-out cases")
    p_b_pack.add_argument("--timeout", type=float, default=2.0, help="Timeout threshold that marks a test as hanging")
    p_b_pack.add_argument(
        "--timeouts",
        choices=("exclude", "include"),
        default="exclude",
        help="Whether testcases that time out go into the bundle (default: exclude)",
    )
    p_b_pack.add_argument(
        "--max-timeout-tests",
        type=int,
        default=DEFAULT_MAX_TIMEOUT_TESTS,
        help="With --timeouts include: maximum timed-out testcases to bundle",
    )
    p_b_pack.add_argument("--from-triage", help="Triage directory to reuse existing results")
    p_b_pack.add_argument("--jobs", "-j", type=int, default=4, help="Concurrency for filtering timeouts")

    p_b_unpack = p_bundle_sub.add_parser("unpack", help="Unpack bundle tarball into directory")
    p_b_unpack.add_argument("--bundle", required=True, help="Path to .tar.gz bundle")
    p_b_unpack.add_argument("--target", default="target/bundle", help="Target directory")
    p_b_unpack.add_argument("--clean", action="store_true", help="Clean target directory before unpacking")

    p_b_dl = p_bundle_sub.add_parser("download", help="Download bundle tarball from URL")
    p_b_dl.add_argument("--url", required=True, help="URL to download bundle from")
    p_b_dl.add_argument("--output", default="target/fuzz-bundle-latest.tar.gz", help="Output path")
    p_b_dl.add_argument("--token", help="GitHub Personal Access Token")

    p_bundle.set_defaults(func=cmd_bundle)

    p_prop = subparsers.add_parser(
        "propose",
        help="Write a pending signature and a report for each new panic of a triage run, ready to commit",
    )
    p_prop.add_argument("--drafts", default="target/triage/draft-signatures.json", help="Draft signatures of the run")
    p_prop.add_argument("--corpus", default="target/panic-corpus", help="Directory holding <id>.html reproducers")
    p_prop.add_argument("--binary", default="./servo/servoshell", help="Path to servoshell binary")
    p_prop.add_argument("--output", default="target/proposals", help="Receives signatures/pending/ and reports/")
    p_prop.add_argument("--repo", help="Repository the reports are committed to (default: GITHUB_REPOSITORY)")
    p_prop.add_argument("--skip-proposed", action="store_true", help="Skip panics that already have a pull request from new-panic/<id>")
    p_prop.add_argument("--max-panics", type=int, default=5, help="Maximum panics to report per run")
    p_prop.add_argument("--attempts", type=int, default=3, help="Runs used for the reproducibility rate")
    p_prop.add_argument("--timeout", type=float, default=5.0, help="Per-run timeout in seconds")
    p_prop.add_argument(
        "--servo-args",
        default="-xfz --enable-experimental-web-platform-features",
        help="Arguments passed to servoshell",
    )
    p_prop.add_argument("--bisect", action="store_true", help="Bisect nightlies and add the regression range")
    p_prop.add_argument("--cache-dir", default="target/bisect", help="With --bisect: where nightlies are downloaded")
    p_prop.add_argument("--token", help="GitHub token (defaults to GITHUB_TOKEN / GH_TOKEN)")
    p_prop.set_defaults(func=cmd_propose)

    p_val = subparsers.add_parser("validate-signatures", help="Validate a signatures file or a pending directory")
    p_val.add_argument("--signatures", default="signatures/rust-panic.json", help="Signatures file or pending directory")
    p_val.set_defaults(func=cmd_validate_signatures)

    p_import = subparsers.add_parser("import", help="Import, triage, deduplicate, and minimize raw fuzzer crashes")
    p_import.add_argument("source", help="Directory or archive (.tar.gz, .zip) containing crash suspects")
    p_import.add_argument("--binary", default="./servo/servoshell", help="Path to servoshell binary (default: ./servo/servoshell)")
    p_import.add_argument("--signatures", default="signatures/rust-panic.json", help="Path to signatures file")
    p_import.add_argument("--corpus", default="target/panic-corpus", help="Output directory for deduplicated minimized reproducers")
    p_import.add_argument("--drafts", default=None, help="Output file for the draft signatures (default: <output>/draft-signatures.json)")
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
