"""What scrimmage init --probe finds in the agent's process, at the first request that offers tools.

It reads the modules the agent already loaded and imports nothing. A module or a class is
only looked at, never called, so this runs none of the team's code. The static read in
init.py skips the same folders.
"""

import inspect
import os
import sys
from pathlib import Path
from types import ModuleType

# Folders that hold libraries or records, not the team's code. Hidden folders are skipped too.
SKIPPED = ("venv", "site-packages", "node_modules", "scrimmage-runs")


def skipped_folder(name: str) -> bool:
    return name in SKIPPED or name.startswith(".")


def here() -> str:
    """This folder, or "" if it has gone, as a child's can: then the probe finds no paths."""
    try:
        return os.path.realpath(os.getcwd())
    except OSError:
        return ""


def paths_of(name: str, root: str) -> list[str]:
    """Every top-level function or top-level class's method of this name in the team's code."""
    found: set[str] = set()
    if not root:
        return []
    # sys.modules can hold anything a program put there, so nothing is assumed about an entry.
    entries: list[tuple[str, object]] = list(sys.modules.items())
    for module_name, module in entries:
        try:
            if isinstance(module, ModuleType) and team_module(module, root):
                found.update(in_module(module, module_name, name))
        except Exception:  # a module that objects to being looked at offers nothing
            continue
    return sorted(found)


def namespace(module: ModuleType) -> dict[str, object]:
    """The module's own names, without going through a lazy module's __getattribute__."""
    return object.__getattribute__(module, "__dict__")


def team_module(module: ModuleType, root: str) -> bool:
    """Whether the module's file is under the folder, and not in a library folder there."""
    source = namespace(module).get("__file__")
    if not isinstance(source, str):
        return False
    real = Path(os.path.realpath(source))
    if not real.is_relative_to(root):
        return False
    return not any(skipped_folder(part) for part in real.relative_to(root).parts[:-1])


def in_module(module: ModuleType, module_name: str, name: str) -> list[str]:
    names = namespace(module)
    paths: list[str] = []
    value = names.get(name)
    # A function imported from another module is found in the module that defines it.
    if inspect.isfunction(value) and value.__module__ == module_name:
        paths.append(f"{module_name}:{name}")
    for class_name, item in names.items():
        if not isinstance(item, type) or item.__module__ != module_name:
            continue
        if item.__qualname__ == class_name and is_method(vars(item).get(name)):
            paths.append(f"{module_name}:{class_name}.{name}")
    return paths


def is_method(value: object) -> bool:
    return inspect.isfunction(value) or isinstance(value, staticmethod | classmethod)
