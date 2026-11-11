from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from .executor import TestStatus, run_single_test

DEFAULT_BUNDLE_ASSET = "fuzz-bundle-latest.tar.gz"


def get_default_bundle_repo() -> str:
    env_repo = os.environ.get("SERVO_FUZZING_REPO") or os.environ.get("SERVO_FUZZING_BUNDLE_REPO")
    if env_repo:
        return env_repo

    try:
        res = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=2,
            cwd=Path(__file__).resolve().parent.parent,
        )
        url = res.stdout.strip()
        if "github.com" in url:
            part = url.split("github.com")[-1].lstrip("/:")
            if part.endswith(".git"):
                part = part[:-4]
            if "/" in part:
                return part
    except Exception:
        pass

    return "servo/fuzzing"


def get_default_cache_dir() -> Path:
    user_cache = os.environ.get("XDG_CACHE_HOME")
    if user_cache:
        return Path(user_cache) / "servo" / "fuzzing"
    return Path.home() / ".cache" / "servo" / "fuzzing"


def resolve_latest_bundle_url(
    repo: Optional[str] = None,
    asset_name: str = DEFAULT_BUNDLE_ASSET,
) -> str:
    target_repo = repo or get_default_bundle_repo()
    return f"https://github.com/{target_repo}/releases/latest/download/{asset_name}"


def is_valid_tar_gz(tar_path: Path) -> bool:
    if not tar_path.is_file() or tar_path.stat().st_size == 0:
        return False
    try:
        with tarfile.open(tar_path, "r:gz") as tar:
            for _ in tar:
                pass
        return True
    except Exception:
        return False


def obtain_bundle(
    bundle_spec: Optional[str] = "latest",
    cache_dir: Optional[Path] = None,
    repo: Optional[str] = None,
    token: Optional[str] = None,
    force_download: bool = False,
) -> Optional[Path]:
    if bundle_spec and Path(bundle_spec).is_file():
        local_f = Path(bundle_spec).resolve()
        if is_valid_tar_gz(local_f):
            return local_f
        print(f"Warning: Specified bundle {local_f} is invalid or corrupted.")

    cache = cache_dir or get_default_cache_dir()
    cache.mkdir(parents=True, exist_ok=True)

    spec_str = str(bundle_spec).strip() if bundle_spec else "latest"
    spec_name = Path(spec_str).name.lower()
    if spec_name in {"crashes", "corpus", "panic-corpus", "new-panic-corpus", "new-panic-corpus.tar.gz"}:
        asset_name = "new-panic-corpus.tar.gz"
    else:
        asset_name = DEFAULT_BUNDLE_ASSET

    cached_bundle = cache / asset_name

    if spec_str.startswith(("http://", "https://")):
        url = spec_str
    else:
        url = resolve_latest_bundle_url(repo=repo, asset_name=asset_name)

    print(f"Resolving latest bundle from {url}...")
    try:
        download_bundle(url=url, output_path=cached_bundle, token=token)
        return cached_bundle
    except Exception as exc:
        print(f"Warning: Could not download latest bundle from {url}: {exc}")
        if cached_bundle.exists() and is_valid_tar_gz(cached_bundle):
            print(f"Falling back to existing cached bundle: {cached_bundle}")
            return cached_bundle

        repo_root = Path(__file__).resolve().parent.parent
        for cand in [repo_root / "target" / DEFAULT_BUNDLE_ASSET, Path.cwd() / "target" / DEFAULT_BUNDLE_ASSET]:
            if cand.is_file() and is_valid_tar_gz(cand):
                print(f"Falling back to local bundle: {cand} ({cand.stat().st_size / 1024:.1f} KB)")
                return cand
        return None


def pack_bundle(
    source_dir: Path,
    output_tar: Path,
    max_tests: int = 300,
    filter_timeouts_with_binary: Optional[Path] = None,
    timeout: float = 2.0,
    from_triage: Optional[Path] = None,
    jobs: int = 4,
) -> Path:
    source_dir = Path(source_dir).resolve()
    output_tar = Path(output_tar).resolve()
    output_tar.parent.mkdir(parents=True, exist_ok=True)

    all_html = sorted(list(source_dir.rglob("*.html")) + list(source_dir.rglob("*.htm")))
    selected: list[Path] = []

    print(f"Scanning {len(all_html)} testcases in {source_dir}...")

    if from_triage:
        triage_path = Path(from_triage).resolve()
        summary_file = triage_path / "summary.json" if triage_path.is_dir() else triage_path
        if summary_file.exists():
            try:
                summary_data = json.loads(summary_file.read_text(encoding="utf-8"))
                pass_paths = {Path(item["testcase"]).resolve() for item in summary_data.get("results", []) if item.get("status") == "PASS"}
                selected = [tc for tc in all_html if tc.resolve() in pass_paths][:max_tests]
                print(f"Selected {len(selected)} clean passing testcases directly from {summary_file.name}")
            except Exception as e:
                print(f"Warning: Failed to parse triage summary from {summary_file}: {e}")

    if not selected and filter_timeouts_with_binary:
        bin_path = Path(filter_timeouts_with_binary).resolve()
        if not bin_path.exists():
            raise FileNotFoundError(f"Binary for timeout filtering not found: {bin_path}")

        health = run_single_test(
            binary=bin_path,
            testcase=Path("about:blank"),
            servo_args=["-xfz"],
            timeout=5.0,
        )
        if health.status in (TestStatus.NEW_CRASH, TestStatus.ERROR):
            err_msg = health.combined_output.strip() or health.error_message or "Unknown failure"
            raise RuntimeError(f"Cannot filter bundle: servoshell failed health check:\n{err_msg}")

        print(f"Filtering out tests that hang/timeout using {bin_path} (timeout={timeout}s, jobs={jobs})...")
        discarded_timeouts = 0
        discarded_errors = 0

        def check_case(tc: Path):
            res = run_single_test(
                binary=bin_path,
                testcase=tc,
                servo_args=["-xfz", "--enable-experimental-web-platform-features"],
                timeout=timeout,
            )
            return tc, res

        with ThreadPoolExecutor(max_workers=jobs) as pool:
            for tc, res in pool.map(check_case, all_html):
                if res.status == TestStatus.TIMEOUT:
                    discarded_timeouts += 1
                elif res.status != TestStatus.PASS:
                    discarded_errors += 1
                elif len(selected) < max_tests:
                    selected.append(tc)

        print(f"Filter summary: {len(selected)} kept, {discarded_timeouts} timeouts discarded, {discarded_errors} errors discarded.")
    elif not selected:
        selected = all_html[:max_tests]

    print(f"Packing {len(selected)} selected testcases into {output_tar}...")
    with tarfile.open(output_tar, "w:gz") as tar:
        for tc in selected:
            rel_name = tc.relative_to(source_dir) if tc.is_relative_to(source_dir) else tc.name
            tar.add(tc, arcname=str(rel_name))

    print(f"Bundle created: {output_tar} ({output_tar.stat().st_size / 1024:.1f} KB)")
    return output_tar


def unpack_bundle(bundle_tar: Path, target_dir: Path, clean_target: bool = False) -> list[Path]:
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
    print(f"Unpacked {len(unpacked)} testcases into {target_dir}")
    return unpacked


def download_bundle(
    url: str,
    output_path: Path,
    token: Optional[str] = None,
    timeout: float = 30.0,
) -> Path:
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_name(output_path.name + ".part")

    for proto in ("http", "https"):
        upper = os.environ.get(f"{proto.upper()}_PROXY")
        if upper and not os.environ.get(f"{proto}_proxy"):
            os.environ[f"{proto}_proxy"] = upper

    headers = {"User-Agent": "servo-fuzzing-bundle/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, headers=headers)
    print(f"Downloading bundle from {url}...")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content_len = resp.headers.get("Content-Length")
            total_size = int(content_len) if content_len and content_len.isdigit() else None

            downloaded = 0
            start_time = time.time()
            last_print = 0.0

            with open(temp_path, "wb") as out_f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    out_f.write(chunk)
                    downloaded += len(chunk)

                    now = time.time()
                    if now - last_print > 0.4:
                        last_print = now
                        elapsed = max(now - start_time, 0.001)
                        speed = (downloaded / 1024 / 1024) / elapsed
                        if total_size:
                            pct = (downloaded / total_size) * 100
                            mb_down = downloaded / 1024 / 1024
                            mb_tot = total_size / 1024 / 1024
                            print(
                                f"\r  Downloading: {pct:5.1f}% ({mb_down:.1f}/{mb_tot:.1f} MB, {speed:.2f} MB/s)",
                                end="",
                                flush=True,
                            )
                        else:
                            mb_down = downloaded / 1024 / 1024
                            print(
                                f"\r  Downloading: {mb_down:.1f} MB ({speed:.2f} MB/s)",
                                end="",
                                flush=True,
                            )

            print()

        temp_path.replace(output_path)
        print(f"Downloaded bundle to {output_path} ({output_path.stat().st_size / 1024 / 1024:.2f} MB)")
        return output_path
    except Exception:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except Exception:
                pass
        raise
