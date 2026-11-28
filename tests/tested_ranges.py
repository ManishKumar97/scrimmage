"""Every TESTED range in the product, found by a scan, so a new gate joins with no edit here.

A gate module says which minor versions of its library it was tested with in a module-level
TESTED. Two shapes exist: a dict of PyPI project to (oldest, newest), or one (oldest, newest)
for the module's own LIBRARY, which must name the PyPI project. A module with the second
shape and no LIBRARY fails the scan, so a new gate cannot be named by accident.
"""

import importlib
import re
from pathlib import Path

import scrimmage

SOURCE = Path(scrimmage.__file__).parent

Range = tuple[tuple[int, int], tuple[int, int]]


def modules_with_tested() -> list[str]:
    """The dotted names, under scrimmage, of modules that set TESTED at their top level."""
    return sorted(
        ".".join(path.relative_to(SOURCE).with_suffix("").parts)
        for path in SOURCE.rglob("*.py")
        if re.search(r"^TESTED\b", path.read_text(), re.MULTILINE)
    )


def ranges() -> dict[str, Range]:
    """PyPI project name to the range its gate was tested with, from every gate module."""
    found: dict[str, Range] = {}
    for dotted in modules_with_tested():
        module = importlib.import_module(f"scrimmage.{dotted}")
        value = module.TESTED
        if isinstance(value, dict):
            new = dict(value)
        else:
            library = getattr(module, "LIBRARY", None)
            assert isinstance(library, str), f"{dotted}: TESTED is one range, so set LIBRARY"
            new = {library: value}
        assert not set(new) & set(found), f"{dotted}: a library already has a range"
        found.update(new)
    return found
