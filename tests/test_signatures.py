"""Tests for signature loading, validation, and matching."""

import json
import tempfile
import unittest
from pathlib import Path

from servo_fuzzing.parser import CrashInfo
from servo_fuzzing.signatures import (
    CrashSignature,
    find_signature_by_id,
    find_signature_by_issue,
    load_signatures,
    match,
    save_signatures,
)


class TestSignatures(unittest.TestCase):
    def setUp(self):
        self.signatures: list[CrashSignature] = [
            {
                "id": "windowproxy-parent-active",
                "message": "assertion failed: parent_proxy.currently_active().is_some()",
                "source_file": "components/script/dom/window/windowproxy.rs",
                "stack_frames": [
                    "<script::dom::servoparser::ParserContext>::initialize_document_object"
                ],
                "issue": "https://github.com/servo/servo/issues/49411",
            },
            {
                "id": "multi-frame-sig",
                "message": "test panic",
                "source_file": "components/test.rs",
                "stack_frames": ["frame_a", "frame_b"],
                "issue": "https://github.com/servo/servo/issues/123",
            },
        ]

    def test_load_valid_signatures(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            data = {"schema_version": 1, "signatures": self.signatures}
            json.dump(data, f)
            path = Path(f.name)

        loaded = load_signatures(path)
        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[0]["id"], "windowproxy-parent-active")
        path.unlink()

    def test_load_invalid_schema(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            # schema_version wrong
            json.dump({"schema_version": 2, "signatures": []}, f)
            path = Path(f.name)

        with self.assertRaises(ValueError):
            load_signatures(path)
        path.unlink()

    def test_duplicate_signature_id(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            sigs = [self.signatures[0], self.signatures[0]]
            json.dump({"schema_version": 1, "signatures": sigs}, f)
            path = Path(f.name)

        with self.assertRaises(ValueError):
            load_signatures(path)
        path.unlink()

    def test_match_exact_and_ordered_frames(self):
        # Matching crash
        crash = CrashInfo(
            message="assertion failed: parent_proxy.currently_active().is_some()",
            source_file="components/script/dom/window/windowproxy.rs",
            stack_frames=[
                "servoshell::panic_hook",
                "<script::dom::servoparser::ParserContext>::initialize_document_object",
                "main",
            ],
        )
        matched_id = match(crash, self.signatures)
        self.assertEqual(matched_id, "windowproxy-parent-active")

    def test_match_ordered_stack_frames(self):
        # Correct order
        crash_good = CrashInfo(
            message="test panic",
            source_file="components/test.rs",
            stack_frames=["first", "frame_a", "middle", "frame_b", "last"],
        )
        self.assertEqual(match(crash_good, self.signatures), "multi-frame-sig")

        # Inverted order: should NOT match
        crash_bad = CrashInfo(
            message="test panic",
            source_file="components/test.rs",
            stack_frames=["first", "frame_b", "middle", "frame_a", "last"],
        )
        self.assertIsNone(match(crash_bad, self.signatures))

    def test_mismatched_message_or_file(self):
        # Mismatched message
        c1 = CrashInfo(
            message="different message",
            source_file="components/script/dom/window/windowproxy.rs",
            stack_frames=["<script::dom::servoparser::ParserContext>::initialize_document_object"],
        )
        self.assertIsNone(match(c1, self.signatures))

        # Mismatched file
        c2 = CrashInfo(
            message="assertion failed: parent_proxy.currently_active().is_some()",
            source_file="components/other/file.rs",
            stack_frames=["<script::dom::servoparser::ParserContext>::initialize_document_object"],
        )
        self.assertIsNone(match(c2, self.signatures))

    def test_find_helpers(self):
        sig = find_signature_by_id(self.signatures, "windowproxy-parent-active")
        self.assertIsNotNone(sig)
        assert sig is not None
        self.assertEqual(sig["issue"], "https://github.com/servo/servo/issues/49411")

        by_issue = find_signature_by_issue(self.signatures, "49411")
        self.assertIsNotNone(by_issue)
        assert by_issue is not None
        self.assertEqual(by_issue["id"], "windowproxy-parent-active")


if __name__ == "__main__":
    unittest.main()
