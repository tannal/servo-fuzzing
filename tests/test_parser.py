"""Tests for crash and backtrace parsing."""

import unittest
from servo_fuzzing.parser import (
    CrashInfo,
    extract_stack_frames,
    normalize_source_path,
    parse_crash,
)


class TestParser(unittest.TestCase):
    def test_normalize_source_path(self):
        # Component path with line
        p1 = normalize_source_path("components/script/dom/window/windowproxy.rs:950")
        self.assertEqual(p1, "components/script/dom/window/windowproxy.rs")

        # Path with line and col
        p2 = normalize_source_path("components/layout/lib.rs:42:15")
        self.assertEqual(p2, "components/layout/lib.rs")

        # Cargo registry path
        p3 = normalize_source_path("/home/user/.cargo/registry/src/index.crates.io-6f17d22bba15001f/euclid-0.22.14/src/size.rs:123:5")
        self.assertEqual(p3, "euclid-0.22.14/src/size.rs")

        # Simple path
        p4 = normalize_source_path("tiny-skia-path-0.12.0/src/alpha_run.rs:88")
        self.assertEqual(p4, "tiny-skia-path-0.12.0/src/alpha_run.rs")

    def test_parse_servoshell_hook_output(self):
        sample_output = """
assertion failed: parent_proxy.currently_active().is_some() (thread Script#4, at components/script/dom/window/windowproxy.rs:950)
stack backtrace:
   0: servoshell::backtrace::print
   1: servoshell::panic_hook::panic_hook
   2: std::panicking::panic_with_hook
   3: std::panicking::panic_handler::{closure#0}
   4: std::sys::backtrace::__rust_end_short_backtrace::<std::panicking::panic_handler::{closure#0}, !>
   5: __rustc::rust_begin_unwind
   6: core::panicking::panic_fmt
   7: core::panicking::panic
   8: <script::dom::servoparser::ParserContext>::initialize_document_object
   9: <script::dom::servoparser::ParserContext>::load_document
  10: <script::dom::servoparser::ParserContext>::process_response_eof
"""
        crash = parse_crash(sample_output)
        self.assertIsNotNone(crash)
        assert crash is not None
        self.assertEqual(crash.message, "assertion failed: parent_proxy.currently_active().is_some()")
        self.assertEqual(crash.source_file, "components/script/dom/window/windowproxy.rs")
        self.assertEqual(crash.thread, "Script#4")
        self.assertEqual(crash.line, 950)
        self.assertIn("<script::dom::servoparser::ParserContext>::initialize_document_object", crash.stack_frames)

    def test_parse_condensed_backtrace(self):
        sample_condensed = (
            "assertion failed: parent_proxy.currently_active().is_some() (thread Script#4, at components/script/dom/window/windowproxy.rs:950) "
            "0: servoshell::backtrace::print 1: servoshell::panic_hook::panic_hook "
            "8: <script::dom::servoparser::ParserContext>::initialize_document_object"
        )
        crash = parse_crash(sample_condensed)
        self.assertIsNotNone(crash)
        assert crash is not None
        self.assertEqual(crash.message, "assertion failed: parent_proxy.currently_active().is_some()")
        self.assertIn("<script::dom::servoparser::ParserContext>::initialize_document_object", crash.stack_frames)

    def test_parse_crates_io_unwrap_panic(self):
        sample_output = """
called `Option::unwrap()` on a `None` value (thread Script#1, at /root/.cargo/registry/src/index.crates.io-6f17d22bba15001f/euclid-0.22.14/src/size.rs:120)
stack backtrace:
   0: servoshell::backtrace::print
   1: servoshell::panic_hook::panic_hook
   2: euclid::size
"""
        crash = parse_crash(sample_output)
        self.assertIsNotNone(crash)
        assert crash is not None
        self.assertEqual(crash.message, "called `Option::unwrap()` on a `None` value")
        self.assertEqual(crash.source_file, "euclid-0.22.14/src/size.rs")
        self.assertIn("euclid::size", crash.stack_frames)

    def test_parse_standard_rust_panic(self):
        sample_output = """
thread 'Script#2' panicked at components/layout/lib.rs:45:10:
some custom panic message
stack backtrace:
   0: std::panicking::begin_panic
   1: layout::process
"""
        crash = parse_crash(sample_output)
        self.assertIsNotNone(crash)
        assert crash is not None
        self.assertEqual(crash.message, "some custom panic message")
        self.assertEqual(crash.source_file, "components/layout/lib.rs")
        self.assertEqual(crash.line, 45)
        self.assertEqual(crash.thread, "Script#2")

    def test_generate_descriptive_name(self):
        from servo_fuzzing.parser import generate_descriptive_name

        # 1. Unreachable with descriptive message
        c1 = CrashInfo(
            message="internal error: entered unreachable code: Found hoisted box with missing or non-BoxFragment.",
            source_file="components/layout/display_list/stacking_context.rs:633",
        )
        name1 = generate_descriptive_name(c1)
        self.assertIn("stacking-context", name1)
        self.assertIn("hoisted-box", name1)
        self.assertNotIn("internal-error", name1)
        self.assertNotIn("panic-", name1)

        # 2. Option unwrap on None
        c2 = CrashInfo(
            message="called `Option::unwrap()` on a `None` value",
            source_file="tiny-skia-path-0.12.0/src/alpha_run.rs:88",
        )
        name2 = generate_descriptive_name(c2)
        self.assertEqual(name2, "tiny-skia-path-alpha-run-unwrap-none")

        # 3. Euclid crate unwrap
        c3 = CrashInfo(
            message="called `Option::unwrap()` on a `None` value",
            source_file="euclid-0.22.14/src/size.rs:120",
        )
        name3 = generate_descriptive_name(c3)
        self.assertEqual(name3, "euclid-size-unwrap-none")

        # 4. Assertion failure expression
        c4 = CrashInfo(
            message="assertion failed: distance_to_stack_bottom <= max_allowable_distance",
            source_file="components/script/dom/stack.rs:42",
        )
        name4 = generate_descriptive_name(c4)
        self.assertIn("stack-distance", name4)
        self.assertNotIn("assertion-failed", name4)


if __name__ == "__main__":
    unittest.main()
