"""Tests for nightly bisection."""
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from servo_fuzzing.bisect import (
    BisectResult,
    Nightly,
    bisect_nightlies,
    build_dir_for,
    fetch_commit_range,
    fetch_nightlies,
    format_bisect_report,
    parse_release,
    reproduces,
)
from servo_fuzzing.executor import TestStatus


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

    def test_launch_failure_is_error_not_good(self):
        failed = SimpleNamespace(status=TestStatus.PASS, exit_code=127, error_message=None,
                                 combined_output="error while loading shared libraries", crash_info=None)
        with patch("servo_fuzzing.bisect.run_single_test", return_value=failed):
            with self.assertRaisesRegex(ValueError, "Failed to launch"):
                reproduces(Path("servoshell"), Path("t.html"), "panic")

    def test_native_crash_without_panic_text_reproduces(self):
        segfault = SimpleNamespace(status=TestStatus.NEW_CRASH, exit_code=-11, error_message=None,
                                   combined_output="", stdout="", stderr="", crash_info=None)
        with patch("servo_fuzzing.bisect.run_single_test", return_value=segfault):
            self.assertTrue(reproduces(Path("servoshell"), Path("t.html"), None, attempts=1))
            self.assertFalse(reproduces(Path("servoshell"), Path("t.html"), "panic", attempts=1))

    def test_build_dir_depends_on_asset(self):
        linux = Nightly("2026-01-01", None, "https://github.com/servo/servo-nightly-builds/releases/download/2026-01-01/linux.tar.gz")
        arm = Nightly("2026-01-01", None, "https://github.com/servo/servo-nightly-builds/releases/download/2026-01-01/arm.tar.gz")
        self.assertNotEqual(build_dir_for(linux, Path("c")), build_dir_for(arm, Path("c")))

    def test_commit_range_follows_pages(self):
        def page(n):
            return {"total_commits": 150, "commits": [
                {"sha": f"{i:040x}", "commit": {"message": f"c{i}"}} for i in range((n - 1) * 100, min(n * 100, 150))
            ]}
        good, bad = make_nightlies(2)
        result = BisectResult(last_good=good, first_bad=bad, tested={})
        with patch("servo_fuzzing.bisect._get_json", side_effect=lambda url, _: page(int(url.rsplit("=", 1)[1]))):
            commits = fetch_commit_range(result)
        self.assertEqual(len(commits), 150)


    def test_nightly_list_ends_where_github_stops_paginating(self):
        def release(tag):
            return {"tag_name": tag, "body": "", "assets": [{"name": "a.tar.gz", "browser_download_url": f"https://x/{tag}"}]}

        def page(url, _token):
            number = int(url.rsplit("=", 1)[1])
            if number > 2:
                raise urllib.error.HTTPError(url, 422, "Only the first 1000 results are available.", None, None)
            return [release(f"2026-0{number}-{day:02d}") for day in range(1, 4)]

        with patch("servo_fuzzing.bisect._get_json", side_effect=page):
            nightlies = fetch_nightlies(asset_name="a.tar.gz")
        self.assertEqual([n.tag for n in nightlies][::5], ["2026-01-01", "2026-02-03"])
        self.assertEqual(len(nightlies), 6)

    def test_nightly_list_error_on_first_page_is_raised(self):
        error = urllib.error.HTTPError("u", 422, "Validation Failed", None, None)
        with patch("servo_fuzzing.bisect._get_json", side_effect=error):
            with self.assertRaises(urllib.error.HTTPError):
                fetch_nightlies(asset_name="a.tar.gz")


if __name__ == "__main__":
    unittest.main()
