"""Run a patch at the moment the agent first imports a module, never before.

The SDK never imports a library itself. It registers one finder with Python's import
system; when the agent imports a module it waits for, the finder lets the real loader
run the module, then hands the new module to the patch.
"""

import importlib.abc
import importlib.machinery
import os
import sys
from collections.abc import Callable, Sequence
from types import ModuleType

Patch = Callable[[ModuleType], None]


class _PatchingLoader(importlib.abc.Loader):
    """Runs the real loader, then the patches, on the new module."""

    def __init__(self, loader: importlib.abc.Loader, patches: list[Patch]) -> None:
        self.loader = loader
        self.patches = patches

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> ModuleType | None:
        return self.loader.create_module(spec)

    def exec_module(self, module: ModuleType) -> None:
        self.loader.exec_module(module)
        for patch in self.patches:
            patch(module)

    def __getattr__(self, attr: str) -> object:
        return getattr(self.loader, attr)


class ImportHook(importlib.abc.MetaPathFinder):
    def __init__(self) -> None:
        self.wanted: dict[str, list[Patch]] = {}
        # The last part of a name, and for a module with that ending its own name and patch.
        self.same_file: dict[str, list[tuple[str, Patch]]] = {}

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None,
        target: ModuleType | None = None,
    ) -> importlib.machinery.ModuleSpec | None:
        patches = self.wanted.get(fullname, [])
        others = self.same_file.get(fullname.rpartition(".")[2], [])
        if not patches and not others:
            return None
        for finder in sys.meta_path:
            if finder is self or not hasattr(finder, "find_spec"):
                continue
            spec = finder.find_spec(fullname, path, target)
            if spec is not None:
                named = [p for name, p in others if name != fullname and is_file_of(spec, name)]
                if spec.loader is not None and (patches or named):
                    spec.loader = _PatchingLoader(spec.loader, [*patches, *named])
                return spec
        return None


def is_file_of(spec: importlib.machinery.ModuleSpec, module: str) -> bool:
    """Whether a spec's file is the one a module name means, as a path ending."""
    ending = os.sep + module.replace(".", os.sep)
    return str(spec.origin).endswith((f"{ending}.py", f"{ending}{os.sep}__init__.py"))


_hook: ImportHook | None = None


def installed() -> ImportHook:
    global _hook
    if _hook is None or _hook not in sys.meta_path:
        _hook = ImportHook()
        sys.meta_path.insert(0, _hook)
    return _hook


def on_import(module: str, patch: Patch) -> None:
    """Patch the module when it is first imported, or now if it already is."""
    if module in sys.modules:  # already imported, such as by another sitecustomize
        patch(sys.modules[module])
        return
    installed().wanted.setdefault(module, []).append(patch)


def on_same_file(module: str, patch: Patch) -> None:
    """Patch a module that is this one's file imported under another name.

    With a script's folder on the path, deep/tools.py is both deep.tools and tools.
    """
    last = module.rpartition(".")[2]
    installed().same_file.setdefault(last, []).append((module, patch))
