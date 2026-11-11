"""Tests for bundle packing and unpacking."""

import tempfile
import unittest
from pathlib import Path

from servo_fuzzing.bundle import pack_bundle, unpack_bundle


class TestBundle(unittest.TestCase):
    def test_pack_and_unpack(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            source = tmp / "src"
            source.mkdir()
            (source / "test1.html").write_text("<html>test 1</html>")
            (source / "test2.html").write_text("<html>test 2</html>")

            tar_path = tmp / "bundle.tar.gz"
            packed = pack_bundle(source_dir=source, output_tar=tar_path)
            self.assertTrue(packed.exists())

            dest = tmp / "dest"
            unpacked = unpack_bundle(bundle_tar=packed, target_dir=dest)
            self.assertEqual(len(unpacked), 2)
            self.assertTrue((dest / "test1.html").exists())
            self.assertTrue((dest / "test2.html").exists())


if __name__ == "__main__":
    unittest.main()
