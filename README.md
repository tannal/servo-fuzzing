# Servo Fuzzing

Automated DOM fuzzing test suite and crash triage harness for the [Servo](https://github.com/servo/servo) browser engine.

This repository serves as a specialized test suite for Servo—analogous to Web Platform Tests (WPT)—designed to run continuous fuzzing test cases during merge jobs, PR gating, and local development.

---

## Architecture & CI Integration

### How It Works in Servo CI

Servo integrates this repository directly into `./mach test-fuzzing` and GitHub Actions merge queue / PR workflows.

### PR Gating Philosophy

1. **Amortizing Fuzzing**: Rather than running infinite standalone fuzzing campaigns, running 100 testcases per generator on every merge amortizes long-running fuzzing over time within a manageable CI budget.
2. **Known Issues Do Not Block**: If an existing, already-tracked crash is reproduced, it is matched against `signatures/rust-panic.json` and ignored. The test run passes.
3. **New Panics Block the PR**: If a crash occurs that does not match any known signature, `./mach test-fuzzing` exits with code `1`, blocking the PR from merging.
4. **PR Author Triage**:
   - The author checks `target/fuzzing/report.md` or the CI step summary.
   - **If the crash is caused by the PR**: The author investigates and fixes the regression.
   - **If the crash is an unrelated pre-existing bug**: The author files a GitHub issue labelled `B-fuzzing` and `I-panic` (using the ready-to-use markdown report in `target/fuzzing/new_crashes/`), and triggers CI again once the next upstream sync has imported the issue into `signatures/rust-panic.json`.

---

## Quick Start for New Developers (After Building Servo)

Once you have built `servoshell` locally (e.g. `../servo/target/debug/servoshell` or `release/servoshell`):

### 1. Install `servo-fuzzing`
```bash
git clone https://github.com/tannal/servo-fuzzing.git
cd servo-fuzzing
pip install -e .
```

### 2. Run `./mach test-fuzzing` (From Servo Checkout)
```bash
# Automatically downloads the latest clean bundle and checks against signatures/
./mach test-fuzzing

# Or specify a custom bundle URL / run:
./mach test-fuzzing --bundle https://github.com/tannal/servo-fuzzing/releases/latest/download/fuzz-bundle-latest.tar.gz
```

### 3. Run Continuous Self-Hosted Fuzzing (`fuzz-continuous.sh`)
```bash
# Install DOM generators (domato, freedom, minerva)
servo-fuzzing setup-generators

# Run continuous generate -> replay -> collect loop against your built servoshell
./fuzz-continuous.sh --binary ../servo/target/release/servoshell --jobs 4 --count 30
```
New panic inputs are saved to `target/panic-corpus/`, with per-round reports in `target/campaign/`.

### 4. Triage & Upload Found Crash HTMLs
You can triage and minimize a directory or archive of raw crash HTMLs locally or via GitHub Actions:

- **Local triage & minimization**:
  ```bash
  servo-fuzzing import ./crashes/ \
    --binary ../servo/target/release/servoshell \
    --signatures signatures/rust-panic.json \
    --corpus target/panic-corpus \
    --lookup-github
  ```
  Draft signatures go to `target/triage/draft-signatures.json`; the files in `signatures/` are not modified.
- **Zero-git CI upload (GitHub Issue / Release / Manual Dispatch)**:
  1. Archive your crash HTMLs (`tar -czf crashes.tar.gz crashes/`).
  2. Open an issue on GitHub, drag-and-drop `crashes.tar.gz` into the issue body, and add the `fuzz-import` label (or comment `/triage`).
  3. Or upload to a GitHub Release (`incoming`) / run **Ingest, Triage & Deduplicate Crashes** (`workflow_dispatch`).
  4. CI deduplicates, minimizes, links existing open/closed Servo issues, flags historical regressions, and uploads the result as the `triage-reports` and `new-panic-corpus` artifacts.

---

## Integration with `./mach`

Run locally:
```bash
./mach test-fuzzing --bundle https://github.com/tannal/servo-fuzzing/actions/runs/43194534865
```

---

## Installation & CLI

### Installation

```bash
pip install -e .
```

### CLI Subcommands

The `servo-fuzzing` CLI provides commands for running, generating, reproducing, and triaging test cases:

```bash
# Run the test suite against a servoshell binary
servo-fuzzing run --binary ./servo/servoshell --config configs/default.json --signatures signatures/rust-panic.json

# Validate signature database schema
servo-fuzzing validate-signatures --signatures signatures/rust-panic.json

# Generate test cases using Domato, Freedom, or Minerva
servo-fuzzing generate --tool domato -n 100 -o target/generated
servo-fuzzing generate -n 100 -o target/generated  # runs all installed tools

# Reproduce and calculate reproducibility rate for a flaky crash
servo-fuzzing reproduce testcase.html --binary ./servo/servoshell --pattern "parent_proxy"

# Bisect a crash across nightly builds to find the upstream commit range
servo-fuzzing bisect testcase.html --pattern "parent_proxy"

# Generate a formatted GitHub issue report from a crash
servo-fuzzing report testcase.html --binary ./servo/servoshell

# Same, as an evidence pack with the regression range for people and agents
servo-fuzzing report testcase.html --binary ./servo/servoshell --pack target/reports/crash --bisect

# Regenerate both signature files from the B-fuzzing issues of servo/servo
servo-fuzzing sync-issues
```

---

## Signature Database (`signatures/rust-panic.json` & `signatures/fixed-history.json`)

Both files are generated from the [`B-fuzzing`](https://github.com/servo/servo/issues?q=label%3AB-fuzzing) issues of `servo/servo` and are never edited by hand or by a triage run:

- **`signatures/rust-panic.json`**: one signature per **open** issue. Matches are classified as `KNOWN_CRASH` and ignored in PR gating so unrelated pre-existing bugs do not block merges.
- **`signatures/fixed-history.json`**: one signature per **closed** issue. Matches are classified as `REGRESSION` (failing PR CI with a link to the previously closed issue).

A crash that matches neither file is new. Triage writes a draft for it to `target/triage/draft-signatures.json`; it becomes known once its issue is filed upstream with the `B-fuzzing` label and the next sync runs. See [GitHub Issues Synchronization](#github-issues-synchronization).

```json
{
  "schema_version": 1,
  "signatures": [
    {
      "id": "issue-49411",
      "message": "assertion failed: parent_proxy.currently_active().is_some()",
      "source_file": "components/script/dom/window/windowproxy.rs",
      "stack_frames": [
        "<script::dom::servoparser::ParserContext>::initialize_document_object",
        "<script::dom::servoparser::ParserContext>::load_document"
      ],
      "issue": "https://github.com/servo/servo/issues/49411"
    }
  ]
}
```

### Matching Algorithm
- **Exact Message**: `entry["message"] == crash.message`
- **Normalized Source File**: `entry["source_file"] == crash.source_file` (strips line/column numbers so line shifts don't break signatures).
- **Ordered Stack Frames**: Each frame specified in `entry["stack_frames"]` must appear in the actual backtrace in the given relative order. The list is empty when the issue has no backtrace; the signature then matches on message and source file alone.

---

## Generating Test Cases

### Supported Fuzzers

1. **[Domato](https://github.com/googleprojectzero/domato)** (Google Project Zero):
   ```bash
   git clone https://github.com/googleprojectzero/domato.git generators/domato
   python generators/domato/generator.py -o target/generated/domato -n 100
   ```

2. **[Freedom](https://github.com/sslab-gatech/freedom)** (SSLab Gatech):
   ```bash
   git clone https://github.com/sslab-gatech/freedom.git generators/freedom
   python generators/freedom/main.py -i 1 -m generate -n 100 -o target/generated/freedom
   ```

3. **[Minerva](https://github.com/ChijinZ/Minerva)** (ChijinZ):
   ```bash
   git clone https://github.com/ChijinZ/Minerva.git generators/minerva
   export MEM_DEP_JSON_PATH=generators/minerva/mod_ref_helper/mem_dep.json
   python generators/minerva/generator.py --output_dir target/generated/minerva --no_of_files 100
   ```

Or automatically via CLI:
```bash
servo-fuzzing generate --clone -n 100 -o target/generated
```

---

## Reducing & Reproducing Test Cases

### Reproducing Flaky Crashes

Estimate crash probability across multiple parallel rounds:

```bash
servo-fuzzing reproduce testcase.html \
    --binary ./servo/servoshell \
    --pattern "parent_proxy" \
    --repeat 10 \
    --parallel 20
```

### Bisecting Across Nightlies

Find the upstream commit range that introduced a crash by bisecting the
[Servo nightly builds](https://github.com/servo/servo-nightly-builds/releases):

```bash
servo-fuzzing bisect crash.html \
    --pattern "Found hoisted box" \
    -o target/bisect/report.md
```

The latest nightly must reproduce the crash. Without `--good`, older nightlies
are searched with doubling steps until one does not reproduce, then binary
search narrows it to adjacent nightlies. The report links the
`servo/servo` compare range between the last good and first bad nightly.
Use `--good`/`--bad YYYY-MM-DD` to bound the search, `--attempts` for flaky
crashes and `--asset` for other platforms. Set `GITHUB_TOKEN` to avoid API
rate limits.

### Crash Reports and Evidence Packs

`report` runs a testcase and prints an issue draft: title, labels, testcase,
reproduction rate and the backtrace without renderer logs. With `--pack` it
writes a directory that a developer, a maintainer of this corpus and a coding
agent can all work from:

```bash
servo-fuzzing report reduced.html --binary ./servo/servoshell \
    --pack target/reports/my-crash --original generated.html --bisect
```

| File | Reader | Content |
|---|---|---|
| `issue.md` | Servo developers | Measured facts only: testcase, reproduction rate, regression range, backtrace |
| `context.json` | agents, scripts | The same facts as data, plus the source around the panic line and every commit in the range with its changed files |
| `AGENT.md` | agents | The analysis task: make the fuzzer's generated names readable, ablate, map the crash to Servo source |
| `analysis.md` | people and agents | Root cause notes, with verified facts apart from hypotheses |
| `signature_draft.json` | people | Draft signature of this crash. It reaches `signatures/rust-panic.json` through the upstream sync once the issue is filed |

`--bisect` runs the nightly bisect above and scores each commit in the range by
how directly it touches the crash: adding the panic message, changing the
panicking file, or changing a file in the backtrace. `issue.md` lists the
closest ones; none of them is confirmed by building it.

The tool never calls a model. To get an analysis, point an agent at the pack
directory and `AGENT.md`, or fill in `analysis.md` by hand. Re-running `report`
regenerates everything except `analysis.md`. Its filled-in sections are copied
into `issue.md` only with `--include-analysis`, so what gets posted upstream is
an explicit choice.

### Reducing Test Cases with Lithium

You can use [Lithium](https://github.com/MozillaSecurity/lithium/) to reduce a testcase to a minimal reproducer:

```bash
# Reduce until the crash disappears
python3 -m lithium crashes -t1 ./servo/servoshell -xzf testcase.html

# Reduce while preserving a specific panic message
python3 -m lithium outputs -t1 -s "assertion failed" ./servo/servoshell -xzf testcase.html
```

The built-in reducer uses Lithium's coarse-to-fine chunk removal: large blocks
first, then progressively smaller blocks, repeating single-line passes until
no further removal preserves the panic. No Lithium installation is needed.

```bash
./servo-fuzzing reduce target/bundle/freedom/1-179437008301.html \
  --binary ../servo/target/debug/servoshell \
  --pattern "assertion failed: end_offset" \
  --output target/panic-corpus/range-end-offset.html
```

For intermittent panics, add `--attempts 3` to try each candidate up to three
times (accepting any matching panic). `--timeout` controls each launch.
Successful reductions are saved immediately; interrupted runs leave the last
accepted testcase at the output path. Resume by reducing that output file.
Trial files live beside the output, so choose an output directory where relative
resource references still resolve.

Or call the built-in reducer from Python:
```python
from servo_fuzzing.reducer import reduce_delta_lines
reduce_delta_lines("./servo/servoshell", Path("testcase.html"), target_message="parent_proxy")
```

---

## GitHub Issues Synchronization

Servo tracks fuzzing bugs using the [`B-fuzzing`](https://github.com/servo/servo/issues?q=state%3Aopen%20label%3A%22B-fuzzing%22) label. `sync-issues` rebuilds both signature files from those issues:

```bash
servo-fuzzing sync-issues \
  --repo servo/servo \
  --signatures signatures/rust-panic.json \
  --history signatures/fixed-history.json
```

- Open issues are written to `--signatures`, closed issues to `--history`, each as `issue-<number>`. Nothing from the previous files is kept, so a signature follows its issue when it is edited, closed or reopened.
- A signature needs a panic line with a source location (`message (thread ..., at file.rs:line)` or `panicked at file.rs:line`). Issues without one (ASAN reports, signals, debugger backtraces, tracking issues) are listed as skipped; `--summary FILE` writes the counts and the list as Markdown.
- The files are only written after every GitHub request succeeded and at least one issue was returned.

`.github/workflows/sync-signatures.yml` runs this daily and on manual dispatch (`gh workflow run sync-signatures.yml`), and opens a pull request when the result differs from the committed files. The fuzzing and import workflows run the same sync before triage, so they compare against the current upstream state without committing anything.

---

## Bundle Format

A bundle is a gzip-compressed tar archive of HTML testcases, with nothing else in it: no manifest, no signatures, no `servoshell`.

```
fuzz-bundle-latest.tar.gz
├── domato/fuzz-00001.html
├── freedom/1-179471886257.html
└── minerva/....html
```

- **Paths** are relative to the directory given to `bundle pack --source` (`target/generated` in CI), so the first component is the generator name. A testcase must be self-contained, because no other files are packed.
- **Selection** (`servo-fuzzing bundle pack`): of the generated testcases, only those with status `PASS` in the triage run (`--from-triage target/triage`) are kept, up to `--max-tests` (300). Crashes and timeouts of the run that built the bundle are left out, so every testcase passed on the nightly that built it.
- **Replay** (`servo-fuzzing run --bundle SPEC`, or `./mach test-fuzzing --bundle SPEC` in Servo): the archive is unpacked into `<output>/bundle`, every `.html`/`.htm` in it is run, and each crash is compared with the two signature files. `SPEC` is a local `.tar.gz`, a URL, or `latest` (the default), which downloads the asset below. The run fails when the bundle cannot be obtained.

| Archive | Content | Where it is published |
|---|---|---|
| `fuzz-bundle-latest.tar.gz` | Passing testcases to replay in Servo CI | Asset of the `latest` release and artifact `fuzz-bundle-latest`, from the weekly `fuzzing-cron.yml` run |
| `new-panic-corpus.tar.gz` | Minimized reproducers of the new panics of one run as `<signature id>.html`, each with its draft signature as `<signature id>.json` | Asset of the `latest` release (weekly run) and artifact `new-panic-corpus` |

The `triage-reports` artifact is not a bundle: it is the `target/triage` directory with `report.md`, `summary.json`, `draft-signatures.json` and one folder per crash.

---

## Shell Scripts

- `./download-latest-nightly.sh`: Downloads the latest Linux `servoshell` nightly into `./servo`.
- `./fuzz-continuous.sh`: Runs `python -m servo_fuzzing.campaign`.

---

## Collect new panic inputs

Replay automatically preserves new Rust panic inputs in `<output>/corpus`, with
logs, draft signatures, and provenance. This collects original inputs; it does
not minimize, reproduce, ignore, or file issues for them.

Collect a local run:

```sh
python -m servo_fuzzing.collect --input ../servo/target/fuzzing --output target/panic-corpus
```

Collect an existing GitHub Actions run:

```sh
gh run download RUN_ID --repo tannal/servo-fuzzing --name triage-reports --dir target/downloaded-triage
python -m servo_fuzzing.collect --input target/downloaded-triage --output target/panic-corpus
```

Use artifact `fuzzing-artifacts` for the ordinary CI workflow. Future runs also
upload `new-panic-corpus`, already arranged for replay. Content hashes prevent
identical HTML from being collected repeatedly. The summary selects only current
NEW_CRASH results with a parsed panic signature, excluding stale folders and
known crashes. Archived inputs work without the original CI filesystem. External
resources not saved in the original report cannot be recovered by this tool.

Latest bundles now refresh on each run. A valid cached archive remains a warned
offline fallback if refreshing fails. Local bundle paths do not download.

### Timed-out testcases in bundles

`bundle pack` drops testcases that time out by default. A replay never fails on
a timeout, so keeping them only costs time, and a looping testcase still gets a
chance to crash before the threshold:

```sh
servo-fuzzing bundle pack --source target/generated --from-triage target/triage \
  --timeouts include --max-timeout-tests 50
```

`--max-timeout-tests` (default 50) bounds that cost; the kept timeouts count
towards `--max-tests`. The weekly campaign reads the `BUNDLE_TIMEOUTS`
repository variable (`exclude` or `include`), which a manual run can override
with the `bundle_timeouts` input.


## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.
