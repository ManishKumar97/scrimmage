"""Every TESTED range in the product, found by a scan, so a new gate joins with no edit here.

A gate module says which minor versions of a library it was tested with in a module-level
TESTED. Two shapes exist: a dict of library to (oldest, newest), or one (oldest, newest)
for the module's own LIBRARY. A module with the second shape and no LIBRARY is named for
itself, with `_gate` taken off.
"""

import importlib
import re
from pathlib import Path

import scrimmage

SOURCE = Path(scrimmage.__file__).parent

Range = tuple[tuple[int, int], tuple[int, int]]


def modules_with_tested() -> list[str]:
    """The modules of the package that set TESTED at their top level, found in the source."""
    return sorted(
        path.stem
        for path in SOURCE.glob("*.py")
        if re.search(r"^TESTED\b", path.read_text(), re.MULTILINE)
    )


def ranges() -> dict[str, Range]:
    """Library name to the range its gate was tested with, from every gate module."""
    found: dict[str, Range] = {}
    for stem in modules_with_tested():
        module = importlib.import_module(f"scrimmage.{stem}")
        value = module.TESTED
        if isinstance(value, dict):
            found.update(value)
        else:
            found[getattr(module, "LIBRARY", stem.removesuffix("_gate"))] = value
    return found
