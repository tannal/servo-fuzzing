# Servo Fuzzing

Automated DOM fuzzing test suite and crash triage harness for the [Servo](https://github.com/servo/servo) browser engine.

This repository serves as a specialized test suite for Servo—analogous to Web Platform Tests (WPT)—designed to run continuous fuzzing test cases during merge jobs, PR gating, and local development.

---

## Architecture & CI Integration

### How It Works in Servo CI

Servo integrates this repository directly into `./mach test-fuzzing` and GitHub Actions merge queue / PR workflows:

```
+-------------------------------------------------------------+
|                      PR / Merge Queue                       |
+-------------------------------------------------------------+
                               |
                               v
               ./mach test-fuzzing --config ...
                               |
                               v
            Execute testcases with headless servoshell
                               |
        +----------------------+----------------------+
        |                                             |
    No Crash                                     Crash Hit!
        |                                             |
     [ PASS ]                                         v
                                  Match against signatures/rust-panic.json
                                                      |
                                       +--------------+--------------+
                                       |                             |
                                 Known Signature               New Crash!
                                       |                             |
                                  [ IGNORED ]                  [ BLOCKS PR ]
                                  (Passes CI)           PR author investigates
```

### PR Gating Philosophy

1. **Amortizing Fuzzing**: Rather than running infinite standalone fuzzing campaigns, running 100 testcases per generator on every merge amortizes long-running fuzzing over time within a manageable CI budget.
2. **Known Issues Do Not Block**: If an existing, already-tracked crash is reproduced, it is matched against `signatures/rust-panic.json` and ignored. The test run passes.
3. **New Panics Block the PR**: If a crash occurs that does not match any known signature, `./mach test-fuzzing` exits with code `1`, blocking the PR from merging.
4. **PR Author Triage**:
   - The author checks `target/fuzzing/report.md` or the CI step summary.
   - **If the crash is caused by the PR**: The author investigates and fixes the regression.
   - **If the crash is an unrelated pre-existing bug**: The author files a GitHub issue labelled `B-fuzzing` and `I-panic` (using the ready-to-use markdown report in `target/fuzzing/new_crashes/`), adds the auto-generated signature to `signatures/rust-panic.json`, and triggers CI again.

---

## Integration with `./mach`

In Servo's `python/servo/testing_commands.py`:

```python
@Command("test-fuzzing", description="Run local Servo fuzzing tests", category="testing")
@CommandArgument("--config", required=True, help="Fuzzing suite configuration passed to servo-fuzzing")
@CommandArgument("--output", help="Artifact directory (defaults to target/fuzzing)")
@CommandBase.common_command_arguments(build_type=True, binary_selection=True)
def test_fuzzing(
    self,
    servo_binary: str,
    build_type: BuildType,
    config: str,
    output: str | None = None,
    **kwargs: Any,
) -> int:
    ...
    args = [
        sys.executable,
        "-m",
        "servo_fuzzing.runner",
        "--binary",
        servo_binary,
        "--config",
        str(Path(config).resolve()),
        "--signatures",
        str(root / "tests/fuzzing/signatures/rust-panic.json"),
        "--output",
        output or str(root / "target/fuzzing"),
    ]
    return subprocess.call(args, env=env)
```

Run locally:
```bash
./mach test-fuzzing --config tests/fuzzing/configs/default.json
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

## Signature Database (`signatures/rust-panic.json`)

The signature database tracks open fuzzing panics. It uses conservative exact-message/file and ordered-stack matching:

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

To synchronize local signatures with the latest open issues on GitHub:

```bash
servo-fuzzing sync-issues --repo servo/servo --signatures signatures/rust-panic.json
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

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.
