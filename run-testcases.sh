#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="$DIR:${PYTHONPATH:-}"

if [ $# -lt 2 ]; then
    echo "Usage: $0 TESTCASE_DIRECTORY SERVOSHELL_BIN [SERVO_EXTRA_ARGS]"
    exit 1
fi

SERVOSHELL=$2
shift 2

python3 -m servo_fuzzing.runner \
    --binary "$SERVOSHELL" \
    --config "$DIR/configs/default.json" \
    --signatures "$DIR/signatures/rust-panic.json" \
    --output "$DIR/target/fuzzing"
