"""Tests for how `run` picks its testcases."""

import io
import tarfile
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from servo_fuzzing.runner import main as runner_main


class TestTestcaseSelection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.binary = self.root / "servoshell"
        self.binary.write_text("#!/bin/sh\nexit 0\n")
        self.binary.chmod(0o755)

    def run_dry(self, *extra):
        out = io.StringIO()
        with redirect_stdout(out):
            code = runner_main(
                ["--binary", str(self.binary), "--output", str(self.root / "out"), "--dry-run", *extra]
            )
        return code, out.getvalue()

    def make_bundle(self, count):
        src = self.root / "src" / "domato"
        src.mkdir(parents=True)
        archive = self.root / "bundle.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            for i in range(count):
                case = src / f"{i}.html"
                case.write_text("<p>")
                tar.add(case, arcname=f"domato/{i}.html")
        return archive

    def test_defaults_to_latest_bundle(self):
        archive = self.make_bundle(3)
        with patch("servo_fuzzing.bundle.obtain_bundle", return_value=archive) as obtain:
            code, out = self.run_dry()
        self.assertEqual(code, 0)
        self.assertEqual(obtain.call_args.kwargs["bundle_spec"], "latest")
        self.assertIn("Found 3 test cases", out)
        self.assertTrue((self.root / "out" / "bundle" / "domato" / "0.html").exists())

    def test_unavailable_bundle_is_an_error(self):
        for extra in ([], ["--bundle", "https://example.invalid/b.tar.gz"]):
            with patch("servo_fuzzing.bundle.obtain_bundle", return_value=None):
                code, out = self.run_dry(*extra)
            self.assertEqual(code, 2)
            self.assertIn("could not obtain bundle", out)

    def test_explicit_testcases_skip_the_bundle(self):
        cases = self.root / "cases"
        cases.mkdir()
        (cases / "a.html").write_text("<p>")
        with patch("servo_fuzzing.bundle.obtain_bundle") as obtain:
            code, out = self.run_dry("--testcases", str(cases))
        self.assertEqual(code, 0)
        obtain.assert_not_called()
        self.assertIn("Found 1 test cases", out)

    def test_generating_run_does_not_download_a_bundle(self):
        with patch("servo_fuzzing.bundle.obtain_bundle") as obtain, patch(
            "servo_fuzzing.generators.GeneratorManager.generate_all", return_value={}
        ):
            code, _ = self.run_dry("--generate")
        self.assertEqual(code, 0)
        obtain.assert_not_called()

    def test_timeouts_flag_filters_manifest_timeout_tests(self):
        import json
        from servo_fuzzing.bundle import pack_bundle

        src = self.root / "gen"
        (src / "domato").mkdir(parents=True)
        pass_tc = src / "domato" / "pass0.html"
        hang_tc = src / "domato" / "hang0.html"
        pass_tc.write_text("<p>ok</p>")
        hang_tc.write_text("<script>while(1){}</script>")
        summary_file = self.root / "summary.json"
        summary_file.write_text(json.dumps({
            "results": [
                {"testcase": str(pass_tc), "status": "PASS"},
                {"testcase": str(hang_tc), "status": "TIMEOUT"},
            ]
        }))
        archive = self.root / "with_timeouts.tar.gz"
        pack_bundle(
            source_dir=src,
            output_tar=archive,
            from_triage=summary_file,
            include_timeouts=True,
            max_timeout_tests=5,
            max_tests=10,
        )

        # Default --timeouts include runs both
        with patch("servo_fuzzing.bundle.obtain_bundle", return_value=archive):
            code, out = self.run_dry("--bundle", "2026-11-23")
        self.assertEqual(code, 0)
        self.assertIn("Found 2 test cases", out)

        # --timeouts exclude filters out hang0.html
        with patch("servo_fuzzing.bundle.obtain_bundle", return_value=archive):
            code, out = self.run_dry("--bundle", "2026-11-23", "--timeouts", "exclude")
        self.assertEqual(code, 0)
        self.assertIn("Excluded 1 bundled timeout testcase(s)", out)
        self.assertIn("Found 1 test cases", out)


if __name__ == "__main__":
    unittest.main()
