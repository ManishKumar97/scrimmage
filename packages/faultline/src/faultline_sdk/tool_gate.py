"""The gate for a hand-written loop's tools, named by import path under [agent] tools.

The swap is made lazily: an import hook waits for the agent to import the tool's
module, then swaps each named function in it before the agent's code can look it up.
"""

import functools
import importlib.abc
import importlib.machinery
import inspect
import sys
from collections.abc import Callable, Sequence
from types import ModuleType
from typing import Any

from faultline_sdk import mocks
from faultline_sdk.errors import FaultlineMockError
from faultline_sdk.state import RunState, Step, current

GATED = "__faultline_gate__"


def _arguments(
    tool: Callable[..., object], args: tuple[Any, ...], kwargs: dict[str, Any]
) -> dict[str, object]:
    try:
        return dict(inspect.signature(tool).bind_partial(*args, **kwargs).arguments)
    except TypeError:
        return {"args": list(args), "kwargs": kwargs}


def _apply(state: RunState, step: Step, tool: Callable[..., object]) -> object:
    """Carry out a decision that does not run the real tool."""
    decision = step.decision
    if decision.action == "refused":
        error = state.refusal(step)
        state.finish(step, error=error)
        raise error
    if decision.action == "fault":
        error = TimeoutError(f"{step.call.name} timed out")
        state.finish(step, error=error)
        raise error
    assert decision.mock is not None
    mock = state.plan.mocks[decision.mock]
    try:
        value = mocks.build(mock.returns, mock.type, tool, state.allowed)
    except FaultlineMockError as exc:
        state.finish(step, error=exc, mistake=str(exc))
        raise
    state.finish(step, result=value)
    return value


def gate(tool: Callable[..., Any], name: str) -> Callable[..., Any]:
    """A function of the same kind as the tool, sync or async, that follows the plan."""
    if inspect.iscoroutinefunction(tool):

        @functools.wraps(tool)
        async def async_gate(*args: Any, **kwargs: Any) -> Any:
            state = current()
            if state is None:
                return await tool(*args, **kwargs)
            step = state.begin(name, _arguments(tool, args, kwargs))
            if step.decision.action != "real":
                return _apply(state, step, tool)
            try:
                result = await tool(*args, **kwargs)
            except BaseException as exc:
                state.finish(step, error=exc)
                raise
            state.finish(step, result=result)
            return result

        setattr(async_gate, GATED, True)
        return async_gate

    @functools.wraps(tool)
    def sync_gate(*args: Any, **kwargs: Any) -> Any:
        state = current()
        if state is None:
            return tool(*args, **kwargs)
        step = state.begin(name, _arguments(tool, args, kwargs))
        if step.decision.action != "real":
            return _apply(state, step, tool)
        try:
            result = tool(*args, **kwargs)
        except BaseException as exc:
            state.finish(step, error=exc)
            raise
        state.finish(step, result=result)
        return result

    setattr(sync_gate, GATED, True)
    return sync_gate


def swap(module: ModuleType, names: list[str]) -> None:
    for name in names:
        tool = getattr(module, name, None)
        if not callable(tool):
            print(f"faultline: {module.__name__} has no function {name}", file=sys.stderr)
            continue
        if not getattr(tool, GATED, False):
            setattr(module, name, gate(tool, name))


def targets(paths: Sequence[str]) -> dict[str, list[str]]:
    """{"acme.tools": ["get_order", ...]} from ["acme.tools:get_order", ...]."""
    out: dict[str, list[str]] = {}
    for path in paths:
        module, _, name = path.partition(":")
        out.setdefault(module, []).append(name)
    return out


class _SwappingLoader(importlib.abc.Loader):
    """Runs the real loader, then swaps the named tools in the new module."""

    def __init__(self, loader: importlib.abc.Loader, names: list[str]) -> None:
        self.loader = loader
        self.names = names

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> ModuleType | None:
        return self.loader.create_module(spec)

    def exec_module(self, module: ModuleType) -> None:
        self.loader.exec_module(module)
        swap(module, self.names)

    def __getattr__(self, attr: str) -> object:
        return getattr(self.loader, attr)


class ToolFinder(importlib.abc.MetaPathFinder):
    def __init__(self, wanted: dict[str, list[str]]) -> None:
        self.wanted = wanted

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None,
        target: ModuleType | None = None,
    ) -> importlib.machinery.ModuleSpec | None:
        names = self.wanted.get(fullname)
        if names is None:
            return None
        for finder in sys.meta_path:
            if finder is self or not hasattr(finder, "find_spec"):
                continue
            spec = finder.find_spec(fullname, path, target)
            if spec is not None:
                if spec.loader is not None:
                    spec.loader = _SwappingLoader(spec.loader, names)
                return spec
        return None


def install(paths: Sequence[str]) -> None:
    wanted = targets(paths)
    for module, names in wanted.items():
        if module in sys.modules:  # already imported, such as by another sitecustomize
            swap(sys.modules[module], names)
    sys.meta_path.insert(0, ToolFinder(wanted))
