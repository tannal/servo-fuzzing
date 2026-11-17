import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

from servo_fuzzing.github_sync import (
    fetch_fuzzing_issues,
    parse_issue_to_signature,
    sync_signatures_from_github,
)
from servo_fuzzing.signatures import load_signatures, save_pending_signature, save_signatures


def issue(number, body, state="open", title="crash"):
    return {
        "number": number,
        "title": title,
        "body": body,
        "state": state,
        "html_url": f"https://github.com/servo/servo/issues/{number}",
    }


PANIC_WITH_BACKTRACE = """
assertion failed: parent_proxy.currently_active().is_some() (thread Script#1, at components/script/dom/window/windowproxy.rs:123)
   0: servoshell::backtrace::print
   1: servoshell::panic_hook::panic_hook
   2: std::panicking::panic_with_hook
   3: std::sys::backtrace::__rust_end_short_backtrace
   4: rust_begin_unwind
   5: <script::dom::window::windowproxy::WindowProxy>::parent
   6: <script::dom::servoparser::ParserContext>::initialize_document_object
   7: script::script_thread::ScriptThread::handle_msg
"""
PANIC_WITHOUT_BACKTRACE = "LengthPercentage not found (thread Script#1, at /home/u/.cargo/git/checkouts/stylo-482338307e42a9ea/890aa12/style/servo/attr.rs:350)"
ASSERT_EQ_PANIC = """
assertion `left == right` failed: Vector length 1 should be a multiple of cols = 0
  left: 0
 right: 1 (thread Script(1,1), at /home/runner/.cargo/registry/src/index.crates.io-1949cf8c6b5b557f/grid-0.15.0/src/lib.rs:481)
"""


class SyncTests(unittest.TestCase):
    def response(self, items, total=None):
        return io.BytesIO(json.dumps({
            "items": items,
            "total_count": len(items) if total is None else total,
            "incomplete_results": False,
        }).encode())

    def test_label_and_pagination(self):
        with patch("servo_fuzzing.github_sync.urllib.request.urlopen", side_effect=[
            self.response([{"number": 1}], 2), self.response([{"number": 2}], 2)
        ]) as request:
            self.assertEqual(len(fetch_fuzzing_issues(per_page=1)), 2)
            query = parse_qs(urlparse(request.call_args_list[0].args[0].full_url).query)
            self.assertEqual(query["q"], ['repo:servo/servo is:issue is:open label:"B-fuzzing"'])
            self.assertEqual(parse_qs(urlparse(request.call_args.args[0].full_url).query)["page"], ["2"])

    def test_incomplete_response_rejected(self):
        with patch("servo_fuzzing.github_sync.urllib.request.urlopen", return_value=io.BytesIO(
            json.dumps({"items": [], "total_count": 1, "incomplete_results": True}).encode()
        )):
            with self.assertRaises(ValueError):
                fetch_fuzzing_issues()

    def test_empty_page_with_nonzero_total_rejected(self):
        with patch("servo_fuzzing.github_sync.urllib.request.urlopen", return_value=self.response([], 1)):
            with self.assertRaises(ValueError):
                fetch_fuzzing_issues()

    def test_signature_frames_skip_panic_plumbing(self):
        sig = parse_issue_to_signature(issue(49411, PANIC_WITH_BACKTRACE))
        self.assertEqual(sig, {
            "id": "issue-49411",
            "message": "assertion failed: parent_proxy.currently_active().is_some()",
            "source_file": "components/script/dom/window/windowproxy.rs",
            "stack_frames": [
                "<script::dom::window::windowproxy::WindowProxy>::parent",
                "<script::dom::servoparser::ParserContext>::initialize_document_object",
            ],
            "issue": "https://github.com/servo/servo/issues/49411",
        })

    def test_signature_frames_drop_closures_and_generic_arguments(self):
        body = PANIC_WITHOUT_BACKTRACE + """
   0: servoshell::backtrace::print
   1: script_bindings::codegen::GenericBindings::WindowBinding::Window_Binding::get_frames::<script::dom::bindings::codegen::DomTypeHolder::DomTypeHolder>::{closure#0}
   2: webrender::quad::prepare_border_image_nine_patch::{{closure}}::{{closure}}
   3: <alloc::boxed::Box<F> as core::ops::function::FnOnce<Args>>::call_once
"""
        self.assertEqual(parse_issue_to_signature(issue(1, body))["stack_frames"], [
            "script_bindings::codegen::GenericBindings::WindowBinding::Window_Binding::get_frames",
            "webrender::quad::prepare_border_image_nine_patch",
        ])

    def test_issue_without_backtrace_has_no_frames(self):
        sig = parse_issue_to_signature(issue(49527, PANIC_WITHOUT_BACKTRACE))
        self.assertEqual(sig["source_file"], "style/servo/attr.rs")
        self.assertEqual(sig["stack_frames"], [])

    def test_assert_eq_panic_uses_assertion_line(self):
        sig = parse_issue_to_signature(issue(36948, ASSERT_EQ_PANIC))
        self.assertEqual(
            sig["message"],
            "assertion `left == right` failed: Vector length 1 should be a multiple of cols = 0",
        )
        self.assertEqual(sig["source_file"], "grid-0.15.0/src/lib.rs")

    def test_issue_without_panic_location_is_skipped(self):
        self.assertIsNone(parse_issue_to_signature(issue(45946, "Tracking issue", title="Automated Fuzzing")))
        self.assertIsNone(parse_issue_to_signature(issue(46406, "Aborted (core dumped)\n", title="double free")))


class RebuildTests(unittest.TestCase):
    STALE = {
        "id": "stale-local-draft",
        "message": "old",
        "source_file": "components/old.rs",
        "stack_frames": ["old"],
        "issue": "https://github.com/servo/servo/issues/TODO",
    }

    def run_sync(self, directory, open_issues, closed_issues, **kwargs):
        signatures, history = Path(directory) / "rust-panic.json", Path(directory) / "fixed-history.json"
        save_signatures(signatures, [self.STALE])
        save_signatures(history, [])
        by_state = {"open": open_issues, "closed": closed_issues}
        with patch(
            "servo_fuzzing.github_sync.fetch_fuzzing_issues",
            side_effect=lambda repo, state, token: by_state[state],
        ):
            result = sync_signatures_from_github(signatures, history, **kwargs)
        return result, load_signatures(signatures), load_signatures(history)

    def test_files_are_regenerated_from_upstream(self):
        with tempfile.TemporaryDirectory() as directory:
            result, active, fixed = self.run_sync(
                directory,
                open_issues=[
                    issue(49527, PANIC_WITHOUT_BACKTRACE),
                    issue(49411, PANIC_WITH_BACKTRACE),
                    issue(45946, "Tracking issue", title="Automated Fuzzing"),
                ],
                closed_issues=[issue(36948, ASSERT_EQ_PANIC, state="closed")],
            )
        self.assertEqual([s["id"] for s in active], ["issue-49411", "issue-49527"])
        self.assertEqual([s["id"] for s in fixed], ["issue-36948"])
        self.assertEqual([s["number"] for s in result.skipped], [45946])
        self.assertIn("[#45946](https://github.com/servo/servo/issues/45946) (open): Automated Fuzzing",
                      result.to_markdown("servo/servo"))

    def pending_setup(self, directory):
        pending, reports = Path(directory) / "pending", Path(directory) / "reports"
        for signature_id, message, source in (
            ("filed-upstream", "LengthPercentage not found", "style/servo/attr.rs"),
            ("still-local", "Not a block context", "components/layout/formatting_contexts.rs"),
        ):
            save_pending_signature(pending, {
                "id": signature_id,
                "message": message,
                "source_file": source,
                "stack_frames": [],
                "issue": f"https://github.com/tannal/servo-fuzzing/tree/main/reports/{signature_id}",
            })
            (reports / signature_id).mkdir(parents=True)
            (reports / signature_id / "issue.md").write_text("draft")
        return pending, reports

    def test_pending_signature_and_report_are_removed_once_filed_upstream(self):
        with tempfile.TemporaryDirectory() as directory:
            pending, reports = self.pending_setup(directory)
            result, _, _ = self.run_sync(
                directory, [issue(49411, PANIC_WITH_BACKTRACE)], [issue(49527, PANIC_WITHOUT_BACKTRACE, state="closed")],
                pending_dir=pending, reports_dir=reports,
            )
            self.assertEqual(sorted(p.name for p in pending.iterdir()), ["still-local.json"])
            self.assertEqual(sorted(p.name for p in reports.iterdir()), ["still-local"])
        self.assertEqual([(p["id"], u["id"]) for p, u in result.resolved], [("filed-upstream", "issue-49527")])
        self.assertIn("`filed-upstream` -> https://github.com/servo/servo/issues/49527", result.to_markdown("servo/servo"))

    def test_dry_run_keeps_pending_files(self):
        with tempfile.TemporaryDirectory() as directory:
            pending, reports = self.pending_setup(directory)
            result, _, _ = self.run_sync(
                directory, [issue(49527, PANIC_WITHOUT_BACKTRACE)], [], dry_run=True,
                pending_dir=pending, reports_dir=reports,
            )
            self.assertEqual(len(list(pending.iterdir())), 2)
            self.assertEqual(len(list(reports.iterdir())), 2)
        self.assertEqual(len(result.resolved), 1)

    def test_dry_run_keeps_files(self):
        with tempfile.TemporaryDirectory() as directory:
            _, active, _ = self.run_sync(directory, [issue(49411, PANIC_WITH_BACKTRACE)], [], dry_run=True)
        self.assertEqual(active, [self.STALE])

    def test_no_upstream_issues_keeps_files(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                self.run_sync(directory, [], [])
            self.assertEqual(load_signatures(Path(directory) / "rust-panic.json"), [self.STALE])

    def test_api_error_keeps_files(self):
        with tempfile.TemporaryDirectory() as directory:
            signatures, history = Path(directory) / "rust-panic.json", Path(directory) / "fixed-history.json"
            save_signatures(signatures, [self.STALE])
            with patch(
                "servo_fuzzing.github_sync.urllib.request.urlopen",
                side_effect=HTTPError("url", 403, "Forbidden", {}, None),
            ):
                with self.assertRaises(HTTPError):
                    sync_signatures_from_github(signatures, history)
            self.assertEqual(load_signatures(signatures), [self.STALE])
            self.assertFalse(history.exists())


if __name__ == "__main__":
    unittest.main()
