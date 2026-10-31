"""The product never loads the lab, and loads a third-party package in one place only.

Product code may import only the standard library. The one exception is the model proxy,
which needs an async HTTP server and client: aiohttp, from the `proxy` extra.
Its tests may also use the test tools in the root dev group, which are never installed
with the product.

The workspace venv has the lab installed, so a stray import would still pass the
other tests. These check what is imported, not what happens to be installed.
"""

import ast
import subprocess
import sys
from pathlib import Path

PACKAGE = Path(__file__).parents[1]
ALLOWED = set(sys.stdlib_module_names) | {"faultline"}
TEST_TOOLS = {"pytest", "opentelemetry"}
# The product's own test helpers in tests/.
TEST_HELPERS = {"fake_jaeger", "spans"}
# The only module that may import a third-party package, and what it may import.
PROXY = Path("src/faultline/proxy/model.py")
PROXY_PACKAGES = {"aiohttp", "yarl"}  # yarl is the URL type aiohttp takes


def test_importing_the_product_loads_only_the_product_and_the_standard_library():
    # Only what the import adds: the interpreter and the venv load modules at start.
    code = (
        "import sys; before = set(sys.modules); import faultline.runs; "
        "print('\\n'.join(set(sys.modules) - before))"
    )
    loaded = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True
    ).stdout.split()

    outside = sorted(name for name in loaded if name.split(".")[0] not in ALLOWED)

    assert outside == []


def imported_roots(path: Path) -> set[str]:
    roots = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            roots.add(node.module.split(".")[0])
    return roots


def allowed_in(path: Path) -> set[str]:
    return ALLOWED | PROXY_PACKAGES if path == PACKAGE / PROXY else ALLOWED


def test_product_code_imports_only_the_product_and_the_standard_library():
    files = sorted((PACKAGE / "src").rglob("*.py"))

    outside = {
        str(path.relative_to(PACKAGE)): sorted(imported_roots(path) - allowed_in(path))
        for path in files
        if imported_roots(path) - allowed_in(path)
    }

    assert PACKAGE / PROXY in files
    assert outside == {}


def test_only_the_model_proxy_imports_aiohttp():
    files = sorted((PACKAGE / "src").rglob("*.py"))

    importing = [path for path in files if imported_roots(path) & PROXY_PACKAGES]

    assert importing == [PACKAGE / PROXY]


def test_product_tests_import_only_the_product_the_standard_library_and_test_tools():
    files = sorted((PACKAGE / "tests").rglob("*.py"))
    allowed = ALLOWED | TEST_TOOLS | TEST_HELPERS

    outside = {
        str(path.relative_to(PACKAGE)): sorted(imported_roots(path) - allowed)
        for path in files
        if imported_roots(path) - allowed
    }

    assert files
    assert outside == {}
