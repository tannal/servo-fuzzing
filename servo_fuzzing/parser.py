from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class CrashInfo:
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
    clean = re.sub(r":\d+(?::\d+)?$", "", path_str.strip())

    m = re.search(r"/(?:git/checkouts|checkouts)/[^/]+/[^/]+/(.+\.rs)", clean)
    if m:
        return m.group(1)
    m = re.search(r"(?:/rustc/[0-9a-f]+|/lib/rustlib/src/rust)/(library/.+\.rs)", clean)
    if m:
        return m.group(1)
    m = re.search(r"stylo-[^/]+/[^/]+/((?:style|components)/.+\.rs)", clean)
    if m:
        return m.group(1)

    cargo_match = re.search(r"/(?:cargo|crates\.io[^/]*)/[^/]+/[^/]+/([^/]+/.+\.rs)", clean)
    if cargo_match:
        return cargo_match.group(1)

    cargo_simple = re.search(r"([a-zA-Z0-9_\-]+-\d+\.\d+\.\d+(?:-[^/]+)?/src/.+\.rs)", clean)
    if cargo_simple:
        return cargo_simple.group(1)

    servo_match = re.search(r"((?:components|ports|tests|etc|style)/.+\.rs)", clean)
    if servo_match:
        return servo_match.group(1)

    return clean.lstrip("./")


def normalize_panic_message(msg: str) -> str:
    """Normalize dynamic numbers, addresses, and dimensions in panic messages for deduplication."""
    clean = msg.strip()
    clean = re.sub(r"\b0x[0-9a-fA-F]+\b", "0x<HEX>", clean)
    clean = re.sub(r"\b\d+x\d+\b", "<DIM>", clean)
    clean = re.sub(r"(?<![a-zA-Z_])-?\d+(?:\.\d+)?\b", "<NUM>", clean)
    return clean


def generate_descriptive_name(crash: CrashInfo) -> str:
    """Generate a clean, human-readable, descriptive identifier for a panic.

    Avoids hashes, random IDs, and boilerplate prefixes like
    'internal-error-entered' or 'assertion-failed'.
    """
    src = normalize_source_path(crash.source_file)
    src_stem = Path(src).stem.replace("_", "-")
    crate_match = re.search(r"([a-zA-Z0-9_\-]+)-\d+\.\d+", src)
    if crate_match and not src_stem.startswith(crate_match.group(1).replace("_", "-")):
        crate_name = crate_match.group(1).replace("_", "-")
        prefix = f"{crate_name}-{src_stem}"
    else:
        prefix = src_stem

    msg = crash.message.strip()

    if "Option::unwrap()" in msg and "None" in msg:
        slug = "unwrap-none"
    elif "Result::unwrap()" in msg and "Err" in msg:
        slug = "unwrap-err"
    elif "Result::unwrap_err()" in msg and "Ok" in msg:
        slug = "unwrap-ok"
    elif "out of bounds" in msg.lower():
        slug = "index-out-of-bounds"
    elif "attempt to divide by zero" in msg.lower():
        slug = "divide-by-zero"
    elif "overflow" in msg.lower():
        slug = "overflow"
    else:
        clean = msg
        clean = re.sub(r"^(?:fatal runtime error|panicked at|internal error):\s*", "", clean, flags=re.I)
        clean = re.sub(r"^(?:entered unreachable code|unreachable code):\s*", "", clean, flags=re.I)
        clean = re.sub(r"^assertion \`[^\`]+\` failed:\s*", "", clean, flags=re.I)
        clean = re.sub(r"^assertion failed:\s*", "", clean, flags=re.I)

        tokens = re.findall(r"[A-Za-z0-9]+", clean.lower())
        stop_words = {
            "internal", "error", "entered", "unreachable", "code",
            "assertion", "failed", "called", "panic", "panicked",
            "found", "with", "or", "and", "a", "an", "the", "in", "on", "at",
            "to", "for", "of", "is", "are", "be", "this", "that", "it",
            "some", "none", "non", "self", "but", "len",
        }
        filtered = [t for t in tokens if t not in stop_words and len(t) > 1]
        if filtered:
            slug = "-".join(filtered[:3])
        elif tokens:
            slug = "-".join(tokens[:2])
        else:
            slug = "crash"

    slug_parts = slug.split("-")
    prefix_parts = prefix.split("-")
    remaining_slug = [p for p in slug_parts if p not in prefix_parts]
    if remaining_slug:
        return f"{prefix}-{'-'.join(remaining_slug)}"
    return f"{prefix}-{slug}"


PANIC_HOOK_PATTERN = re.compile(
    r"(?:^|\n)(?P<message>[^\n]+?)\s*\((?:thread\s+(?P<thread>.+?),\s*)?at\s+(?P<path>[^:\n\)]+)(?::(?P<line>\d+)(?::(?P<col>\d+))?)?\)",
    re.MULTILINE,
)

STD_PANIC_PATTERN = re.compile(
    r"thread\s+'(?P<thread>[^']+)'\s+panicked\s+at\s+(?P<path>[^:]+):(?P<line>\d+)(?::(?P<col>\d+))?:\s*\n(?P<message>[^\n]+)",
    re.MULTILINE,
)

FATAL_SIGNAL_PATTERN = re.compile(
    r"(?:Segmentation fault|Illegal instruction|Aborted|Bus error)(?:\s*\(core dumped\))?",
    re.MULTILINE,
)

IGNORE_FRAME_PREFIXES = (
    "servoshell::backtrace::print",
    "servoshell::panic_hook::panic_hook",
    "std::panicking::",
    "core::panicking::",
    "__rustc::",
)

FRAME_PATTERN = re.compile(
    r"^\s*(?:frame\s+)?#?(\d+):?\s+(?:0x[0-9a-fA-F]+\s+(?:in\s+)?)?(?:[a-zA-Z0-9_\-\.]+[`\s]+)?(.+)$"
)


def extract_stack_frames(output: str) -> list[str]:
    frames: list[str] = []
    lines = output.splitlines() if "\n" in output else re.split(r"\s+(?=\d+:\s*)", output)

    for line in lines:
        m = FRAME_PATTERN.match(line.strip())
        if not m:
            continue
        sym = re.split(r"\s+at\s+", m.group(2).strip())[0].strip()
        sym = re.split(r"\(", sym)[0].strip()
        if sym and not any(sym.startswith(p) for p in IGNORE_FRAME_PREFIXES):
            frames.append(sym)

    return frames


ASSERT_OPERANDS_PATTERN = re.compile(
    r"(?P<head>assertion `[^`\n]+` failed[^\n]*)\n\s*left: [^\n]*\n\s*(?P<last>right: [^\n]*)"
)
MAX_MESSAGE_CONTINUATION_LINES = 5


def _unclosed_brackets(text: str) -> int:
    return sum(text.count(c) for c in ")]}") - sum(text.count(c) for c in "([{")


def _full_panic_message(message: str, output: str) -> str:
    """Recover the start of a panic message that spans several lines.

    The panic hook prints the location after the last line, so `message` is only that
    last line: ` right: <value>` for `assert_eq!`, or the tail of an `assert!`
    condition that rustfmt wrapped.
    """
    if message.lstrip().startswith("right: "):
        for m in ASSERT_OPERANDS_PATTERN.finditer(output):
            if m.group("last").startswith(message.strip()):
                return m.group("head")
        return message

    position = output.find(message)
    if position == -1 or _unclosed_brackets(message) <= 0:
        return message
    joined = message.strip()
    for line in reversed(output[:position].splitlines()[-MAX_MESSAGE_CONTINUATION_LINES:]):
        joined = f"{line.strip()} {joined}"
        if _unclosed_brackets(joined) <= 0:
            return joined
    return message


def _make_crash_info(
    message: str,
    path_str: str,
    output: str,
    thread: Optional[str] = None,
    line: Optional[str] = None,
    col: Optional[str] = None,
) -> CrashInfo:
    message = _full_panic_message(message, output)
    if message.startswith("panic: "):
        message = message[7:].strip()
    return CrashInfo(
        message=message.strip(),
        source_file=normalize_source_path(path_str.strip()),
        stack_frames=extract_stack_frames(output),
        thread=thread,
        line=int(line) if line else None,
        column=int(col) if col else None,
        raw_source=path_str.strip(),
        raw_output=output,
    )


PANIC_HOOK_MARKER = "Panic hook called."
PANIC_MESSAGE_KEYWORDS = ("panic", "assertion failed", "called `", "unwrap()", "entered unreachable code")


def _find_panic_hook_match(output: str) -> Optional[re.Match]:
    """Pick the panic line among every `message (at location)` line in the output.

    Page output such as CSS warnings (`gradient(at center 10em ...)`) has the same
    shape, so a line naming a Rust source file or panic wording wins over the rest,
    and lines printed before the panic hook ran are only a last resort.
    """
    matches = list(PANIC_HOOK_PATTERN.finditer(output))
    marker = output.find(PANIC_HOOK_MARKER)
    after_hook = [m for m in matches if m.start() >= marker] if marker != -1 else []

    def looks_like_panic(m: re.Match) -> bool:
        return ".rs" in m.group("path") or any(k in m.group("message").lower() for k in PANIC_MESSAGE_KEYWORDS)

    for candidates in (after_hook, matches):
        for m in candidates:
            if looks_like_panic(m):
                return m
    if "panic_hook" not in output:
        return None
    for candidates in (after_hook, matches):
        if candidates:
            return candidates[0]
    return None


def parse_crash(output: str) -> Optional[CrashInfo]:
    if not output:
        return None

    m = _find_panic_hook_match(output)
    if m:
        return _make_crash_info(
            message=m.group("message"),
            path_str=m.group("path"),
            output=output,
            thread=m.group("thread"),
            line=m.group("line"),
            col=m.group("col"),
        )

    std_match = STD_PANIC_PATTERN.search(output)
    if std_match:
        return _make_crash_info(
            message=std_match.group("message"),
            path_str=std_match.group("path"),
            output=output,
            thread=std_match.group("thread"),
            line=std_match.group("line"),
            col=std_match.group("col"),
        )

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
