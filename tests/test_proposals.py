"""Tests for pending signatures and the per-panic proposals built from a triage run."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from servo_fuzzing.cli import main as cli_main
from servo_fuzzing.evidence import Evidence
from servo_fuzzing.executor import TestResult, TestStatus
from servo_fuzzing.parser import CrashInfo
from servo_fuzzing.proposals import existing_proposal, propose_panics
from servo_fuzzing.signatures import (
    load_known_signatures,
    load_pending_signatures,
    save_pending_signature,
    save_signatures,
    sync_draft_signatures,
)

REPO = "tannal/servo-fuzzing"


def draft(signature_id, message="Not a block context", source="components/layout/formatting_contexts.rs"):
    return {
        "id": signature_id,
        "message": message,
        "source_file": source,
        "stack_frames": ["<layout::table::layout::TableLayout>::layout"],
        "issue": "https://github.com/servo/servo/issues/TODO",
    }


def evidence_for(testcase, message="Not a block context", source="components/layout/formatting_contexts.rs"):
    crash = CrashInfo(
        message=message,
        source_file=source,
        line=699,
        thread="Script#1",
        stack_frames=["<layout::table::layout::TableLayout>::layout"],
    )
    result = TestResult(
        testcase=testcase,
        status=TestStatus.NEW_CRASH,
        exit_code=-6,
        combined_output=f"{message} (thread Script#1, at {source}:699)\n",
        crash_info=crash,
    )
    return Evidence(testcase=testcase, result=result, servo_version="Servo 0.8.0-ec2138dac", attempts=3, crashes=3)


class ProposalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.drafts = self.root / "triage" / "draft-signatures.json"
        self.corpus = self.root / "corpus"
        self.corpus.mkdir()
        self.out = self.root / "proposals"

    def write_drafts(self, *drafts, reproducers=True):
        save_signatures(self.drafts, list(drafts))
        if reproducers:
            for entry in drafts:
                (self.corpus / f"{entry['id']}.html").write_text("<iframe style='display: table-cell'></iframe>\n")

    def propose(self, gather=evidence_for, **kwargs):
        return propose_panics(self.drafts, self.corpus, self.out, gather=gather, repo=REPO, **kwargs)

    def test_writes_pending_signature_and_report(self):
        self.write_drafts(draft("formatting-contexts-not-block"))
        proposed = self.propose()

        self.assertEqual([entry["id"] for entry in proposed], ["formatting-contexts-not-block"])
        pending = load_pending_signatures(self.out / "signatures" / "pending")
        self.assertEqual(pending, proposed)
        self.assertEqual(
            pending[0]["issue"],
            "https://github.com/tannal/servo-fuzzing/tree/main/reports/formatting-contexts-not-block",
        )
        report = self.out / "reports" / "formatting-contexts-not-block"
        for name in ("issue.md", "context.json", "AGENT.md", "analysis.md", "formatting-contexts-not-block.html"):
            self.assertTrue((report / name).is_file(), name)
        self.assertIn("Not a block context", (report / "issue.md").read_text())

    def test_already_proposed_panic_is_not_run_again(self):
        self.write_drafts(draft("known-one"), draft("new-one", message="other"))
        ran = []

        def gather(testcase):
            ran.append(testcase.name)
            return evidence_for(testcase, message="other")

        proposed = self.propose(
            gather=gather,
            find_existing=lambda i: "https://github.com/tannal/servo-fuzzing/pull/20" if i == "known-one" else None,
        )
        self.assertEqual([entry["id"] for entry in proposed], ["new-one"])
        self.assertEqual(ran, ["new-one.html"])
        self.assertFalse((self.out / "reports" / "known-one").exists())

    def test_reproducer_showing_another_panic_is_left_out(self):
        self.write_drafts(draft("flaky"))
        self.assertEqual(self.propose(gather=lambda t: evidence_for(t, message="something else")), [])
        self.assertEqual(self.propose(gather=lambda t: None), [])
        self.assertFalse((self.out / "signatures").exists())

    def test_missing_reproducer_and_unsafe_id_are_left_out(self):
        self.write_drafts(draft("no-reproducer"), draft("../escape"), reproducers=False)
        self.assertEqual(self.propose(), [])

    def test_limit_and_stale_output(self):
        self.write_drafts(draft("a", message="one"), draft("b", message="two"))
        (self.out / "reports" / "stale").mkdir(parents=True)
        proposed = self.propose(gather=lambda t: evidence_for(t, message="one" if t.stem == "a" else "two"), limit=1)
        self.assertEqual([entry["id"] for entry in proposed], ["a"])
        self.assertEqual([p.name for p in (self.out / "reports").iterdir()], ["a"])

    def test_no_drafts_file(self):
        self.assertEqual(self.propose(), [])

    def test_existing_proposal_queries_pull_requests_of_any_state(self):
        with patch("servo_fuzzing.proposals._get_json", return_value=[{"html_url": "https://x/pull/3"}]) as get:
            self.assertEqual(existing_proposal(REPO, "a-b", "t"), "https://x/pull/3")
        url = get.call_args.args[0]
        self.assertIn("/repos/tannal/servo-fuzzing/pulls?state=all&head=tannal%3Anew-panic/a-b", url)
        with patch("servo_fuzzing.proposals._get_json", return_value=[]):
            self.assertIsNone(existing_proposal(REPO, "a-b", None))

    def test_cli_skips_existing_pull_requests_and_validates_the_result(self):
        self.write_drafts(draft("known-one"), draft("new-one", message="other"))
        calls = []

        def gather(**kwargs):
            calls.append(kwargs)
            return evidence_for(kwargs["testcase"], message="other")

        with patch("servo_fuzzing.cli.gather_evidence", side_effect=gather), patch(
            "servo_fuzzing.cli.existing_proposal",
            side_effect=lambda repo, i, token: "https://x/pull/3" if i == "known-one" else None,
        ):
            code = cli_main([
                "propose", "--drafts", str(self.drafts), "--corpus", str(self.corpus), "--output", str(self.out),
                "--binary", "servoshell", "--repo", REPO, "--skip-proposed", "--bisect",
            ])
        self.assertEqual(code, 0)
        self.assertEqual([c["testcase"].name for c in calls], ["new-one.html"])
        self.assertTrue(calls[0]["bisect"])
        self.assertEqual(calls[0]["servo_args"], ["-xfz", "--enable-experimental-web-platform-features"])
        self.assertEqual(cli_main(["validate-signatures", "--signatures", str(self.out / "signatures" / "pending")]), 0)


class PendingSignatureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.signatures = self.root / "signatures" / "rust-panic.json"
        self.pending = self.signatures.parent / "pending"
        upstream = draft("issue-1", message="upstream panic", source="components/a.rs")
        upstream["issue"] = "https://github.com/servo/servo/issues/1"
        save_signatures(self.signatures, [upstream])

    def test_known_signatures_include_pending_ones(self):
        self.assertEqual([s["id"] for s in load_known_signatures(self.signatures)], ["issue-1"])
        save_pending_signature(self.pending, draft("local-panic"))
        (self.pending / "README.md").write_text("not a signature")
        self.assertEqual([s["id"] for s in load_known_signatures(self.signatures)], ["issue-1", "local-panic"])

    def test_pending_file_name_must_be_the_id(self):
        self.pending.mkdir()
        (self.pending / "other.json").write_text(json.dumps(draft("local-panic")))
        with self.assertRaisesRegex(ValueError, "file name"):
            load_pending_signatures(self.pending)

    def test_invalid_pending_signature_names_the_file(self):
        self.pending.mkdir()
        (self.pending / "broken.json").write_text('{"id": "broken"}')
        with self.assertRaisesRegex(ValueError, "broken.json"):
            load_known_signatures(self.signatures)

    def test_pending_panic_is_not_drafted_again(self):
        save_pending_signature(self.pending, draft("local-panic"))
        triage = self.root / "triage"
        for name, message in (("crash_0", "Not a block context"), ("crash_1", "brand new")):
            crash_dir = triage / "new_crashes" / name
            crash_dir.mkdir(parents=True)
            (crash_dir / "test.html").write_text("<p></p>")
            (crash_dir / "signature_draft.json").write_text(json.dumps(draft("x", message=message)))
        drafts = sync_draft_signatures(triage, self.signatures)
        self.assertEqual([d["message"] for d in drafts], ["brand new"])


if __name__ == "__main__":
    unittest.main()
