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
    data = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("schema_version"), int)
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
    data = {
        "schema_version": 1,
        "signatures": signatures,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def match(crash: CrashInfo, signatures: list[CrashSignature]) -> Optional[str]:
    crash_norm = normalize_source_path(crash.source_file)
    for entry in signatures:
        if entry["message"] != crash.message:
            continue
        if entry["source_file"] != crash.source_file and normalize_source_path(entry["source_file"]) != crash_norm:
            continue

        frames = iter(crash.stack_frames)
        if all(any(actual == wanted or wanted in actual for actual in frames) for wanted in entry["stack_frames"]):
            return entry["id"]

    return None


def find_signature_by_id(signatures: list[CrashSignature], signature_id: str) -> Optional[CrashSignature]:
    return next((s for s in signatures if s["id"] == signature_id), None)


def find_signature_by_issue(signatures: list[CrashSignature], issue: str) -> Optional[CrashSignature]:
    issue_target = f"/{issue.lstrip('#')}"
    return next((s for s in signatures if s["issue"] == issue or s["issue"].rstrip("/").endswith(issue_target)), None)
