#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="$DIR:${PYTHONPATH:-}"

if [ $# -ne 1 ]; then
    echo "Usage: $0 TESTCASE_DIRECTORY"
    exit 1
fi

python3 -c "
import sys
from pathlib import Path
from servo_fuzzing.parser import parse_crash
from servo_fuzzing.signatures import load_signatures, match

dir_path = Path(sys.argv[1])
sig_file = Path('$DIR/signatures/rust-panic.json')
sigs = load_signatures(sig_file) if sig_file.exists() else []

for txt_file in sorted(dir_path.glob('*.txt')):
    content = txt_file.read_text(encoding='utf-8', errors='replace')
    crash = parse_crash(content)
    if crash is not None and not match(crash, sigs):
        print(f'{txt_file.stem} crashed: {crash.message} ({crash.source_file})')
" "$1"
