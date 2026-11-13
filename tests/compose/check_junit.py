"""Assert a JUnit XML file holds exactly N passed tests and nothing else.

Usage: python check_junit.py FILE N
Exit 0 when the file has N tests, none skipped, failed or errored. Exit 1 otherwise.
"""

import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def count(path: Path) -> dict[str, int]:
    """Sum the counters over every testsuite in the file."""
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    if not suites:
        raise ValueError(f"{path}: no testsuite element")
    total = {"tests": 0, "skipped": 0, "failures": 0, "errors": 0}
    for suite in suites:
        for key in total:
            total[key] += int(suite.get(key, "0"))
    return total


def problems(path: Path, expected: int) -> list[str]:
    got = count(path)
    found = [
        f"{key} is {got[key]}, expected 0" for key in ("skipped", "failures", "errors") if got[key]
    ]
    passed = got["tests"] - got["skipped"] - got["failures"] - got["errors"]
    if passed != expected or got["tests"] != expected:
        found.append(f"{passed} passed of {got['tests']} tests, expected exactly {expected}")
    return found


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: check_junit.py FILE N", file=sys.stderr)
        return 2
    try:
        found = problems(Path(argv[1]), int(argv[2]))
    except (OSError, ValueError, ET.ParseError) as exc:
        print(f"check_junit: {exc}", file=sys.stderr)
        return 1
    for line in found:
        print(f"check_junit: {line}", file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
