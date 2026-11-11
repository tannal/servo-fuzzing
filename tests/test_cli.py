"""Tests for CLI subcommands."""

import tempfile
import unittest
from pathlib import Path

from servo_fuzzing.cli import main as cli_main


class TestCLI(unittest.TestCase):
    def test_validate_signatures_valid(self):
        sig_file = Path("signatures/rust-panic.json")
        code = cli_main(["validate-signatures", "--signatures", str(sig_file)])
        self.assertEqual(code, 0)

    def test_validate_signatures_invalid(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("invalid json")
            bad_path = Path(f.name)

        code = cli_main(["validate-signatures", "--signatures", str(bad_path)])
        self.assertEqual(code, 1)
        bad_path.unlink()

    def test_dry_run_run_subcommand(self):
        # Test dry-run mode of run
        with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
            f.write("#!/bin/sh\nexit 0\n")
            fake_bin = Path(f.name)
            fake_bin.chmod(0o755)

        code = cli_main([
            "run",
            "--binary", str(fake_bin),
            "--config", "configs/default.json",
            "--signatures", "signatures/rust-panic.json",
            "--dry-run",
        ])
        self.assertEqual(code, 0)
        fake_bin.unlink()

    def test_add_signature_cli(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write('{"schema_version": 1, "signatures": []}')
            sig_path = Path(f.name)

        code = cli_main([
            "add-signature",
            "--signatures", str(sig_path),
            "--id", "test-sig",
            "--message", "test panic",
            "--source-file", "components/test.rs",
            "--stack-frame", "test_frame",
            "--issue", "https://github.com/servo/servo/issues/999",
        ])
        self.assertEqual(code, 0)
        sig_path.unlink()

    def test_reduce_cli_missing_testcase(self):
        code = cli_main([
            "reduce",
            "nonexistent.html",
            "--binary", "/bin/true",
        ])
        self.assertEqual(code, 1)

    def test_bundle_cli_pack_and_unpack(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            src = Path(tmp_dir) / "src"
            src.mkdir()
            (src / "test.html").write_text("<p>Hello</p>", encoding="utf-8")
            tar_path = Path(tmp_dir) / "bundle.tar.gz"
            dest = Path(tmp_dir) / "dest"

            code_pack = cli_main([
                "bundle",
                "pack",
                "--source", str(src),
                "--output", str(tar_path),
            ])
            self.assertEqual(code_pack, 0)
            self.assertTrue(tar_path.exists())

            code_unpack = cli_main([
                "bundle",
                "unpack",
                "--bundle", str(tar_path),
                "--target", str(dest),
            ])
            self.assertEqual(code_unpack, 0)
            self.assertTrue((dest / "test.html").exists())


if __name__ == "__main__":
    unittest.main()
