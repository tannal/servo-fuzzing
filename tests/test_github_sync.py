import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from servo_fuzzing.github_sync import fetch_open_fuzzing_issues, sync_signatures_from_github

class SyncTests(unittest.TestCase):
    def response(self, items, total):
        return io.BytesIO(json.dumps({"items": items, "total_count": total,
                                    "incomplete_results": False}).encode())

    def test_label_and_pagination(self):
        with patch("servo_fuzzing.github_sync.urllib.request.urlopen", side_effect=[
            self.response([{"number": 1}], 2), self.response([{"number": 2}], 2)
        ]) as request:
            self.assertEqual(len(fetch_open_fuzzing_issues(per_page=1)), 2)
            query = parse_qs(urlparse(request.call_args_list[0].args[0].full_url).query)
            self.assertEqual(query["q"], ['repo:servo/servo is:issue is:open label:"B-fuzzing"'])
            self.assertEqual(parse_qs(urlparse(request.call_args.args[0].full_url).query)["page"], ["2"])

    def test_api_error_does_not_change_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "signatures.json"
            original = '{"schema_version":1,"signatures":[]}'
            path.write_text(original)
            with patch("servo_fuzzing.github_sync.urllib.request.urlopen", side_effect=HTTPError("url",403,"Forbidden",{},None)):
                with self.assertRaises(HTTPError):
                    sync_signatures_from_github(path)
            self.assertEqual(path.read_text(), original)

    def test_incomplete_response_rejected(self):
        with patch("servo_fuzzing.github_sync.urllib.request.urlopen", return_value=io.BytesIO(
            json.dumps({"items": [], "total_count": 1, "incomplete_results": True}).encode()
        )):
            with self.assertRaises(ValueError):
                fetch_open_fuzzing_issues()

    def test_empty_page_with_nonzero_total_rejected(self):
        with patch("servo_fuzzing.github_sync.urllib.request.urlopen", return_value=self.response([],1)):
            with self.assertRaises(ValueError):
                fetch_open_fuzzing_issues()
