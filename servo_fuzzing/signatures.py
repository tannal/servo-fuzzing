"""Conservative exact-message/file and ordered-stack matching.

Loads, validates, saves, and matches crash signatures against the database of
known issues.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional, TypedDict

from .parser import CrashInfo, normalize_source_path


class CrashSignature(TypedDict):
    id: str
    message: str
    source_file: str
    stack_frames: list[str]
    issue: str


def load_signatures(path: Path) -> list[CrashSignature]:
    """Load and validate signatures from a JSON database file."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(data, dict)
        or type(data.get("schema_version")) is not int
        or data["schema_version"] != 1
        or not isinstance(data.get("signatures"), list)
    ):
        raise ValueError("Unsupported signature database")
    seen = set()
    for entry in data["signatures"]:
        if not isinstance(entry, dict):
            raise ValueError("Each signature must be an object")
        for field in ("id", "message", "source_file", "issue"):
            if not isinstance(entry.get(field), str) or not entry[field].strip():
                raise ValueError(f"Signature needs nonempty {field}")
        frames = entry.get("stack_frames")
        if not isinstance(frames, list) or not frames or not all(isinstance(f, str) and f.strip() for f in frames):
            raise ValueError("Signature needs nonempty stack_frames")
        if entry["id"] in seen:
            raise ValueError("Duplicate signature id")
        seen.add(entry["id"])
    return data["signatures"]


def save_signatures(path: Path, signatures: list[CrashSignature]) -> None:
    """Save signatures to a JSON database file adhering to schema version 1."""
    data = {
        "schema_version": 1,
        "signatures": signatures,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def match(crash: CrashInfo, signatures: list[CrashSignature]) -> str | None:
    """Matches a CrashInfo against known signatures.

    Conservative exact-message/file and ordered-stack matching.
    """
    for entry in signatures:
        # Check message and source_file
        # Allow normalized matching in case signatures or crash differ in trailing line numbers
        entry_source = normalize_source_path(entry["source_file"])
        crash_source = normalize_source_path(crash.source_file)

        if entry["message"] != crash.message:
            continue
        if entry["source_file"] != crash.source_file and entry_source != crash_source:
            continue

        frames = iter(crash.stack_frames)
        if all(any(actual == wanted or wanted in actual for actual in frames) for wanted in entry["stack_frames"]):
            return entry["id"]
    return None


def find_signature_by_id(signatures: list[CrashSignature], signature_id: str) -> Optional[CrashSignature]:
    """Find a signature entry by ID."""
    for sig in signatures:
        if sig["id"] == signature_id:
            return sig
    return None


def find_signature_by_issue(signatures: list[CrashSignature], issue: str) -> Optional[CrashSignature]:
    """Find a signature entry by GitHub issue URL or issue number."""
    for sig in signatures:
        if sig["issue"] == issue or sig["issue"].rstrip("/").endswith(f"/{issue.lstrip('#')}"):
            return sig
    return None
