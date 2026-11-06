"""Run a patch at the moment the agent first imports a module, never before.

The SDK never imports a library itself. It registers one finder with Python's import
system; when the agent imports a module it waits for, the finder lets the real loader
run the module, then hands the new module to the patch.
"""

import importlib.abc
import importlib.machinery
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

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None,
        target: ModuleType | None = None,
    ) -> importlib.machinery.ModuleSpec | None:
        patches = self.wanted.get(fullname)
        if patches is None:
            return None
        for finder in sys.meta_path:
            if finder is self or not hasattr(finder, "find_spec"):
                continue
            spec = finder.find_spec(fullname, path, target)
            if spec is not None:
                if spec.loader is not None:
                    spec.loader = _PatchingLoader(spec.loader, patches)
                return spec
        return None


_hook: ImportHook | None = None


def on_import(module: str, patch: Patch) -> None:
    """Patch the module when it is first imported, or now if it already is."""
    global _hook
    if module in sys.modules:  # already imported, such as by another sitecustomize
        patch(sys.modules[module])
        return
    if _hook is None or _hook not in sys.meta_path:
        _hook = ImportHook()
        sys.meta_path.insert(0, _hook)
    _hook.wanted.setdefault(module, []).append(patch)
