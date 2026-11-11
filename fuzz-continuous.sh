#!/usr/bin/env bash
# Compatibility adapter: generator/replay/archiving logic lives in Python.
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "$script_dir"
exec "${PYTHON:-python3}" -m servo_fuzzing.campaign "$@"
