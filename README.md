# Servo Fuzzing

Automated DOM fuzzing, bundle replay, crash triage, bisection, and upstream issue management harness for the [Servo](https://github.com/servo/servo) browser engine.

The project serves two core goals:

1. **Deterministic PR Gating in `servo/servo`**: Replay pinned, versioned **1,200-testcase** bundles via `./mach test-fuzzing` (`python3 -m servo_fuzzing.runner`, standard-library-only) against `signatures/rust-panic.json` (open upstream issues ignored) and `signatures/fixed-history.json` (closed upstream issues flagged as regressions).
2. **Amortized Discovery & Upstream Reporting**: Run scheduled DOM fuzzing (`domato`, `freedom`, `minerva`), self-hosted campaigns (`./fuzz-continuous.sh`), and raw crash imports; automatically deduplicate, minimize via delta debugging, bisect across Servo nightlies, build structured evidence packs, file upstream GitHub issues directly, and update signature history without committing HTML testcases into git.

---

## Quick Start

### Install

```bash
# Replay, triage, minimization, bisection, and reporting (no third-party runtime dependencies)
pip install -e .

# Also enable DOM generators (installs cffi for Minerva)
pip install -e .[generators]
```

On Linux, `servoshell` requires GStreamer and XKB runtime libraries:

```bash
sudo apt-get install -y \
  libgstreamer1.0-0 libgstreamer-plugins-base1.0-0 libgstreamer-plugins-bad1.0-0 \
  gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
  gstreamer1.0-gl libxkbcommon0
```

Download the latest `servoshell` nightly for your OS and CPU architecture (`linux` x86_64/aarch64, `macos` Apple Silicon/Intel, `windows` x86_64) into `./servo/`:

```bash
python3 download-latest-nightly.py
# Or on Linux/macOS: ./download-latest-nightly.sh
# Or via the CLI:    ./servo-fuzzing download-nightly
```

### Replay a Bundle Against a Local Servo Build

From a `servo/servo` checkout (uses Mach's Python interpreter and the stdlib-only `servo_fuzzing.runner` entrypoint):

```bash
./mach test-fuzzing
./mach test-fuzzing --bundle bundle-2026-11-23
./mach test-fuzzing --runner ../fuzzing --bundle ../fuzzing/target/fuzz-bundle-latest.tar.gz --jobs 4
```

Directly from this repository:

```bash
python3 -m servo_fuzzing.runner \
  --binary ./servo/servoshell \
  --bundle latest \
  --signatures signatures/rust-panic.json \
  --timeouts exclude \
  --jobs 4 \
  --timeout 2.0
```

### Continuous Self-Hosted Fuzzing

```bash
# Clone Domato, Freedom, and Minerva into generators/
servo-fuzzing setup-generators

# Generate, replay, deduplicate into target/panic-corpus/, pack, upload to `incoming`,
# and trigger import-crashes.yml
./fuzz-continuous.sh --binary ./servo/servoshell --jobs 4 --count 30
```

Environment overrides for `fuzz-continuous.sh`:
- `SKIP_UPLOAD=1`: keep `target/new-panic-corpus.tar.gz` local without uploading.
- `SERVO_FUZZING_REPO`: target GitHub repository (default: `tannal/servo-fuzzing`).
- `INCOMING_TAG`: release tag for crash uploads (default: `incoming`).

### Import Raw Crash Archives

- **Local triage & minimization**:
  ```bash
  servo-fuzzing import ./crashes/ \
    --binary ./servo/servoshell \
    --signatures signatures/rust-panic.json \
    --corpus target/panic-corpus \
    --lookup-github
  ```
- **Zero-git CI upload**:
  1. Archive raw HTML files (`tar -cJf crashes.tar.xz crashes/`).
  2. Either drag-and-drop the archive into a GitHub issue with label `fuzz-import` or comment `/triage`, upload it to an `incoming` / `incoming-*` release and dispatch `import-crashes.yml`, or supply `bundle_url` via `workflow_dispatch`.

---

## Bundles: Refresh, Versioning & Resolution Convention

Every scheduled bundle refresh (`fuzzing-cron.yml`) packs **1,200 testcases** balanced equally across all three DOM generators (**400 x `domato` + 400 x `freedom` + 400 x `minerva`**) and embeds a `manifest.json` metadata file:

```
fuzz-bundle-YYYY-MM-DD.tar.gz
├── manifest.json
├── domato/fuzz-00001.html        (400 files)
├── freedom/1-179471886257.html   (400 files)
└── minerva/0001.html             (400 files)
```

`manifest.json` records `schema_version`, `version`, `created_at`, `servo_version`, `timeouts` (`exclude` or `include`), `per_generator`, `total_tests`, `generators` (per-generator file counts), and `timeout_tests` (relative paths of included testcases that timed out during bundle creation).

### Spec Resolution (`--bundle SPEC`)

`servo_fuzzing.bundle.resolve_bundle_spec` and `obtain_bundle` resolve bundle specifiers as follows:

| `--bundle SPEC` input | Resolved URL / Path | Cache Behavior |
|---|---|---|
| `latest` or `fuzz-bundle-latest.tar.gz` (default) | `releases/latest/download/fuzz-bundle-latest.tar.gz` | Checks server every run; falls back to valid cache offline |
| `YYYY-MM-DD`, `bundle-YYYY-MM-DD`, `fuzz-bundle-YYYY-MM-DD.tar.gz` (with optional `.2`, `.3` same-day suffix) | `releases/download/bundle-YYYY-MM-DD(.N)/fuzz-bundle-YYYY-MM-DD(.N).tar.gz` | Immutable: reuses cached file unless `--force-download` is passed |
| `crashes`, `corpus`, `panic-corpus`, `new-panic-corpus` | `releases/latest/download/new-panic-corpus.tar.gz` | Checks server every run |
| `https://...` URL | Downloaded directly | Immutable if filename matches `fuzz-bundle-YYYY-MM-DD*.tar.gz` |
| Local `.tar.gz` path | Read directly | No network access |

Override the default bundle repository via `SERVO_FUZZING_REPO`.

### Configurable Timeouts (Packing & Replay)

- **During bundle creation (`servo-fuzzing bundle pack` / `fuzzing-cron.yml`)**:
  - `--per-generator 400 --max-tests 1200` enforces an equal quota per generator subdirectory.
  - `--timeouts exclude|include` (default: `exclude`; configured in CI via `vars.BUNDLE_TIMEOUTS` or manual workflow input `bundle_timeouts`): when set to `include`, reserves up to `--max-timeout-tests` slots (default `50` in CLI, `150` in `fuzzing-cron.yml`) distributed across generators and lists them in `manifest.json` `"timeout_tests"`. When set to `exclude`, `fuzzing-cron.yml` oversamples generation (`600 x 3 = 1800`) so the packed bundle reliably fills `400 x 3 = 1200` passing testcases.
- **During bundle replay (`servo-fuzzing run` / `python3 -m servo_fuzzing.runner`)**:
  - `--timeouts include|exclude` (default: `include`): when set to `exclude`, skips any testcase listed in `manifest.json` `"timeout_tests"` without spending timeout budget on them (`fuzzing-ci.yml` passes `vars.REPLAY_TIMEOUTS || 'exclude'`).

---

## Signatures & Crash Classification

Crashes are parsed by `servo_fuzzing/parser.py` and matched in `servo_fuzzing/executor.py` into three distinct classifications:

| Status | Source Database | PR CI Behavior | Purpose |
|---|---|---|---|
| `KNOWN_CRASH` | `signatures/rust-panic.json` (open `B-fuzzing` issues) | Ignored (exit `0`) | Prevent known pre-existing bugs from blocking unrelated PRs |
| `REGRESSION` | `signatures/fixed-history.json` (closed `B-fuzzing` issues) | Fails CI (exit `1`) | Catch reappearance of previously fixed bugs and link the closed issue |
| `NEW_CRASH` | Matches neither database | Fails CI (exit `1`) | Block new regressions; trigger minimization, bisection, and upstream filing in discovery jobs |

### Matching & Normalization

- **Normalized Message** (`normalize_panic_message`): normalizes dynamic numbers (`<NUM>`), dimensions (`<DIM>`), and hex addresses (`0x<HEX>`), and recovers multi-line `assert_eq!` / wrapped `assert!` messages while ignoring CSS/renderer log lines before the panic hook.
- **Normalized Source File** (`normalize_source_path`): strips line/column numbers, Cargo registry/checkout prefixes, and rustc toolchain prefixes.
- **Ordered Stack Frames**: each frame in `stack_frames` must appear as a substring in order in the backtrace (an empty list places no frame constraint and matches on message + source file).
- **Shared Flags**: `DEFAULT_SERVO_ARGS` (`-xfz --enable-experimental-web-platform-features`) is shared across execution, reduction, bisection, and evidence reporting.

---

## Discovery, Minimization, Bisection & Upstream Issue Filing

Discovery and import runs follow one pipeline without storing raw or unfiled HTML reproducers in git:

```
generate / import
  └─> run_suite (classify KNOWN_CRASH / REGRESSION / NEW_CRASH)
        └─> sync-drafts --lookup-github (deduplicate + delta-line minimize -> target/triage/draft-signatures.json & target/panic-corpus/)
              └─> propose --bisect (verify repro, bisect servo/servo-nightly-builds, score commits -> target/proposals/<id>/)
                    └─> file-issues (file or link upstream GitHub issue + update rust-panic.json / fixed-history.json)
```

### Local Diagnostic & Filing Commands

```bash
# Minimize a crashing HTML file via coarse-to-fine delta line reduction
servo-fuzzing reduce crash.html \
  --binary ./servo/servoshell \
  --pattern "Found hoisted box" \
  --output reduced.html \
  --attempts 3

# Estimate flakiness of a reproducer
servo-fuzzing reproduce crash.html \
  --binary ./servo/servoshell \
  --pattern "parent_proxy" \
  --repeat 10 \
  --parallel 20

# Bisect across prebuilt servo/servo-nightly-builds releases
servo-fuzzing bisect reduced.html --pattern "Found hoisted box" -o target/bisect.md

# Build a full evidence pack (issue.md, context.json, AGENT.md, analysis.md, signature_draft.json)
servo-fuzzing report reduced.html \
  --binary ./servo/servoshell \
  --pack target/reports/my-crash \
  --original crash.html \
  --bisect

# Generate proposals for drafted crashes, then preview upstream filing
servo-fuzzing propose --binary ./servo/servoshell --bisect
servo-fuzzing file-issues --repo servo/servo --dry-run
```

### CI Configuration (Variables & Secrets)

| Name | Kind | Default | Purpose |
|---|---|---|---|
| `UPSTREAM_ISSUE_REPO` | Variable | Current repository | Target repo for upstream lookup and issue filing (e.g. `servo/servo`) |
| `UPSTREAM_ISSUE_TOKEN` | Secret | Empty | GitHub token with issue write permission when `UPSTREAM_ISSUE_REPO` differs from the current repo (automatically runs `--dry-run` if unset) |
| `BUNDLE_VERSION` | Variable | `latest` | Bundle spec replayed by `fuzzing-ci.yml` on PRs |
| `BUNDLE_TIMEOUTS` | Variable | `exclude` | Whether `fuzzing-cron.yml` includes (`include`) or excludes (`exclude`) timeouts in published bundles |
| `REPLAY_TIMEOUTS` | Variable | `exclude` | Whether `fuzzing-ci.yml` PR replay skips (`exclude`) or runs (`include`) manifest `timeout_tests` |

---

## CLI Reference

Entrypoints: `servo-fuzzing <subcommand>`, plus stdlib-compatible module entrypoints `python3 -m servo_fuzzing.runner`, `python3 -m servo_fuzzing.campaign`, and `python3 -m servo_fuzzing.collect`.

| Command | Role |
|---|---|
| `run` | Replay testcases or a bundle via `servo_fuzzing.runner` (`--bundle`, `--timeouts include\|exclude`, `--config`, `--testcases`) |
| `download-nightly` | Auto-detect OS/CPU architecture and download + verify + extract the matching `servoshell` nightly |
| `setup-generators` | Clone or check `domato`, `freedom`, and `minerva` in `generators/` |
| `generate` | Generate HTML testcases (`--tool domato\|freedom\|minerva`, `-n`, `-o`) |
| `bundle pack\|unpack\|download` | Pack balanced versioned bundles with `manifest.json`, unpack, or download |
| `import` | Ingest directory or archive (`.tar.*`, `.zip`), triage, deduplicate, minimize, and link upstream issues (`--lookup-github`) |
| `reduce` | Minimize a reproducer (`--method delta\|lithium`, `--signature`, `--attempts`) |
| `reproduce` | Estimate crash reproducibility (`--mode estimate\|repeat\|parallel`) |
| `bisect` | Binary-search `servo/servo-nightly-builds` to find `[last_good..first_bad]` commits |
| `report` | Generate a single-crash GitHub issue draft or `--pack DIR` evidence bundle |
| `propose` | Turn `draft-signatures.json` into bisected evidence proposals in `target/proposals/` |
| `file-issues` | File upstream GitHub issues from proposals and update `rust-panic.json` / `fixed-history.json` |
| `sync-issues` | Regenerate `signatures/rust-panic.json` (open) and `signatures/fixed-history.json` (closed) from `servo/servo` `B-fuzzing` issues |
| `sync-drafts` | Deduplicate and minimize `new_crashes/` into `draft-signatures.json` and `target/panic-corpus/` |
| `add-signature` | Manually append a signature entry to `signatures/rust-panic.json` |
| `validate-signatures` | Validate schema and uniqueness of signature JSON files |

---

## GitHub Actions Workflows

| Workflow | Trigger | Purpose |
|---|---|---|
| [.github/workflows/fuzzing-ci.yml](.github/workflows/fuzzing-ci.yml) | PR, merge queue, daily `02:00 UTC`, manual | Runs deterministic `checks` (pytest, `actionlint`, JSON & signature validation), PR `bundle-replay` against the 1,200-testcase bundle via `servo_fuzzing.runner`, and scheduled `fuzzing` discovery -> `report-new-panics.yml` |
| [.github/workflows/fuzzing-cron.yml](.github/workflows/fuzzing-cron.yml) | Weekly Sunday `00:00 UTC`, manual | Generates balanced testcases (`600x3` when excluding timeouts, `400x3` when including), triages, proposes & bisects new panics, publishes immutable `bundle-YYYY-MM-DD` (400 x 3 = 1,200 tests + `manifest.json`) + `latest` releases, and triggers `report-new-panics.yml` |
| [.github/workflows/import-crashes.yml](.github/workflows/import-crashes.yml) | `incoming`/`incoming-*` releases, issue `/triage` or `fuzz-import`, manual | Imports crash archives, deduplicates & minimizes, bisects proposals, comments `report.md` on triggering issues, and triggers `report-new-panics.yml` |
| [.github/workflows/report-new-panics.yml](.github/workflows/report-new-panics.yml) | `workflow_call` | Least-privilege workflow that runs `file-issues` and opens a signature-only PR updating `signatures/rust-panic.json` and `signatures/fixed-history.json` |
| [.github/workflows/sync-signatures.yml](.github/workflows/sync-signatures.yml) | Daily `06:00 UTC`, manual | Syncs `rust-panic.json` and `fixed-history.json` from upstream `B-fuzzing` issues and opens a PR on changes |

---

## Development & Testing

```bash
# Run full unit test suite
python3 -m unittest discover tests -v

# Validate signature files and runner dry-run
servo-fuzzing validate-signatures --signatures signatures/rust-panic.json
servo-fuzzing validate-signatures --signatures signatures/fixed-history.json
bash -n fuzz-continuous.sh
```

**Runtime Constraint**: `servo_fuzzing/runner.py` and the replay modules it imports (`executor.py`, `bundle.py`, `parser.py`, `reporter.py`, `signatures.py`, `collect.py`) must use **Python standard library modules only** so `servo/servo`'s `./mach test-fuzzing` executes cleanly under Mach's interpreter without `pip install` dependencies.
