"""Print the gate test files, one per line, from tools/gate-tests.txt.

    python tools/gate_tests.py                # every gate test
    python tools/gate_tests.py --without-range  # all but the range check

The range check, tests/test_tested_range.py, says a library is newer than the range its gate
was tested with. That is a release-time check, not a break, so the next-release legs and a
widening trial leave it out. Standard library only.
"""

from __future__ import annotations

import sys
from pathlib import Path

RANGE_TEST = "tests/test_tested_range.py"
LIST = Path(__file__).with_name("gate-tests.txt")


def main(argv: list[str]) -> int:
    if argv not in ([], ["--without-range"]):
        print(__doc__, file=sys.stderr)
        return 2
    files = LIST.read_text().split()
    if argv:
        files = [f for f in files if f != RANGE_TEST]
    print("\n".join(files))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
