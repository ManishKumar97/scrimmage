"""The product never loads the lab or a third-party package.

The workspace venv has the lab installed, so a stray import would still pass the
other tests. These check what is imported, not what happens to be installed.
"""

import ast
import subprocess
import sys
from pathlib import Path

PACKAGE = Path(__file__).parents[1]
ALLOWED = set(sys.stdlib_module_names) | {"faultline", "pytest"}


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


def test_product_code_and_tests_import_only_the_product_and_the_standard_library():
    files = sorted(PACKAGE.rglob("*.py"))

    outside = {
        str(path.relative_to(PACKAGE)): sorted(imported_roots(path) - ALLOWED)
        for path in files
        if imported_roots(path) - ALLOWED
    }

    assert files
    assert outside == {}
