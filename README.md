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
   - **If the crash is an unrelated pre-existing bug**: The author files a GitHub issue labelled `B-fuzzing` and `I-panic` (using the ready-to-use markdown report in `target/fuzzing/new_crashes/`), adds the auto-generated signature to `signatures/rust-panic.json`, and triggers CI again.

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
New panic inputs are saved to `testcases/corpus/`, with per-round reports in `target/campaign/`.

### 4. Triage & Upload Found Crash HTMLs
You can triage and minimize a directory or archive of raw crash HTMLs locally or via GitHub Actions:

- **Local triage & minimization**:
  ```bash
  servo-fuzzing import ./crashes/ \
    --binary ../servo/target/release/servoshell \
    --signatures signatures/rust-panic.json \
    --corpus testcases/corpus \
    --lookup-github
  ```
- **Zero-git CI upload (GitHub Issue / Release / Manual Dispatch)**:
  1. Archive your crash HTMLs (`tar -czf crashes.tar.gz crashes/`).
  2. Open an issue on GitHub, drag-and-drop `crashes.tar.gz` into the issue body, and add the `fuzz-import` label (or comment `/triage`).
  3. Or upload to a GitHub Release (`incoming`) / run **Ingest, Triage & Deduplicate Crashes** (`workflow_dispatch`).
  4. CI automatically deduplicates, minimizes reproducers into `testcases/corpus/`, links existing open/closed Servo issues, flags historical regressions, and opens a PR.

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
servo-fuzzing generate --tool domato -n 100 -o testcases/
servo-fuzzing generate -n 100 -o testcases/  # runs all installed tools

# Reproduce and calculate reproducibility rate for a flaky crash
servo-fuzzing reproduce testcases/issue-49411.html --binary ./servo/servoshell --pattern "parent_proxy"

# Generate a formatted GitHub issue report from a crash
servo-fuzzing report testcase.html --binary ./servo/servoshell

# Sync open B-fuzzing issues from GitHub to update rust-panic.json
servo-fuzzing sync-issues --signatures signatures/rust-panic.json --repo servo/servo
```

---

## Signature Database (`signatures/rust-panic.json` & `signatures/fixed-history.json`)

The harness separates **known open issues**, **upstream issue deduplication**, and **historical regression detection**:

- **`signatures/rust-panic.json`**: Tracks **open** fuzzing panics. Matches are classified as `KNOWN_CRASH` and ignored in PR gating so unrelated pre-existing bugs do not block merges.
- **`signatures/fixed-history.json`**: Tracks **closed/fixed** fuzzing panics. Matches are classified as `REGRESSION` (failing PR CI with a link to the previously closed issue).
- **Upstream Issue Lookup (`--lookup-github`)**: During `import` / `sync-drafts`, new panics search `servo/servo` open and closed issues to link existing issues automatically instead of leaving `TODO`.

```json
{
  "schema_version": 1,
  "signatures": [
    {
      "id": "windowproxy-parent-active",
      "message": "assertion failed: parent_proxy.currently_active().is_some()",
      "source_file": "components/script/dom/window/windowproxy.rs",
      "stack_frames": [
        "<script::dom::servoparser::ParserContext>::initialize_document_object"
      ],
      "issue": "https://github.com/servo/servo/issues/49411"
    }
  ]
}
```

### Matching Algorithm
- **Exact Message**: `entry["message"] == crash.message`
- **Normalized Source File**: `entry["source_file"] == crash.source_file` (strips line/column numbers so line shifts don't break signatures).
- **Ordered Stack Frames**: Each frame specified in `entry["stack_frames"]` must appear in the actual backtrace in the given relative order.

---

## Generating Test Cases

### Supported Fuzzers

1. **[Domato](https://github.com/googleprojectzero/domato)** (Google Project Zero):
   ```bash
   git clone https://github.com/googleprojectzero/domato.git generators/domato
   python generators/domato/generator.py -o testcases/ -n 100
   ```

2. **[Freedom](https://github.com/sslab-gatech/freedom)** (SSLab Gatech):
   ```bash
   git clone https://github.com/sslab-gatech/freedom.git generators/freedom
   python generators/freedom/main.py -i 1 -m generate -n 100 -o testcases/
   ```

3. **[Minerva](https://github.com/ChijinZ/Minerva)** (ChijinZ):
   ```bash
   git clone https://github.com/ChijinZ/Minerva.git generators/minerva
   export MEM_DEP_JSON_PATH=generators/minerva/mod_ref_helper/mem_dep.json
   python generators/minerva/generator.py --output_dir testcases/ --no_of_files 100
   ```

Or automatically via CLI:
```bash
servo-fuzzing generate --clone -n 100 -o testcases/
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
./servo-fuzzing reduce testcases/bundle/freedom/1-179437008301.html \
  --binary ../servo/target/debug/servoshell \
  --pattern "assertion failed: end_offset" \
  --output testcases/corpus/range-end-offset.html
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

Servo tracks fuzzing bugs using the [`B-fuzzing`](https://github.com/servo/servo/issues?q=state%3Aopen%20label%3A%22B-fuzzing%22) label.

To synchronize local signatures with the latest open and closed `B-fuzzing` issues on GitHub:

```bash
servo-fuzzing sync-issues \
  --repo servo/servo \
  --signatures signatures/rust-panic.json \
  --history signatures/fixed-history.json \
  --include-closed
```

A scheduled GitHub Actions workflow (`.github/workflows/sync-signatures.yml`) runs daily to keep the signature database up to date.

---

## Backward-Compatible Shell Scripts

The legacy shell scripts are retained and updated as wrappers around the modern Python package:

- `./download-latest-nightly.sh`: Downloads latest Linux `servoshell`.
- `./run-testcases.sh`: Executes testcases in headless mode.
- `./print-unknown-panic.sh`: Checks `.txt` logs for unknown panics against `rust-panic.json`.
- `./print-github-report.sh`: Generates a draft GitHub issue.
- `./repeat.sh`: Repeats testcase execution.
- `./parallelize.sh`: Executes instances in parallel.
- `./reproduce.sh`: Tests reproducibility rate.

---




## Collect new panic inputs

Replay automatically preserves new Rust panic inputs in `<output>/corpus`, with
logs, draft signatures, and provenance. This collects original inputs; it does
not minimize, reproduce, ignore, or file issues for them.

Collect a local run into your regression corpus:

```sh
python -m servo_fuzzing.collect --input ../servo/target/fuzzing --output testcases/corpus
```

Collect an existing GitHub Actions run:

```sh
gh run download RUN_ID --repo tannal/servo-fuzzing --name triage-reports --dir target/downloaded-triage
python -m servo_fuzzing.collect --input target/downloaded-triage --output testcases/corpus
```

Use artifact `fuzzing-artifacts` for the ordinary CI workflow. Future runs also
upload `new-panic-corpus`, already arranged for replay. Content hashes prevent
identical HTML from being collected repeatedly. The summary selects only current
NEW_CRASH results with a parsed panic signature, excluding stale folders and
known crashes. Archived inputs work without the original CI filesystem. External
resources not saved in the original report cannot be recovered by this tool.

Latest bundles now refresh on each run. A valid cached archive remains a warned
offline fallback if refreshing fails. Local bundle paths do not download.


## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.
