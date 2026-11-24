"""Download and extract the latest Servo nightly binary for the current host."""
from __future__ import annotations

import argparse
import hashlib
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Optional

from .bundle import download_bundle

DEFAULT_NIGHTLY_REPO = "servo/servo-nightly-builds"


@dataclass(frozen=True)
class PlatformTarget:
    os_name: str
    arch: str
    asset_name: str
    archive_format: str
    binary_name: str
    note: Optional[str] = None


_TARGETS: dict[tuple[str, str], PlatformTarget] = {
    ("linux", "x86_64"): PlatformTarget(
        os_name="linux",
        arch="x86_64",
        asset_name="servo-x86_64-linux-gnu.tar.gz",
        archive_format="tar.gz",
        binary_name="servoshell",
    ),
    ("linux", "aarch64"): PlatformTarget(
        os_name="linux",
        arch="aarch64",
        asset_name="servo-aarch64-linux-gnu.tar.gz",
        archive_format="tar.gz",
        binary_name="servoshell",
    ),
    ("macos", "aarch64"): PlatformTarget(
        os_name="macos",
        arch="aarch64",
        asset_name="servo-aarch64-apple-darwin.dmg",
        archive_format="dmg",
        binary_name="servoshell",
    ),
    ("macos", "x86_64"): PlatformTarget(
        os_name="macos",
        arch="x86_64",
        asset_name="servo-x86_64-apple-darwin.dmg",
        archive_format="dmg",
        binary_name="servoshell",
    ),
    ("windows", "x86_64"): PlatformTarget(
        os_name="windows",
        arch="x86_64",
        asset_name="servo-x86_64-windows-msvc.zip",
        archive_format="zip",
        binary_name="servoshell.exe",
    ),
    ("windows", "aarch64"): PlatformTarget(
        os_name="windows",
        arch="aarch64",
        asset_name="servo-x86_64-windows-msvc.zip",
        archive_format="zip",
        binary_name="servoshell.exe",
        note="No native Windows ARM64 Servo nightly asset; using x86_64 build (runs via Windows x64 emulation).",
    ),
}


def normalize_os(raw: str) -> str:
    value = raw.strip().lower()
    if value == "linux":
        return "linux"
    if value in {"darwin", "macos", "osx", "mac"}:
        return "macos"
    if value in {"windows", "win32", "win64", "cygwin", "msys"} or value.startswith(("mingw", "msys", "cygwin")):
        return "windows"
    raise ValueError(f"Unsupported operating system: {raw!r} (expected linux, macos, or windows)")


def normalize_arch(raw: str) -> str:
    value = raw.strip().lower()
    if value in {"x86_64", "amd64", "x64"}:
        return "x86_64"
    if value in {"aarch64", "arm64", "armv8", "armv8l", "armv8b"}:
        return "aarch64"
    raise ValueError(f"Unsupported CPU architecture: {raw!r} (expected x86_64/amd64 or aarch64/arm64)")


def _macos_is_arm64_host() -> bool:
    try:
        res = subprocess.run(
            ["sysctl", "-n", "hw.optional.arm64"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        return res.returncode == 0 and res.stdout.strip() == "1"
    except (OSError, subprocess.SubprocessError):
        return False


def detect_platform(
    os_override: Optional[str] = None,
    arch_override: Optional[str] = None,
) -> PlatformTarget:
    os_name = normalize_os(os_override if os_override is not None else platform.system())
    arch = normalize_arch(arch_override if arch_override is not None else platform.machine())

    if os_override is None and arch_override is None and os_name == "macos" and arch == "x86_64":
        if _macos_is_arm64_host():
            arch = "aarch64"

    target = _TARGETS.get((os_name, arch))
    if target is None:
        raise ValueError(f"Unsupported platform combination: {os_name}/{arch}")
    return target


def default_nightly_asset() -> str:
    try:
        return detect_platform().asset_name
    except ValueError:
        return _TARGETS[("linux", "x86_64")].asset_name


def nightly_download_url(
    asset_name: str,
    repo: str = DEFAULT_NIGHTLY_REPO,
    tag: str = "latest",
) -> str:
    tag_clean = tag.strip() if tag else "latest"
    if tag_clean.lower() == "latest":
        return f"https://github.com/{repo}/releases/latest/download/{asset_name}"
    return f"https://github.com/{repo}/releases/download/{tag_clean}/{asset_name}"


def fetch_expected_sha256(
    asset_url: str,
    token: Optional[str] = None,
    timeout: float = 30.0,
) -> Optional[str]:
    for proto in ("http", "https"):
        upper = os.environ.get(f"{proto.upper()}_PROXY")
        if upper and not os.environ.get(f"{proto}_proxy"):
            os.environ[f"{proto}_proxy"] = upper

    headers = {"User-Agent": "servo-fuzzing-nightly/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(f"{asset_url}.sha256", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise

    match = re.search(r"\b([0-9a-fA-F]{64})\b", text)
    return match.group(1).lower() if match else None


def verify_sha256(archive_path: Path, expected: str) -> None:
    expected_clean = expected.strip().lower()
    digest = hashlib.sha256()
    with Path(archive_path).open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    actual = digest.hexdigest().lower()
    if actual != expected_clean:
        raise ValueError(f"SHA256 mismatch for {archive_path.name}: expected {expected_clean}, got {actual}")


def _validate_member_path(name: str) -> None:
    if not name or name.startswith(("/", "\\")) or re.match(r"^[a-zA-Z]:", name):
        raise ValueError(f"Unsafe archive entry path: {name!r}")
    parts = [part for part in re.split(r"[\\/]", name) if part]
    if ".." in parts:
        raise ValueError(f"Unsafe archive traversal path: {name!r}")


def _unwrap_single_root(stage_dir: Path) -> Path:
    entries = [p for p in stage_dir.iterdir() if not p.name.startswith(".")]
    if len(entries) == 1 and entries[0].is_dir() and not entries[0].name.endswith(".app"):
        return entries[0]
    return stage_dir


def find_servoshell_binary(root: Path) -> Path:
    root = Path(root)
    for rel in (
        Path("servoshell"),
        Path("servoshell.exe"),
        Path("servo") / "servoshell",
        Path("servo") / "servoshell.exe",
        Path("Servo.app") / "Contents" / "MacOS" / "servoshell",
    ):
        cand = root / rel
        if cand.is_file():
            return cand

    for name in ("servoshell", "servoshell.exe"):
        for match in sorted(root.rglob(name)):
            if match.is_file():
                return match
    raise FileNotFoundError(f"servoshell binary not found in {root}")


def _extract_tar_gz(archive: Path, stage_dir: Path) -> None:
    resolved_stage = stage_dir.resolve()
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        for member in members:
            _validate_member_path(member.name)
            if member.issym() or member.islnk():
                link = member.linkname
                if link.startswith(("/", "\\")) or re.match(r"^[a-zA-Z]:", link):
                    raise ValueError(f"Unsafe archive symlink target: {link!r}")
                if member.issym():
                    link_dest = (resolved_stage / PurePosixPath(member.name).parent / link).resolve()
                else:
                    link_dest = (resolved_stage / link).resolve()
                if not link_dest.is_relative_to(resolved_stage):
                    raise ValueError(f"Unsafe archive link escaping target directory: {link!r}")
        tar.extractall(stage_dir, members=members)


def _extract_zip(archive: Path, stage_dir: Path) -> None:
    with zipfile.ZipFile(archive, "r") as zf:
        for info in zf.infolist():
            _validate_member_path(info.filename)
        zf.extractall(stage_dir)


def _extract_dmg(archive: Path, stage_dir: Path) -> None:
    if sys.platform != "darwin":
        raise RuntimeError(
            "Cannot extract macOS .dmg archives on a non-macOS host; pass --download-only to save the .dmg file."
        )

    mount_dir = Path(tempfile.mkdtemp(prefix="servo-dmg-mount-"))
    try:
        subprocess.run(
            ["hdiutil", "attach", str(archive), "-nobrowse", "-readonly", "-mountpoint", str(mount_dir)],
            check=True,
        )
        apps = sorted(p for p in mount_dir.iterdir() if p.is_dir() and p.name.endswith(".app"))
        if not apps:
            raise FileNotFoundError(f"No .app bundle found inside mounted DMG: {archive}")
        app_src = next((p for p in apps if p.name.lower() == "servo.app"), apps[0])
        app_dest = stage_dir / app_src.name
        if shutil.which("ditto"):
            subprocess.run(["ditto", str(app_src), str(app_dest)], check=True)
        else:
            shutil.copytree(app_src, app_dest, symlinks=True)

        macos_dir = app_dest / "Contents" / "MacOS"
        if not macos_dir.is_dir():
            raise FileNotFoundError(f"Missing Contents/MacOS directory inside {app_dest}")

        exe = macos_dir / "servoshell"
        if not exe.is_file():
            candidates = sorted(p for p in macos_dir.iterdir() if p.is_file() and os.access(p, os.X_OK))
            if not candidates:
                candidates = sorted(p for p in macos_dir.iterdir() if p.is_file())
            if not candidates:
                raise FileNotFoundError(f"No executable found in {macos_dir}")
            exe = candidates[0]

        exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        try:
            subprocess.run(
                ["xattr", "-dr", "com.apple.quarantine", str(app_dest)],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            pass

        wrapper = stage_dir / "servoshell"
        wrapper.write_text(
            f'#!/bin/sh\nexec "$(dirname "$0")/{app_dest.name}/Contents/MacOS/{exe.name}" "$@"\n',
            encoding="utf-8",
        )
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    finally:
        for detach_cmd in (
            ["hdiutil", "detach", str(mount_dir), "-quiet"],
            ["hdiutil", "detach", str(mount_dir), "-force", "-quiet"],
        ):
            try:
                res = subprocess.run(detach_cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if res.returncode == 0:
                    break
            except OSError:
                break
        if mount_dir.exists() and not any(mount_dir.iterdir()):
            mount_dir.rmdir()


def extract_nightly_archive(
    archive: Path,
    dest_dir: Path,
    archive_format: Optional[str] = None,
) -> Path:
    archive = Path(archive).resolve()
    dest_dir = Path(dest_dir).resolve()
    dest_dir.parent.mkdir(parents=True, exist_ok=True)

    fmt = archive_format
    if not fmt:
        lower = archive.name.lower()
        if lower.endswith((".tar.gz", ".tgz")):
            fmt = "tar.gz"
        elif lower.endswith(".zip"):
            fmt = "zip"
        elif lower.endswith(".dmg"):
            fmt = "dmg"
        else:
            raise ValueError(f"Unsupported archive format: {archive.name}")

    with tempfile.TemporaryDirectory(prefix=".servo-extract-", dir=dest_dir.parent) as tmp_dir:
        stage = Path(tmp_dir) / "stage"
        stage.mkdir(parents=True, exist_ok=True)
        if fmt == "tar.gz":
            _extract_tar_gz(archive, stage)
        elif fmt == "zip":
            _extract_zip(archive, stage)
        elif fmt == "dmg":
            _extract_dmg(archive, stage)
        else:
            raise ValueError(f"Unsupported archive format: {fmt}")

        payload_root = _unwrap_single_root(stage)
        find_servoshell_binary(payload_root)

        if dest_dir.exists():
            shutil.rmtree(dest_dir)
        shutil.move(str(payload_root), str(dest_dir))

    binary = find_servoshell_binary(dest_dir)
    if os.name != "nt":
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return binary


def download_latest_nightly(
    output_dir: Path = Path("servo"),
    tag: str = "latest",
    repo: str = DEFAULT_NIGHTLY_REPO,
    os_override: Optional[str] = None,
    arch_override: Optional[str] = None,
    token: Optional[str] = None,
    verify: bool = True,
    download_only: bool = False,
    keep_archive: bool = False,
) -> Path:
    target = detect_platform(os_override=os_override, arch_override=arch_override)
    url = nightly_download_url(target.asset_name, repo=repo, tag=tag)
    output_dir = Path(output_dir).resolve()

    print(f"Detected platform: {target.os_name}/{target.arch} -> {target.asset_name}")
    if target.note:
        print(f"Note: {target.note}")
    print(f"Release URL: {url}")

    if download_only:
        output_dir.mkdir(parents=True, exist_ok=True)
        archive_path = output_dir / target.asset_name
    else:
        archive_path = output_dir.parent / target.asset_name

    token_val = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    download_bundle(url=url, output_path=archive_path, token=token_val, timeout=120.0)

    if verify:
        expected = fetch_expected_sha256(url, token=token_val)
        if expected:
            verify_sha256(archive_path, expected)
            print(f"Verified SHA256: {expected}")
        else:
            print(f"Warning: {target.asset_name}.sha256 not available; skipping checksum verification.")

    if download_only:
        print(f"Saved nightly archive to {archive_path}")
        return archive_path

    try:
        binary = extract_nightly_archive(archive_path, output_dir, archive_format=target.archive_format)
    finally:
        if not keep_archive and archive_path.exists():
            archive_path.unlink()

    print(f"Installed servoshell binary at {binary}")
    return binary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="download-latest-nightly",
        description="Detect OS and CPU architecture and download the matching Servo nightly build.",
    )
    parser.add_argument("-o", "--output", default="servo", help="Destination directory (default: servo)")
    parser.add_argument("--tag", default="latest", help="Nightly release tag (default: latest)")
    parser.add_argument("--repo", default=DEFAULT_NIGHTLY_REPO, help=f"GitHub releases repo (default: {DEFAULT_NIGHTLY_REPO})")
    parser.add_argument("--os", dest="os_override", default=None, help="Override target OS (linux, macos, windows)")
    parser.add_argument("--arch", dest="arch_override", default=None, help="Override target CPU arch (x86_64, aarch64)")
    parser.add_argument("--token", default=None, help="GitHub token (defaults to GITHUB_TOKEN / GH_TOKEN)")
    parser.add_argument("--no-verify", action="store_true", help="Skip .sha256 checksum verification")
    parser.add_argument("--download-only", action="store_true", help="Save archive without extracting")
    parser.add_argument("--keep-archive", action="store_true", help="Keep downloaded archive after extraction")
    parser.add_argument("--print-target", action="store_true", help="Print detected target and URL without downloading")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.print_target:
            target = detect_platform(os_override=args.os_override, arch_override=args.arch_override)
            url = nightly_download_url(target.asset_name, repo=args.repo, tag=args.tag)
            print(f"os={target.os_name}")
            print(f"arch={target.arch}")
            print(f"asset={target.asset_name}")
            print(f"format={target.archive_format}")
            print(f"binary={target.binary_name}")
            print(f"url={url}")
            if target.note:
                print(f"note={target.note}")
            return 0

        download_latest_nightly(
            output_dir=Path(args.output),
            tag=args.tag,
            repo=args.repo,
            os_override=args.os_override,
            arch_override=args.arch_override,
            token=args.token,
            verify=not args.no_verify,
            download_only=args.download_only,
            keep_archive=args.keep_archive,
        )
        return 0
    except Exception as exc:
        print(f"Error downloading Servo nightly: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
