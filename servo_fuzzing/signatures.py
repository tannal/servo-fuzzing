from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Callable, Optional, TypedDict

from .parser import CrashInfo, generate_descriptive_name, normalize_panic_message, normalize_source_path


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
        if not isinstance(frames, list) or not all(isinstance(f, str) and f.strip() for f in frames):
            raise ValueError("Signature needs a stack_frames list of nonempty strings")
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
    """Return the id of the first signature whose message, source file and frames fit `crash`.

    `stack_frames` must appear in order in the backtrace; an empty list places no
    constraint on it.
    """
    crash_norm = normalize_source_path(crash.source_file)
    crash_msg_norm = normalize_panic_message(crash.message)
    for entry in signatures:
        if entry["message"] != crash.message and normalize_panic_message(entry["message"]) != crash_msg_norm:
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


def default_fixed_history_path(signatures_path: Path) -> Path:
    return Path(signatures_path).with_name("fixed-history.json")


def find_matching_signature_entry(
    message: str,
    source_file: str,
    signatures: list[CrashSignature],
) -> Optional[CrashSignature]:
    msg_norm = normalize_panic_message(message)
    src_norm = normalize_source_path(source_file)
    for s in signatures:
        if (s["message"] == message or normalize_panic_message(s["message"]) == msg_norm) and (
            normalize_source_path(s["source_file"]) == src_norm
        ):
            return s
    return None


def _unique_signature_id(
    draft: dict,
    msg: str,
    src: str,
    existing_sigs: list[CrashSignature],
    taken: Callable[[str], bool] = lambda _id: False,
) -> str:
    draft_id = str(draft.get("id", "")).strip()
    if not draft_id or draft_id.startswith(("panic-", "sign-", "sig-")):
        base_id = generate_descriptive_name(CrashInfo(message=msg, source_file=src))
    else:
        base_id = draft_id
    sig_id = base_id
    counter = 1
    while any(s["id"] == sig_id for s in existing_sigs) or taken(sig_id):
        sig_id = f"{base_id}-{counter}"
        counter += 1
    return sig_id


def _save_corpus_repro(
    crash_dir: Path,
    corpus_path: Path,
    entry: CrashSignature,
    reduce_binary: Optional[str | Path],
    reduce_timeout: float,
    args: list[str],
) -> None:
    repro_files = [
        f for f in crash_dir.iterdir()
        if f.is_file() and (f.suffix.lower() in (".html", ".htm") or f.name.endswith(".html"))
    ]
    if not repro_files:
        return
    repro_file = repro_files[0]
    target_html = corpus_path / f"{entry['id']}.html"
    if reduce_binary and Path(reduce_binary).exists():
        print(f"Minimizing reproducer for {entry['id']} -> {target_html.name}...")
        try:
            from .reducer import reduce_delta_lines
            reduce_delta_lines(
                binary=reduce_binary,
                testcase=repro_file,
                target_signature=entry,
                output_path=target_html,
                timeout=reduce_timeout,
                servo_args=args,
            )
        except Exception as e:
            print(f"Reduction failed ({e}), saving original reproducer: {target_html.name}")
            shutil.copy2(repro_file, target_html)
    else:
        shutil.copy2(repro_file, target_html)


def _load_corpus_drafts(corpus_path: Path) -> list[CrashSignature]:
    """Drafts stored beside the reproducers of earlier runs as `<id>.json`."""
    entries: list[CrashSignature] = []
    for sidecar in sorted(corpus_path.glob("*.json")):
        try:
            entry = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(entry, dict) and entry.get("id") == sidecar.stem and entry.get("message") and entry.get("source_file"):
            entries.append(entry)
    return entries


def default_drafts_path(triage_dir: Path) -> Path:
    return Path(triage_dir) / "draft-signatures.json"


def prune_fixed_corpus(
    corpus_dir: str | Path,
    history_path: str | Path,
    *,
    dry_run: bool = False,
) -> list[Path]:
    """Delete corpus entries whose issue is closed upstream.

    `history_path` holds one signature per closed issue, so a reproducer named after
    one of those ids is no longer evidence of a live bug. Both corpus layouts are
    handled: `<id>.html` / `<id>.json` from `import` and `<id>/` from `collect`.
    Directories left empty are removed as well.
    """
    corpus = Path(corpus_dir)
    if not corpus.is_dir():
        return []

    history = Path(history_path)
    fixed_ids = {signature["id"] for signature in load_signatures(history)} if history.is_file() else set()

    removed: list[Path] = []
    for sig_id in sorted(fixed_ids):
        for candidate in (corpus / f"{sig_id}.html", corpus / f"{sig_id}.json", corpus / sig_id):
            if not candidate.exists():
                continue
            removed.append(candidate)
            if dry_run:
                continue
            if candidate.is_dir():
                shutil.rmtree(candidate)
            else:
                candidate.unlink()

    if not dry_run:
        for leftover in sorted(corpus.iterdir()):
            if leftover.is_dir() and not any(leftover.iterdir()):
                leftover.rmdir()

    return removed


def sync_draft_signatures(
    triage_dir: Path,
    signatures_path: Path,
    corpus_out_dir: Optional[Path] = None,
    drafts_path: Optional[Path] = None,
    reduce_binary: Optional[str | Path] = None,
    reduce_timeout: float = 2.0,
    servo_args: Optional[list[str]] = None,
    fixed_history_path: Optional[Path] = None,
    lookup_github: bool = False,
    repo: str = "servo/servo",
    token: Optional[str] = None,
) -> list[CrashSignature]:
    """Deduplicate the new crashes of a triage run into `drafts_path`.

    The known signatures and the fixed history are only read: they are regenerated
    from upstream issues by `sync-issues`, so a crash becomes known once its issue
    is filed with the B-fuzzing label.

    `drafts_path` describes this run only and is removed when the run has no new
    panic. `corpus_out_dir` may be shared between runs: each reproducer `<id>.html`
    has its draft beside it as `<id>.json`, so a panic seen before keeps its id and
    a different panic never takes over an existing file.
    """
    signatures_path = Path(signatures_path)
    drafts_path = Path(drafts_path) if drafts_path else default_drafts_path(triage_dir)
    history_path = Path(fixed_history_path) if fixed_history_path else default_fixed_history_path(signatures_path)
    existing_sigs = load_signatures(signatures_path) if signatures_path.exists() else []
    fixed_sigs = load_signatures(history_path) if history_path.exists() else []
    new_crashes_dir = Path(triage_dir) / "new_crashes"

    drafts_path.unlink(missing_ok=True)
    if not new_crashes_dir.is_dir():
        return []

    corpus_path = Path(corpus_out_dir).resolve() if corpus_out_dir else None
    corpus_drafts: list[CrashSignature] = []
    if corpus_path:
        corpus_path.mkdir(parents=True, exist_ok=True)
        corpus_drafts = _load_corpus_drafts(corpus_path)

    def id_taken_in_corpus(sig_id: str) -> bool:
        return corpus_path is not None and any((corpus_path / f"{sig_id}{ext}").exists() for ext in (".html", ".json"))

    added: list[CrashSignature] = []
    seen_regressions: set[tuple[str, str]] = set()
    args = servo_args if servo_args is not None else ["-xfz", "--enable-experimental-web-platform-features"]

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

        if find_matching_signature_entry(msg, src, existing_sigs):
            continue

        fixed_match = find_matching_signature_entry(msg, src, fixed_sigs)
        if fixed_match:
            reg_key = (normalize_panic_message(msg), src)
            if reg_key not in seen_regressions:
                seen_regressions.add(reg_key)
                print(f"[REGRESSION] {fixed_match['id']} matches previously closed issue: {fixed_match['issue']}")
                if corpus_path:
                    _save_corpus_repro(crash_dir, corpus_path, fixed_match, reduce_binary, reduce_timeout, args)
            continue

        issue_url = draft.get("issue") or "https://github.com/servo/servo/issues/TODO"
        if lookup_github and issue_url.endswith("/TODO"):
            from .github_sync import find_upstream_issue
            upstream = find_upstream_issue(msg, src, repo=repo, token=token)
            if upstream:
                issue_url = upstream["url"]
                if upstream["state"] == "closed":
                    print(f"[REGRESSION] Panic matches closed upstream issue: {issue_url}")
                else:
                    print(f"[DEDUP] Linked panic to existing open upstream issue: {issue_url}")

        earlier = find_matching_signature_entry(msg, src, corpus_drafts)
        if earlier:
            sig_id = earlier["id"]
        else:
            sig_id = _unique_signature_id(draft, msg, src, existing_sigs + fixed_sigs, id_taken_in_corpus)
        new_entry: CrashSignature = {
            "id": sig_id,
            "message": msg,
            "source_file": src,
            "stack_frames": draft.get("stack_frames") or [],
            "issue": issue_url,
        }
        existing_sigs.append(new_entry)
        added.append(new_entry)
        print(f"New panic signature draft: {sig_id} ({src}) -> {issue_url}")

        if corpus_path:
            _save_corpus_repro(crash_dir, corpus_path, new_entry, reduce_binary, reduce_timeout, args)
            (corpus_path / f"{sig_id}.json").write_text(json.dumps(new_entry, indent=2) + "\n", encoding="utf-8")

    if added:
        save_signatures(drafts_path, added)
        print(f"Wrote {len(added)} draft signature(s) to {drafts_path}")

    return added
