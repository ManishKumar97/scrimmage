"""The gate for a hand-written loop's tools, named by import path under [agent] tools.

The swap is made lazily: an import hook waits for the agent to import the tool's
module, then swaps each named function in it before the agent's code can look it up.
"""

import builtins
import functools
import importlib.machinery
import inspect
import sys
from collections.abc import Callable, Sequence, Set
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from types import ModuleType
from typing import Any, NoReturn

from scrimmage_core import capture, hooks, mocks
from scrimmage_core.config import Fault
from scrimmage_core.errors import ScrimmageMockError
from scrimmage_core.state import RunState, Step, current, inside

GATED = "__scrimmage_gate__"


def mark(function: Callable[..., Any]) -> Callable[..., Any]:
    """Name a function as a gate. The flag holds the function itself, as functools.wraps
    copies attributes: a wrapper around a gate gets the flag but does not pass is_gate."""
    setattr(function, GATED, function)
    return function


def is_gate(function: object) -> bool:
    return getattr(function, GATED, None) is function


def _arguments(
    tool: Callable[..., object],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    bound: bool = False,
    built: list[inspect.Signature] | None = None,
) -> dict[str, object]:
    """The call's arguments by name. bound: the first is self or cls, which is never
    recorded: its repr can show an object's state, and it says nothing about the call.
    built: the tool's signature, kept here by its first successful build."""
    try:
        if built:
            signature = built[0]
        else:
            signature = inspect.signature(tool)
            if built is not None:
                built.append(signature)
        arguments = dict(signature.bind_partial(*args, **kwargs).arguments)
    except TypeError:
        return {"args": list(args[1:] if bound else args), "kwargs": kwargs}
    first = next(iter(signature.parameters.values()), None)
    if bound and first is not None and first.name in arguments:
        if first.kind is inspect.Parameter.VAR_POSITIONAL:  # def m(*args): self is args[0]
            arguments[first.name] = tuple(arguments[first.name])[1:]
        else:
            del arguments[first.name]
    return arguments


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
            problem = ScrimmageMockError(f"the fault cannot raise {fault.raises}: {exc}")
            state.finish(step, error=problem, mistake=str(problem))
            raise problem from exc
        state.finish(step, error=error)
        raise error
    assert decision.mock is not None
    mock = state.plan.mocks[decision.mock]
    try:
        value = gated.shape(mocks.build(mock.returns, mock.type, gated.tool, state.allowed))
    except ScrimmageMockError as exc:
        state.finish(step, error=exc, mistake=str(exc))
        raise
    state.finish(step, result=value)
    return value


def ended(state: RunState, step: Step, gated: Gated, result: object) -> None:
    """Write a real call's line. A recording run keeps what a mock of it needs, or why not."""
    if state.recording:
        step.extra["recorded"] = state.recorded(
            lambda: capture.tool_value(
                result, gated.arguments, gated.tool, gated.shape, state.allowed
            )
        )
    state.finish(step, result=result)


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
                with inside(step), gated.during():
                    result = await tool(*args, **kwargs)
            except BaseException as exc:
                state.finish(step, error=exc)
                raise
            ended(state, step, gated, result)
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
            with inside(step), gated.during():
                result = tool(*args, **kwargs)
        except BaseException as exc:
            state.finish(step, error=exc)
            raise
        ended(state, step, gated, result)
        return result

    return mark(sync_gate)


def gate(tool: Callable[..., Any], name: str, *, bound: bool = False) -> Callable[..., Any]:
    """The gate for a function, by the name the model sees. bound: a method's function,
    whose first argument is self or cls."""

    built: list[inspect.Signature] = []  # building a signature costs more than the call's gate

    def describe(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Gated:
        return Gated(name, _arguments(tool, args, kwargs, bound=bound, built=built), tool)

    return gate_with(tool, describe)


class ListedToolMissing(ImportError):
    """A tool under [agent] tools is not in its module. The import fails on purpose."""


def cannot_gate(name: str, message: str) -> NoReturn:
    run = current()
    if run is not None:
        run.cannot_gate(name, message)
    raise ListedToolMissing(message)


def swap(
    module: ModuleType,
    names: list[str],
    renamed: dict[str, str] | None = None,
    listed: str | None = None,
) -> None:
    """Swap each named function or method; its label uses the name the model sees.

    listed is the name the tools list gives this file, when it was imported by another.
    """
    for name in names:
        class_name, dot, method = name.partition(".")
        tool = getattr(module, class_name, None)
        if listed is not None and tool is None:
            continue
        path = f"{listed or module.__name__}:{name}"
        seen = (renamed or {}).get(path, method or name)
        if dot:
            # A method listed under its own name keeps it, even if it is an alias of another.
            mine = {n.partition(".")[2] for n in names if n.partition(".")[0] == class_name}
            swap_method(tool, path, method, seen, mine - {method})
            continue
        if hasattr(tool, "invoke") and hasattr(tool, "_run"):
            run = current()
            if run is not None:  # a framework's tool object: only its adapter can vouch for it
                run.listed.discard(seen)
            continue
        if isinstance(tool, type):  # calling it makes an object: no tool ran
            cannot_gate(
                seen,
                f"{path} is listed under [agent] tools, but it is a class, so it cannot be "
                f"gated as a tool; list each of its tool methods as {path}.method",
            )
        if not callable(tool):
            cannot_gate(
                name,
                f"{module.__name__}:{name} is listed under [agent] tools, but "
                f"{module.__name__} has no function {name}",
            )
        if not is_gate(tool):
            setattr(module, name, gate(tool, seen))


def swap_method(
    owner: object, path: str, method: str, seen: str, listed: Set[str] = frozenset()
) -> None:
    """Gate a method on its class, so every instance, made before or after, goes through it.

    The function is found in the class's method order, so one from a base class is gated
    on the named class alone. A staticmethod or classmethod keeps its kind.
    """
    where = f"{path} is listed under [agent] tools, but"
    class_path = path.rpartition(".")[0]
    if not isinstance(owner, type):
        what = "its module has no such class" if owner is None else f"{class_path} is not a class"
        cannot_gate(seen, f"{where} {what}, so it cannot be gated")
    found = next((c.__dict__[method] for c in owner.__mro__ if method in c.__dict__), None)
    if found is None:
        cannot_gate(
            seen,
            f"{where} {class_path} has no method {method}; an attribute set in __init__ "
            "cannot be gated when the module loads, so define it as a method on the class",
        )
    static, of_class = isinstance(found, staticmethod), isinstance(found, classmethod)
    function: Any = found.__func__ if static or of_class else found
    if not inspect.isfunction(function):
        what = "a property" if isinstance(found, property) else "not a method"
        what = "a nested class" if isinstance(found, type) else what
        cannot_gate(seen, f"{where} it is {what}, so it cannot be gated as a tool")
    if is_gate(function):
        if method in owner.__dict__:
            return  # this class is gated already
        function = inspect.unwrap(function, stop=lambda f: not is_gate(f))  # a base class's gate
    gated = gate(function, seen, bound=not static)
    if static:
        gated = staticmethod(gated)
    elif of_class:
        gated = classmethod(gated)
    # An alias in a class body, fetch = get, is the same object under another name.
    aliases = {
        name
        for c in owner.__mro__
        for name, value in c.__dict__.items()
        if value is found and name not in listed and inspect.getattr_static(owner, name) is found
    }
    try:
        for name in sorted(aliases | {method}):
            setattr(owner, name, gated)
    except (TypeError, AttributeError) as exc:  # a metaclass that refuses new attributes
        cannot_gate(seen, f"{where} its class does not let Scrimmage set the gate on it: {exc}")


def module_spec(name: str, extra: list[str]) -> importlib.machinery.ModuleSpec | None:
    """Where a module is, without importing it or its packages."""
    path: list[str] | None = [*sys.path, *extra]
    spec = None
    for part in name.split("."):
        spec = importlib.machinery.PathFinder.find_spec(part, path)
        if spec is None:
            return None
        path = list(spec.submodule_search_locations or [])
    return spec


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
        other = functools.partial(swap, names=names, renamed=names_seen, listed=module)
        hooks.on_same_file(module, other)
