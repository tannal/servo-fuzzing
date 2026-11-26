"""Prepare evidence-backed proposals for new panics and file upstream GitHub issues."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

from .evidence import Evidence, gather_evidence, write_evidence_pack
from .github_sync import _github_headers, find_upstream_issue
from .parser import normalize_panic_message, normalize_source_path
from .reporter import PLACEHOLDER_FRAME
from .signatures import (
    CrashSignature,
    find_matching_signature_entry,
    load_signatures,
    save_signatures,
)

PROPOSAL_MANIFEST = "proposal.json"
PROPOSALS_INDEX = "proposals.json"
DEFAULT_ISSUE_LABELS = ["B-fuzzing", "I-panic", "C-has-manual-testcase"]
MAX_TITLE_LEN = 200
MAX_BODY_LEN = 60000
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

GatherFn = Callable[..., Optional[Evidence]]
FindIssueFn = Callable[[str, str], Optional[dict[str, str]]]
CreateIssueFn = Callable[[str, str, str, list[str], str], str]


def _id_with_panic_hash(base_id: str, msg: str, src: str) -> str:
    panic = f"{normalize_panic_message(msg)}\n{normalize_source_path(src)}"
    return f"{base_id}-{hashlib.sha1(panic.encode('utf-8')).hexdigest()[:8]}"


def _clean_frames(frames: list[str]) -> list[str]:
    return [f for f in frames if f and f.strip() and f != PLACEHOLDER_FRAME]


def _find_reproducer(corpus_dir: Path, draft_id: str) -> Optional[Path]:
    flat = corpus_dir / f"{draft_id}.html"
    if flat.is_file():
        return flat
    nested = corpus_dir / draft_id / f"{draft_id}.html"
    if nested.is_file():
        return nested
    repro = corpus_dir / draft_id / "repro.html"
    if repro.is_file():
        return repro
    return None


def propose_panics(
    drafts_path: Path,
    corpus_dir: Path,
    out_dir: Path,
    gather: Optional[GatherFn] = None,
    target_repo: str = "servo/servo",
    find_existing: Optional[FindIssueFn] = None,
    limit: Optional[int] = 5,
    binary: Optional[Path] = None,
    bisect: bool = False,
    token: Optional[str] = None,
    fixed_history_path: Optional[Path] = None,
) -> list[dict[str, Any]]:
    """Build per-panic proposal directories under `out_dir` from `drafts_path`.

    Each proposal records whether the panic matches an existing open upstream issue,
    regresses an existing closed upstream issue, or is brand-new, together with its
    evidence pack (`issue.md`, `context.json`, `AGENT.md`, `analysis.md`).
    """
    out_dir = Path(out_dir).resolve()
    shutil.rmtree(out_dir, ignore_errors=True)
    drafts_path = Path(drafts_path)
    corpus_dir = Path(corpus_dir)

    if not drafts_path.is_file():
        print(f"No draft signatures at {drafts_path}; nothing to propose.")
        return []

    if gather is None:
        if binary is None:
            raise ValueError("Either gather callback or binary path is required")
        bin_path = Path(binary)
        gather = lambda tc, regresses_issue=None: gather_evidence(
            binary=bin_path,
            testcase=tc,
            bisect=bisect,
            token=token,
            regresses_issue=regresses_issue,
        )

    lookup = find_existing or (lambda msg, src: find_upstream_issue(msg, src, repo=target_repo, token=token))
    fixed_sigs = (
        load_signatures(Path(fixed_history_path))
        if fixed_history_path and Path(fixed_history_path).exists()
        else []
    )

    proposals: list[dict[str, Any]] = []
    used_ids: set[str] = set()

    for draft in load_signatures(drafts_path):
        draft_id = draft["id"]
        if limit is not None and len(proposals) >= limit:
            print(f"Limit of {limit} proposal(s) reached; {draft_id} is left for a later run.")
            break

        msg = draft["message"]
        src = draft["source_file"]
        cleaned_sig: CrashSignature = {
            "id": draft_id,
            "message": msg,
            "source_file": src,
            "stack_frames": _clean_frames(list(draft.get("stack_frames") or [])),
            "issue": draft.get("issue") or f"https://github.com/{target_repo}/issues/TODO",
        }

        proposal_id = draft_id if _ID_RE.match(draft_id) else "crash"
        if proposal_id in used_ids:
            proposal_id = _id_with_panic_hash(proposal_id, msg, src)
        while proposal_id in used_ids:
            proposal_id = f"{proposal_id}-dup"
        used_ids.add(proposal_id)
        cleaned_sig["id"] = proposal_id

        upstream = lookup(msg, src)
        if upstream and upstream.get("state") == "open":
            prop_dir = out_dir / proposal_id
            prop_dir.mkdir(parents=True, exist_ok=True)
            cleaned_sig["issue"] = upstream["url"]
            proposal_data = {
                "id": proposal_id,
                "status": "existing_open",
                "target_repo": target_repo,
                "issue": upstream["url"],
                "regresses_issue": None,
                "signature": cleaned_sig,
            }
            (prop_dir / PROPOSAL_MANIFEST).write_text(json.dumps(proposal_data, indent=2) + "\n", encoding="utf-8")
            proposals.append(proposal_data)
            print(f"[PROPOSE] {proposal_id} -> already tracked open upstream: {upstream['url']}")
            continue

        repro = _find_reproducer(corpus_dir, draft_id)
        if not repro:
            print(f"Skipping {draft_id}: reproducer missing in {corpus_dir}")
            continue

        fixed_match = find_matching_signature_entry(msg, src, fixed_sigs) if fixed_sigs else None
        upstream_closed = upstream if upstream and upstream.get("state") == "closed" else None
        is_regression = bool(upstream_closed or fixed_match)
        regresses_issue = (
            upstream_closed["url"]
            if upstream_closed
            else (fixed_match["issue"] if fixed_match else None)
        )
        status = "regression" if is_regression else "new"
        if is_regression:
            cleaned_sig["issue"] = regresses_issue or cleaned_sig["issue"]

        evidence = gather(repro, regresses_issue=regresses_issue)
        if evidence is None:
            print(f"Skipping {draft_id}: reproducer {repro.name} no longer crashes")
            continue
        if evidence.crash:
            if (
                normalize_panic_message(evidence.crash.message) != normalize_panic_message(msg)
                or normalize_source_path(evidence.crash.source_file) != normalize_source_path(src)
            ):
                print(
                    f"Skipping {draft_id}: reproducer panicked differently "
                    f"({evidence.crash.source_file}: {evidence.crash.message})"
                )
                continue

        prop_dir = out_dir / proposal_id
        write_evidence_pack(evidence, prop_dir)
        sig_draft_path = prop_dir / "signature_draft.json"
        sig_draft_path.write_text(json.dumps(cleaned_sig, indent=2) + "\n", encoding="utf-8")

        proposal_data = {
            "id": proposal_id,
            "status": status,
            "target_repo": target_repo,
            "issue": cleaned_sig["issue"] if is_regression else None,
            "regresses_issue": regresses_issue,
            "signature": cleaned_sig,
        }
        (prop_dir / PROPOSAL_MANIFEST).write_text(json.dumps(proposal_data, indent=2) + "\n", encoding="utf-8")
        proposals.append(proposal_data)
        print(f"[PROPOSE] {proposal_id} -> status={status}")

    if proposals:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / PROPOSALS_INDEX).write_text(
            json.dumps({"target_repo": target_repo, "proposals": proposals}, indent=2) + "\n",
            encoding="utf-8",
        )
    return proposals


def create_github_issue(
    repo: str,
    title: str,
    body: str,
    labels: list[str],
    token: str,
) -> str:
    """Create a GitHub issue on `repo`, retrying without labels if permissions disallow setting them."""
    url = f"https://api.github.com/repos/{repo}/issues"
    headers = _github_headers(token=token, user_agent="servo-fuzzing-reporter")
    headers["Content-Type"] = "application/json"

    def _post(payload: dict[str, Any]) -> str:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return str(data["html_url"])

    try:
        payload: dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        return _post(payload)
    except urllib.error.HTTPError as err:
        if labels and err.code in (403, 422):
            return _post({"title": title, "body": body})
        raise


def _unique_sig_id(base_id: str, issue_url: str, existing: list[CrashSignature]) -> str:
    taken = {s["id"] for s in existing}
    if base_id not in taken:
        return base_id
    m = re.search(r"/issues/(\d+)$", issue_url)
    if m:
        issue_id = f"issue-{m.group(1)}"
        if issue_id not in taken:
            return issue_id
    counter = 1
    while f"{base_id}-{counter}" in taken:
        counter += 1
    return f"{base_id}-{counter}"


def _append_unique_signature(
    target_list: list[CrashSignature],
    sig: CrashSignature,
    issue_url: str,
) -> bool:
    if find_matching_signature_entry(sig["message"], sig["source_file"], target_list):
        return False
    entry: CrashSignature = {
        "id": _unique_sig_id(sig["id"], issue_url, target_list),
        "message": sig["message"],
        "source_file": sig["source_file"],
        "stack_frames": _clean_frames(list(sig.get("stack_frames") or [])),
        "issue": issue_url,
    }
    target_list.append(entry)
    return True


def _extract_issue_title(issue_md: str, msg: str, src: str, is_regression: bool) -> str:
    first_line = issue_md.splitlines()[0] if issue_md else ""
    m = re.match(r"^### Title:\s*`(.+)`\s*$", first_line)
    if m:
        raw = m.group(1).strip()
        title = f"regression: {raw}" if is_regression and not raw.lower().startswith("regression") else raw
    else:
        prefix = "regression: panic" if is_regression else "panic"
        title = f"{prefix}: {msg} ({src})"
    return title[:MAX_TITLE_LEN]


def file_upstream_issues(
    proposals_dir: Path,
    repo: str = "servo/servo",
    token: Optional[str] = None,
    dry_run: bool = False,
    signatures_path: Optional[Path] = None,
    history_path: Optional[Path] = None,
    find_existing: Optional[FindIssueFn] = None,
    create_issue: Optional[CreateIssueFn] = None,
) -> list[dict[str, Any]]:
    """File upstream GitHub issues for proposals and update signature databases."""
    proposals_dir = Path(proposals_dir)
    if not proposals_dir.is_dir():
        return []

    auth_token = (
        token
        or os.environ.get("UPSTREAM_ISSUE_TOKEN")
        or os.environ.get("GITHUB_TOKEN")
        or os.environ.get("GH_TOKEN")
    )
    lookup = find_existing or (lambda msg, src: find_upstream_issue(msg, src, repo=repo, token=auth_token))
    poster = create_issue or create_github_issue

    sig_path = Path(signatures_path) if signatures_path else None
    hist_path = Path(history_path) if history_path else None
    existing_sigs = load_signatures(sig_path) if sig_path and sig_path.exists() else []
    history_sigs = load_signatures(hist_path) if hist_path and hist_path.exists() else []
    sigs_changed = False
    hist_changed = False

    records: list[dict[str, Any]] = []

    for prop_dir in sorted(p for p in proposals_dir.iterdir() if p.is_dir()):
        manifest_file = prop_dir / PROPOSAL_MANIFEST
        if not manifest_file.is_file():
            continue
        prop = json.loads(manifest_file.read_text(encoding="utf-8"))
        sig: CrashSignature = prop["signature"]
        status = prop.get("status", "new")
        msg = sig["message"]
        src = sig["source_file"]
        regresses_issue = prop.get("regresses_issue")

        if status == "existing_open" and prop.get("issue"):
            issue_url = prop["issue"]
            if not dry_run and sig_path and _append_unique_signature(existing_sigs, sig, issue_url):
                sigs_changed = True
            records.append({"id": sig["id"], "status": status, "action": "linked_existing", "issue": issue_url})
            continue

        # Final idempotency check before creating a new issue
        live_match = lookup(msg, src)
        if live_match and live_match.get("state") == "open":
            issue_url = live_match["url"]
            if not dry_run and sig_path and _append_unique_signature(existing_sigs, sig, issue_url):
                sigs_changed = True
            records.append({"id": sig["id"], "status": status, "action": "linked_existing", "issue": issue_url})
            continue
        if live_match and live_match.get("state") == "closed" and not regresses_issue:
            regresses_issue = live_match["url"]
            status = "regression"

        already_known = find_matching_signature_entry(msg, src, existing_sigs)
        if already_known:
            records.append({
                "id": sig["id"],
                "status": status,
                "action": "already_known",
                "issue": already_known["issue"],
            })
            continue

        issue_md_path = prop_dir / "issue.md"
        issue_md = issue_md_path.read_text(encoding="utf-8", errors="replace") if issue_md_path.is_file() else ""
        title = _extract_issue_title(issue_md, msg, src, is_regression=(status == "regression"))
        body = re.sub(r"^### Title:.*\n+", "", issue_md, count=1).strip() if issue_md else f"Panic in `{src}`: `{msg}`"
        if regresses_issue and regresses_issue not in body:
            body = f"**Regresses**: {regresses_issue}\n\n{body}"
        body = body[:MAX_BODY_LEN]

        if dry_run:
            records.append({"id": sig["id"], "status": status, "action": "dry_run", "issue": None, "title": title})
            continue

        if not auth_token:
            raise RuntimeError("GitHub token is required to file upstream issues unless --dry-run is set")

        issue_url = poster(repo, title, body, list(DEFAULT_ISSUE_LABELS), auth_token)
        print(f"Filed issue for {sig['id']}: {issue_url}")

        if sig_path and _append_unique_signature(existing_sigs, sig, issue_url):
            sigs_changed = True
        if status == "regression" and regresses_issue and hist_path:
            if _append_unique_signature(history_sigs, sig, regresses_issue):
                hist_changed = True

        records.append({"id": sig["id"], "status": status, "action": "filed", "issue": issue_url})

    if not dry_run:
        if sigs_changed and sig_path:
            save_signatures(sig_path, existing_sigs)
        if hist_changed and hist_path:
            save_signatures(hist_path, history_sigs)

    return records


def format_filing_summary(records: list[dict[str, Any]], repo: str) -> str:
    lines = [
        f"### New Panic Upstream Issue Filing (`{repo}`)",
        "",
        "| Proposal | Classification | Action | Issue |",
        "| --- | --- | --- | --- |",
    ]
    if not records:
        lines.append("| (none) | - | skipped | - |")
    else:
        for rec in records:
            issue = rec.get("issue") or "-"
            issue_col = f"[{issue}]({issue})" if issue.startswith("http") else issue
            lines.append(f"| `{rec['id']}` | `{rec['status']}` | `{rec['action']}` | {issue_col} |")
    return "\n".join(lines) + "\n"
