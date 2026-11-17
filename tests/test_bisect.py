"""Tests for nightly bisection."""
import unittest

from servo_fuzzing.bisect import BisectResult, Nightly, bisect_nightlies, format_bisect_report, parse_release


def make_nightlies(count):
    return [Nightly(tag=f"2026-01-{i + 1:02d}", sha=f"{i:07x}", asset_url=f"https://x/{i}") for i in range(count)]


class TestBisect(unittest.TestCase):
    def run_bisect(self, count, first_bad, **kwargs):
        nightlies = make_nightlies(count)
        calls = []

        def is_bad(nightly):
            calls.append(nightly.tag)
            return nightlies.index(nightly) >= first_bad

        return bisect_nightlies(nightlies, is_bad, **kwargs), calls

    def test_finds_first_bad_with_doubling_search(self):
        result, calls = self.run_bisect(30, 7)
        self.assertEqual(result.first_bad.tag, "2026-01-08")
        self.assertEqual(result.last_good.tag, "2026-01-07")
        self.assertEqual(len(calls), len(set(calls)))
        self.assertLess(len(calls), 12)

    def test_respects_explicit_good_and_bad(self):
        result, calls = self.run_bisect(30, 12, good="2026-01-05", bad="2026-01-20")
        self.assertEqual(result.first_bad.tag, "2026-01-13")
        self.assertTrue(all("2026-01-05" <= tag <= "2026-01-20" for tag in calls))

    def test_latest_not_reproducing_is_error(self):
        with self.assertRaisesRegex(ValueError, "does not reproduce"):
            self.run_bisect(10, 99)

    def test_reproduces_on_oldest_is_error(self):
        with self.assertRaisesRegex(ValueError, "oldest available"):
            self.run_bisect(10, 0)

    def test_good_that_reproduces_is_error(self):
        with self.assertRaisesRegex(ValueError, "also reproduces"):
            self.run_bisect(10, 2, good="2026-01-04")

    def test_parse_release(self):
        release = {
            "tag_name": "2026-11-15",
            "body": "Nightly build based on servo/servo@ec2138dac1f2ef16ed23406b035bb2db05826794",
            "assets": [
                {"name": "servo-x86_64-linux-gnu.tar.gz", "browser_download_url": "https://dl/linux.tar.gz"},
                {"name": "servo-aarch64-android.apk", "browser_download_url": "https://dl/android.apk"},
            ],
        }
        nightly = parse_release(release, "servo-x86_64-linux-gnu.tar.gz")
        self.assertEqual(nightly.sha, "ec2138dac1f2ef16ed23406b035bb2db05826794")
        self.assertEqual(nightly.asset_url, "https://dl/linux.tar.gz")
        self.assertIsNone(parse_release(release, "missing.tar.gz"))
        self.assertIsNone(parse_release({**release, "tag_name": "v0.8.0-rc1"}, "servo-x86_64-linux-gnu.tar.gz"))

    def test_report_includes_compare_url(self):
        good, bad = make_nightlies(2)
        result = BisectResult(last_good=good, first_bad=bad, tested={good.tag: False, bad.tag: True})
        report = format_bisect_report(result, __import__("pathlib").Path("t.html"), ["abc fix"])
        self.assertIn("https://github.com/servo/servo/compare/0000000...0000001", report)
        self.assertIn("- abc fix", report)


if __name__ == "__main__":
    unittest.main()
