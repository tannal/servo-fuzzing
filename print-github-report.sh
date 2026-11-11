#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="$DIR:${PYTHONPATH:-}"

if [ $# -lt 2 ]; then
    echo "Usage: $0 TESTCASE SERVO_BIN"
    exit 1
fi

python3 -m servo_fuzzing.cli report "$1" --binary "$2"
