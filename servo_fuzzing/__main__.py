"""Main entry point when executing `python -m servo_fuzzing`."""

import sys
from servo_fuzzing.cli import main

if __name__ == "__main__":
    sys.exit(main())
