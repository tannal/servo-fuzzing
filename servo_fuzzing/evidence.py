"""Evidence pack for one crash: a factual issue draft for Servo developers plus
machine-readable context and a brief so a person or an agent can add the root
cause analysis. Nothing here calls a model; analysis is a separate file."""

from __future__ import annotations

import json
import re
import shutil
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .bisect import UPSTREAM_REPO, BisectResult, _get_json
from .executor import TestResult
from .parser import CrashInfo
from .reporter import make_signature_draft

SCHEMA_VERSION = 1
MAX_SUSPECT_LOOKUPS = 100
SNIPPET_CONTEXT_LINES = 15
ISSUE_BACKTRACE_FRAMES = 40
ANALYSIS_FILE = "analysis.md"

_FRAME_RE = re.compile(r"^\s*\d+:\s")
_FRAME_LOCATION_RE = re.compile(r"^\s+at\s")
_FRAME_MODULE_RE = re.compile(r"<?([a-z_][a-z0-9_]*(?:::[a-z_][a-z0-9_]*)+)")
_VERSION_SHA_RE = re.compile(r"-([0-9a-f]{7,40})\b")
_BACKTRACE_END = "__rust_begin_short_backtrace"
_PANIC_MACHINERY_RE = re.compile(
    r"backtrace::print|panic_hook|std::panicking|rust_end_short_backtrace|rust_begin_unwind"
    r"|core::panicking|core::(option|result)::(unwrap|expect)_failed"
)
STRONG_SUSPECT_SCORE = 3

_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_EMPTY_TABLE_RE = re.compile(r"^\|.*\|\n\|[-:| ]+\|$")

JsonFetcher = Callable[[str], Any]
TextFetcher = Callable[[str], str]


def github_fetchers(token: Optional[str] = None) -> tuple[JsonFetcher, TextFetcher]:
    def fetch_text(url: str) -> str:
        request = urllib.request.Request(url, headers={"User-Agent": "servo-fuzzing-report"})
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8", errors="replace")

    return (lambda url: _get_json(url, token)), fetch_text


@dataclass
class Evidence:
    testcase: Path
    result: TestResult
    servo_version: str = "unknown"
    servo_args: list[str] = field(default_factory=lambda: ["-xfz"])
    attempts: int = 1
    crashes: int = 1
    original: Optional[Path] = None
    bisect: Optional[BisectResult] = None
    commits: list[dict[str, Any]] = field(default_factory=list)
    snippet: Optional[dict[str, Any]] = None

    @property
    def crash(self) -> Optional[CrashInfo]:
        return self.result.crash_info

    @property
    def command(self) -> str:
        return " ".join(["servoshell", *self.servo_args, self.testcase.name])

    @property
    def suspects(self) -> list[dict[str, Any]]:
        """Commits touching the panic's own file, else any touching the backtrace."""
        scored = sorted((c for c in self.commits if c.get("score")), key=lambda c: -c["score"])
        return [c for c in scored if c["score"] >= STRONG_SUSPECT_SCORE] or scored


def version_sha(servo_version: str) -> Optional[str]:
    match = _VERSION_SHA_RE.search(servo_version)
    return match.group(1) if match else None


def frame_source_hints(frames: list[str]) -> list[str]:
    """`<layout::table::layout::TableLayout>::f` -> `layout/table/layout`."""
    hints: list[str] = []
    for frame in frames:
        if _PANIC_MACHINERY_RE.search(frame):
            continue
        match = _FRAME_MODULE_RE.search(frame.split(": ", 1)[-1])
        if not match:
            continue
        hint = match.group(1).replace("::", "/")
        if hint.split("/")[0] not in ("std", "core", "alloc") and hint not in hints:
            hints.append(hint)
    return hints


def score_commit(commit: dict[str, Any], crash: CrashInfo) -> tuple[int, list[str]]:
    """Rank a commit by how directly its diff touches the crash site."""
    files = commit.get("files", [])
    names = [f["filename"] for f in files]
    score, reasons = 0, []

    needle = crash.message.strip()[:60]
    if needle and any(
        line.startswith("+") and needle in line
        for f in files
        for line in (f.get("patch") or "").splitlines()
    ):
        score += 5
        reasons.append("adds the panic message")
    if crash.source_file in names:
        score += 3
        reasons.append(f"changes {crash.source_file}")

    hints = frame_source_hints(crash.stack_frames)
    frame_files = sorted(
        n for n in names
        if n != crash.source_file and any(f"/{h}.rs" in n or f"/{h}/mod.rs" in n for h in hints)
    )
    if frame_files:
        score += 2
        reasons.append("changes files in the backtrace: " + ", ".join(frame_files[:3]))

    component = "/".join(crash.source_file.split("/")[:2])
    if not score and "/" in component and any(n.startswith(component + "/") for n in names):
        score += 1
        reasons.append(f"changes {component}/")
    return score, reasons


def fetch_range_commits(result: BisectResult, crash: Optional[CrashInfo], fetch: JsonFetcher) -> list[dict[str, Any]]:
    """Commits between the last good and first bad nightly, scored against the crash."""
    if not (result.last_good.sha and result.first_bad.sha):
        return []
    base = f"https://api.github.com/repos/{UPSTREAM_REPO}"
    listed: list[dict[str, Any]] = []
    for page in range(1, 31):
        data = fetch(f"{base}/compare/{result.last_good.sha}...{result.first_bad.sha}?per_page=100&page={page}")
        batch = data.get("commits", [])
        listed.extend(batch)
        if not batch or len(listed) >= data.get("total_commits", 0):
            break

    commits: list[dict[str, Any]] = []
    for index, item in enumerate(listed):
        commit: dict[str, Any] = {
            "sha": item["sha"],
            "title": item["commit"]["message"].splitlines()[0],
            "url": f"https://github.com/{UPSTREAM_REPO}/commit/{item['sha']}",
            "files": [],
            "score": 0,
            "reasons": [],
        }
        if crash and index < MAX_SUSPECT_LOOKUPS:
            try:
                detail = fetch(f"{base}/commits/{item['sha']}")
                files = [{"filename": f["filename"], "patch": f.get("patch")} for f in detail.get("files", [])]
                commit["score"], commit["reasons"] = score_commit({"files": files}, crash)
                commit["files"] = [f["filename"] for f in files]
            except Exception as exc:
                commit["reasons"] = [f"could not fetch changed files: {exc}"]
        commits.append(commit)
    return commits


def fetch_source_snippet(crash: CrashInfo, ref: str, fetch_text: TextFetcher) -> Optional[dict[str, Any]]:
    """Source around the panic line at the revision that crashed."""
    if not (crash.line and crash.source_file.endswith(".rs")):
        return None
    text = fetch_text(f"https://raw.githubusercontent.com/{UPSTREAM_REPO}/{ref}/{crash.source_file}")
    lines = text.splitlines()
    if crash.line > len(lines):
        return None
    start = max(1, crash.line - SNIPPET_CONTEXT_LINES)
    end = min(len(lines), crash.line + SNIPPET_CONTEXT_LINES)
    return {
        "ref": ref,
        "file": crash.source_file,
        "start_line": start,
        "panic_line": crash.line,
        "lines": lines[start - 1:end],
    }


def trimmed_backtrace(output: str, max_frames: int = ISSUE_BACKTRACE_FRAMES) -> str:
    """The panic line and its frames, without logs, panic machinery or thread startup."""
    lines = output.splitlines()
    first_frame = next((i for i, line in enumerate(lines) if _FRAME_RE.match(line)), None)
    if first_frame is None:
        return "\n".join(lines[-max_frames:])
    kept = lines[max(0, first_frame - 1):first_frame]
    frames = 0
    in_machinery = True
    for line in lines[first_frame:]:
        if _FRAME_RE.match(line):
            if _BACKTRACE_END in line or frames >= max_frames:
                break
            in_machinery = in_machinery and bool(_PANIC_MACHINERY_RE.search(line))
            frames += not in_machinery
        elif not _FRAME_LOCATION_RE.match(line):
            break
        if not in_machinery:
            kept.append(line)
    return "\n".join(kept)


def filled_analysis(text: str) -> str:
    """analysis.md without its title, comments and sections nobody filled in."""
    sections = re.split(r"^(?=## )", _COMMENT_RE.sub("", text), flags=re.M)
    kept = []
    for section in sections:
        heading, _, body = section.partition("\n")
        body = body.strip()
        if heading.startswith("## ") and body and not _EMPTY_TABLE_RE.match(body):
            kept.append(f"**{heading[3:].strip()}**\n\n{body}")
    return "\n\n".join(kept)


def render_issue(evidence: Evidence, analysis: Optional[str] = None) -> str:
    crash = evidence.crash
    if crash:
        location = f"at {crash.source_file}" + (f":{crash.line}" if crash.line else "")
        thread = f"thread {crash.thread}, " if crash.thread else ""
        title = f"panic: {crash.message} ({thread}{location})"
    else:
        title = f"Crash in {evidence.testcase.name}"

    try:
        content = evidence.testcase.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        content = f"<!-- Failed to read {evidence.testcase} -->"

    lines = [
        f"### Title: `{title}`",
        "",
        "**Labels**: `B-fuzzing`, `I-panic`, `C-has-manual-testcase`",
        "",
        "#### Minimal testcase:",
        "```html",
        content,
        "```",
        "",
        f"**Command**: `{evidence.command}`",
        f"**Servo Version**: `{evidence.servo_version}`",
        f"**Reproducibility**: {evidence.crashes}/{evidence.attempts} runs",
    ]

    bisect = evidence.bisect
    if bisect:
        lines += [
            "",
            "#### Regression range (nightly builds):",
            f"- Last good: `{bisect.last_good.tag}` (`{(bisect.last_good.sha or 'unknown')[:10]}`)",
            f"- First bad: `{bisect.first_bad.tag}` (`{(bisect.first_bad.sha or 'unknown')[:10]}`)",
        ]
        if bisect.compare_url:
            lines.append(f"- {len(evidence.commits)} commits: {bisect.compare_url}")
        if evidence.suspects:
            lines += ["", "Commits in that range that touch the crash site (not verified by building each one):"]
            lines += [
                f"- {c['sha'][:10]} {c['title']}: {'; '.join(c['reasons'])}"
                for c in evidence.suspects[:5]
            ]

    analysis = filled_analysis(analysis) if analysis else ""
    if analysis:
        lines += ["", "#### Analysis:", analysis]

    lines += [
        "",
        "#### Backtrace:",
        "```text",
        trimmed_backtrace(evidence.result.combined_output) or "(no output captured)",
        "```",
    ]
    return "\n".join(lines) + "\n"


def build_context(evidence: Evidence, files: dict[str, Optional[str]]) -> dict[str, Any]:
    crash = evidence.crash
    bisect = evidence.bisect
    return {
        "schema": SCHEMA_VERSION,
        "testcase": {"name": evidence.testcase.name, "bytes": evidence.testcase.stat().st_size},
        "command": evidence.command,
        "servo_version": evidence.servo_version,
        "reproduction": {
            "attempts": evidence.attempts,
            "crashes": evidence.crashes,
            "servo_args": evidence.servo_args,
            "exit_code": evidence.result.exit_code,
        },
        "crash": crash and {
            "message": crash.message,
            "source_file": crash.source_file,
            "line": crash.line,
            "thread": crash.thread,
            "stack_frames": crash.stack_frames,
            "frame_source_hints": frame_source_hints(crash.stack_frames),
            "signature_draft": make_signature_draft(crash),
        },
        "source_snippet": evidence.snippet,
        "bisect": bisect and {
            "last_good": {"tag": bisect.last_good.tag, "sha": bisect.last_good.sha},
            "first_bad": {"tag": bisect.first_bad.tag, "sha": bisect.first_bad.sha},
            "compare_url": bisect.compare_url,
            "tested": {tag: "bad" if bad else "good" for tag, bad in sorted(bisect.tested.items())},
            "commits": evidence.commits,
        },
        "files": files,
    }


AGENT_BRIEF = """# Root cause analysis brief

You are helping a Servo developer locate the root cause of a crash found by
fuzzing. Everything measured by tooling is in `context.json`; `issue.md` is the
same evidence formatted for people. Write your findings into `{analysis}`.

## Inputs
- `{testcase}`: the reduced testcase that crashes.{original}
- `output.log`: full output of the crashing run.
- `context.json`: panic location, backtrace, source around the panic line,
  reproduction rate{bisect}.

## Tasks
1. **Make the testcase readable.** Fuzzer output uses meaningless names
   (`x4`, `class4`, `fuzzvar00012`) and long random call sequences. Rename
   identifiers after their role, drop what a re-run shows is unnecessary, and
   describe in order what the page does up to the crash. Keep a copy that still
   crashes; verify with: `{command}`.
2. **Ablate.** Remove or change one ingredient at a time and record which
   variants still crash, with run counts.
3. **Map to Servo source.** Starting from `crash.source_file` and
   `crash.frame_source_hints`, explain which invariant the panic guards and how
   the testcase reaches it. The binary is a release build, so the function
   holding the panic line is often inlined and missing from the backtrace;
   `source_snippet` shows it. If `bisect.commits` is present, start with the
   commits that have the highest `score` and read their diffs.
4. **Write `{analysis}`** using the headings already in that file.

## Rules
- Keep **verified** facts (you ran it or read it in the source at the crashing
  revision) apart from **hypotheses**. Say how each verified fact was checked.
- Do not edit `issue.md` or `context.json`; they are regenerated by
  `servo-fuzzing report`. `{analysis}` is kept across regenerations.
- Do not file or comment on Servo issues. Servo's contribution policy restricts
  AI-generated content, so a maintainer of this corpus decides what is posted.
"""

ANALYSIS_TEMPLATE = """# Analysis of `{title}`

<!-- Written by people and/or agents. Mark who wrote each section. Not part of
issue.md unless `servo-fuzzing report --include-analysis` is used. -->

## Readable testcase

## What the testcase does

## Ablation

| Variant | Runs crashing |
|---|---|

## Root cause: verified

## Root cause: hypotheses

## Suggested fix direction
"""


def write_evidence_pack(evidence: Evidence, out_dir: Path, include_analysis: bool = False) -> Path:
    """Write the pack and return the path of issue.md. An existing analysis.md is kept."""
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    testcase_copy = out_dir / evidence.testcase.name
    if testcase_copy != evidence.testcase.resolve():
        shutil.copy2(evidence.testcase, testcase_copy)
    original_name = None
    if evidence.original and evidence.original.is_file():
        stem = "unreduced" if evidence.testcase.stem == "original" else "original"
        original_name = f"{stem}{evidence.original.suffix}"
        shutil.copy2(evidence.original, out_dir / original_name)
    (out_dir / "output.log").write_text(evidence.result.combined_output, encoding="utf-8")
    if evidence.crash:
        (out_dir / "signature_draft.json").write_text(
            json.dumps(make_signature_draft(evidence.crash), indent=2) + "\n", encoding="utf-8"
        )

    crash = evidence.crash
    title = crash.message if crash else evidence.testcase.name
    analysis_path = out_dir / ANALYSIS_FILE
    if not analysis_path.exists():
        analysis_path.write_text(ANALYSIS_TEMPLATE.format(title=title), encoding="utf-8")

    files = {
        "testcase": evidence.testcase.name,
        "original": original_name,
        "log": "output.log",
        "signature_draft": "signature_draft.json" if evidence.crash else None,
        "issue": "issue.md",
        "brief": "AGENT.md",
        "analysis": ANALYSIS_FILE,
    }
    (out_dir / "context.json").write_text(
        json.dumps(build_context(evidence, files), indent=2) + "\n", encoding="utf-8"
    )
    (out_dir / "AGENT.md").write_text(
        AGENT_BRIEF.format(
            analysis=ANALYSIS_FILE,
            testcase=evidence.testcase.name,
            original=f"\n- `{original_name}`: the testcase before reduction." if original_name else "",
            bisect=", nightly regression range and the commits in it" if evidence.bisect else "",
            command=evidence.command,
        ),
        encoding="utf-8",
    )

    analysis = analysis_path.read_text(encoding="utf-8") if include_analysis else None
    issue_path = out_dir / "issue.md"
    issue_path.write_text(render_issue(evidence, analysis=analysis), encoding="utf-8")
    return issue_path
