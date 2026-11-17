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

    def test_pack_from_triage(self):
        import json
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            source = tmp / "src"
            source.mkdir()
            tc1 = source / "test1.html"
            tc2 = source / "test2.html"
            tc1.write_text("<html>test 1</html>")
            tc2.write_text("<html>test 2</html>")

            triage_dir = tmp / "triage"
            triage_dir.mkdir()
            summary_data = {
                "results": [
                    {"testcase": str(tc1), "status": "PASS"},
                    {"testcase": str(tc2), "status": "TIMEOUT"},
                ]
            }
            (triage_dir / "summary.json").write_text(json.dumps(summary_data))

            tar_path = tmp / "bundle.tar.gz"
            packed = pack_bundle(source_dir=source, output_tar=tar_path, from_triage=triage_dir)
            self.assertTrue(packed.exists())

            dest = tmp / "dest"
            unpacked = unpack_bundle(bundle_tar=packed, target_dir=dest)
            self.assertEqual(len(unpacked), 1)
            self.assertTrue((dest / "test1.html").exists())
            self.assertFalse((dest / "test2.html").exists())

    def _triaged_source(self, tmp, passing, timed_out):
        import json
        source = tmp / "src"
        source.mkdir()
        results = []
        for prefix, count, status in (("pass", passing, "PASS"), ("hang", timed_out, "TIMEOUT")):
            for i in range(count):
                tc = source / f"{prefix}{i}.html"
                tc.write_text(f"<html>{prefix} {i}</html>")
                results.append({"testcase": str(tc), "status": status})
        (tmp / "summary.json").write_text(json.dumps({"results": results}))
        return source, tmp / "summary.json"

    def _packed_names(self, tmp, **kwargs):
        packed = pack_bundle(output_tar=tmp / "bundle.tar.gz", **kwargs)
        return sorted(p.name for p in unpack_bundle(bundle_tar=packed, target_dir=tmp / "dest"))

    def test_pack_from_triage_can_include_timeouts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            source, summary = self._triaged_source(tmp, passing=2, timed_out=2)
            names = self._packed_names(tmp, source_dir=source, from_triage=summary, include_timeouts=True)
            self.assertEqual(names, ["hang0.html", "hang1.html", "pass0.html", "pass1.html"])

    def test_included_timeouts_are_capped_and_count_towards_max_tests(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            source, summary = self._triaged_source(tmp, passing=5, timed_out=5)
            names = self._packed_names(
                tmp,
                source_dir=source,
                from_triage=summary,
                include_timeouts=True,
                max_timeout_tests=2,
                max_tests=4,
            )
            self.assertEqual(names, ["hang0.html", "hang1.html", "pass0.html", "pass1.html"])


if __name__ == "__main__":
    unittest.main()
