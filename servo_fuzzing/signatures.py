from __future__ import annotations

import json
import shutil
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


def sync_draft_signatures(
    triage_dir: Path,
    signatures_path: Path,
    corpus_out_dir: Optional[Path] = None,
    reduce_binary: Optional[str | Path] = None,
    reduce_timeout: float = 2.0,
) -> list[CrashSignature]:
    signatures_path = Path(signatures_path)
    existing_sigs = load_signatures(signatures_path) if signatures_path.exists() else []
    new_crashes_dir = Path(triage_dir) / "new_crashes"

    if not new_crashes_dir.is_dir():
        return []

    corpus_path = Path(corpus_out_dir).resolve() if corpus_out_dir else None
    if corpus_path:
        corpus_path.mkdir(parents=True, exist_ok=True)

    added: list[CrashSignature] = []

    for crash_dir in sorted(new_crashes_dir.iterdir()):
        if not crash_dir.is_dir():
            continue
        draft_file = crash_dir / "signature_draft.json"
        if not draft_file.is_file():
            continue

        try:
            draft = json.loads(draft_file.read_text(encoding="utf-8"))
        except Exception:
            continue

        msg = draft.get("message", "").strip()
        src = normalize_source_path(draft.get("source_file", "").strip())
        if not msg or not src:
            continue

        is_dup = any(
            s["message"] == msg and normalize_source_path(s["source_file"]) == src
            for s in existing_sigs
        )
        if is_dup:
            continue

        base_id = draft.get("id") or f"panic-{Path(src).stem}"
        sig_id = base_id
        counter = 1
        while any(s["id"] == sig_id for s in existing_sigs):
            sig_id = f"{base_id}-{counter}"
            counter += 1

        new_entry: CrashSignature = {
            "id": sig_id,
            "message": msg,
            "source_file": src,
            "stack_frames": draft.get("stack_frames") or ["main"],
            "issue": draft.get("issue") or "https://github.com/servo/servo/issues/TODO",
        }
        existing_sigs.append(new_entry)
        added.append(new_entry)
        print(f"Added new panic signature: {sig_id} ({src})")

        if corpus_path:
            repro_files = list(crash_dir.glob("*.html"))
            if repro_files:
                repro_file = repro_files[0]
                target_html = corpus_path / f"{sig_id}.html"
                if reduce_binary and Path(reduce_binary).exists():
                    print(f"Minimizing reproducer for {sig_id} -> {target_html.name}...")
                    try:
                        from .reducer import reduce_delta_lines
                        reduce_delta_lines(
                            binary=reduce_binary,
                            testcase=repro_file,
                            target_signature=new_entry,
                            output_path=target_html,
                            timeout=reduce_timeout,
                        )
                    except Exception as e:
                        print(f"Reduction failed ({e}), saving original reproducer: {target_html.name}")
                        shutil.copy2(repro_file, target_html)
                else:
                    shutil.copy2(repro_file, target_html)

    if added:
        save_signatures(signatures_path, existing_sigs)

    return added
