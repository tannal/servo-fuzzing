"""Tests for OS/hardware detection and Servo nightly download/extraction."""
from __future__ import annotations

import hashlib
import io
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from servo_fuzzing import nightly


class TestNightlyDownloader(unittest.TestCase):
    def test_detect_platform_all_targets_and_aliases(self):
        cases = [
            ("linux", "x86_64", "linux", "x86_64", "servo-x86_64-linux-gnu.tar.gz", "tar.gz", "servoshell", None),
            ("Linux", "amd64", "linux", "x86_64", "servo-x86_64-linux-gnu.tar.gz", "tar.gz", "servoshell", None),
            ("linux", "aarch64", "linux", "aarch64", "servo-aarch64-linux-gnu.tar.gz", "tar.gz", "servoshell", None),
            ("linux", "arm64", "linux", "aarch64", "servo-aarch64-linux-gnu.tar.gz", "tar.gz", "servoshell", None),
            ("Darwin", "arm64", "macos", "aarch64", "servo-aarch64-apple-darwin.dmg", "dmg", "servoshell", None),
            ("macos", "aarch64", "macos", "aarch64", "servo-aarch64-apple-darwin.dmg", "dmg", "servoshell", None),
            ("macos", "x86_64", "macos", "x86_64", "servo-x86_64-apple-darwin.dmg", "dmg", "servoshell", None),
            ("Windows", "AMD64", "windows", "x86_64", "servo-x86_64-windows-msvc.zip", "zip", "servoshell.exe", None),
            ("win32", "x64", "windows", "x86_64", "servo-x86_64-windows-msvc.zip", "zip", "servoshell.exe", None),
            (
                "Windows",
                "ARM64",
                "windows",
                "aarch64",
                "servo-x86_64-windows-msvc.zip",
                "zip",
                "servoshell.exe",
                "emulation",
            ),
        ]
        for os_in, arch_in, exp_os, exp_arch, exp_asset, exp_fmt, exp_bin, note_substr in cases:
            with self.subTest(os_in=os_in, arch_in=arch_in):
                target = nightly.detect_platform(os_override=os_in, arch_override=arch_in)
                self.assertEqual(target.os_name, exp_os)
                self.assertEqual(target.arch, exp_arch)
                self.assertEqual(target.asset_name, exp_asset)
                self.assertEqual(target.archive_format, exp_fmt)
                self.assertEqual(target.binary_name, exp_bin)
                if note_substr:
                    self.assertIsNotNone(target.note)
                    assert target.note is not None
                    self.assertIn(note_substr, target.note.lower())
                else:
                    self.assertIsNone(target.note)

    def test_macos_rosetta_detection_and_explicit_arch_override(self):
        with (
            patch("servo_fuzzing.nightly.platform.system", return_value="Darwin"),
            patch("servo_fuzzing.nightly.platform.machine", return_value="x86_64"),
            patch("servo_fuzzing.nightly._macos_is_arm64_host", return_value=True),
        ):
            rosetta = nightly.detect_platform()
            self.assertEqual(rosetta.os_name, "macos")
            self.assertEqual(rosetta.arch, "aarch64")
            self.assertEqual(rosetta.asset_name, "servo-aarch64-apple-darwin.dmg")

            forced_x64 = nightly.detect_platform(arch_override="x86_64")
            self.assertEqual(forced_x64.arch, "x86_64")
            self.assertEqual(forced_x64.asset_name, "servo-x86_64-apple-darwin.dmg")

    def test_unsupported_os_and_32bit_arch_raise(self):
        with self.assertRaisesRegex(ValueError, "Unsupported operating system"):
            nightly.detect_platform(os_override="freebsd", arch_override="x86_64")
        with self.assertRaisesRegex(ValueError, "Unsupported CPU architecture"):
            nightly.detect_platform(os_override="linux", arch_override="i686")

    def test_nightly_download_url(self):
        self.assertEqual(
            nightly.nightly_download_url("servo-aarch64-apple-darwin.dmg"),
            "https://github.com/servo/servo-nightly-builds/releases/latest/download/servo-aarch64-apple-darwin.dmg",
        )
        self.assertEqual(
            nightly.nightly_download_url("servo-x86_64-windows-msvc.zip", tag="2026-11-23"),
            "https://github.com/servo/servo-nightly-builds/releases/download/2026-11-23/servo-x86_64-windows-msvc.zip",
        )

    def test_verify_sha256_and_fetch_expected(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = b"servo-nightly-binary"
            archive = Path(tmp) / "servo.tar.gz"
            archive.write_bytes(payload)
            expected = hashlib.sha256(payload).hexdigest()

            nightly.verify_sha256(archive, expected.upper())
            with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
                nightly.verify_sha256(archive, "0" * 64)

        class _FakeResp:
            def __init__(self, body: bytes):
                self._body = body

            def read(self):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        url = "https://github.com/servo/servo-nightly-builds/releases/latest/download/a.tar.gz"
        fake_hash = "a" * 64
        with patch(
            "servo_fuzzing.nightly.urllib.request.urlopen",
            return_value=_FakeResp(f"{fake_hash}  a.tar.gz\n".encode("utf-8")),
        ):
            self.assertEqual(nightly.fetch_expected_sha256(url), fake_hash)

        with patch(
            "servo_fuzzing.nightly.urllib.request.urlopen",
            side_effect=urllib.error.HTTPError(url + ".sha256", 404, "Not Found", None, None),
        ):
            self.assertIsNone(nightly.fetch_expected_sha256(url))

    def test_extract_tar_gz_unwraps_and_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "servo-x86_64-linux-gnu.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                data = b"#!/bin/sh\necho ok\n"
                info = tarfile.TarInfo("servo/servoshell")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))

            dest = root / "servo"
            binary = nightly.extract_nightly_archive(archive, dest)
            self.assertEqual(binary, dest.resolve() / "servoshell")
            self.assertTrue(binary.is_file())
            if os.name != "nt":
                self.assertTrue(os.access(binary, os.X_OK))

            bad = root / "bad.tar.gz"
            with tarfile.open(bad, "w:gz") as tar:
                info = tarfile.TarInfo("../escape")
                info.size = 1
                tar.addfile(info, io.BytesIO(b"x"))
            with self.assertRaisesRegex(ValueError, "Unsafe archive"):
                nightly.extract_nightly_archive(bad, root / "out")

    def test_extract_zip_wrapped_flat_and_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            wrapped = root / "wrapped.zip"
            with zipfile.ZipFile(wrapped, "w") as zf:
                zf.writestr("servo/servoshell.exe", b"MZ")
            dest_wrapped = root / "out_wrapped"
            bin_wrapped = nightly.extract_nightly_archive(wrapped, dest_wrapped)
            self.assertEqual(bin_wrapped, dest_wrapped.resolve() / "servoshell.exe")
            self.assertTrue(bin_wrapped.is_file())

            flat = root / "flat.zip"
            with zipfile.ZipFile(flat, "w") as zf:
                zf.writestr("servoshell.exe", b"MZ")
                zf.writestr("resources/data.txt", b"res")
            dest_flat = root / "out_flat"
            bin_flat = nightly.extract_nightly_archive(flat, dest_flat)
            self.assertEqual(bin_flat, dest_flat.resolve() / "servoshell.exe")
            self.assertTrue((dest_flat / "resources" / "data.txt").is_file())

            bad = root / "bad.zip"
            with zipfile.ZipFile(bad, "w") as zf:
                zf.writestr("../escape.exe", b"bad")
            with self.assertRaisesRegex(ValueError, "Unsafe archive"):
                nightly.extract_nightly_archive(bad, root / "out_bad")

    def test_extract_dmg_non_macos_errors_with_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            dmg = Path(tmp) / "servo-aarch64-apple-darwin.dmg"
            dmg.write_bytes(b"dmg")
            with patch.object(sys, "platform", "linux"):
                with self.assertRaisesRegex(RuntimeError, "--download-only"):
                    nightly.extract_nightly_archive(dmg, Path(tmp) / "servo")

    def test_extract_dmg_on_macos_creates_wrapper_and_detaches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dmg = root / "servo-aarch64-apple-darwin.dmg"
            dmg.write_bytes(b"dmg")
            commands: list[list[str]] = []

            def fake_run(cmd, **kwargs):
                commands.append(cmd)
                if cmd[:2] == ["hdiutil", "attach"]:
                    mount_point = Path(cmd[cmd.index("-mountpoint") + 1])
                    macos_dir = mount_point / "Servo.app" / "Contents" / "MacOS"
                    macos_dir.mkdir(parents=True)
                    exe = macos_dir / "servoshell"
                    exe.write_bytes(b"macho")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with (
                patch.object(sys, "platform", "darwin"),
                patch("servo_fuzzing.nightly.shutil.which", return_value=None),
                patch("servo_fuzzing.nightly.subprocess.run", side_effect=fake_run),
            ):
                dest = root / "servo"
                binary = nightly.extract_nightly_archive(dmg, dest)

            self.assertEqual(binary, dest.resolve() / "servoshell")
            self.assertTrue((dest / "Servo.app" / "Contents" / "MacOS" / "servoshell").is_file())
            wrapper_text = binary.read_text(encoding="utf-8")
            self.assertIn('Servo.app/Contents/MacOS/servoshell', wrapper_text)
            self.assertTrue(os.access(binary, os.X_OK))
            self.assertTrue(any(cmd[:2] == ["hdiutil", "detach"] for cmd in commands))

    def test_download_latest_nightly_end_to_end_mocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload_buf = io.BytesIO()
            with tarfile.open(fileobj=payload_buf, mode="w:gz") as tar:
                data = b"#!/bin/sh\necho servo\n"
                info = tarfile.TarInfo("servo/servoshell")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            payload = payload_buf.getvalue()
            expected_hash = hashlib.sha256(payload).hexdigest()

            def fake_download(url, output_path, token=None, timeout=120.0):
                output_path = Path(output_path)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(payload)
                return output_path

            dest = root / "servo"
            archive_file = root / "servo-aarch64-linux-gnu.tar.gz"
            with (
                patch("servo_fuzzing.nightly.download_bundle", side_effect=fake_download),
                patch("servo_fuzzing.nightly.fetch_expected_sha256", return_value=expected_hash),
            ):
                binary = nightly.download_latest_nightly(
                    output_dir=dest,
                    os_override="linux",
                    arch_override="arm64",
                )

            self.assertEqual(binary, dest.resolve() / "servoshell")
            self.assertTrue(binary.is_file())
            self.assertFalse(archive_file.exists())

    def test_cli_script_print_target(self):
        repo_root = Path(__file__).resolve().parent.parent
        res = subprocess.run(
            [
                sys.executable,
                str(repo_root / "download-latest-nightly.py"),
                "--print-target",
                "--os",
                "macos",
                "--arch",
                "arm64",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("asset=servo-aarch64-apple-darwin.dmg", res.stdout)
        self.assertIn("format=dmg", res.stdout)


if __name__ == "__main__":
    unittest.main()
