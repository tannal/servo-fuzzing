"""Tests for pruning corpus entries whose issue is closed upstream."""

import json
import tempfile
import unittest
from pathlib import Path

from servo_fuzzing.signatures import prune_fixed_corpus


def _write_signatures(path: Path, signatures: list[dict]) -> None:
    path.write_text(
        json.dumps({"schema_version": 1, "signatures": signatures}, indent=2) + "\n",
        encoding="utf-8",
    )


class TestPruneFixedCorpus(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.corpus = self.root / "corpus"
        self.corpus.mkdir()
        self.history = self.root / "fixed-history.json"
        _write_signatures(
            self.history,
            [
                {
                    "id": "issue-49411",
                    "message": "assertion failed",
                    "source_file": "components/script/dom/window/windowproxy.rs",
                    "stack_frames": [],
                    "issue": "https://github.com/servo/servo/issues/49411",
                }
            ],
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_removes_flat_entries_of_closed_issues(self):
        (self.corpus / "issue-49411.html").write_text("<html>x</html>", encoding="utf-8")
        (self.corpus / "issue-49411.json").write_text("{}", encoding="utf-8")
        (self.corpus / "still-open-panic.html").write_text("<html>y</html>", encoding="utf-8")

        removed = prune_fixed_corpus(self.corpus, self.history)

        self.assertEqual(
            sorted(p.name for p in removed),
            ["issue-49411.html", "issue-49411.json"],
        )
        self.assertFalse((self.corpus / "issue-49411.html").exists())
        self.assertFalse((self.corpus / "issue-49411.json").exists())
        self.assertTrue((self.corpus / "still-open-panic.html").exists())

    def test_removes_collected_corpus_directories(self):
        entry = self.corpus / "issue-49411"
        entry.mkdir()
        (entry / "repro.html").write_text("<html>x</html>", encoding="utf-8")
        (entry / "provenance.json").write_text("{}", encoding="utf-8")

        removed = prune_fixed_corpus(self.corpus, self.history)

        self.assertEqual([p.name for p in removed], ["issue-49411"])
        self.assertFalse(entry.exists())

    def test_dry_run_keeps_files(self):
        target = self.corpus / "issue-49411.html"
        target.write_text("<html>x</html>", encoding="utf-8")

        removed = prune_fixed_corpus(self.corpus, self.history, dry_run=True)

        self.assertEqual([p.name for p in removed], ["issue-49411.html"])
        self.assertTrue(target.exists())

    def test_missing_corpus_directory_is_ignored(self):
        removed = prune_fixed_corpus(self.root / "absent", self.history)
        self.assertEqual(removed, [])

    def test_missing_history_file_prunes_nothing(self):
        target = self.corpus / "issue-49411.html"
        target.write_text("<html>x</html>", encoding="utf-8")

        removed = prune_fixed_corpus(self.corpus, self.root / "absent.json")

        self.assertEqual(removed, [])
        self.assertTrue(target.exists())


if __name__ == "__main__":
    unittest.main()
