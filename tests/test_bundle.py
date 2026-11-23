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

    def test_resolve_bundle_spec_versioning_and_immutable_cache(self):
        from unittest.mock import patch
        from servo_fuzzing.bundle import obtain_bundle, resolve_bundle_spec

        url, asset, imm = resolve_bundle_spec("latest", repo="tannal/servo-fuzzing")
        self.assertEqual(url, "https://github.com/tannal/servo-fuzzing/releases/latest/download/fuzz-bundle-latest.tar.gz")
        self.assertEqual(asset, "fuzz-bundle-latest.tar.gz")
        self.assertFalse(imm)

        url, asset, imm = resolve_bundle_spec("crashes", repo="tannal/servo-fuzzing")
        self.assertTrue(url.endswith("/new-panic-corpus.tar.gz"))
        self.assertFalse(imm)

        for spec in ("2026-11-23", "bundle-2026-11-23", "fuzz-bundle-2026-11-23.tar.gz"):
            url, asset, imm = resolve_bundle_spec(spec, repo="tannal/servo-fuzzing")
            self.assertEqual(
                url,
                "https://github.com/tannal/servo-fuzzing/releases/download/bundle-2026-11-23/fuzz-bundle-2026-11-23.tar.gz",
            )
            self.assertEqual(asset, "fuzz-bundle-2026-11-23.tar.gz")
            self.assertTrue(imm)

        url, asset, imm = resolve_bundle_spec("2026-11-23.2", repo="tannal/servo-fuzzing")
        self.assertTrue(url.endswith("/bundle-2026-11-23.2/fuzz-bundle-2026-11-23.2.tar.gz"))
        self.assertTrue(imm)

        with tempfile.TemporaryDirectory() as tmpdir:
            cache = Path(tmpdir)
            cached = cache / "fuzz-bundle-2026-11-23.tar.gz"
            cached.write_bytes(b"cached")
            with patch("servo_fuzzing.bundle.is_valid_tar_gz", return_value=True), patch(
                "servo_fuzzing.bundle.download_bundle"
            ) as dl:
                self.assertEqual(obtain_bundle("bundle-2026-11-23", cache_dir=cache, repo="tannal/servo-fuzzing"), cached)
                dl.assert_not_called()
                obtain_bundle("bundle-2026-11-23", cache_dir=cache, repo="tannal/servo-fuzzing", force_download=True)
                dl.assert_called_once()

    def test_pack_per_generator_balanced_and_manifest(self):
        import json
        from servo_fuzzing.bundle import read_bundle_manifest

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            src = tmp / "generated"
            results = []
            for gen in ("domato", "freedom", "minerva"):
                gdir = src / gen
                gdir.mkdir(parents=True)
                for i in range(6):
                    tc = gdir / f"p{i}.html"
                    tc.write_text(f"<p>{gen} p{i}</p>")
                    results.append({"testcase": str(tc), "status": "PASS"})
                for i in range(2):
                    tc = gdir / f"t{i}.html"
                    tc.write_text(f"<p>{gen} t{i}</p>")
                    results.append({"testcase": str(tc), "status": "TIMEOUT"})

            summary_file = tmp / "summary.json"
            summary_file.write_text(json.dumps({"results": results}))

            # 1. Timeouts excluded -> 4 per generator (12 total)
            tar_excl = tmp / "excl.tar.gz"
            pack_bundle(
                source_dir=src,
                output_tar=tar_excl,
                from_triage=summary_file,
                per_generator=4,
                max_tests=12,
                include_timeouts=False,
                version="bundle-2026-11-23",
                servo_version="Servo 0.8.0",
            )
            dest_excl = tmp / "dest_excl"
            unpacked_excl = unpack_bundle(tar_excl, dest_excl)
            self.assertEqual(len(unpacked_excl), 12)
            m_excl = read_bundle_manifest(dest_excl)
            self.assertIsNotNone(m_excl)
            assert m_excl is not None
            self.assertEqual(m_excl["version"], "bundle-2026-11-23")
            self.assertEqual(m_excl["timeouts"], "exclude")
            self.assertEqual(m_excl["generators"], {"domato": 4, "freedom": 4, "minerva": 4})
            self.assertEqual(m_excl["timeout_tests"], [])

            # 2. Timeouts included -> max_timeout_tests=3 (1 per generator) + 3 pass each = 12 total
            tar_incl = tmp / "incl.tar.gz"
            pack_bundle(
                source_dir=src,
                output_tar=tar_incl,
                from_triage=summary_file,
                per_generator=4,
                max_tests=12,
                include_timeouts=True,
                max_timeout_tests=3,
                version="bundle-2026-11-23.2",
            )
            dest_incl = tmp / "dest_incl"
            unpacked_incl = unpack_bundle(tar_incl, dest_incl)
            self.assertEqual(len(unpacked_incl), 12)
            m_incl = read_bundle_manifest(dest_incl)
            assert m_incl is not None
            self.assertEqual(m_incl["timeouts"], "include")
            self.assertEqual(m_incl["generators"], {"domato": 4, "freedom": 4, "minerva": 4})
            self.assertEqual(sorted(m_incl["timeout_tests"]), ["domato/t0.html", "freedom/t0.html", "minerva/t0.html"])


if __name__ == "__main__":
    unittest.main()
