from __future__ import annotations

import argparse
import os
import shutil
import tarfile
import zipfile
from pathlib import Path
from typing import Any, Optional

from .executor import TestResult, TestStatus, run_single_test, run_suite
from .reporter import write_artifacts
from .runner import get_servo_version
from .signatures import CrashSignature, load_signatures, sync_draft_signatures


ARCHIVE_EXTENSIONS = (
    ".tar.gz",
    ".tgz",
    ".tar.bz2",
    ".tbz2",
    ".tar.xz",
    ".txz",
    ".tar",
    ".zip",
)

HTML_EXTENSIONS = (".html", ".htm", ".xhtml")


def is_archive(path: Path) -> bool:
    name = path.name.lower()
    return any(name.endswith(ext) for ext in ARCHIVE_EXTENSIONS)


def unpack_archive_if_needed(source_path: Path, unpack_dir: Path) -> list[Path]:
    source_path = source_path.resolve()
    if not source_path.exists():
        return []

    unpack_dir = unpack_dir.resolve()
    unpack_dir.mkdir(parents=True, exist_ok=True)

    search_dirs: list[Path] = []

    if source_path.is_file():
        name = source_path.name.lower()
        if any(name.endswith(ext) for ext in (".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz", ".tar")):
            print(f"Unpacking tar archive {source_path.name} into {unpack_dir}...")
            with tarfile.open(source_path, "r:*") as tar:
                tar.extractall(unpack_dir)
            search_dirs.append(unpack_dir)
        elif name.endswith(".zip"):
            print(f"Unpacking zip archive {source_path.name} into {unpack_dir}...")
            with zipfile.ZipFile(source_path, "r") as z:
                z.extractall(unpack_dir)
            search_dirs.append(unpack_dir)
        else:
            search_dirs.append(source_path)
    elif source_path.is_dir():
        found_archives: list[Path] = []
        for cand in source_path.iterdir():
            if cand.is_file() and is_archive(cand):
                found_archives.append(cand)

        if found_archives:
            for arch in found_archives:
                arch_target = unpack_dir / arch.stem
                arch_target.mkdir(parents=True, exist_ok=True)
                print(f"Unpacking archive {arch.name} into {arch_target}...")
                if arch.name.lower().endswith(".zip"):
                    with zipfile.ZipFile(arch, "r") as z:
                        z.extractall(arch_target)
                else:
                    with tarfile.open(arch, "r:*") as tar:
                        tar.extractall(arch_target)
                search_dirs.append(arch_target)
        search_dirs.append(source_path)

    return search_dirs


def find_import_testcases(search_paths: list[Path], max_tests: Optional[int] = None) -> list[Path]:
    found: list[Path] = []
    seen: set[Path] = set()

    for p in search_paths:
        p_res = p.resolve()
        if not p_res.exists():
            continue

        if p_res.is_file():
            lower = p_res.name.lower()
            if any(lower.endswith(ext) for ext in HTML_EXTENSIONS) and p_res not in seen:
                seen.add(p_res)
                found.append(p_res)
            continue

        for file_path in p_res.rglob("*"):
            if not file_path.is_file():
                continue
            lower = file_path.name.lower()
            if any(lower.endswith(ext) for ext in HTML_EXTENSIONS):
                resolved = file_path.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    found.append(resolved)

    found.sort()
    if max_tests is not None and max_tests > 0:
        found = found[:max_tests]
    return found


def import_crashes(
    source: Path,
    binary: Path,
    signatures_path: Path,
    corpus_dir: Optional[Path] = None,
    output_dir: Optional[Path] = None,
    timeout: float = 2.0,
    concurrency: int = 4,
    max_tests: Optional[int] = None,
    reduce: bool = True,
    reduce_timeout: float = 2.0,
) -> dict[str, Any]:
    binary_path = Path(binary).resolve()
    if not binary_path.exists() or not os.access(binary_path, os.X_OK):
        print(f"Error: servoshell binary not found or not executable: {binary_path}")
        return {"fatal_error": True, "error": f"Binary not found: {binary_path}"}

    out_dir = (output_dir or Path("target/triage")).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # Clean existing new_crashes in triage directory
    shutil.rmtree(out_dir / "new_crashes", ignore_errors=True)

    unpack_dir = out_dir / "unpacked_raw"
    search_paths = unpack_archive_if_needed(source, unpack_dir)
    testcases = find_import_testcases(search_paths, max_tests=max_tests)

    print(f"Scanning raw fuzzer suspect testcases from {source}...")
    print(f"Found {len(testcases)} candidate testcase(s) to triage.")

    if not testcases:
        print(f"Notice: No HTML testcases found in {source}.")
        return {
            "fatal_error": False,
            "total_scanned": 0,
            "passed": 0,
            "timeouts": 0,
            "known_crashes": 0,
            "new_crashes": 0,
            "errors": 0,
            "signatures_added": 0,
            "added_signatures": [],
        }

    # Health check
    print(f"Performing health check: {binary_path} about:blank ...")
    health = run_single_test(
        binary=binary_path,
        testcase=Path("about:blank"),
        servo_args=["-xfz"],
        timeout=10.0,
    )
    if health.status in (TestStatus.NEW_CRASH, TestStatus.ERROR):
        print("FATAL: servoshell failed health check on about:blank!")
        print(health.combined_output)
        return {"fatal_error": True, "error": "Health check on about:blank failed"}
    print("Health check passed.")

    servo_version = get_servo_version(binary_path)
    sig_path = Path(signatures_path).resolve()
    signatures = load_signatures(sig_path) if sig_path.exists() else []
    print(f"Loaded {len(signatures)} known crash signatures.")
    print(f"Triaging {len(testcases)} tests with {concurrency} workers (timeout={timeout}s)...\n")

    def on_result(res: TestResult, current: int, total: int) -> None:
        prefix = f"[{current:4d}/{total:4d}]"
        name = res.testcase.name
        if res.status == TestStatus.PASS:
            print(f"{prefix} PASS        : {name} ({res.duration:.2f}s)")
        elif res.status == TestStatus.KNOWN_CRASH:
            print(f"{prefix} KNOWN CRASH: {name} -> {res.matched_signature_id} ({res.duration:.2f}s)")
        elif res.status == TestStatus.NEW_CRASH:
            msg = res.crash_info.message if res.crash_info else "Unexpected crash"
            print(f"{prefix} NEW CRASH  : {name} -> {msg} ({res.duration:.2f}s)")
        elif res.status == TestStatus.TIMEOUT:
            print(f"{prefix} TIMEOUT    : {name} (> {timeout}s)")
        else:
            print(f"{prefix} ERROR      : {name} ({res.error_message})")

    summary = run_suite(
        binary=binary_path,
        testcases=testcases,
        servo_args=["-xfz", "--enable-experimental-web-platform-features"],
        timeout=timeout,
        concurrency=concurrency,
        signatures=signatures,
        on_result=on_result,
    )

    write_artifacts(summary, output_dir=out_dir, servo_version=servo_version)

    corpus_out = Path(corpus_dir).resolve() if corpus_dir else None
    added_sigs: list[CrashSignature] = []

    if summary.new_crashes > 0:
        print(f"\nDeduplicating {summary.new_crashes} new crash reproducer(s)...")
        added_sigs = sync_draft_signatures(
            triage_dir=out_dir,
            signatures_path=sig_path,
            corpus_out_dir=corpus_out,
            reduce_binary=binary_path if reduce else None,
            reduce_timeout=reduce_timeout,
        )

    print("\n" + "=" * 72)
    print("                 CRASH IMPORT & TRIAGE SUMMARY")
    print("=" * 72)
    print(f"Total Suspects Scanned : {len(testcases)}")
    print(f"Benign / Passed        : {summary.passed} (discarded)")
    print(f"Timeouts / Hangs       : {summary.timeouts} (discarded)")
    print(f"Known Crashes          : {summary.known_crashes} (already tracked)")
    print(f"Execution Errors       : {summary.errors}")
    print(f"New Crashes Triaged    : {summary.new_crashes}")
    print(f"Unique Panics Added    : {len(added_sigs)} (after deduplication)")
    if added_sigs:
        print("------------------------------------------------------------------------")
        for s in added_sigs:
            sig_name = s["id"]
            print(f"  * {sig_name}: {s['message'][:50]}... ({s['source_file']})")
            if corpus_out:
                corpus_file = corpus_out / f"{sig_name}.html"
                print(f"    Corpus file: {corpus_file}")
    print("=" * 72 + "\n")

    return {
        "fatal_error": False,
        "total_scanned": len(testcases),
        "passed": summary.passed,
        "timeouts": summary.timeouts,
        "known_crashes": summary.known_crashes,
        "new_crashes": summary.new_crashes,
        "errors": summary.errors,
        "signatures_added": len(added_sigs),
        "added_signatures": added_sigs,
    }
