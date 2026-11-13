"""Assert a JUnit XML file holds exactly N passed tests and nothing else.

Usage: python check_junit.py FILE N
Exit 0 when the file has N test cases and every one passed. Exit 1 otherwise.

It counts leaf testcase elements, not the attributes of the suites, so a nested suite is
never counted twice and an attribute that disagrees with the cases cannot hide one.
A case passes only if it has no skipped, failure, error, rerun or flaky child and no
xpass property. A suite attribute for skipped, failures or errors that is not 0 fails
the file, and so does any negative counter.
"""

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

_BAD_CHILDREN = {
    "skipped",
    "failure",
    "error",
    "rerunFailure",
    "rerunError",
    "flakyFailure",
    "flakyError",
}
_COUNTERS = ("tests", "skipped", "failures", "errors")


def _case_problem(case: ET.Element) -> str | None:
    name = f"{case.get('classname', '')}::{case.get('name', '?')}"
    for child in case:
        if child.tag in _BAD_CHILDREN:
            return f"{name}: has a {child.tag} element"
        if child.tag == "properties" and any(
            "xpass" in (p.get("name") or "").lower() for p in child
        ):
            return f"{name}: is an xpass"
    return None


def problems(path: Path, expected: int) -> list[str]:
    root = ET.parse(path).getroot()
    suites = [e for e in root.iter() if e.tag == "testsuite"]
    cases = [e for e in root.iter() if e.tag == "testcase"]
    if not suites:
        raise ValueError(f"{path}: no testsuite element")
    found: list[str] = []
    for suite in suites:
        for key in _COUNTERS:
            value = int(suite.get(key, "0"))
            if value < 0:
                found.append(f"suite {suite.get('name', '?')}: {key} is negative")
            elif key != "tests" and value:
                found.append(f"suite {suite.get('name', '?')}: {key} is {value}, expected 0")
    bad = [p for p in map(_case_problem, cases) if p]
    found += bad
    if len(cases) - len(bad) != expected or len(cases) != expected:
        found.append(
            f"{len(cases) - len(bad)} passed of {len(cases)} cases, expected exactly {expected}"
        )
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
