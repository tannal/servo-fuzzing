from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .executor import DEFAULT_SERVO_ARGS, TestStatus, run_single_test

DEFAULT_BUNDLE_ASSET = "fuzz-bundle-latest.tar.gz"
DEFAULT_MAX_TIMEOUT_TESTS = 50
DEFAULT_PER_GENERATOR = 400
BUNDLE_MANIFEST_NAME = "manifest.json"
VERSION_SPEC_RE = re.compile(r"^(?:fuzz-)?(?:bundle-)?(\d{4}-\d{2}-\d{2}(?:\.\d+)?)(?:\.tar\.gz)?$")


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


def resolve_bundle_spec(
    bundle_spec: Optional[str] = "latest",
    repo: Optional[str] = None,
) -> tuple[str, str, bool]:
    """Resolve a bundle spec into ``(download_url, cache_asset_name, is_immutable)``."""
    target_repo = repo or get_default_bundle_repo()
    spec_str = str(bundle_spec).strip() if bundle_spec else "latest"

    if spec_str.startswith(("http://", "https://")):
        url_path = urllib.parse.urlparse(spec_str).path
        name = Path(url_path).name or DEFAULT_BUNDLE_ASSET
        is_immutable = bool(VERSION_SPEC_RE.match(name))
        return spec_str, name, is_immutable

    spec_lower = spec_str.lower()
    if spec_lower in {"latest", DEFAULT_BUNDLE_ASSET}:
        return (
            resolve_latest_bundle_url(repo=target_repo, asset_name=DEFAULT_BUNDLE_ASSET),
            DEFAULT_BUNDLE_ASSET,
            False,
        )

    if Path(spec_str).name.lower() in {"crashes", "corpus", "panic-corpus", "new-panic-corpus", "new-panic-corpus.tar.gz"}:
        return (
            resolve_latest_bundle_url(repo=target_repo, asset_name="new-panic-corpus.tar.gz"),
            "new-panic-corpus.tar.gz",
            False,
        )

    m = VERSION_SPEC_RE.match(spec_str)
    if m:
        ver = m.group(1)
        tag = f"bundle-{ver}"
        asset = f"fuzz-bundle-{ver}.tar.gz"
        return (
            f"https://github.com/{target_repo}/releases/download/{tag}/{asset}",
            asset,
            True,
        )

    asset = f"fuzz-bundle-{spec_str}.tar.gz"
    return (
        f"https://github.com/{target_repo}/releases/download/{spec_str}/{DEFAULT_BUNDLE_ASSET}",
        asset,
        True,
    )


def read_bundle_manifest(bundle_dir: Path) -> Optional[dict]:
    """Read ``manifest.json`` from an unpacked bundle directory, if present."""
    manifest_path = Path(bundle_dir) / BUNDLE_MANIFEST_NAME
    if not manifest_path.is_file():
        return None
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


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

    url, asset_name, is_immutable = resolve_bundle_spec(bundle_spec=bundle_spec, repo=repo)
    cached_bundle = cache / asset_name

    if is_immutable and not force_download and cached_bundle.exists() and is_valid_tar_gz(cached_bundle):
        print(f"Using cached immutable bundle: {cached_bundle}")
        return cached_bundle

    print(f"Resolving bundle from {url}...")
    try:
        download_bundle(url=url, output_path=cached_bundle, token=token)
        return cached_bundle
    except Exception as exc:
        print(f"Warning: Could not download bundle from {url}: {exc}")
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
    max_tests: int = 1200,
    filter_timeouts_with_binary: Optional[Path] = None,
    timeout: float = 2.0,
    from_triage: Optional[Path] = None,
    jobs: int = 4,
    include_timeouts: bool = False,
    max_timeout_tests: int = DEFAULT_MAX_TIMEOUT_TESTS,
    per_generator: Optional[int] = None,
    version: Optional[str] = None,
    servo_version: Optional[str] = None,
) -> Path:
    """Pack testcases into a bundle with a ``manifest.json`` metadata file.

    Timed-out testcases are dropped unless ``include_timeouts`` is set, in which
    case up to ``max_timeout_tests`` of them are kept: a replay never fails on a
    timeout, but each one costs a full timeout and may still crash before it.
    """
    source_dir = Path(source_dir).resolve()
    output_tar = Path(output_tar).resolve()
    output_tar.parent.mkdir(parents=True, exist_ok=True)

    all_html = sorted(list(source_dir.rglob("*.html")) + list(source_dir.rglob("*.htm")))
    selected: list[Path] = []
    selected_timeouts: set[Path] = set()

    def _rel_pos(tc: Path) -> Path:
        return tc.relative_to(source_dir) if tc.is_relative_to(source_dir) else Path(tc.name)

    def _gen_key(tc: Path) -> str:
        rel = _rel_pos(tc)
        return rel.parts[0] if len(rel.parts) > 1 else "root"

    def select(passing: list[Path], timed_out: list[Path]) -> list[Path]:
        nonlocal selected_timeouts
        if per_generator is not None and per_generator > 0:
            groups_pass: dict[str, list[Path]] = {}
            groups_to: dict[str, list[Path]] = {}
            for tc in passing:
                groups_pass.setdefault(_gen_key(tc), []).append(tc)
            for tc in timed_out:
                groups_to.setdefault(_gen_key(tc), []).append(tc)
            gen_names = sorted(set(groups_pass) | set(groups_to))
            kept: set[Path] = set()
            chosen_timeouts: set[Path] = set()
            to_budget = min(max_timeout_tests, max_tests) if include_timeouts else 0
            n_gens = max(len(gen_names), 1)
            base_to, rem_to = divmod(to_budget, n_gens)
            for idx, gen_name in enumerate(gen_names):
                gen_to_cap = min(per_generator, base_to + (1 if idx < rem_to else 0)) if include_timeouts else 0
                gen_to = groups_to.get(gen_name, [])[:gen_to_cap]
                gen_pass = groups_pass.get(gen_name, [])[: max(0, per_generator - len(gen_to))]
                kept.update(gen_pass)
                kept.update(gen_to)
                chosen_timeouts.update(gen_to)
            ordered = [tc for tc in all_html if tc in kept][:max_tests]
            ordered_set = set(ordered)
            selected_timeouts = chosen_timeouts & ordered_set
            print(
                f"Timeouts: {len(selected_timeouts)} of {len(timed_out)} "
                f"{'included' if include_timeouts else 'excluded'} (per_generator={per_generator})."
            )
            return ordered

        kept_timeouts = timed_out[: max(0, min(max_timeout_tests, max_tests))] if include_timeouts else []
        kept = set(passing[: max_tests - len(kept_timeouts)]) | set(kept_timeouts)
        selected_timeouts = set(kept_timeouts)
        print(
            f"Timeouts: {len(kept_timeouts)} of {len(timed_out)} "
            f"{'included' if include_timeouts else 'excluded'}."
        )
        return [tc for tc in all_html if tc in kept]

    print(f"Scanning {len(all_html)} testcases in {source_dir}...")

    if from_triage:
        triage_path = Path(from_triage).resolve()
        summary_file = triage_path / "summary.json" if triage_path.is_dir() else triage_path
        if summary_file.exists():
            try:
                summary_data = json.loads(summary_file.read_text(encoding="utf-8"))
                statuses = {
                    Path(item["testcase"]).resolve(): item.get("status")
                    for item in summary_data.get("results", [])
                }
                selected = select(
                    [tc for tc in all_html if statuses.get(tc.resolve()) == TestStatus.PASS.value],
                    [tc for tc in all_html if statuses.get(tc.resolve()) == TestStatus.TIMEOUT.value],
                )
                print(f"Selected {len(selected)} testcases directly from {summary_file.name}")
            except Exception as e:
                print(f"Warning: Failed to parse triage summary from {summary_file}: {e}")

    if not selected and filter_timeouts_with_binary:
        bin_path = Path(filter_timeouts_with_binary).resolve()
        if not bin_path.exists():
            raise FileNotFoundError(f"Binary for timeout filtering not found: {bin_path}")

        health = run_single_test(
            binary=bin_path,
            testcase=Path("about:blank"),
            servo_args=list(DEFAULT_SERVO_ARGS),
            timeout=5.0,
        )
        if health.status in (TestStatus.NEW_CRASH, TestStatus.ERROR):
            err_msg = health.combined_output.strip() or health.error_message or "Unknown failure"
            raise RuntimeError(f"Cannot filter bundle: servoshell failed health check:\n{err_msg}")

        print(f"Classifying tests using {bin_path} (timeout={timeout}s, jobs={jobs})...")
        passing: list[Path] = []
        timed_out: list[Path] = []
        discarded_errors = 0

        def check_case(tc: Path):
            res = run_single_test(
                binary=bin_path,
                testcase=tc,
                servo_args=list(DEFAULT_SERVO_ARGS),
                timeout=timeout,
            )
            return tc, res

        with ThreadPoolExecutor(max_workers=jobs) as pool:
            for tc, res in pool.map(check_case, all_html):
                if res.status == TestStatus.TIMEOUT:
                    timed_out.append(tc)
                elif res.status != TestStatus.PASS:
                    discarded_errors += 1
                else:
                    passing.append(tc)

        selected = select(passing, timed_out)
        print(f"Filter summary: {len(selected)} kept, {discarded_errors} errors discarded.")
    elif not selected:
        selected = select(all_html, [])

    gen_counts: dict[str, int] = {}
    for tc in selected:
        rel = _rel_pos(tc)
        if len(rel.parts) > 1:
            gen_counts[rel.parts[0]] = gen_counts.get(rel.parts[0], 0) + 1

    manifest = {
        "schema_version": 1,
        "version": version or "unversioned",
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "servo_version": servo_version or "unknown",
        "timeouts": "include" if include_timeouts else "exclude",
        "per_generator": per_generator,
        "total_tests": len(selected),
        "generators": gen_counts,
        "timeout_tests": sorted(_rel_pos(tc).as_posix() for tc in selected if tc in selected_timeouts),
    }

    print(f"Packing {len(selected)} selected testcases into {output_tar}...")
    with tarfile.open(output_tar, "w:gz") as tar:
        for tc in selected:
            rel_name = _rel_pos(tc)
            tar.add(tc, arcname=str(rel_name))
        manifest_bytes = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
        info = tarfile.TarInfo(BUNDLE_MANIFEST_NAME)
        info.size = len(manifest_bytes)
        info.mtime = int(time.time())
        tar.addfile(info, io.BytesIO(manifest_bytes))

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
