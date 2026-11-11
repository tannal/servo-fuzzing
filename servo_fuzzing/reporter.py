from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from .executor import SuiteSummary, TestResult, TestStatus
from .parser import CrashInfo, generate_descriptive_name, normalize_panic_message, normalize_source_path


def make_signature_draft(crash: CrashInfo, issue_url: str = "https://github.com/servo/servo/issues/TODO") -> dict[str, Any]:
    sig_id = generate_descriptive_name(crash)

    frames = [
        f for f in crash.stack_frames
        if any(k in f.lower() for k in ("servo", "script", "layout", "style", "net"))
    ][:3]
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
    crash = result.crash_info
    if crash:
        loc = f"at {crash.source_file}" + (f":{crash.line}" if crash.line else "")
        thread_info = f"thread {crash.thread}, " if crash.thread else ""
        title = f"panic: {crash.message} ({thread_info}{loc})"
    else:
        title = f"Crash in {result.testcase.name}"

    try:
        testcase_content = result.testcase.read_text(encoding="utf-8", errors="replace")
    except Exception:
        testcase_content = f"<!-- Failed to read {result.testcase} -->"

    lines = [
        f"### Title: `{title}`",
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
        lines.append(json.dumps(make_signature_draft(crash), indent=2))
    else:
        lines.append("// Non-panic crash or unexpected exit code")
    lines.append("```")

    return "\n".join(lines)


def _group_unique_crashes(results: list[TestResult], status: TestStatus) -> list[tuple[TestResult, int]]:
    grouped: dict[tuple[str, str], tuple[TestResult, int]] = {}
    for r in results:
        if r.status != status:
            continue
        if r.crash_info:
            key = (
                normalize_panic_message(r.crash_info.message),
                normalize_source_path(r.crash_info.source_file),
            )
        else:
            key = (r.error_message or "unknown", str(r.exit_code))
        if key not in grouped:
            grouped[key] = (r, 1)
        else:
            rep, count = grouped[key]
            grouped[key] = (rep, count + 1)
    return list(grouped.values())


def print_cli_summary(summary: SuiteSummary) -> None:
    unique_new = _group_unique_crashes(summary.results, TestStatus.NEW_CRASH)
    unique_reg = _group_unique_crashes(summary.results, TestStatus.REGRESSION)
    print()
    print("=" * 72)
    print("                SERVO FUZZING SUITE SUMMARY")
    print("=" * 72)
    print(f"Total Tests Run   : {summary.total}")
    print(f"Passed            : {summary.passed}")
    print(f"Known Crashes     : {summary.known_crashes} (ignored existing issues)")
    print(f"Regressions       : {summary.regressions} ({len(unique_reg)} unique closed issues reappeared)")
    print(f"Timeouts          : {summary.timeouts}")
    print(f"Errors            : {summary.errors}")
    print(f"New / Unknown     : {summary.new_crashes} ({len(unique_new)} unique)")
    print(f"Execution Time    : {summary.duration:.2f} seconds")
    print("=" * 72)

    if summary.known_crashes > 0:
        print("\n[Known Crashes (Passed / Ignored)]:")
        for r in summary.results:
            if r.status == TestStatus.KNOWN_CRASH:
                sig_text = f" [{r.matched_signature_id}]" if r.matched_signature_id else ""
                issue_text = f" -> {r.matched_issue}" if r.matched_issue else ""
                print(f"  * {r.testcase.name}{sig_text}{issue_text}")

    if unique_reg:
        print("\n" + "!" * 72)
        print("  [FAIL] HISTORICAL REGRESSIONS DETECTED (CLOSED ISSUES REAPPEARED)!")
        print("!" * 72)
        for r, count in unique_reg:
            dup_note = f" (and {count - 1} duplicate(s))" if count > 1 else ""
            print(f"--- REGRESSION: {r.testcase.name}{dup_note} ---")
            print(f"  Signature : {r.matched_signature_id} -> {r.matched_issue}")
            if r.crash_info:
                print(f"  Panic     : {r.crash_info.message}")
                print(f"  Source    : {r.crash_info.source_file}")
            print()

    if unique_new:
        print("\n" + "!" * 72)
        print("  [FAIL] UNEXPECTED / NEW PANICS DETECTED!")
        print("  This blocks the PR / CI to prevent merging regressions into Servo.")
        print("!" * 72)
        print("\nPR Author / Triage Instructions:")
        print("  1. Review the failures below to check if they are caused by this PR.")
        print("  2. If caused by this PR: please resolve the panic before merging.")
        print("  3. If unrelated / pre-existing: open or label the issue 'B-fuzzing' on GitHub")
        print("     and add the draft signature shown below to signatures/rust-panic.json.\n")

        for r, count in unique_new:
            dup_note = f" (and {count - 1} duplicate(s))" if count > 1 else ""
            print(f"--- FAILED TEST: {r.testcase.name}{dup_note} ---")
            if r.crash_info:
                print(f"  Panic: {r.crash_info.message}")
                loc = r.crash_info.source_file + (f":{r.crash_info.line}" if r.crash_info.line else "")
                print(f"  Source: {loc}")
                if r.crash_info.stack_frames:
                    print("  Frames:")
                    for frame in r.crash_info.stack_frames[:5]:
                        print(f"    - {frame}")

                print(f"\n  Reproduce in Servo: ./mach run -z {r.testcase}")
                print(f"  Debug with lldb:   ./mach run -z --debugger=lldb {r.testcase}")
                print(f"  Re-run this test:  ./mach test-fuzzing --dev {r.testcase}")

                print("\n  Draft signature for signatures/rust-panic.json:")
                print(json.dumps(make_signature_draft(r.crash_info), indent=4))
            elif r.error_message:
                print(f"  Error: {r.error_message}")
            print()
    elif not unique_reg:
        print("\n  [PASS] All tests passed (or reproduced known issues).")
    print("=" * 72)


def write_artifacts(summary: SuiteSummary, output_dir: Path, servo_version: str = "latest") -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    unique_new = _group_unique_crashes(summary.results, TestStatus.NEW_CRASH)
    unique_reg = _group_unique_crashes(summary.results, TestStatus.REGRESSION)

    summary_data = {
        "total": summary.total,
        "passed": summary.passed,
        "known_crashes": summary.known_crashes,
        "regressions": summary.regressions,
        "unique_regressions": len(unique_reg),
        "new_crashes": summary.new_crashes,
        "unique_new_crashes": len(unique_new),
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

    md_lines = [
        "# Servo Fuzzing Test Results",
        "",
        f"- **Status**: {'PASSED' if summary.is_success else 'FAILED'}",
        f"- **Total Tests**: {summary.total}",
        f"- **Passed**: {summary.passed}",
        f"- **Known Crashes (Ignored)**: {summary.known_crashes}",
        f"- **Historical Regressions**: {summary.regressions} ({len(unique_reg)} unique)",
        f"- **New Crashes (Total)**: {summary.new_crashes}",
        f"- **Unique New Panics**: {len(unique_new)}",
        f"- **Timeouts**: {summary.timeouts}",
        f"- **Duration**: {summary.duration:.2f}s",
        "",
    ]

    if unique_reg:
        md_lines.extend([
            "## Historical Regressions (Closed Issues Reappeared)",
            "",
            "| Signature ID | Representative Testcase | Occurrences | Closed Issue | Location |",
            "| --- | --- | --- | --- | --- |",
        ])
        for r, count in unique_reg:
            sig_id = r.matched_signature_id or (generate_descriptive_name(r.crash_info) if r.crash_info else r.testcase.stem)
            issue_link = f"[{r.matched_issue}]({r.matched_issue})" if r.matched_issue else "N/A"
            loc = r.crash_info.source_file if r.crash_info else "N/A"
            if r.crash_info and r.crash_info.line:
                loc += f":{r.crash_info.line}"
            md_lines.append(f"| `{sig_id}` | `{r.testcase.name}` | {count} | {issue_link} | `{loc}` |")
        md_lines.append("")

    if unique_new:
        md_lines.extend([
            "## Unique New Panics / Crashes",
            "",
            "| Signature ID | Representative Testcase | Occurrences | Message | Location |",
            "| --- | --- | --- | --- | --- |",
        ])
        for r, count in unique_new:
            sig_id = generate_descriptive_name(r.crash_info) if r.crash_info else r.testcase.stem
            msg = r.crash_info.message if r.crash_info else (r.error_message or "Unknown")
            loc = r.crash_info.source_file if r.crash_info else "N/A"
            if r.crash_info and r.crash_info.line:
                loc += f":{r.crash_info.line}"
            md_lines.append(f"| `{sig_id}` | `{r.testcase.name}` | {count} | `{msg}` | `{loc}` |")
        md_lines.append("")

    if summary.known_crashes > 0:
        known_grouped: dict[tuple[str, str], tuple[TestResult, int]] = {}
        for r in summary.results:
            if r.status == TestStatus.KNOWN_CRASH:
                k = (r.matched_signature_id or "unknown", r.matched_issue or "")
                if k not in known_grouped:
                    known_grouped[k] = (r, 1)
                else:
                    rep, c = known_grouped[k]
                    known_grouped[k] = (rep, c + 1)

        md_lines.extend([
            "## Known Issues Hit (Ignored)",
            "",
            "| Signature ID | Representative Testcase | Occurrences | Issue |",
            "| --- | --- | --- | --- |",
        ])
        for (sig_id, issue), (r, count) in known_grouped.items():
            issue_link = f"[{issue}]({issue})" if issue else "N/A"
            md_lines.append(f"| `{sig_id}` | `{r.testcase.name}` | {count} | {issue_link} |")
        md_lines.append("")

    (output_dir / "report.md").write_text("\n".join(md_lines), encoding="utf-8")

    actionable = unique_new + unique_reg
    if actionable:
        new_crashes_dir = output_dir / "new_crashes"
        new_crashes_dir.mkdir(parents=True, exist_ok=True)
        for r, _count in actionable:
            case_dir = new_crashes_dir / r.testcase.stem
            case_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(r.testcase, case_dir / r.testcase.name)
            (case_dir / "output.log").write_text(r.combined_output, encoding="utf-8")
            if r.crash_info:
                issue_url = r.matched_issue or "https://github.com/servo/servo/issues/TODO"
                draft_sig = make_signature_draft(r.crash_info, issue_url=issue_url)
                (case_dir / "signature_draft.json").write_text(
                    json.dumps(draft_sig, indent=2) + "\n", encoding="utf-8"
                )
            issue_text = make_github_issue_draft(r, servo_version=servo_version)
            (case_dir / "github_issue_draft.md").write_text(issue_text + "\n", encoding="utf-8")
