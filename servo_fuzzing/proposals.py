"""Turn the new panics of a triage run into reviewable files: a pending signature
and an evidence pack each. A workflow commits them as one pull request per panic."""

from __future__ import annotations

import re
import shutil
import urllib.parse
from pathlib import Path
from typing import Callable, Optional

from .bisect import _get_json
from .evidence import Evidence, write_evidence_pack
from .parser import normalize_panic_message, normalize_source_path
from .signatures import CrashSignature, load_signatures, save_pending_signature

BRANCH_PREFIX = "new-panic/"
PENDING_SUBDIR = Path("signatures") / "pending"
REPORTS_SUBDIR = Path("reports")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

Gather = Callable[[Path], Optional[Evidence]]
FindExisting = Callable[[str], Optional[str]]


def existing_proposal(repo: str, signature_id: str, token: Optional[str] = None) -> Optional[str]:
    """URL of the pull request (open, merged or closed) that proposed `signature_id`, if any."""
    owner = repo.split("/")[0]
    head = urllib.parse.quote(f"{owner}:{BRANCH_PREFIX}{signature_id}")
    pulls = _get_json(f"https://api.github.com/repos/{repo}/pulls?state=all&head={head}&per_page=1", token)
    return pulls[0]["html_url"] if isinstance(pulls, list) and pulls else None


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

    A draft is left out when `find_existing` returns a pull request for its id, its
    reproducer is missing, or the reproducer no longer shows the drafted panic.
    """
    out_dir = Path(out_dir)
    shutil.rmtree(out_dir, ignore_errors=True)
    drafts_path = Path(drafts_path)
    if not drafts_path.is_file():
        print(f"No draft signatures at {drafts_path}; nothing to propose.")
        return []

    proposed: list[CrashSignature] = []
    for draft in load_signatures(drafts_path):
        signature_id = draft["id"]
        if limit is not None and len(proposed) >= limit:
            print(f"Limit of {limit} proposal(s) reached; {signature_id} is left for a later run.")
            continue
        if not _ID_RE.match(signature_id):
            print(f"Skipping {signature_id!r}: not usable as a file and branch name.")
            continue
        existing = find_existing(signature_id) if find_existing else None
        if existing:
            print(f"Skipping {signature_id}: already proposed in {existing}")
            continue
        testcase = Path(corpus_dir) / f"{signature_id}.html"
        if not testcase.is_file():
            print(f"Skipping {signature_id}: no reproducer at {testcase}.")
            continue

        evidence = gather(testcase.resolve())
        crash = evidence.crash if evidence else None
        if (
            crash is None
            or normalize_panic_message(crash.message) != normalize_panic_message(draft["message"])
            or normalize_source_path(crash.source_file) != normalize_source_path(draft["source_file"])
        ):
            print(f"Skipping {signature_id}: {testcase.name} did not reproduce the drafted panic.")
            continue

        report_dir = out_dir / REPORTS_SUBDIR / signature_id
        write_evidence_pack(evidence, report_dir)

        entry: CrashSignature = dict(draft)  # type: ignore[assignment]
        if entry["issue"].endswith("/TODO"):
            entry["issue"] = report_url(repo, signature_id)
        save_pending_signature(out_dir / PENDING_SUBDIR, entry)
        proposed.append(entry)
        print(f"Proposed {signature_id}: {report_dir}")
    return proposed
