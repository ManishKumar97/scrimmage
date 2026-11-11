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
    PACKAGES / "scrimmage-core" / "src" / "scrimmage_core": {"scrimmage_core", "scrimmage"},
    PACKAGES / "scrimmage" / "src" / "scrimmage": {"scrimmage"},
}
DYNAMIC = {"import_module", "__import__"}
# The only places that import a computed name: the team's own module from a mock's
# type, and the agent's function in the starter. Both are under SCRIMMAGE_ALLOW_IMPORTS.
COMPUTED_ALLOWED = {("mocks.py", "import_object"), ("start.py", "main")}


def imported_roots(path: Path) -> set[str]:
    """Top-level names a module imports, including importlib.import_module("x").

    A computed name is allowed only in the functions named in COMPUTED_ALLOWED.
    """
    roots: set[str] = set()
    tree = ast.parse(path.read_text(), str(path))
    owner: dict[ast.AST, str] = {}
    for func in ast.walk(tree):
        if isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
            for inner in ast.walk(func):
                owner.setdefault(inner, func.name)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in DYNAMIC and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    roots.add(arg.value.split(".")[0])
                elif (path.name, owner.get(node, "")) not in COMPUTED_ALLOWED:
                    roots.add(f"<computed name in {owner.get(node, 'module level')}>")
    return roots


CASES = [(p, ours) for src, ours in ALLOWED.items() for p in sorted(src.rglob("*.py"))]
# The Scrimmage MCP server is a command of its own, installed with the mcp extra. It never
# loads into the team's process: it starts each Scrimmage command as a child.
OWN_PROCESS = {"mcp_server.py": {"anyio", "jsonschema", "mcp", "mcp_types"}}


@pytest.mark.parametrize(("path", "ours"), CASES, ids=[str(p.name) for p, _ in CASES])
def test_imports_only_the_standard_library(path: Path, ours: set[str]) -> None:
    ours = ours | OWN_PROCESS.get(path.name, set())
    outside = imported_roots(path) - set(sys.stdlib_module_names) - ours
    assert not outside, f"{path} imports {sorted(outside)}"
