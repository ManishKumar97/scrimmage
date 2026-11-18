"""The SDK loads into the team's own process, so it imports only the standard library.
A third-party import there could clash with the team's versions."""

import ast
import sys
from pathlib import Path

import pytest

PACKAGES = Path(__file__).parents[1] / "packages"
# Each source tree, with the first-party names it may import.
ALLOWED = {
    PACKAGES / "scrimmage" / "src" / "scrimmage_core": {"scrimmage_core"},
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
OWN_PROCESS = {
    "mcp_server.py": {"anyio", "jsonschema", "mcp", "mcp_types"},
    # scrimmage cert, installed with the cert extra, run by hand and never imported by the rest.
    "cert.py": {"cryptography"},
}


# Standard-library modules of a newer Python the package supports. Each is imported only
# under a version check, so an older Python's sys.stdlib_module_names does not list it.
NEWER_STDLIB = {"annotationlib"}  # Python 3.14


@pytest.mark.parametrize(("path", "ours"), CASES, ids=[str(p.name) for p, _ in CASES])
def test_imports_only_the_standard_library(path: Path, ours: set[str]) -> None:
    ours = ours | OWN_PROCESS.get(path.name, set())
    outside = imported_roots(path) - set(sys.stdlib_module_names) - NEWER_STDLIB - ours
    assert not outside, f"{path} imports {sorted(outside)}"


SERVER = "scrimmage_core.mcp_server"


def imports_the_server(path: Path) -> bool:
    """Whether a module imports the MCP server, by any of the ways Python allows."""
    tree = ast.parse(path.read_text(), str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            alias.name == SERVER or alias.name.startswith(SERVER + ".") for alias in node.names
        ):
            return True
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level == 0 and (module == SERVER or module.startswith(SERVER + ".")):
                return True
            names = {alias.name for alias in node.names}
            package = module == "scrimmage_core" and node.level == 0
            if (package or (node.level == 1 and not module)) and "mcp_server" in names:
                return True
            if node.level == 1 and module == "mcp_server":
                return True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute | ast.Name):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
            argument = node.args[0] if node.args else None
            if (
                name in DYNAMIC
                and isinstance(argument, ast.Constant)
                and str(argument.value).startswith(SERVER)
            ):
                return True
    return False


def test_only_the_mcp_server_command_loads_the_mcp_server(tmp_path: Path) -> None:
    """The server is a command of its own: no other module imports it, so the scrimmage
    command and the code that loads into the agent never bring in the MCP library. The
    import-linter contract cannot see this one, as the server is inside its source package."""
    core = PACKAGES / "scrimmage" / "src" / "scrimmage_core"
    found = [
        p.name
        for p in sorted(core.rglob("*.py"))
        if p.name != "mcp_server.py" and imports_the_server(p)
    ]
    assert not found, found
    for line in (  # every way of importing it is seen, so a pass above means something
        "from scrimmage_core import mcp_server",
        "from scrimmage_core import cli, mcp_server as server",
        "import scrimmage_core.mcp_server",
        "from scrimmage_core.mcp_server import main",
        "from . import mcp_server",
        "from .mcp_server import main",
        "importlib.import_module('scrimmage_core.mcp_server')",
    ):
        (tmp_path / "probe.py").write_text(line + "\n")
        assert imports_the_server(tmp_path / "probe.py"), line
    (tmp_path / "probe.py").write_text("from scrimmage_core import mcp_gate\n")
    assert not imports_the_server(tmp_path / "probe.py")
