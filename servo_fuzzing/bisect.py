"""Bisect a crash across Servo nightly builds to find the upstream commit range."""
from __future__ import annotations

import json
import re
import shutil
import tarfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .bundle import download_bundle
from .executor import TestStatus, run_single_test
from .github_sync import _github_headers

DEFAULT_NIGHTLY_REPO = "servo/servo-nightly-builds"
DEFAULT_ASSET = "servo-x86_64-linux-gnu.tar.gz"
UPSTREAM_REPO = "servo/servo"
_SHA_RE = re.compile(r"servo/servo@([0-9a-f]{7,40})")
_NIGHTLY_TAG_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass
class Nightly:
    tag: str
    sha: Optional[str]
    asset_url: str


@dataclass
class BisectResult:
    last_good: Nightly
    first_bad: Nightly
    tested: dict[str, bool]

    @property
    def compare_url(self) -> Optional[str]:
        if self.last_good.sha and self.first_bad.sha:
            return f"https://github.com/{UPSTREAM_REPO}/compare/{self.last_good.sha}...{self.first_bad.sha}"
        return None


def _get_json(url: str, token: Optional[str]) -> object:
    request = urllib.request.Request(url, headers=_github_headers(token, user_agent="servo-fuzzing-bisect"))
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def parse_release(release: dict, asset_name: str) -> Optional[Nightly]:
    if not _NIGHTLY_TAG_RE.match(release.get("tag_name", "")):
        return None
    asset = next((a for a in release.get("assets", []) if a.get("name") == asset_name), None)
    if asset is None:
        return None
    match = _SHA_RE.search(release.get("body") or "")
    return Nightly(
        tag=release["tag_name"],
        sha=match.group(1) if match else None,
        asset_url=asset["browser_download_url"],
    )


def fetch_nightlies(
    repo: str = DEFAULT_NIGHTLY_REPO,
    asset_name: str = DEFAULT_ASSET,
    token: Optional[str] = None,
    oldest: Optional[str] = None,
    max_pages: int = 20,
) -> list[Nightly]:
    """Return nightlies that ship `asset_name`, oldest first, stopping once older than `oldest`."""
    nightlies: list[Nightly] = []
    for page in range(1, max_pages + 1):
        releases = _get_json(f"https://api.github.com/repos/{repo}/releases?per_page=100&page={page}", token)
        if not releases:
            break
        for release in releases:
            if release.get("draft"):
                continue
            nightly = parse_release(release, asset_name)
            if nightly:
                nightlies.append(nightly)
        if oldest and any(r["tag_name"] < oldest for r in releases):
            break
    nightlies.sort(key=lambda n: n.tag)
    return nightlies


def _find_servoshell(root: Path) -> Path:
    for name in ("servoshell", "servoshell.exe"):
        direct = root / "servo" / name
        if direct.exists():
            return direct
        found = next(root.rglob(name), None)
        if found:
            return found
    raise FileNotFoundError(f"servoshell not found in {root}")


def build_dir_for(nightly: Nightly, cache_dir: Path) -> Path:
    owner_repo = "/".join(nightly.asset_url.split("/")[3:5])
    asset = nightly.asset_url.rsplit("/", 1)[-1]
    return cache_dir / owner_repo.replace("/", "_") / asset / nightly.tag


def install_nightly(nightly: Nightly, cache_dir: Path, token: Optional[str] = None) -> Path:
    build_dir = build_dir_for(nightly, cache_dir)
    try:
        return _find_servoshell(build_dir)
    except FileNotFoundError:
        pass
    archive = download_bundle(nightly.asset_url, build_dir.with_name(f"{nightly.tag}.tar.gz"), token=token, timeout=120)
    if build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True)
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(build_dir)
    archive.unlink()
    return _find_servoshell(build_dir)


def reproduces(
    binary: Path,
    testcase: Path,
    pattern: Optional[str],
    attempts: int = 3,
    timeout: float = 10.0,
    servo_args: Optional[list[str]] = None,
) -> bool:
    regex = re.compile(pattern) if pattern else None
    for _ in range(attempts):
        result = run_single_test(binary=binary, testcase=testcase, servo_args=servo_args, timeout=timeout)
        if result.status == TestStatus.ERROR or result.exit_code == 127:
            detail = result.error_message or (result.combined_output or "").strip()[-500:]
            raise ValueError(f"Failed to launch {binary}: {detail}")
        if regex is None:
            if result.crash_info is not None:
                return True
        elif regex.search(result.combined_output or (result.stdout + result.stderr)):
            return True
    return False


def bisect_nightlies(
    nightlies: list[Nightly],
    is_bad: Callable[[Nightly], bool],
    good: Optional[str] = None,
    bad: Optional[str] = None,
) -> BisectResult:
    """Find the first bad nightly. Without `good`, search backwards with doubling steps."""
    if not nightlies:
        raise ValueError("No nightlies available")
    tags = [n.tag for n in nightlies]
    tested: dict[str, bool] = {}

    def check(index: int) -> bool:
        nightly = nightlies[index]
        if nightly.tag not in tested:
            print(f"Testing nightly {nightly.tag} ({nightly.sha or 'unknown sha'})...")
            tested[nightly.tag] = is_bad(nightly)
            print(f"  -> {'BAD (reproduces)' if tested[nightly.tag] else 'GOOD'}")
        return tested[nightly.tag]

    def index_of(tag: str) -> int:
        if tag not in tags:
            raise ValueError(f"Nightly {tag} not found (available: {tags[0]} .. {tags[-1]})")
        return tags.index(tag)

    bad_idx = index_of(bad) if bad else len(nightlies) - 1
    if not check(bad_idx):
        raise ValueError(f"Testcase does not reproduce on bad nightly {tags[bad_idx]}")

    if good:
        good_idx = index_of(good)
        if good_idx >= bad_idx:
            raise ValueError("Good nightly must be older than bad nightly")
        if check(good_idx):
            raise ValueError(f"Testcase also reproduces on good nightly {tags[good_idx]}")
    else:
        step = 1
        good_idx = bad_idx
        while True:
            candidate = max(bad_idx - step, 0)
            if not check(candidate):
                good_idx = candidate
                break
            bad_idx = candidate
            if candidate == 0:
                raise ValueError(f"Testcase reproduces on the oldest available nightly {tags[0]}")
            step *= 2

    while bad_idx - good_idx > 1:
        mid = (good_idx + bad_idx) // 2
        if check(mid):
            bad_idx = mid
        else:
            good_idx = mid

    return BisectResult(last_good=nightlies[good_idx], first_bad=nightlies[bad_idx], tested=tested)


def fetch_commit_range(result: BisectResult, token: Optional[str] = None, max_pages: int = 30) -> list[str]:
    if not (result.last_good.sha and result.first_bad.sha):
        return []
    base = f"https://api.github.com/repos/{UPSTREAM_REPO}/compare/{result.last_good.sha}...{result.first_bad.sha}"
    commits: list[dict] = []
    for page in range(1, max_pages + 1):
        data = _get_json(f"{base}?per_page=100&page={page}", token)
        batch = data.get("commits", [])
        commits.extend(batch)
        if not batch or len(commits) >= data.get("total_commits", 0):
            break
    return [f"{c['sha'][:10]} {c['commit']['message'].splitlines()[0]}" for c in commits]


def format_bisect_report(result: BisectResult, testcase: Path, commits: list[str]) -> str:
    lines = [
        f"## Nightly bisect for `{testcase.name}`",
        "",
        f"- Last good nightly: `{result.last_good.tag}` (servo/servo@{result.last_good.sha or 'unknown'})",
        f"- First bad nightly: `{result.first_bad.tag}` (servo/servo@{result.first_bad.sha or 'unknown'})",
    ]
    if result.compare_url:
        lines.append(f"- Commit range: {result.compare_url}")
    lines += ["", "| Nightly | Result |", "|---|---|"]
    lines += [f"| {tag} | {'bad' if bad else 'good'} |" for tag, bad in sorted(result.tested.items())]
    if commits:
        lines += ["", f"### Commits in range ({len(commits)})", ""]
        lines += [f"- {c}" for c in commits]
    return "\n".join(lines) + "\n"


def run_bisect(
    testcase: Path,
    pattern: Optional[str] = None,
    good: Optional[str] = None,
    bad: Optional[str] = None,
    attempts: int = 3,
    timeout: float = 10.0,
    servo_args: Optional[list[str]] = None,
    cache_dir: Path = Path("target/bisect"),
    repo: str = DEFAULT_NIGHTLY_REPO,
    asset_name: str = DEFAULT_ASSET,
    token: Optional[str] = None,
    keep_builds: bool = False,
) -> tuple[BisectResult, list[str]]:
    cache_dir = Path(cache_dir).resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    nightlies = fetch_nightlies(repo=repo, asset_name=asset_name, token=token, oldest=good)
    if bad:
        nightlies = [n for n in nightlies if n.tag <= bad]
    print(f"Found {len(nightlies)} nightlies with {asset_name}: {nightlies[0].tag} .. {nightlies[-1].tag}")

    def is_bad(nightly: Nightly) -> bool:
        binary = install_nightly(nightly, cache_dir, token=token)
        try:
            return reproduces(binary, testcase, pattern, attempts=attempts, timeout=timeout, servo_args=servo_args)
        finally:
            if not keep_builds:
                shutil.rmtree(build_dir_for(nightly, cache_dir), ignore_errors=True)

    result = bisect_nightlies(nightlies, is_bad, good=good, bad=bad)
    try:
        commits = fetch_commit_range(result, token=token)
    except Exception as exc:
        print(f"Warning: could not fetch commit range: {exc}")
        commits = []
    return result, commits
