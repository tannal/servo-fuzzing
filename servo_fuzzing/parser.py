"""Crash output parser for Servo fuzzing.

Extracts panic messages, source locations, and stack backtrace frames from
servoshell output logs or GitHub issue descriptions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CrashInfo:
    """Structured representation of a crash / panic."""

    message: str
    source_file: str
    stack_frames: list[str] = field(default_factory=list)
    thread: Optional[str] = None
    line: Optional[int] = None
    column: Optional[int] = None
    raw_source: Optional[str] = None
    raw_output: str = ""

    def __post_init__(self) -> None:
        self.message = self.message.strip()
        self.source_file = self.source_file.strip()


def normalize_source_path(path_str: str) -> str:
    """Normalize source file paths from Rust backtraces.

    Examples:
        - "components/script/dom/window/windowproxy.rs:950" -> "components/script/dom/window/windowproxy.rs"
        - ".../cargo/registry/src/.../euclid-0.22.14/src/size.rs:123" -> "euclid-0.22.14/src/size.rs"
        - "./components/layout/lib.rs:10:5" -> "components/layout/lib.rs"
    """
    clean = path_str.strip()

    # Strip line and column numbers (:123:45 or :123)
    clean = re.sub(r":\d+(?::\d+)?$", "", clean)

    # Normalize cargo registry crate paths (e.g. .../euclid-0.22.14/src/size.rs)
    cargo_match = re.search(r"/(?:cargo|crates\.io[^/]*)/[^/]+/[^/]+/([^/]+/.+\.rs)", clean)
    if cargo_match:
        return cargo_match.group(1)

    cargo_simple = re.search(r"([a-zA-Z0-9_\-]+-\d+\.\d+\.\d+(?:-[^/]+)?/src/.+\.rs)", clean)
    if cargo_simple:
        return cargo_simple.group(1)

    # Normalize Servo repository components / ports / etc.
    servo_match = re.search(r"((?:components|ports|tests|etc)/.+\.rs)", clean)
    if servo_match:
        return servo_match.group(1)

    # Strip leading ./ or /
    clean = clean.lstrip("./")
    return clean


# Regexp for servoshell custom panic hook format:
# "<message> (thread <thread>, at <path>:<line>)" or "<message> (at <path>:<line>)"
PANIC_HOOK_PATTERN = re.compile(
    r"(?:^|\n)(?P<message>[^\n]+?)\s*\((?:thread\s+(?P<thread>[^,]+),\s*)?at\s+(?P<path>[^:\n\)]+)(?::(?P<line>\d+)(?::(?P<col>\d+))?)?\)",
    re.MULTILINE,
)

# Standard Rust panic:
# "thread '<thread>' panicked at <path>:<line>:<col>:" followed by message
STD_PANIC_PATTERN = re.compile(
    r"thread\s+'(?P<thread>[^']+)'\s+panicked\s+at\s+(?P<path>[^:]+):(?P<line>\d+)(?::(?P<col>\d+))?:\s*\n(?P<message>[^\n]+)",
    re.MULTILINE,
)

# Fatal signals / aborts
FATAL_SIGNAL_PATTERN = re.compile(
    r"(?:Segmentation fault|Illegal instruction|Aborted|Bus error)(?:\s*\(core dumped\))?",
    re.MULTILINE,
)


def extract_stack_frames(output: str) -> list[str]:
    """Extract backtrace frames from output.

    Handles both multi-line stack backtraces and single-line condensed backtraces.
    """
    frames: list[str] = []

    # If the output contains multi-line format "   0: symbol"
    frame_matches = list(re.finditer(r"^\s*(\d+):\s*(.+)$", output, re.MULTILINE))
    if frame_matches:
        for match in frame_matches:
            raw_sym = match.group(2).strip()
            # If the line contains " at /path/to/file.rs:line:col", extract the symbol name
            sym = re.split(r"\s+at\s+", raw_sym)[0].strip()
            if sym and not sym.startswith("servoshell::backtrace::print") and not sym.startswith("servoshell::panic_hook::panic_hook"):
                frames.append(sym)
        return frames

    # Check for single-line condensed format "0: sym 1: sym 2: sym"
    condensed_tokens = re.split(r"\s+(?=\d+:\s*)", output)
    for token in condensed_tokens:
        token_match = re.match(r"^\s*\d+:\s*(.+)$", token.strip())
        if token_match:
            raw_sym = token_match.group(1).strip()
            sym = re.split(r"\s+at\s+", raw_sym)[0].strip()
            if sym and not sym.startswith("servoshell::backtrace::print") and not sym.startswith("servoshell::panic_hook::panic_hook"):
                frames.append(sym)

    return frames


def parse_crash(output: str) -> Optional[CrashInfo]:
    """Parse crash information from process output.

    Returns CrashInfo if a panic/crash is identified, otherwise None.
    """
    if not output:
        return None

    # Try servoshell panic hook format first
    for match in PANIC_HOOK_PATTERN.finditer(output):
        message = match.group("message").strip()
        # Filter out false positives (e.g. debug logs)
        if "panic" in message.lower() or "assertion failed" in message or "called `" in message or "unwrap()" in message or "panic_hook" in output:
            path_str = match.group("path").strip()
            thread = match.group("thread")
            line = int(match.group("line")) if match.group("line") else None
            col = int(match.group("col")) if match.group("col") else None
            source_file = normalize_source_path(path_str)
            frames = extract_stack_frames(output)

            # Strip any leading "panic: " if present in message
            if message.startswith("panic: "):
                message = message[7:].strip()

            return CrashInfo(
                message=message,
                source_file=source_file,
                stack_frames=frames,
                thread=thread,
                line=line,
                column=col,
                raw_source=path_str,
                raw_output=output,
            )

    # Try standard Rust panic format
    std_match = STD_PANIC_PATTERN.search(output)
    if std_match:
        message = std_match.group("message").strip()
        path_str = std_match.group("path").strip()
        thread = std_match.group("thread")
        line = int(std_match.group("line")) if std_match.group("line") else None
        col = int(std_match.group("col")) if std_match.group("col") else None
        source_file = normalize_source_path(path_str)
        frames = extract_stack_frames(output)

        if message.startswith("panic: "):
            message = message[7:].strip()

        return CrashInfo(
            message=message,
            source_file=source_file,
            stack_frames=frames,
            thread=thread,
            line=line,
            column=col,
            raw_source=path_str,
            raw_output=output,
        )

    # Check for general unhandled fatal signals without explicit rust panic
    signal_match = FATAL_SIGNAL_PATTERN.search(output)
    if signal_match:
        return CrashInfo(
            message=signal_match.group(0).strip(),
            source_file="<signal>",
            stack_frames=extract_stack_frames(output),
            thread=None,
            line=None,
            column=None,
            raw_source="<signal>",
            raw_output=output,
        )

    return None
