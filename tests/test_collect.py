import json
import tempfile
import unittest
from pathlib import Path

from servo_fuzzing.collect import collect_panics


class CollectTests(unittest.TestCase):
    def test_collects_only_current_new_panics_and_deduplicates_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run, corpus = root / "run", root / "corpus"
            results = []
            for name, status in [("first", "NEW_CRASH"), ("duplicate", "NEW_CRASH"),
                                 ("known", "KNOWN_CRASH"), ("old", "NEW_CRASH")]:
                detail = run / "new_crashes" / name
                detail.mkdir(parents=True)
                (detail / (name + ".html")).write_text("<script>crash()</script>")
                (detail / "signature_draft.json").write_text(json.dumps({"message": "panic", "source_file": "a.rs"}))
                (detail / "output.log").write_text("panic log")
                if name != "old":
                    results.append({"status": status, "testcase": "/missing/ci/" + name + ".html"})
            (run / "summary.json").write_text(json.dumps({"results": results}))
            self.assertEqual(collect_panics(run, corpus), 1)
            self.assertEqual(collect_panics(run, corpus), 0)
            saved = list(corpus.glob("*/repro.html"))
            self.assertEqual(len(saved), 1)
            self.assertTrue((saved[0].parent / "output.log").is_file())
            self.assertTrue((saved[0].parent / "provenance.json").is_file())

    def test_missing_summary_is_an_error(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FileNotFoundError):
                collect_panics(Path(directory), Path(directory) / "corpus")
