import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from servo_fuzzing.bundle import obtain_bundle

class RefreshTests(unittest.TestCase):
    def test_latest_checks_server_even_when_cache_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            archive = cache / "fuzz-bundle-latest.tar.gz"
            archive.write_bytes(b"old bundle")
            with patch("servo_fuzzing.bundle.download_bundle") as download:
                self.assertEqual(obtain_bundle(cache_dir=cache), archive)
                download.assert_called_once()

    def test_local_bundle_does_not_download(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "local.tar.gz"
            archive.write_bytes(b"local bundle")
            with patch("servo_fuzzing.bundle.is_valid_tar_gz", return_value=True), patch("servo_fuzzing.bundle.download_bundle") as download:
                self.assertEqual(obtain_bundle(str(archive)), archive)
                download.assert_not_called()
