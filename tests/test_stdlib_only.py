"""The SDK and the marker load into the team's own process, so they import only the
standard library. A third-party import there could clash with the team's versions.
The marker must also work without the SDK installed, so it never imports it."""

import ast
import sys
from pathlib import Path

import pytest

PACKAGES = Path(__file__).parents[1] / "packages"
# Each source tree, with the first-party names it may import.
ALLOWED = {
    PACKAGES / "faultline" / "src" / "faultline_sdk": {"faultline_sdk", "faultline"},
    PACKAGES / "faultline-marker" / "src" / "faultline": {"faultline"},
}
DYNAMIC = {"import_module", "__import__"}


def imported_roots(path: Path) -> set[str]:
    """Top-level names a module imports, including importlib.import_module("x")."""
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(), str(path))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else getattr(func, "id", "")
            )
            if name in DYNAMIC and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    roots.add(arg.value.split(".")[0])
                else:
                    roots.add("<computed name>")
    return roots


CASES = [(p, ours) for src, ours in ALLOWED.items() for p in sorted(src.rglob("*.py"))]


@pytest.mark.parametrize(("path", "ours"), CASES, ids=[str(p.name) for p, _ in CASES])
def test_imports_only_the_standard_library(path: Path, ours: set[str]) -> None:
    outside = imported_roots(path) - set(sys.stdlib_module_names) - ours
    assert not outside, f"{path} imports {sorted(outside)}"
