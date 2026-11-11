"""Preserve new panic reproducers from a local or downloaded run artifact.

Usage: python -m servo_fuzzing.collect --input target/fuzzing --output testcases/corpus
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path


def collect_panics(run_dir: Path, corpus: Path) -> int:
    """Copy new Rust panics, with evidence, using content-addressed names.

    The summary, not directory presence, selects results from this run. Inputs
    come from archived reproducers, so original CI workspace paths are unnecessary.
    This preserves inputs; it does not reproduce, minimize, or ignore the panic.
    """
    run_dir, corpus = Path(run_dir).resolve(), Path(corpus).resolve()
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    copied = 0
    for result in summary["results"]:
        if result["status"] != "NEW_CRASH":
            continue
        name = Path(result["testcase"]).name
        source = run_dir / "new_crashes" / Path(name).stem
        signature = source / "signature_draft.json"
        # A nonzero exit without parsed panic evidence is not a new Rust panic.
        if not signature.is_file():
            continue
        signature_data = json.loads(signature.read_text(encoding="utf-8"))
        if not signature_data.get("message") or not signature_data.get("source_file"):
            continue
        testcase = source / name
        data = testcase.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        destination = corpus / ("panic-" + digest)
        destination.mkdir(parents=True, exist_ok=True)
        saved = destination / "repro.html"
        if not saved.exists():
            saved.write_bytes(data)
            for filename in ("output.log", "signature_draft.json", "github_issue_draft.md"):
                if (source / filename).is_file():
                    shutil.copy2(source / filename, destination / filename)
            (destination / "provenance.json").write_text(json.dumps({
                "testcase": result["testcase"], "sha256": digest,
                "run_summary": str(run_dir / "summary.json"),
                "result": result,
            }, indent=2) + "\n", encoding="utf-8")
            copied += 1
    return copied


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Extracted run directory containing summary.json")
    parser.add_argument("--output", type=Path, required=True, help="Destination corpus directory")
    args = parser.parse_args(argv)
    try:
        count = collect_panics(args.input, args.output)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(2, f"Cannot collect panic corpus: {error}\n")
    print(f"Collected {count} new panic testcase(s) into {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
