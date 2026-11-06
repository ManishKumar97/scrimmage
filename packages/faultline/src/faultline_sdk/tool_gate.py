"""The gate for a hand-written loop's tools, named by import path under [agent] tools.

The swap is made lazily: an import hook waits for the agent to import the tool's
module, then swaps each named function in it before the agent's code can look it up.
"""

import builtins
import functools
import importlib.machinery
import inspect
import sys
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from types import ModuleType
from typing import Any

from faultline_sdk import hooks, mocks
from faultline_sdk.config import Fault
from faultline_sdk.errors import FaultlineMockError
from faultline_sdk.state import RunState, Step, current

GATED = "__faultline_gate__"


def mark(function: Callable[..., Any]) -> Callable[..., Any]:
    """Name a function as a gate. The flag holds the function itself, as functools.wraps
    copies attributes: a wrapper around a gate gets the flag but does not pass is_gate."""
    setattr(function, GATED, function)
    return function


def is_gate(function: object) -> bool:
    return getattr(function, GATED, None) is function


def _arguments(
    tool: Callable[..., object], args: tuple[Any, ...], kwargs: dict[str, Any]
) -> dict[str, object]:
    try:
        return dict(inspect.signature(tool).bind_partial(*args, **kwargs).arguments)
    except TypeError:
        return {"args": list(args), "kwargs": kwargs}


def injected(state: RunState, fault: Fault, name: str, failure: type[Exception]) -> Exception:
    """The error a fault raises. failure is the type the caller treats as a tool failure."""
    if fault.do == "timeout":
        return TimeoutError(f"{name} timed out")
    if fault.raises is None:
        return failure(f"{name} failed")
    if ":" in fault.raises:
        kind = mocks.import_object(fault.raises, state.allowed)
    else:
        kind = getattr(builtins, fault.raises)
    if not (isinstance(kind, type) and issubclass(kind, Exception)):
        raise TypeError(f"{kind!r} is not an exception class")
    return kind(f"{name} failed")


@dataclass(frozen=True)
class Gated:
    """What a gate needs to know about one call."""

    name: str  # the name the model sees
    arguments: dict[str, object]
    tool: Callable[..., object]  # the team's function, whose return type a mock is built from
    failure: type[Exception] = RuntimeError  # what the caller treats as a tool failure
    during: Callable[[], AbstractContextManager[object]] = nullcontext  # around the real call
    shape: Callable[[object], object] = lambda value: value  # a mock, as the caller expects it


Describe = Callable[[tuple[Any, ...], dict[str, Any]], Gated | None]


def apply(state: RunState, step: Step, gated: Gated) -> object:
    """Carry out a decision that does not run the real tool."""
    decision = step.decision
    if decision.action == "refused":
        error = state.refusal(step)
        # The message names the two fixes, so it belongs in the run table.
        mistake = str(error) if decision.reason == "unlisted" else None
        state.finish(step, error=error, mistake=mistake)
        raise error
    if decision.action == "fault":
        assert decision.fault is not None
        fault = state.plan.faults[decision.fault]
        try:
            error = injected(state, fault, step.call.name, gated.failure)
        except Exception as exc:  # a bad raises is a mistake in the test, not the agent's failure
            problem = FaultlineMockError(f"the fault cannot raise {fault.raises}: {exc}")
            state.finish(step, error=problem, mistake=str(problem))
            raise problem from exc
        state.finish(step, error=error)
        raise error
    assert decision.mock is not None
    mock = state.plan.mocks[decision.mock]
    try:
        value = gated.shape(mocks.build(mock.returns, mock.type, gated.tool, state.allowed))
    except FaultlineMockError as exc:
        state.finish(step, error=exc, mistake=str(exc))
        raise
    state.finish(step, result=value)
    return value


def gate_with(tool: Callable[..., Any], describe: Describe) -> Callable[..., Any]:
    """A function of the same kind as the tool, sync or async, that follows the plan.

    describe says what this call is; None lets it straight through.
    """
    if inspect.iscoroutinefunction(tool):

        @functools.wraps(tool)
        async def async_gate(*args: Any, **kwargs: Any) -> Any:
            state = current()
            gated = None if state is None else describe(args, kwargs)
            if state is None or gated is None:
                return await tool(*args, **kwargs)
            step = state.begin(gated.name, gated.arguments)
            if step.decision.action != "real":
                return apply(state, step, gated)
            try:
                with gated.during():
                    result = await tool(*args, **kwargs)
            except BaseException as exc:
                state.finish(step, error=exc)
                raise
            state.finish(step, result=result)
            return result

        return mark(async_gate)

    @functools.wraps(tool)
    def sync_gate(*args: Any, **kwargs: Any) -> Any:
        state = current()
        gated = None if state is None else describe(args, kwargs)
        if state is None or gated is None:
            return tool(*args, **kwargs)
        step = state.begin(gated.name, gated.arguments)
        if step.decision.action != "real":
            return apply(state, step, gated)
        try:
            with gated.during():
                result = tool(*args, **kwargs)
        except BaseException as exc:
            state.finish(step, error=exc)
            raise
        state.finish(step, result=result)
        return result

    return mark(sync_gate)


def gate(tool: Callable[..., Any], name: str) -> Callable[..., Any]:
    """The gate for a plain function, by the name the model sees."""
    return gate_with(tool, lambda args, kwargs: Gated(name, _arguments(tool, args, kwargs), tool))


class ListedToolMissing(ImportError):
    """A tool under [agent] tools is not in its module. The import fails on purpose."""


def swap(module: ModuleType, names: list[str], renamed: dict[str, str] | None = None) -> None:
    """Swap each named function; its label uses the name the model sees."""
    for name in names:
        tool = getattr(module, name, None)
        seen = (renamed or {}).get(f"{module.__name__}:{name}", name)
        if hasattr(tool, "invoke") and hasattr(tool, "_run"):
            run = current()
            if run is not None:  # a framework's tool object: only its adapter can vouch for it
                run.listed.discard(seen)
            continue
        if not callable(tool):
            message = (
                f"{module.__name__}:{name} is listed under [agent] tools, but "
                f"{module.__name__} has no function {name}"
            )
            run = current()
            if run is not None:
                run.cannot_gate(name, message)
            raise ListedToolMissing(message)
        if not is_gate(tool):
            setattr(module, name, gate(tool, seen))


def module_exists(name: str, extra: list[str]) -> bool:
    """Whether a module can be found, without importing it or its packages."""
    path: list[str] | None = [*sys.path, *extra]
    for part in name.split("."):
        spec = importlib.machinery.PathFinder.find_spec(part, path)
        if spec is None:
            return False
        path = list(spec.submodule_search_locations or [])
    return True


def targets(paths: Sequence[str]) -> dict[str, list[str]]:
    """{"package.tools": ["name", ...]} from ["package.tools:name", ...]."""
    out: dict[str, list[str]] = {}
    for path in paths:
        module, _, name = path.partition(":")
        out.setdefault(module, []).append(name)
    return out


def install(paths: Sequence[str], renamed: dict[str, str] | None = None) -> None:
    names_seen = renamed or {}
    for module, names in targets(paths).items():
        hooks.on_import(module, functools.partial(swap, names=names, renamed=names_seen))
