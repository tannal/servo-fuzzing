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

    m = re.search(r"/(?:git/checkouts|checkouts)/[^/]+/[^/]+/((?:style|components|ports|tests)/.+\.rs)", clean)
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


def _make_crash_info(
    message: str,
    path_str: str,
    output: str,
    thread: Optional[str] = None,
    line: Optional[str] = None,
    col: Optional[str] = None,
) -> CrashInfo:
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


def parse_crash(output: str) -> Optional[CrashInfo]:
    if not output:
        return None

    for m in PANIC_HOOK_PATTERN.finditer(output):
        path_str = m.group("path")
        msg = m.group("message")
        if ".rs" in path_str or any(k in msg.lower() for k in ("panic", "assertion failed", "called `", "unwrap()", "entered unreachable code")) or "panic_hook" in output:
            return _make_crash_info(
                message=msg,
                path_str=path_str,
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
