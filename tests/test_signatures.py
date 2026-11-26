"""Tests for signature loading, validation, and matching."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from servo_fuzzing.parser import CrashInfo
from servo_fuzzing.signatures import (
    CrashSignature,
    find_signature_by_id,
    find_signature_by_issue,
    load_signatures,
    match,
    save_signatures,
    sync_draft_signatures,
)


class TestSignatures(unittest.TestCase):
    def setUp(self):
        self.signatures: list[CrashSignature] = [
            {
                "id": "windowproxy-parent-active",
                "message": "assertion failed: parent_proxy.currently_active().is_some()",
                "source_file": "components/script/dom/window/windowproxy.rs",
                "stack_frames": [
                    "<script::dom::servoparser::ParserContext>::initialize_document_object"
                ],
                "issue": "https://github.com/servo/servo/issues/49411",
            },
            {
                "id": "multi-frame-sig",
                "message": "test panic",
                "source_file": "components/test.rs",
                "stack_frames": ["frame_a", "frame_b"],
                "issue": "https://github.com/servo/servo/issues/123",
            },
        ]

    def test_load_valid_signatures(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            data = {"schema_version": 1, "signatures": self.signatures}
            json.dump(data, f)
            path = Path(f.name)

        loaded = load_signatures(path)
        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[0]["id"], "windowproxy-parent-active")
        path.unlink()

    def test_load_invalid_schema(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            # schema_version wrong
            json.dump({"schema_version": 2, "signatures": []}, f)
            path = Path(f.name)

        with self.assertRaises(ValueError):
            load_signatures(path)
        path.unlink()

    def test_duplicate_signature_id(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            sigs = [self.signatures[0], self.signatures[0]]
            json.dump({"schema_version": 1, "signatures": sigs}, f)
            path = Path(f.name)

        with self.assertRaises(ValueError):
            load_signatures(path)
        path.unlink()

    def test_match_exact_and_ordered_frames(self):
        # Matching crash
        crash = CrashInfo(
            message="assertion failed: parent_proxy.currently_active().is_some()",
            source_file="components/script/dom/window/windowproxy.rs",
            stack_frames=[
                "servoshell::panic_hook",
                "<script::dom::servoparser::ParserContext>::initialize_document_object",
                "main",
            ],
        )
        matched_id = match(crash, self.signatures)
        self.assertEqual(matched_id, "windowproxy-parent-active")

    def test_match_ordered_stack_frames(self):
        # Correct order
        crash_good = CrashInfo(
            message="test panic",
            source_file="components/test.rs",
            stack_frames=["first", "frame_a", "middle", "frame_b", "last"],
        )
        self.assertEqual(match(crash_good, self.signatures), "multi-frame-sig")

        # Inverted order: should NOT match
        crash_bad = CrashInfo(
            message="test panic",
            source_file="components/test.rs",
            stack_frames=["first", "frame_b", "middle", "frame_a", "last"],
        )
        self.assertIsNone(match(crash_bad, self.signatures))

    def test_mismatched_message_or_file(self):
        # Mismatched message
        c1 = CrashInfo(
            message="different message",
            source_file="components/script/dom/window/windowproxy.rs",
            stack_frames=["<script::dom::servoparser::ParserContext>::initialize_document_object"],
        )
        self.assertIsNone(match(c1, self.signatures))

        # Mismatched file
        c2 = CrashInfo(
            message="assertion failed: parent_proxy.currently_active().is_some()",
            source_file="components/other/file.rs",
            stack_frames=["<script::dom::servoparser::ParserContext>::initialize_document_object"],
        )
        self.assertIsNone(match(c2, self.signatures))

    def test_find_helpers(self):
        sig = find_signature_by_id(self.signatures, "windowproxy-parent-active")
        self.assertIsNotNone(sig)
        assert sig is not None
        self.assertEqual(sig["issue"], "https://github.com/servo/servo/issues/49411")

        by_issue = find_signature_by_issue(self.signatures, "49411")
        self.assertIsNotNone(by_issue)
        assert by_issue is not None
        self.assertEqual(by_issue["id"], "windowproxy-parent-active")

    def test_sync_draft_signatures_deduplication(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            sigs_file = tmp / "rust-panic.json"
            save_signatures(sigs_file, self.signatures)

            triage_dir = tmp / "triage"
            new_crashes = triage_dir / "new_crashes"
            # Create 3 crash dirs, all with the SAME panic message and source file
            for i in range(1, 4):
                c_dir = new_crashes / f"crash_{i}"
                c_dir.mkdir(parents=True)
                (c_dir / f"test_{i}.html").write_text(f"<html>test {i}</html>")
                draft = {
                    "id": "stacking_context-hoisted-box",
                    "message": "Found hoisted box with missing BoxFragment",
                    "source_file": "components/layout/display_list/stacking_context.rs",
                    "stack_frames": ["stacking_context"],
                    "issue": "https://github.com/servo/servo/issues/TODO",
                }
                (c_dir / "signature_draft.json").write_text(json.dumps(draft))

            corpus_dir = tmp / "corpus"
            added = sync_draft_signatures(triage_dir, sigs_file, corpus_out_dir=corpus_dir)
            # Only ONE draft despite 3 crash dirs, and the known signatures are untouched
            self.assertEqual(len(added), 1)
            self.assertEqual(load_signatures(sigs_file), self.signatures)
            drafts = load_signatures(triage_dir / "draft-signatures.json")
            self.assertEqual([d["message"] for d in drafts], ["Found hoisted box with missing BoxFragment"])

            # Only ONE reproducer html in corpus_dir
            repros = list(corpus_dir.glob("*.html"))
            self.assertEqual(len(repros), 1)

    def test_sync_draft_signatures_detects_fixed_history_regression(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            sigs_file = tmp / "rust-panic.json"
            history_file = tmp / "fixed-history.json"
            save_signatures(sigs_file, self.signatures)
            save_signatures(history_file, [
                {
                    "id": "stacking-context-hoisted-box-missing",
                    "message": "internal error: entered unreachable code: Found hoisted box with missing or non-BoxFragment.",
                    "source_file": "components/layout/display_list/stacking_context.rs",
                    "stack_frames": ["stacking_context"],
                    "issue": "https://github.com/servo/servo/issues/46109",
                }
            ])

            triage_dir = tmp / "triage"
            c_dir = triage_dir / "new_crashes" / "crash_1"
            c_dir.mkdir(parents=True)
            (c_dir / "test_1.html").write_text("<html>hoisted</html>")
            (c_dir / "signature_draft.json").write_text(json.dumps({
                "id": "stacking-context-hoisted-box-missing",
                "message": "internal error: entered unreachable code: Found hoisted box with missing or non-BoxFragment.",
                "source_file": "components/layout/display_list/stacking_context.rs",
                "stack_frames": ["stacking_context"],
                "issue": "https://github.com/servo/servo/issues/TODO",
            }))

            corpus_dir = tmp / "corpus"
            added = sync_draft_signatures(
                triage_dir,
                sigs_file,
                corpus_out_dir=corpus_dir,
                fixed_history_path=history_file,
            )
            # A crash matching a closed issue is a regression, not a new draft
            self.assertEqual(len(added), 0)
            self.assertEqual(len(load_signatures(sigs_file)), len(self.signatures))
            self.assertFalse((triage_dir / "draft-signatures.json").exists())
            # Reproducer is still saved under the historical signature ID
            self.assertTrue((corpus_dir / "stacking-context-hoisted-box-missing.html").exists())

    def test_sync_draft_signatures_links_upstream_closed_issue(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            sigs_file = tmp / "rust-panic.json"
            history_file = tmp / "fixed-history.json"
            save_signatures(sigs_file, self.signatures)
            save_signatures(history_file, [])

            triage_dir = tmp / "triage"
            c_dir = triage_dir / "new_crashes" / "crash_1"
            c_dir.mkdir(parents=True)
            (c_dir / "test_1.html").write_text("<html>hoisted</html>")
            (c_dir / "signature_draft.json").write_text(json.dumps({
                "id": "stacking-context-hoisted-box-missing",
                "message": "internal error: entered unreachable code: Found hoisted box with missing or non-BoxFragment.",
                "source_file": "components/layout/display_list/stacking_context.rs",
                "stack_frames": ["stacking_context"],
                "issue": "https://github.com/servo/servo/issues/TODO",
            }))

            with patch("servo_fuzzing.github_sync.find_upstream_issue", return_value={
                "url": "https://github.com/servo/servo/issues/46109",
                "state": "closed",
                "number": "46109",
                "title": "Found hoisted box with missing or non-BoxFragment",
            }):
                drafts_file = tmp / "drafts.json"
                added = sync_draft_signatures(
                    triage_dir,
                    sigs_file,
                    drafts_path=drafts_file,
                    fixed_history_path=history_file,
                    lookup_github=True,
                )

            # The draft carries the upstream link; the synced files stay as they were
            self.assertEqual([s["issue"] for s in added], ["https://github.com/servo/servo/issues/46109"])
            self.assertEqual(load_signatures(drafts_file), added)
            self.assertEqual(load_signatures(history_file), [])
            self.assertEqual(load_signatures(sigs_file), self.signatures)

    def test_sync_draft_signatures_include_regressions_emits_draft(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            sigs_file = tmp / "rust-panic.json"
            history_file = tmp / "fixed-history.json"
            save_signatures(sigs_file, self.signatures)
            save_signatures(history_file, [
                {
                    "id": "issue-46109",
                    "message": "internal error: entered unreachable code: Found hoisted box with missing or non-BoxFragment.",
                    "source_file": "components/layout/display_list/stacking_context.rs",
                    "stack_frames": ["stacking_context"],
                    "issue": "https://github.com/servo/servo/issues/46109",
                }
            ])

            triage_dir = tmp / "triage"
            c_dir = triage_dir / "new_crashes" / "test_1"
            c_dir.mkdir(parents=True)
            (c_dir / "test_1.html").write_text("<html>hoisted</html>")
            (c_dir / "signature_draft.json").write_text(json.dumps({
                "id": "stacking-context-hoisted-box-missing",
                "message": "internal error: entered unreachable code: Found hoisted box with missing or non-BoxFragment.",
                "source_file": "components/layout/display_list/stacking_context.rs",
                "stack_frames": ["stacking_context"],
                "issue": "https://github.com/servo/servo/issues/46109",
            }))

            corpus_dir = tmp / "corpus"
            added = sync_draft_signatures(
                triage_dir,
                sigs_file,
                corpus_out_dir=corpus_dir,
                fixed_history_path=history_file,
                include_regressions=True,
            )
            self.assertEqual(len(added), 1)
            self.assertEqual(added[0]["id"], "issue-46109")
            self.assertEqual(added[0]["issue"], "https://github.com/servo/servo/issues/46109")
            self.assertTrue((corpus_dir / "issue-46109.html").is_file())
            self.assertTrue((corpus_dir / "issue-46109.json").is_file())
            self.assertEqual(load_signatures(triage_dir / "draft-signatures.json"), added)

    def test_save_corpus_repro_skips_reduction_for_flaky_crash(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            sigs_file = tmp / "rust-panic.json"
            save_signatures(sigs_file, [])
            fake_bin = tmp / "servoshell"
            fake_bin.write_text("#!/bin/sh\nexit 0\n")

            triage_dir = tmp / "triage"
            c_dir = triage_dir / "new_crashes" / "flaky_1"
            c_dir.mkdir(parents=True)
            (c_dir / "flaky_1.html").write_text("<html>flaky</html>")
            (c_dir / "signature_draft.json").write_text(json.dumps({
                "id": "flaky-panic",
                "message": "flaky panic",
                "source_file": "components/layout/flaky.rs",
                "stack_frames": ["flaky_fn"],
                "issue": "https://github.com/servo/servo/issues/TODO",
            }))

            corpus_dir = tmp / "corpus"
            with (
                patch("servo_fuzzing.signatures._probe_reproducibility", return_value=1),
                patch("servo_fuzzing.reducer.reduce_delta_lines", side_effect=AssertionError("should not reduce")),
            ):
                added = sync_draft_signatures(
                    triage_dir,
                    sigs_file,
                    corpus_out_dir=corpus_dir,
                    reduce_binary=fake_bin,
                )
            self.assertEqual(len(added), 1)
            self.assertEqual((corpus_dir / "flaky-panic.html").read_text(), "<html>flaky</html>")


class TestDraftsAcrossRuns(unittest.TestCase):
    """`draft-signatures.json` describes one run; the reproducer corpus is shared between runs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sigs_file = self.root / "rust-panic.json"
        save_signatures(self.sigs_file, [])
        self.corpus = self.root / "corpus"
        self.runs = 0

    def run_triage(self, *messages, drafts_path=None):
        self.runs += 1
        triage = self.root / "triage"
        new_crashes = triage / "new_crashes"
        shutil.rmtree(new_crashes, ignore_errors=True)
        for i, message in enumerate(messages):
            crash_dir = new_crashes / f"crash_{i}"
            crash_dir.mkdir(parents=True)
            (crash_dir / "test.html").write_text(f"<p>run {self.runs}: {message}</p>")
            (crash_dir / "signature_draft.json").write_text(
                json.dumps(
                    {
                        "id": "panic-123",
                        "message": message,
                        "source_file": "components/layout/flow.rs",
                        "stack_frames": ["layout::flow::layout"],
                    }
                )
            )
        return sync_draft_signatures(triage, self.sigs_file, corpus_out_dir=self.corpus, drafts_path=drafts_path)

    def test_different_panics_with_the_same_name_keep_separate_reproducers(self):
        first = self.run_triage("assertion failed: expected block size positive")
        second = self.run_triage("assertion failed: expected block size nonnegative")

        self.assertNotEqual(first[0]["id"], second[0]["id"])
        self.assertIn("size positive", (self.corpus / f"{first[0]['id']}.html").read_text())
        self.assertIn("size nonnegative", (self.corpus / f"{second[0]['id']}.html").read_text())
        self.assertEqual(json.loads((self.corpus / f"{second[0]['id']}.json").read_text()), second[0])

    def test_same_panic_keeps_its_id_in_a_later_run(self):
        first = self.run_triage("assertion failed: expected block size positive")
        self.run_triage("assertion failed: expected block size nonnegative")
        again = self.run_triage("assertion failed: expected block size positive")

        self.assertEqual(again[0]["id"], first[0]["id"])
        self.assertEqual(len(list(self.corpus.glob("*.html"))), 2)
        self.assertIn("run 3", (self.corpus / f"{first[0]['id']}.html").read_text())

    def test_reproducer_without_a_draft_is_not_overwritten(self):
        first = self.run_triage("assertion failed: expected block size positive")
        (self.corpus / f"{first[0]['id']}.json").unlink()
        second = self.run_triage("assertion failed: expected block size nonnegative")

        self.assertNotEqual(first[0]["id"], second[0]["id"])
        self.assertIn("size positive", (self.corpus / f"{first[0]['id']}.html").read_text())

    def test_run_without_new_panics_removes_earlier_drafts(self):
        for drafts_path in (None, self.root / "elsewhere" / "drafts.json"):
            written = drafts_path or self.root / "triage" / "draft-signatures.json"
            self.run_triage("assertion failed: expected block size positive", drafts_path=drafts_path)
            self.assertTrue(written.exists())
            self.assertEqual(self.run_triage(drafts_path=drafts_path), [])
            self.assertFalse(written.exists())


if __name__ == "__main__":
    unittest.main()
