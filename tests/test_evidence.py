"""Tests for the crash evidence pack."""

import json
import tempfile
import unittest
from pathlib import Path

from servo_fuzzing.bisect import BisectResult, Nightly
from servo_fuzzing.evidence import (
    Evidence,
    fetch_range_commits,
    fetch_source_snippet,
    filled_analysis,
    frame_source_hints,
    render_issue,
    score_commit,
    trimmed_backtrace,
    version_sha,
    write_evidence_pack,
)
from servo_fuzzing.executor import TestResult, TestStatus
from servo_fuzzing.parser import CrashInfo

OUTPUT = """libEGL warning: failed to open /dev/dri/card0
Not a block context (thread Script#1, at components/layout/formatting_contexts.rs:699)
   0: servoshell::backtrace::print
   1: core::panicking::panic_fmt
   1: <layout::table::layout::TableLayout>::layout
   2: std::sys::backtrace::__rust_begin_short_backtrace::<x>
   3: start_thread
[2026-11-17T05:00:00Z ERROR servoshell] renderer noise
"""


def make_crash():
    return CrashInfo(
        message="Not a block context",
        source_file="components/layout/formatting_contexts.rs",
        line=699,
        thread="Script#1",
        stack_frames=[
            "servoshell::backtrace::print",
            "core::panicking::panic_fmt",
            "<layout::table::layout::TableLayout>::layout",
        ],
    )


def make_bisect():
    return BisectResult(
        last_good=Nightly("2026-11-12", "a" * 40, "url"),
        first_bad=Nightly("2026-11-13", "b" * 40, "url"),
        tested={"2026-11-12": False, "2026-11-13": True},
    )


def make_evidence(tmp, **kwargs):
    testcase = tmp / "crash.html"
    testcase.write_text('<iframe style="display: table-cell"></iframe>\n')
    result = TestResult(
        testcase=testcase,
        status=TestStatus.NEW_CRASH,
        exit_code=-11,
        combined_output=OUTPUT,
        crash_info=make_crash(),
    )
    return Evidence(testcase=testcase, result=result, servo_version="Servo 0.8.0-ec2138dac", attempts=3, crashes=3, **kwargs)


class TestEvidence(unittest.TestCase):
    def test_version_sha(self):
        self.assertEqual(version_sha("Version: Servo 0.8.0-ec2138dac"), "ec2138dac")
        self.assertIsNone(version_sha("unknown"))

    def test_frame_source_hints_skip_std(self):
        self.assertEqual(
            frame_source_hints(make_crash().stack_frames),
            ["layout/table/layout"],
        )

    def test_score_commit_ranks_by_distance_to_crash_site(self):
        crash = make_crash()
        adds_panic = {"files": [{
            "filename": "components/layout/formatting_contexts.rs",
            "patch": '+            _ => panic!("Not a block context"),',
        }]}
        in_backtrace = {"files": [{"filename": "components/layout/table/layout.rs", "patch": "+x"}]}
        same_component = {"files": [{"filename": "components/layout/flexbox/mod.rs", "patch": "+x"}]}
        unrelated = {"files": [{"filename": "components/script/dom/node.rs", "patch": "+x"}]}

        scores = [score_commit(c, crash)[0] for c in (adds_panic, in_backtrace, same_component, unrelated)]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual(scores[-1], 0)
        self.assertGreater(scores[2], 0)
        self.assertIn("adds the panic message", score_commit(adds_panic, crash)[1])

    def test_fetch_range_commits_scores_each_commit(self):
        def fetch(url):
            if "/compare/" in url:
                return {"total_commits": 2, "commits": [
                    {"sha": "1" * 40, "commit": {"message": "script: unrelated\n\nbody"}},
                    {"sha": "2" * 40, "commit": {"message": "layout: align-content for table cells"}},
                ]}
            if url.endswith("2" * 40):
                return {"files": [{"filename": "components/layout/formatting_contexts.rs", "patch": "+x"}]}
            return {"files": [{"filename": "components/script/dom/node.rs"}]}

        commits = fetch_range_commits(make_bisect(), make_crash(), fetch)
        self.assertEqual([c["title"] for c in commits], ["script: unrelated", "layout: align-content for table cells"])
        self.assertEqual([c["score"] for c in commits], [0, 3])
        self.assertEqual(commits[1]["files"], ["components/layout/formatting_contexts.rs"])

    def test_fetch_source_snippet_is_centred_on_the_panic_line(self):
        source = "\n".join(f"line {n}" for n in range(1, 1001))
        snippet = fetch_source_snippet(make_crash(), "ec2138dac", lambda url: source)
        self.assertEqual(snippet["panic_line"], 699)
        self.assertEqual(snippet["lines"][699 - snippet["start_line"]], "line 699")

    def test_trimmed_backtrace_drops_logs_and_thread_startup(self):
        trimmed = trimmed_backtrace(OUTPUT)
        self.assertTrue(trimmed.startswith("Not a block context"))
        self.assertIn("TableLayout", trimmed)
        for noise in ("libEGL", "backtrace::print", "panic_fmt", "rust_begin_short_backtrace", "start_thread", "renderer noise"):
            self.assertNotIn(noise, trimmed)

    def test_filled_analysis_drops_empty_sections(self):
        text = (
            "# Analysis\n\n<!-- note -->\n\n## Readable testcase\n\n"
            "## Ablation\n\n| Variant | Runs crashing |\n|---|---|\n\n"
            "## Root cause: verified\n\nThe cell is rebuilt as Replaced.\n"
        )
        self.assertEqual(filled_analysis(text), "**Root cause: verified**\n\nThe cell is rebuilt as Replaced.")

    def test_render_issue_includes_range_and_suspects(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            evidence = make_evidence(Path(tmpdir), bisect=make_bisect(), commits=[
                {"sha": "1" * 40, "title": "script: unrelated", "score": 0, "reasons": []},
                {"sha": "2" * 40, "title": "layout: table cells", "score": 8, "reasons": ["adds the panic message"]},
            ])
            issue = render_issue(evidence)
            self.assertIn("panic: Not a block context (thread Script#1, at components/layout/formatting_contexts.rs:699)", issue)
            self.assertIn("**Reproducibility**: 3/3 runs", issue)
            self.assertIn("Last good: `2026-11-12`", issue)
            self.assertIn("2222222222 layout: table cells: adds the panic message", issue)
            self.assertNotIn("script: unrelated", issue)

    def test_weak_suspects_are_listed_only_without_a_strong_one(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            weak = {"sha": "1" * 40, "title": "weak", "score": 2, "reasons": ["r"]}
            strong = {"sha": "2" * 40, "title": "strong", "score": 3, "reasons": ["r"]}
            self.assertEqual(make_evidence(Path(tmpdir), commits=[weak, strong]).suspects, [strong])
            self.assertEqual(make_evidence(Path(tmpdir), commits=[weak]).suspects, [weak])

    def test_pack_keeps_existing_analysis_and_only_merges_it_on_request(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            evidence = make_evidence(tmp)
            pack = tmp / "pack"

            issue = write_evidence_pack(evidence, pack)
            self.assertNotIn("#### Analysis", issue.read_text())
            context = json.loads((pack / "context.json").read_text())
            self.assertEqual(context["crash"]["line"], 699)
            self.assertEqual(context["reproduction"]["crashes"], 3)
            self.assertIsNone(context["bisect"])
            for name in context["files"].values():
                if name:
                    self.assertTrue((pack / name).exists(), name)

            (pack / "analysis.md").write_text("# A\n\n## Root cause: verified\n\nRebuilt as Replaced.\n")
            write_evidence_pack(evidence, pack)
            self.assertIn("Rebuilt as Replaced.", (pack / "analysis.md").read_text())
            self.assertNotIn("Rebuilt as Replaced.", (pack / "issue.md").read_text())

            write_evidence_pack(evidence, pack, include_analysis=True)
            self.assertIn("Rebuilt as Replaced.", (pack / "issue.md").read_text())


if __name__ == "__main__":
    unittest.main()
