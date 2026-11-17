from __future__ import annotations

import json
import os
import re
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode

from .parser import (
    normalize_panic_message,
    normalize_source_path,
    parse_crash,
)
from .signatures import CrashSignature, save_signatures


def _github_headers(token: Optional[str] = None, user_agent: str = "servo-fuzzing-sync") -> dict[str, str]:
    auth_token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": user_agent,
    }
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    return headers


def fetch_fuzzing_issues(
    repo: str = "servo/servo",
    state: str = "open",
    token: Optional[str] = None,
    per_page: int = 100,
) -> list[dict[str, Any]]:
    headers = _github_headers(token=token)

    if not 1 <= per_page <= 100:
        raise ValueError("per_page must be between 1 and 100")

    all_issues: list[dict[str, Any]] = []
    page = 1
    while True:
        query = urlencode({
            "q": f'repo:{repo} is:issue is:{state} label:"B-fuzzing"',
            "per_page": per_page,
            "page": page,
        })
        url = f"https://api.github.com/search/issues?{query}"
        print(f"Fetching {url}")
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))

        if (
            not isinstance(data, dict)
            or not isinstance(data.get("items"), list)
            or not isinstance(data.get("total_count"), int)
            or data.get("incomplete_results") is not False
        ):
            raise ValueError("Invalid or incomplete GitHub issue-search response")

        if data["total_count"] > 1000:
            raise ValueError("Issue search exceeds GitHub's 1000-result limit")

        items = data["items"]
        print(f"GitHub returned {len(items)} issues on page {page}; total={data['total_count']}")
        for issue in items:
            if not isinstance(issue, dict) or "number" not in issue or "pull_request" in issue:
                raise ValueError("Invalid issue in GitHub issue-search response")

        all_issues.extend(items)
        if len(all_issues) == data["total_count"]:
            return all_issues
        if not items or len(all_issues) > data["total_count"]:
            raise ValueError("GitHub issue-search pagination does not match its total count")
        page += 1


RUNTIME_FRAME_PREFIXES = (
    "std::",
    "core::",
    "alloc::",
    "rust_begin_unwind",
    "rust_panic",
    "__rust",
    "servoshell::",
    "servo_constellation::logging::",
    "log::",
    "unknown>",
    "start_thread",
    "clone",
    "__libc",
)

MAX_SIGNATURE_FRAMES = 2


CLOSURE_SUFFIX = re.compile(r"::(?:\{\{closure\}\}|\{closure#\d+\})$")


def _strip_build_specific_suffix(frame: str) -> str:
    """Drop trailing closure markers and generic arguments from a frame.

    Whether these are printed depends on symbol mangling and inlining. Frames are
    matched as substrings, so the shortened frame fits either form.
    """
    while True:
        shortened = CLOSURE_SUFFIX.sub("", frame)
        if shortened.endswith(">"):
            depth = 0
            for index in range(len(shortened) - 1, -1, -1):
                depth += {">": 1, "<": -1}.get(shortened[index], 0)
                if depth == 0:
                    if shortened[index - 2:index] == "::" and index > 2:
                        shortened = shortened[:index - 2]
                    break
        if shortened == frame:
            return frame
        frame = shortened


def _select_signature_frames(frames: list[str]) -> list[str]:
    """Keep the innermost frames that belong to the crashing code, not to panic plumbing."""
    selected: list[str] = []
    for frame in frames:
        if "panic_hook" in frame or re.sub(r"^(?:<|&mut |&)+", "", frame).startswith(RUNTIME_FRAME_PREFIXES):
            continue
        frame = _strip_build_specific_suffix(frame)
        # A recursive function must not require the same recursion depth to match.
        if frame not in selected:
            selected.append(frame)
    return selected[:MAX_SIGNATURE_FRAMES]


def parse_issue_to_signature(issue_data: dict[str, Any]) -> Optional[CrashSignature]:
    """Turn a B-fuzzing issue into a signature, or None when it states no panic location.

    `stack_frames` is empty when the issue has no backtrace; such a signature matches
    on message and source file alone.
    """
    title = issue_data.get("title", "")
    body = issue_data.get("body", "") or ""
    body = re.sub(r"(?m)^\s*[│┃] ?", "", body)
    issue_number = issue_data.get("number")

    crash = parse_crash(body) or (parse_crash(title) if title else None)
    if crash is None or crash.source_file == "<signal>":
        return None

    return {
        "id": f"issue-{issue_number}",
        "message": crash.message,
        "source_file": crash.source_file,
        "stack_frames": _select_signature_frames(crash.stack_frames),
        "issue": issue_data.get("html_url", ""),
    }


@dataclass
class SyncResult:
    open_signatures: list[CrashSignature] = field(default_factory=list)
    closed_signatures: list[CrashSignature] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)

    def to_markdown(self, repo: str) -> str:
        lines = [
            f"### B-fuzzing sync from `{repo}`",
            "",
            "| | Signatures |",
            "|---|---|",
            f"| Open issues -> `rust-panic.json` | {len(self.open_signatures)} |",
            f"| Closed issues -> `fixed-history.json` | {len(self.closed_signatures)} |",
            f"| Issues without a parseable panic location | {len(self.skipped)} |",
        ]
        if self.skipped:
            lines += ["", "Skipped issues (ASAN reports, signals, debugger backtraces, tracking issues):", ""]
            lines += [
                f"- [#{issue['number']}]({issue['html_url']}) ({issue['state']}): {issue['title']}"
                for issue in self.skipped
            ]
        return "\n".join(lines) + "\n"


def _signatures_from_issues(issues: list[dict[str, Any]], result: SyncResult) -> list[CrashSignature]:
    signatures: list[CrashSignature] = []
    for issue in sorted(issues, key=lambda i: i["number"]):
        sig = parse_issue_to_signature(issue)
        if sig is None:
            result.skipped.append({
                "number": issue["number"],
                "html_url": issue.get("html_url", ""),
                "state": issue.get("state", ""),
                "title": issue.get("title", ""),
            })
            continue
        signatures.append(sig)
    return signatures


def sync_signatures_from_github(
    signatures_path: Path,
    history_path: Path,
    repo: str = "servo/servo",
    token: Optional[str] = None,
    dry_run: bool = False,
) -> SyncResult:
    """Regenerate both signature files from the B-fuzzing issues of `repo`.

    Open issues become `signatures_path`, closed ones `history_path`. Nothing from the
    previous files is kept, so a signature follows its issue when it is edited, closed
    or reopened. Both files are written only after every request succeeded.
    """
    print(f"Fetching B-fuzzing issues from {repo}...")
    open_issues = fetch_fuzzing_issues(repo=repo, state="open", token=token)
    closed_issues = fetch_fuzzing_issues(repo=repo, state="closed", token=token)
    print(f"Retrieved {len(open_issues)} open and {len(closed_issues)} closed B-fuzzing issues.")
    if not open_issues and not closed_issues:
        raise ValueError(f"No B-fuzzing issues found in {repo}; refusing to empty the signature files")

    result = SyncResult()
    result.open_signatures = _signatures_from_issues(open_issues, result)
    result.closed_signatures = _signatures_from_issues(closed_issues, result)

    if dry_run:
        print("[Dry Run] Signature files left unchanged.")
    else:
        save_signatures(Path(signatures_path), result.open_signatures)
        save_signatures(Path(history_path), result.closed_signatures)
        print(f"Wrote {len(result.open_signatures)} signatures to {signatures_path}.")
        print(f"Wrote {len(result.closed_signatures)} signatures to {history_path}.")
    for issue in result.skipped:
        print(f"  skipped #{issue['number']} ({issue['state']}): {issue['title']}")
    return result


def _build_issue_search_phrase(message: str, source_file: str) -> str:
    clean = message.strip()
    clean = re.sub(r"^(?:internal error:\s*entered unreachable code:\s*|assertion failed:\s*|panicked at:\s*)", "", clean, flags=re.I)
    if "Option::unwrap()" in clean or "Result::unwrap()" in clean:
        stem = Path(normalize_source_path(source_file)).name
        return f'"{clean}" "{stem}"'
    words = [w for w in re.findall(r"[A-Za-z0-9_.:-]+", clean) if not w.isdigit()]
    phrase = " ".join(words[:8])
    return f'"{phrase}"' if phrase else f'"{Path(source_file).name}"'


def find_upstream_issue(
    message: str,
    source_file: str,
    repo: str = "servo/servo",
    token: Optional[str] = None,
) -> Optional[dict[str, str]]:
    """Search GitHub for an existing open or closed issue matching a panic."""
    phrase = _build_issue_search_phrase(message, source_file)
    query = urlencode({"q": f"repo:{repo} is:issue {phrase}", "per_page": 10})
    url = f"https://api.github.com/search/issues?{query}"
    req = urllib.request.Request(url, headers=_github_headers(token=token, user_agent="servo-fuzzing-lookup"))

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None

    items = data.get("items")
    if not isinstance(items, list):
        return None

    msg_norm = normalize_panic_message(message).lower()
    src_norm = normalize_source_path(source_file).lower()
    src_name = Path(src_norm).name.lower()

    best_open: Optional[dict[str, str]] = None
    best_closed: Optional[dict[str, str]] = None

    for item in items:
        if not isinstance(item, dict) or "pull_request" in item:
            continue
        title = str(item.get("title", ""))
        body = str(item.get("body", "") or "")
        haystack = normalize_panic_message(f"{title}\n{body}").lower()

        msg_match = msg_norm in haystack
        src_match = src_norm in haystack or src_name in haystack
        if not (msg_match and (src_match or len(msg_norm) > 20)):
            continue

        entry = {
            "url": str(item.get("html_url", "")),
            "state": str(item.get("state", "open")),
            "number": str(item.get("number", "")),
            "title": title,
        }
        if entry["state"] == "open" and best_open is None:
            best_open = entry
        elif entry["state"] == "closed" and best_closed is None:
            best_closed = entry

    return best_open or best_closed
