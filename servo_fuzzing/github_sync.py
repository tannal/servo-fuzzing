"""GitHub issue synchronization for Servo fuzzing.

Fetches open issues labelled `B-fuzzing` from GitHub, parses their crash
descriptions and backtraces, and synchronizes the signatures database.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from pathlib import Path
from typing import Any, Optional

from .parser import extract_stack_frames, normalize_source_path, parse_crash
from .signatures import (
    CrashSignature,
    find_signature_by_issue,
    load_signatures,
    save_signatures,
)


def fetch_open_fuzzing_issues(
    repo: str = "servo/servo",
    token: Optional[str] = None,
    per_page: int = 100,
) -> list[dict[str, Any]]:
    """Fetch open issues labelled 'B-fuzzing' from the GitHub REST API."""
    auth_token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "servo-fuzzing-sync",
    }
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    all_issues: list[dict[str, Any]] = []
    page = 1

    while True:
        url = (
            f"https://api.github.com/repos/{repo}/issues"
            f"?labels=B-fuzzing&state=open&per_page={per_page}&page={page}"
        )
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if not data:
                    break
                # Filter out pull requests
                for item in data:
                    if "pull_request" not in item:
                        all_issues.append(item)
                if len(data) < per_page:
                    break
                page += 1
        except Exception as exc:
            print(f"Error fetching issues from {url}: {exc}")
            break

    return all_issues


def parse_issue_to_signature(issue_data: dict[str, Any]) -> Optional[CrashSignature]:
    """Extract a CrashSignature from a GitHub issue title and body."""
    title = issue_data.get("title", "")
    body = issue_data.get("body", "") or ""
    issue_url = issue_data.get("html_url", "")
    issue_number = issue_data.get("number")

    # Try parsing body first for backtrace
    crash = parse_crash(body)

    # If body didn't yield a crash, try parsing the title
    if crash is None and title:
        crash = parse_crash(title)

    if crash is None:
        # Fallback heuristic: check title for panic format
        # e.g. "panic: assertion failed: foo (thread Script#7, at components/.../bar.rs:950)"
        m = re.search(r"(?:panic:\s*)?(.+?)\s*\((?:thread\s+[^,]+,\s*)?at\s+([^:]+)(?::\d+)?\)", title)
        if m:
            message = m.group(1).strip()
            source_file = normalize_source_path(m.group(2).strip())
            frames = extract_stack_frames(body)
            if not frames:
                frames = ["<script::dom>"]
            crash_id = f"issue-{issue_number}" if issue_number else "fuzzing-issue"
            return {
                "id": crash_id,
                "message": message,
                "source_file": source_file,
                "stack_frames": frames,
                "issue": issue_url,
            }
        return None

    # Pick up to 2 distinctive frames from backtrace
    selected_frames = []
    for frame in crash.stack_frames:
        if len(selected_frames) >= 2:
            break
        if "servoshell::" not in frame and "panic_hook" not in frame:
            selected_frames.append(frame)

    if not selected_frames:
        selected_frames = [crash.source_file]

    sig_id = f"issue-{issue_number}" if issue_number else f"sig-{Path(crash.source_file).stem}"

    return {
        "id": sig_id,
        "message": crash.message,
        "source_file": crash.source_file,
        "stack_frames": selected_frames,
        "issue": issue_url,
    }


def sync_signatures_from_github(
    signatures_path: Path,
    repo: str = "servo/servo",
    token: Optional[str] = None,
    dry_run: bool = False,
) -> tuple[int, int]:
    """Synchronize open B-fuzzing issues from GitHub with rust-panic.json.

    Returns:
        (added_count, existing_count)
    """
    signatures_path = Path(signatures_path)
    existing_sigs: list[CrashSignature] = []
    if signatures_path.exists():
        existing_sigs = load_signatures(signatures_path)

    print(f"Fetching open B-fuzzing issues from {repo}...")
    issues = fetch_open_fuzzing_issues(repo=repo, token=token)
    print(f"Retrieved {len(issues)} open B-fuzzing issues.")

    added = 0
    existing = 0

    for issue in issues:
        issue_url = issue.get("html_url", "")
        already_present = find_signature_by_issue(existing_sigs, issue_url)
        if already_present:
            existing += 1
            continue

        sig = parse_issue_to_signature(issue)
        if sig:
            # Ensure unique id
            base_id = sig["id"]
            counter = 1
            while any(s["id"] == sig["id"] for s in existing_sigs):
                sig["id"] = f"{base_id}-{counter}"
                counter += 1

            existing_sigs.append(sig)
            added += 1
            print(f"  + Added signature: {sig['id']} -> {sig['issue']} ({sig['message']})")

    if added > 0 and not dry_run:
        save_signatures(signatures_path, existing_sigs)
        print(f"Saved {len(existing_sigs)} signatures to {signatures_path}.")
    elif dry_run:
        print(f"[Dry Run] Would have added {added} signatures.")

    return added, existing


def check_and_prune_closed_issues(
    signatures_path: Path,
    archive_path: Path,
    token: Optional[str] = None,
    dry_run: bool = False,
) -> list[CrashSignature]:
    """Check if issues in rust-panic.json were CLOSED upstream.

    When an issue is fixed and closed in servo/servo:
    - Moves the signature to fixed-history.json.
    - Removes it from rust-panic.json so that any regression is caught!
    """
    signatures_path = Path(signatures_path)
    archive_path = Path(archive_path)

    if not signatures_path.exists():
        return []

    sigs = load_signatures(signatures_path)
    archived: list[CrashSignature] = load_signatures(archive_path) if archive_path.exists() else []

    auth_token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "servo-fuzzing-prune",
    }
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    active_sigs: list[CrashSignature] = []
    pruned: list[CrashSignature] = []

    print(f"Checking {len(sigs)} signatures against upstream issue status...")
    for sig in sigs:
        issue_url = sig["issue"]
        # Extract issue number, e.g. https://github.com/servo/servo/issues/49411 -> 49411
        m = re.search(r"/issues/(\d+)$", issue_url)
        if not m:
            active_sigs.append(sig)
            continue

        issue_num = m.group(1)
        api_url = f"https://api.github.com/repos/servo/servo/issues/{issue_num}"
        req = urllib.request.Request(api_url, headers=headers)

        try:
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                state = data.get("state", "open")
                if state == "closed":
                    print(f"  🎉 Issue #{issue_num} is CLOSED (Fixed upstream)! Moving signature '{sig['id']}' to archive.")
                    pruned.append(sig)
                    archived.append(sig)
                else:
                    active_sigs.append(sig)
        except Exception as exc:
            # If rate limit or network issue, keep signature active
            print(f"  Warning: Could not check issue #{issue_num}: {exc}")
            active_sigs.append(sig)

    if pruned and not dry_run:
        save_signatures(signatures_path, active_sigs)
        save_signatures(archive_path, archived)
        print(f"Pruned {len(pruned)} fixed signatures from {signatures_path} (saved to {archive_path}).")
    elif dry_run and pruned:
        print(f"[Dry Run] Would have pruned {len(pruned)} signatures: {[s['id'] for s in pruned]}")

    return pruned
