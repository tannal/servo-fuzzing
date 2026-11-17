"""Turn the new panics of a triage run into reviewable files: a pending signature
and an evidence pack each. A workflow commits them as one pull request per panic."""

from __future__ import annotations

import base64
import json
import re
import shutil
import tempfile
import urllib.parse
from pathlib import Path
from typing import Callable, NamedTuple, Optional

from .bisect import _get_json
from .evidence import Evidence, write_evidence_pack
from .parser import normalize_panic_message, normalize_source_path
from .signatures import CrashSignature, load_signatures, save_pending_signature

BRANCH_PREFIX = "new-panic/"
PENDING_SUBDIR = Path("signatures") / "pending"
REPORTS_SUBDIR = Path("reports")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")



class Proposal(NamedTuple):
    url: str
    signature: Optional[dict]
    """The pending signature on the pull request's head, None when it cannot be read."""


Gather = Callable[[Path], Optional[Evidence]]
FindExisting = Callable[[str], Optional[Proposal]]


def existing_proposal(repo: str, signature_id: str, token: Optional[str] = None) -> Optional[Proposal]:
    """The pull request (open, merged or closed) from branch `new-panic/<signature_id>`, if any."""
    owner = repo.split("/")[0]
    head = urllib.parse.quote(f"{owner}:{BRANCH_PREFIX}{signature_id}")
    pulls = _get_json(f"https://api.github.com/repos/{repo}/pulls?state=all&head={head}&per_page=1", token)
    if not isinstance(pulls, list) or not pulls:
        return None
    signature = None
    try:
        path = f"{PENDING_SUBDIR.as_posix()}/{signature_id}.json"
        blob = _get_json(f"https://api.github.com/repos/{repo}/contents/{path}?ref={pulls[0]['head']['sha']}", token)
        signature = json.loads(base64.b64decode(blob["content"]))
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"Warning: could not read the signature proposed in {pulls[0]['html_url']}: {error}")
    return Proposal(pulls[0]["html_url"], signature if isinstance(signature, dict) else None)


def _same_panic(signature: Optional[dict], draft: CrashSignature) -> bool:
    """Whether a proposed signature is for the drafted panic; an unreadable one counts as the same."""
    if signature is None:
        return True
    return normalize_panic_message(str(signature.get("message", ""))) == normalize_panic_message(
        draft["message"]
    ) and normalize_source_path(str(signature.get("source_file", ""))) == normalize_source_path(draft["source_file"])


def report_url(repo: str, signature_id: str) -> str:
    return f"https://github.com/{repo}/tree/main/{REPORTS_SUBDIR.as_posix()}/{signature_id}"


def propose_panics(
    drafts_path: Path,
    corpus_dir: Path,
    out_dir: Path,
    gather: Gather,
    repo: str,
    find_existing: Optional[FindExisting] = None,
    limit: Optional[int] = None,
) -> list[CrashSignature]:
    """Write `signatures/pending/<id>.json` and `reports/<id>/` under `out_dir` for each draft.

    A draft is left out when `find_existing` returns a pull request for the same panic,
    its reproducer is missing, or the reproducer no longer shows the drafted panic.
    Different panics can get the same descriptive id; when the pull request found
    for an id is about another panic, the draft is proposed as `<id>-1`, `<id>-2`, ...
    """
    out_dir = Path(out_dir)
    shutil.rmtree(out_dir, ignore_errors=True)
    drafts_path = Path(drafts_path)
    if not drafts_path.is_file():
        print(f"No draft signatures at {drafts_path}; nothing to propose.")
        return []

    drafts = load_signatures(drafts_path)
    reserved = {draft["id"] for draft in drafts}
    proposed: list[CrashSignature] = []
    for draft in drafts:
        draft_id = draft["id"]
        if limit is not None and len(proposed) >= limit:
            print(f"Limit of {limit} proposal(s) reached; {draft_id} is left for a later run.")
            continue
        if not _ID_RE.match(draft_id):
            print(f"Skipping {draft_id!r}: not usable as a file and branch name.")
            continue

        signature_id, suffix, existing = draft_id, 0, None
        while find_existing:
            existing = find_existing(signature_id)
            if existing is None and (signature_id == draft_id or signature_id not in reserved):
                break
            if existing and _same_panic(existing.signature, draft):
                break
            suffix += 1
            signature_id = f"{draft_id}-{suffix}"
        if existing:
            print(f"Skipping {draft_id}: already proposed in {existing.url}")
            continue
        reserved.add(signature_id)

        reproducer = Path(corpus_dir) / f"{draft_id}.html"
        if not reproducer.is_file():
            print(f"Skipping {draft_id}: no reproducer at {reproducer}.")
            continue

        with tempfile.TemporaryDirectory() as renamed:
            testcase = reproducer.resolve()
            if signature_id != draft_id:
                testcase = Path(shutil.copy2(reproducer, Path(renamed) / f"{signature_id}.html"))
            evidence = gather(testcase)
            crash = evidence.crash if evidence else None
            if (
                crash is None
                or normalize_panic_message(crash.message) != normalize_panic_message(draft["message"])
                or normalize_source_path(crash.source_file) != normalize_source_path(draft["source_file"])
            ):
                print(f"Skipping {draft_id}: {reproducer.name} did not reproduce the drafted panic.")
                continue
            report_dir = out_dir / REPORTS_SUBDIR / signature_id
            write_evidence_pack(evidence, report_dir)

        entry: CrashSignature = dict(draft)  # type: ignore[assignment]
        entry["id"] = signature_id
        if entry["issue"].endswith("/TODO"):
            entry["issue"] = report_url(repo, signature_id)
        save_pending_signature(out_dir / PENDING_SUBDIR, entry)
        proposed.append(entry)
        print(f"Proposed {signature_id}: {report_dir}")
    return proposed
