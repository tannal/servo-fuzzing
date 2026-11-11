"""Bundle management for Servo fuzzing.

Packs, unpacks, and manages clean, non-timing-out testcase bundles as tarballs.
Automatically resolves and downloads the latest release bundle from GitHub.
"""

from __future__ import annotations

import os
import shutil
import tarfile
import urllib.request
from pathlib import Path
from typing import Optional

from .executor import TestStatus, run_single_test

DEFAULT_BUNDLE_REPO = "servo/fuzzing"
DEFAULT_BUNDLE_ASSET = "fuzz-bundle-latest.tar.gz"


def get_default_cache_dir() -> Path:
    """Return default user cache directory for fuzzing bundles."""
    user_cache = os.environ.get("XDG_CACHE_HOME")
    if user_cache:
        return Path(user_cache) / "servo" / "fuzzing"
    return Path.home() / ".cache" / "servo" / "fuzzing"


def resolve_latest_bundle_url(
    repo: str = DEFAULT_BUNDLE_REPO,
    asset_name: str = DEFAULT_BUNDLE_ASSET,
) -> str:
    """Return the direct download URL for the latest GitHub Release bundle asset."""
    return f"https://github.com/{repo}/releases/latest/download/{asset_name}"


def obtain_bundle(
    bundle_spec: Optional[str] = "latest",
    cache_dir: Optional[Path] = None,
    repo: str = DEFAULT_BUNDLE_REPO,
    token: Optional[str] = None,
    force_download: bool = False,
) -> Optional[Path]:
    """Obtain a bundle tarball from local path, URL, or latest GitHub Release.

    Args:
        bundle_spec: Local path, URL, or 'latest' / 'auto'.
        cache_dir: Directory to cache downloaded bundles.
        repo: GitHub repository (default: servo/fuzzing).
        token: Optional GitHub token.
        force_download: Ignore cached tarball and re-download.

    Returns:
        Path to local bundle .tar.gz, or None if download failed and no cache available.
    """
    if bundle_spec and Path(bundle_spec).is_file():
        return Path(bundle_spec).resolve()

    # Determine target cache path
    cache = cache_dir or get_default_cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    cached_bundle = cache / DEFAULT_BUNDLE_ASSET

    if cached_bundle.exists() and not force_download and bundle_spec in (None, "", "latest", "auto"):
        print(f"Using cached bundle: {cached_bundle} ({cached_bundle.stat().st_size / 1024:.1f} KB)")
        return cached_bundle

    # Determine URL
    if bundle_spec and (bundle_spec.startswith("http://") or bundle_spec.startswith("https://")):
        url = bundle_spec
    else:
        url = resolve_latest_bundle_url(repo=repo)

    print(f"Resolving latest bundle from {url}...")
    try:
        download_bundle(url=url, output_path=cached_bundle, token=token)
        return cached_bundle
    except Exception as exc:
        print(f"Warning: Could not download latest bundle from {url}: {exc}")
        if cached_bundle.exists():
            print(f"Falling back to existing cached bundle: {cached_bundle}")
            return cached_bundle
        return None


def pack_bundle(
    source_dir: Path,
    output_tar: Path,
    max_tests: int = 300,
    filter_timeouts_with_binary: Optional[Path] = None,
    timeout: float = 2.0,
) -> Path:
    """Pack a directory of HTML test cases into a compressed tarball.

    Optionally filters out testcases that time out without panicking.
    """
    source_dir = Path(source_dir).resolve()
    output_tar = Path(output_tar).resolve()
    output_tar.parent.mkdir(parents=True, exist_ok=True)

    all_html = sorted(list(source_dir.rglob("*.html")) + list(source_dir.rglob("*.htm")))
    selected: list[Path] = []

    print(f"Scanning {len(all_html)} testcases in {source_dir}...")

    if filter_timeouts_with_binary and filter_timeouts_with_binary.exists():
        print(f"Filtering out tests that hang/timeout using {filter_timeouts_with_binary} (timeout={timeout}s)...")
        for idx, tc in enumerate(all_html, 1):
            if len(selected) >= max_tests:
                break
            res = run_single_test(
                binary=filter_timeouts_with_binary,
                testcase=tc,
                servo_args=["-xfz"],
                timeout=timeout,
            )
            if res.status == TestStatus.TIMEOUT:
                # Discard testcases that hang without panic
                continue
            selected.append(tc)
    else:
        selected = all_html[:max_tests]

    print(f"Packing {len(selected)} selected testcases into {output_tar}...")
    with tarfile.open(output_tar, "w:gz") as tar:
        for tc in selected:
            rel_name = tc.relative_to(source_dir) if tc.is_relative_to(source_dir) else tc.name
            tar.add(tc, arcname=str(rel_name))

    print(f"✅ Bundle successfully created: {output_tar} ({output_tar.stat().st_size / 1024:.1f} KB)")
    return output_tar


def unpack_bundle(bundle_tar: Path, target_dir: Path, clean_target: bool = False) -> list[Path]:
    """Unpack a bundle tarball into target_dir."""
    bundle_tar = Path(bundle_tar).resolve()
    target_dir = Path(target_dir).resolve()

    if not bundle_tar.exists():
        raise FileNotFoundError(f"Bundle file not found: {bundle_tar}")

    if clean_target and target_dir.exists():
        shutil.rmtree(target_dir)

    target_dir.mkdir(parents=True, exist_ok=True)

    with tarfile.open(bundle_tar, "r:gz") as tar:
        tar.extractall(target_dir)

    unpacked = sorted(list(target_dir.rglob("*.html")) + list(target_dir.rglob("*.htm")))
    print(f"✅ Unpacked {len(unpacked)} testcases into {target_dir}")
    return unpacked


def download_bundle(
    url: str,
    output_path: Path,
    token: Optional[str] = None,
) -> Path:
    """Download bundle tarball from URL."""
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    headers = {"User-Agent": "servo-fuzzing-bundle"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, headers=headers)
    print(f"Downloading bundle from {url}...")
    with urllib.request.urlopen(req) as resp, open(output_path, "wb") as out_f:
        shutil.copyfileobj(resp, out_f)

    print(f"Downloaded bundle to {output_path} ({output_path.stat().st_size / 1024:.1f} KB)")
    return output_path
