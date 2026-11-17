"""Say whether a branch needs the kind tests, from the files it changes against a base.

    python tools/kind_needed.py origin/main

Exit 0 prints each changed file that needs kind, one per line.
Exit 1 prints nothing: no changed file needs kind.
Exit 2 means git could not compare against the base, so the answer is unknown.

A file needs kind when it is Kubernetes code, the page that shows its manifests, or a test
file the kind tests read.
A change to product code alone does not need kind; the kind run on main at the end of each
slice covers it.
Standard library only, so it runs before any environment is installed.
"""

from __future__ import annotations

import subprocess
import sys

PREFIXES = (
    "tests/k8s/",
    "tests/test_k8s_",
    "images/runner/",
    "tests/example/",
    "tests/fakes/",
    "tests/fixtures/openai/",
)
FILES = ("tests/test_service_k8s.py", "docs/kubernetes.md", "tests/compose/harness.py")


def needed(paths: list[str]) -> list[str]:
    """The paths, in their order, that need the kind tests."""
    return [path for path in paths if path.startswith(PREFIXES) or path in FILES]


def changed(base: str) -> list[str]:
    """The files this branch changes since it left base, as `git diff base...HEAD` names them.

    With --no-renames a moved file is named at both paths, so a file moved out of a
    Kubernetes folder still counts.
    """
    out = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", f"{base}...HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [line for line in out.splitlines() if line]


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: kind_needed.py <base>", file=sys.stderr)
        return 2
    try:
        paths = changed(argv[0])
    except subprocess.CalledProcessError as error:
        print(f"git diff against {argv[0]} failed: {error.stderr.strip()}", file=sys.stderr)
        return 2
    found = needed(paths)
    for path in found:
        print(path)
    return 0 if found else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
