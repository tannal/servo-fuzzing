"""Reporting and artifact generation for Servo fuzzing.

Produces formatted terminal output, JSON summaries, and GitHub Actions markdown reports.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Optional

from .executor import SuiteSummary, TestResult, TestStatus
from .parser import CrashInfo


def make_signature_draft(crash: CrashInfo, issue_url: str = "https://github.com/servo/servo/issues/TODO") -> dict[str, Any]:
    """Generate a draft signature entry for rust-panic.json."""
    # Create an identifier from the source file and message snippet
    file_stem = Path(crash.source_file).stem
    msg_words = "".join(c if c.isalnum() else "-" for c in crash.message.lower()).split("-")
    msg_clean = "-".join(w for w in msg_words[:4] if w)
    sig_id = f"{file_stem}-{msg_clean}" if msg_clean else file_stem

    # Pick up to 3 meaningful frames
    frames = []
    for f in crash.stack_frames:
        if len(frames) >= 3:
            break
        if "servo" in f.lower() or "script" in f.lower() or "layout" in f.lower() or "style" in f.lower() or "net" in f.lower():
            frames.append(f)
    if not frames and crash.stack_frames:
        frames = crash.stack_frames[:2]

    return {
        "id": sig_id,
        "message": crash.message,
        "source_file": crash.source_file,
        "stack_frames": frames if frames else ["<fill_in_distinctive_frame>"],
        "issue": issue_url,
    }


def make_github_issue_draft(result: TestResult, servo_version: str = "latest", cmd_args: str = "-xfz") -> str:
    """Format a draft GitHub issue report matching Servo conventions."""
    crash = result.crash_info
    title_suffix = ""
    if crash:
        loc = f"at {crash.source_file}"
        if crash.line:
            loc += f":{crash.line}"
        thread_info = f"thread {crash.thread}, " if crash.thread else ""
        title_suffix = f"panic: {crash.message} ({thread_info}{loc})"
    else:
        title_suffix = f"Crash in {result.testcase.name}"

    testcase_content = ""
    try:
        testcase_content = result.testcase.read_text(encoding="utf-8", errors="replace")
    except Exception:
        testcase_content = f"<!-- Failed to read {result.testcase} -->"

    report = [
        f"### Title: `{title_suffix}`",
        "",
        "**Labels**: `B-fuzzing`, `I-panic`, `C-has-manual-testcase`",
        "",
        "#### Minimal testcase:",
        "```html",
        testcase_content.strip(),
        "```",
        "",
        f"**Command**: `servoshell {cmd_args} {result.testcase.name}`",
        f"**Servo Version**: `{servo_version}`",
        "",
        "#### Backtrace / Output:",
        "```text",
        result.combined_output.strip() or "(no output captured)",
        "```",
        "",
        "#### Draft Signature for `signatures/rust-panic.json`:",
        "```json",
    ]

    if crash:
        draft_sig = make_signature_draft(crash)
        report.append(json.dumps(draft_sig, indent=2))
    else:
        report.append("// Non-panic crash or unexpected exit code")
    report.append("```")

    return "\n".join(report)


def print_cli_summary(summary: SuiteSummary) -> None:
    """Print an informative test report to the terminal."""
    print()
    print("=" * 72)
    print("                SERVO FUZZING SUITE SUMMARY")
    print("=" * 72)
    print(f"Total Tests Run   : {summary.total}")
    print(f"Passed            : {summary.passed}")
    print(f"Known Crashes     : {summary.known_crashes} (ignored existing issues)")
    print(f"Timeouts          : {summary.timeouts}")
    print(f"Errors            : {summary.errors}")
    print(f"New / Unknown     : {summary.new_crashes}")
    print(f"Execution Time    : {summary.duration:.2f} seconds")
    print("=" * 72)

    # Print known crashes
    if summary.known_crashes > 0:
        print("\n[Known Crashes (Passed / Ignored)]:")
        for r in summary.results:
            if r.status == TestStatus.KNOWN_CRASH:
                sig_text = f" [{r.matched_signature_id}]" if r.matched_signature_id else ""
                issue_text = f" -> {r.matched_issue}" if r.matched_issue else ""
                print(f"  • {r.testcase.name}{sig_text}{issue_text}")

    # Print new crashes
    if summary.new_crashes > 0:
        print("\n" + "!" * 72)
        print("  ❌ UNEXPECTED / NEW PANICS DETECTED!")
        print("  This blocks the PR / CI to prevent merging regressions into Servo.")
        print("!" * 72)
        print("\nPR Author / Triage Instructions:")
        print("  1. Review the failures below to check if they are caused by this PR.")
        print("  2. If caused by this PR: please resolve the panic before merging.")
        print("  3. If unrelated / pre-existing: open or label the issue 'B-fuzzing' on GitHub")
        print("     and add the draft signature shown below to signatures/rust-panic.json.\n")

        for r in summary.results:
            if r.status == TestStatus.NEW_CRASH:
                print(f"--- FAILED TEST: {r.testcase.name} ---")
                if r.crash_info:
                    print(f"  Panic: {r.crash_info.message}")
                    loc = r.crash_info.source_file
                    if r.crash_info.line:
                        loc += f":{r.crash_info.line}"
                    print(f"  Source: {loc}")
                    if r.crash_info.stack_frames:
                        print("  Frames:")
                        for frame in r.crash_info.stack_frames[:5]:
                            print(f"    - {frame}")

                    print(f"\n  👉 Reproduce in Servo: ./mach run -z {r.testcase}")
                    print(f"  👉 Debug with lldb:   ./mach run -z --debugger=lldb {r.testcase}")
                    print(f"  👉 Re-run this test:  ./mach test-fuzzing --dev {r.testcase}")

                    print("\n  Draft signature for signatures/rust-panic.json:")
                    draft = make_signature_draft(r.crash_info)
                    print(json.dumps(draft, indent=4))
                elif r.error_message:
                    print(f"  Error: {r.error_message}")
                print()
    else:
        print("\n  ✅ SUCCESS: All tests passed (or reproduced known issues).")
    print("=" * 72)


def write_artifacts(summary: SuiteSummary, output_dir: Path, servo_version: str = "latest") -> None:
    """Save test artifacts, JSON summary, and reports into output_dir."""
    output_dir.mkdir(parents=True, exist_ok=True)

    # 1. summary.json
    summary_data = {
        "total": summary.total,
        "passed": summary.passed,
        "known_crashes": summary.known_crashes,
        "new_crashes": summary.new_crashes,
        "timeouts": summary.timeouts,
        "errors": summary.errors,
        "duration": summary.duration,
        "success": summary.is_success,
        "results": [
            {
                "testcase": str(r.testcase),
                "status": r.status.value,
                "exit_code": r.exit_code,
                "duration": r.duration,
                "matched_signature": r.matched_signature_id,
                "issue": r.matched_issue,
                "panic_message": r.crash_info.message if r.crash_info else None,
                "source_file": r.crash_info.source_file if r.crash_info else None,
                "line": r.crash_info.line if r.crash_info else None,
            }
            for r in summary.results
        ],
    }
    (output_dir / "summary.json").write_text(json.dumps(summary_data, indent=2), encoding="utf-8")

    # 2. report.md (GitHub Actions step summary format)
    md_lines = [
        "# Servo Fuzzing Test Results",
        "",
        f"- **Status**: {'✅ PASSED' if summary.is_success else '❌ FAILED'}",
        f"- **Total Tests**: {summary.total}",
        f"- **Passed**: {summary.passed}",
        f"- **Known Crashes (Ignored)**: {summary.known_crashes}",
        f"- **New Crashes**: {summary.new_crashes}",
        f"- **Timeouts**: {summary.timeouts}",
        f"- **Duration**: {summary.duration:.2f}s",
        "",
    ]

    if summary.new_crashes > 0:
        md_lines.extend([
            "## ❌ New Panics / Crashes",
            "",
            "| Testcase | Message | Location |",
            "| --- | --- | --- |",
        ])
        for r in summary.results:
            if r.status == TestStatus.NEW_CRASH:
                msg = r.crash_info.message if r.crash_info else (r.error_message or "Unknown")
                loc = r.crash_info.source_file if r.crash_info else "N/A"
                if r.crash_info and r.crash_info.line:
                    loc += f":{r.crash_info.line}"
                md_lines.append(f"| `{r.testcase.name}` | `{msg}` | `{loc}` |")
        md_lines.append("")

    if summary.known_crashes > 0:
        md_lines.extend([
            "## ℹ️ Known Issues Hit (Ignored)",
            "",
            "| Testcase | Signature ID | Issue |",
            "| --- | --- | --- |",
        ])
        for r in summary.results:
            if r.status == TestStatus.KNOWN_CRASH:
                issue_link = f"[{r.matched_issue}]({r.matched_issue})" if r.matched_issue else "N/A"
                md_lines.append(f"| `{r.testcase.name}` | `{r.matched_signature_id}` | {issue_link} |")
        md_lines.append("")

    (output_dir / "report.md").write_text("\n".join(md_lines), encoding="utf-8")

    # 3. New crash detail directories
    new_crashes_dir = output_dir / "new_crashes"
    if summary.new_crashes > 0:
        new_crashes_dir.mkdir(parents=True, exist_ok=True)
        for r in summary.results:
            if r.status == TestStatus.NEW_CRASH:
                case_dir = new_crashes_dir / r.testcase.stem
                case_dir.mkdir(parents=True, exist_ok=True)

                # Copy testcase
                shutil.copy2(r.testcase, case_dir / r.testcase.name)

                # Output log
                (case_dir / "output.log").write_text(r.combined_output, encoding="utf-8")

                # Signature draft
                if r.crash_info:
                    draft_sig = make_signature_draft(r.crash_info)
                    (case_dir / "signature_draft.json").write_text(
                        json.dumps(draft_sig, indent=2) + "\n", encoding="utf-8"
                    )

                # GitHub issue draft
                issue_text = make_github_issue_draft(r, servo_version=servo_version)
                (case_dir / "github_issue_draft.md").write_text(issue_text + "\n", encoding="utf-8")
