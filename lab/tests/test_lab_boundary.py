"""The lab never imports the product.

The lab is a customer: its agent writes traces with OpenTelemetry's names, and its tests
check the lab alone. Tests that need both packages live in the root tests/ folder.
"""

import ast
from pathlib import Path

LAB = Path(__file__).parents[1]


def imports_faultline(path: Path) -> bool:
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            modules = [node.module]
        else:
            continue
        if any(module.split(".")[0] == "faultline" for module in modules):
            return True
    return False


def test_no_lab_file_imports_the_product():
    files = sorted(LAB.rglob("*.py"))

    importing = [
        str(path.relative_to(LAB)) for path in files if imports_faultline(path)
    ]

    assert files
    assert importing == []
