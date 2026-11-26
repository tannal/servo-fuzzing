"""Tests for panic proposals and direct upstream issue filing."""

import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from servo_fuzzing.evidence import Evidence
from servo_fuzzing.executor import TestResult, TestStatus
from servo_fuzzing.parser import CrashInfo
from servo_fuzzing.proposals import (
    create_github_issue,
    file_upstream_issues,
    format_filing_summary,
    propose_panics,
)
from servo_fuzzing.reporter import PLACEHOLDER_FRAME
from servo_fuzzing.signatures import load_signatures, save_signatures


def _make_evidence(tc: Path, msg: str, src: str, regresses_issue=None) -> Evidence:
    crash = CrashInfo(message=msg, source_file=src, line=42, thread="Script#1", stack_frames=["layout::run"])
    res = TestResult(testcase=tc, status=TestStatus.NEW_CRASH, exit_code=101, combined_output=f"{msg} (at {src}:42)", crash_info=crash)
    return Evidence(testcase=tc, result=res, servo_version="Servo 0.8.0-abc1234", attempts=3, crashes=3, regresses_issue=regresses_issue)


class TestProposals(unittest.TestCase):
    def test_propose_panics_strips_placeholder_handles_collisions_and_links_upstream(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            drafts = tmp / "drafts.json"
            corpus = tmp / "corpus"
            corpus.mkdir()
            out = tmp / "proposals"

            tc1 = corpus / "layout-overflow.html"
            tc1.write_text("<p>1</p>")
            tc2 = corpus / "hoisted-box.html"
            tc2.write_text("<p>regressed</p>")
            tc3 = corpus / "open-bug.html"
            tc3.write_text("<p>open</p>")
            tc4 = corpus / "BadName_1.html"
            tc4.write_text("<p>4</p>")
            tc5 = corpus / "BadName_2.html"
            tc5.write_text("<p>5</p>")

            save_signatures(drafts, [
                {
                    "id": "layout-overflow",
                    "message": "attempt to add with overflow",
                    "source_file": "components/layout/flow.rs",
                    "stack_frames": [PLACEHOLDER_FRAME],
                    "issue": "https://github.com/servo/servo/issues/TODO",
                },
                {
                    "id": "hoisted-box",
                    "message": "Found hoisted box with missing",
                    "source_file": "components/layout/stacking_context.rs",
                    "stack_frames": ["layout::stacking_context"],
                    "issue": "https://github.com/servo/servo/issues/TODO",
                },
                {
                    "id": "open-bug",
                    "message": "open upstream panic",
                    "source_file": "components/script/dom/node.rs",
                    "stack_frames": ["script::node"],
                    "issue": "https://github.com/servo/servo/issues/TODO",
                },
                {
                    "id": "BadName_1",
                    "message": "first sanitized panic",
                    "source_file": "components/script/dom/a.rs",
                    "stack_frames": ["script::a"],
                    "issue": "https://github.com/servo/servo/issues/TODO",
                },
                {
                    "id": "BadName_2",
                    "message": "second sanitized panic",
                    "source_file": "components/script/dom/b.rs",
                    "stack_frames": ["script::b"],
                    "issue": "https://github.com/servo/servo/issues/TODO",
                },
            ])

            def fake_lookup(msg, _src):
                if "hoisted box" in msg:
                    return {"url": "https://github.com/servo/servo/issues/46109", "state": "closed", "number": "46109", "title": "hoisted"}
                if "open upstream" in msg:
                    return {"url": "https://github.com/servo/servo/issues/49999", "state": "open", "number": "49999", "title": "open"}
                return None

            def fake_gather(tc, regresses_issue=None):
                if "hoisted-box" in tc.name:
                    return _make_evidence(tc, "Found hoisted box with missing", "components/layout/stacking_context.rs", regresses_issue)
                if "BadName_1" in tc.name:
                    return _make_evidence(tc, "first sanitized panic", "components/script/dom/a.rs", regresses_issue)
                if "BadName_2" in tc.name:
                    return _make_evidence(tc, "second sanitized panic", "components/script/dom/b.rs", regresses_issue)
                return _make_evidence(tc, "attempt to add with overflow", "components/layout/flow.rs", regresses_issue)

            proposals = propose_panics(
                drafts_path=drafts,
                corpus_dir=corpus,
                out_dir=out,
                gather=fake_gather,
                find_existing=fake_lookup,
                limit=10,
            )
            self.assertEqual(len(proposals), 5)
            by_id = {p["id"]: p for p in proposals}
            self.assertEqual(by_id["layout-overflow"]["status"], "new")
            self.assertEqual(by_id["layout-overflow"]["signature"]["stack_frames"], [])
            self.assertEqual(by_id["hoisted-box"]["status"], "regression")
            self.assertEqual(by_id["hoisted-box"]["regresses_issue"], "https://github.com/servo/servo/issues/46109")
            self.assertIn("**Regresses**: https://github.com/servo/servo/issues/46109", (out / "hoisted-box" / "issue.md").read_text())
            self.assertEqual(by_id["open-bug"]["status"], "existing_open")
            self.assertEqual(by_id["open-bug"]["issue"], "https://github.com/servo/servo/issues/49999")
            self.assertIn("crash", by_id)
            self.assertTrue(any(pid.startswith("crash-") for pid in by_id))

    def test_file_upstream_issues_creates_links_and_updates_databases(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            sigs_file = tmp / "rust-panic.json"
            hist_file = tmp / "fixed-history.json"
            save_signatures(sigs_file, [])
            save_signatures(hist_file, [])

            props_dir = tmp / "proposals"
            # 1. Brand-new panic
            p1 = props_dir / "new-panic"
            p1.mkdir(parents=True)
            (p1 / "issue.md").write_text("### Title: `panic: new bug (at a.rs:1)`\n\nBody details\n")
            (p1 / "proposal.json").write_text(json.dumps({
                "id": "new-panic",
                "status": "new",
                "issue": None,
                "regresses_issue": None,
                "signature": {
                    "id": "new-panic",
                    "message": "new bug",
                    "source_file": "a.rs",
                    "stack_frames": ["a::run"],
                    "issue": "https://github.com/servo/servo/issues/TODO",
                },
            }))

            # 2. Regression panic
            p2 = props_dir / "reg-panic"
            p2.mkdir(parents=True)
            (p2 / "issue.md").write_text("### Title: `panic: regressed bug (at b.rs:2)`\n\nBody details\n")
            (p2 / "proposal.json").write_text(json.dumps({
                "id": "reg-panic",
                "status": "regression",
                "issue": "https://github.com/servo/servo/issues/100",
                "regresses_issue": "https://github.com/servo/servo/issues/100",
                "signature": {
                    "id": "reg-panic",
                    "message": "regressed bug",
                    "source_file": "b.rs",
                    "stack_frames": ["b::run"],
                    "issue": "https://github.com/servo/servo/issues/100",
                },
            }))

            # 3. Already existing open issue
            p3 = props_dir / "existing-panic"
            p3.mkdir(parents=True)
            (p3 / "proposal.json").write_text(json.dumps({
                "id": "existing-panic",
                "status": "existing_open",
                "issue": "https://github.com/servo/servo/issues/200",
                "regresses_issue": None,
                "signature": {
                    "id": "existing-panic",
                    "message": "existing bug",
                    "source_file": "c.rs",
                    "stack_frames": [],
                    "issue": "https://github.com/servo/servo/issues/200",
                },
            }))

            created = []

            def fake_create(repo, title, body, labels, _token):
                num = 500 + len(created)
                created.append((repo, title, body, labels))
                return f"https://github.com/{repo}/issues/{num}"

            # Dry-run must not post or mutate signature files
            dry_records = file_upstream_issues(
                proposals_dir=props_dir,
                repo="servo/servo",
                dry_run=True,
                signatures_path=sigs_file,
                history_path=hist_file,
                find_existing=lambda _m, _s: None,
                create_issue=fake_create,
            )
            self.assertEqual(len(created), 0)
            self.assertEqual(load_signatures(sigs_file), [])
            self.assertEqual(len(dry_records), 3)

            # Live run files 2 issues, links the 3rd, and updates both databases
            records = file_upstream_issues(
                proposals_dir=props_dir,
                repo="servo/servo",
                token="tok",
                dry_run=False,
                signatures_path=sigs_file,
                history_path=hist_file,
                find_existing=lambda _m, _s: None,
                create_issue=fake_create,
            )
            self.assertEqual(len(created), 2)
            self.assertTrue(any("regression:" in t.lower() for _, t, _, _ in created))
            active = load_signatures(sigs_file)
            history = load_signatures(hist_file)
            self.assertEqual(len(active), 3)
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["issue"], "https://github.com/servo/servo/issues/100")
            self.assertIn("New Panic Upstream Issue Filing", format_filing_summary(records, "servo/servo"))

    def test_create_github_issue_retries_without_labels_on_422(self):
        calls = []

        class Resp:
            def __init__(self, url):
                self._url = url
            def read(self):
                return json.dumps({"html_url": self._url}).encode("utf-8")
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass

        def fake_urlopen(req, timeout=30):
            payload = json.loads(req.data.decode("utf-8"))
            calls.append(payload)
            if "labels" in payload:
                raise urllib.error.HTTPError(req.full_url, 422, "Unprocessable Entity", None, io.BytesIO(b""))
            return Resp("https://github.com/servo/servo/issues/99999")

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            url = create_github_issue("servo/servo", "title", "body", ["B-fuzzing"], "tok")
        self.assertEqual(url, "https://github.com/servo/servo/issues/99999")
        self.assertEqual(len(calls), 2)
        self.assertIn("labels", calls[0])
        self.assertNotIn("labels", calls[1])

    def test_propose_panics_marks_fixed_history_regression_even_for_fork_repo(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            drafts = tmp / "drafts.json"
            corpus = tmp / "corpus"
            out_dir = tmp / "proposals"
            history = tmp / "fixed-history.json"
            corpus.mkdir()

            msg = "index out of bounds: the len is 7 but the index is 15"
            src = "components/shared/paint/display_list.rs"
            save_signatures(history, [{
                "id": "issue-42215",
                "message": msg,
                "source_file": src,
                "stack_frames": ["display_list::build"],
                "issue": "https://github.com/servo/servo/issues/42215",
            }])
            save_signatures(drafts, [{
                "id": "issue-42215",
                "message": msg,
                "source_file": src,
                "stack_frames": ["display_list::build"],
                "issue": "https://github.com/servo/servo/issues/42215",
            }])
            (corpus / "issue-42215.html").write_text("<p>dl</p>")

            proposals = propose_panics(
                drafts_path=drafts,
                corpus_dir=corpus,
                out_dir=out_dir,
                gather=lambda tc, regresses_issue=None: _make_evidence(tc, msg, src, regresses_issue=regresses_issue),
                target_repo="tannal/servo-fuzzing",
                find_existing=lambda _m, _s: None,
                fixed_history_path=history,
            )
            self.assertEqual(len(proposals), 1)
            self.assertEqual(proposals[0]["status"], "regression")
            self.assertEqual(proposals[0]["regresses_issue"], "https://github.com/servo/servo/issues/42215")


if __name__ == "__main__":
    unittest.main()
