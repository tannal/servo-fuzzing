from __future__ import annotations

import json
import os
import re
import urllib.request
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode

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
    auth_token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "servo-fuzzing-sync",
    }
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    if not 1 <= per_page <= 100:
        raise ValueError("per_page must be between 1 and 100")

    all_issues: list[dict[str, Any]] = []
    page = 1
    while True:
        query = urlencode({
            "q": f'repo:{repo} is:issue is:open label:"B-fuzzing"',
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


def parse_issue_to_signature(issue_data: dict[str, Any]) -> Optional[CrashSignature]:
    title = issue_data.get("title", "")
    body = issue_data.get("body", "") or ""
    body = re.sub(r"(?m)^\s*[│┃] ?", "", body)
    issue_url = issue_data.get("html_url", "")
    issue_number = issue_data.get("number")

    crash = parse_crash(body) or (parse_crash(title) if title else None)

    if crash is None:
        m = re.search(r"(?:panic:\s*)?(.+?)\s*\((?:thread\s+[^,]+,\s*)?at\s+([^:]+)(?::\d+)?\)", title)
        if m:
            message = m.group(1).strip()
            source_file = normalize_source_path(m.group(2).strip())
            frames = extract_stack_frames(body) or ["<script::dom>"]
            crash_id = f"issue-{issue_number}" if issue_number else "fuzzing-issue"
            return {
                "id": crash_id,
                "message": message,
                "source_file": source_file,
                "stack_frames": frames,
                "issue": issue_url,
            }
        return None

    selected_frames = [
        f for f in crash.stack_frames
        if "servoshell::" not in f and "panic_hook" not in f
    ][:2]
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
    signatures_path = Path(signatures_path)
    existing_sigs: list[CrashSignature] = load_signatures(signatures_path) if signatures_path.exists() else []

    print(f"Fetching open B-fuzzing issues from {repo}...")
    issues = fetch_open_fuzzing_issues(repo=repo, token=token)
    print(f"Retrieved {len(issues)} open B-fuzzing issues.")

    added = 0
    existing = 0

    for issue in issues:
        issue_url = issue.get("html_url", "")
        if find_signature_by_issue(existing_sigs, issue_url):
            existing += 1
            continue

        sig = parse_issue_to_signature(issue)
        if sig:
            base_id = sig["id"]
            counter = 1
            while any(s["id"] == sig["id"] for s in existing_sigs):
                sig["id"] = f"{base_id}-{counter}"
                counter += 1

            existing_sigs.append(sig)
            added += 1
            print(f"  + Added signature: {sig['id']} -> {sig['issue']} ({sig['message']})")
        else:
            print(f"  Skipped {issue_url}: no parseable panic signature")

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
        m = re.search(r"/issues/(\d+)$", sig["issue"])
        if not m:
            active_sigs.append(sig)
            continue

        issue_num = m.group(1)
        api_url = f"https://api.github.com/repos/servo/servo/issues/{issue_num}"
        req = urllib.request.Request(api_url, headers=headers)

        try:
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("state", "open") == "closed":
                    print(f"  Issue #{issue_num} is CLOSED (Fixed upstream). Moving '{sig['id']}' to archive.")
                    pruned.append(sig)
                    archived.append(sig)
                else:
                    active_sigs.append(sig)
        except Exception as exc:
            print(f"  Warning: Could not check issue #{issue_num}: {exc}")
            active_sigs.append(sig)

    if pruned and not dry_run:
        save_signatures(signatures_path, active_sigs)
        save_signatures(archive_path, archived)
        print(f"Pruned {len(pruned)} fixed signatures from {signatures_path} (saved to {archive_path}).")
    elif dry_run and pruned:
        print(f"[Dry Run] Would have pruned {len(pruned)} signatures: {[s['id'] for s in pruned]}")

    return pruned
