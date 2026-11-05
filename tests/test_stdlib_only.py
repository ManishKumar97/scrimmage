"""The SDK and the marker load into the team's own process, so they import only the
standard library. A third-party import there could clash with the team's versions."""

import ast
import sys
from pathlib import Path

import pytest

PACKAGES = Path(__file__).parents[1] / "packages"
SOURCES = [
    PACKAGES / "faultline" / "src" / "faultline_sdk",
    PACKAGES / "faultline-marker" / "src" / "faultline",
]
OURS = {"faultline_sdk", "faultline"}


def imported_roots(path: Path) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(), str(path))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize("path", [p for s in SOURCES for p in sorted(s.rglob("*.py"))])
def test_imports_only_the_standard_library(path: Path) -> None:
    outside = imported_roots(path) - set(sys.stdlib_module_names) - OURS
    assert not outside, f"{path.name} imports {sorted(outside)}"
