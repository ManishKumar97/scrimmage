"""The gate for a hand-written loop's tools, named by import path under [agent] tools.

The swap is made lazily: an import hook waits for the agent to import the tool's
module, then swaps each named function in it before the agent's code can look it up.
"""

import builtins
import functools
import importlib.machinery
import inspect
import sys
import typing
from collections.abc import Callable, Sequence, Set
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from types import ModuleType
from typing import Any, NoReturn

from scrimmage_core import capture, changes, delay, hooks, mocks
from scrimmage_core import decide as engine
from scrimmage_core import state as states
from scrimmage_core.config import Fault, slow_seconds
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


def whole(value: object) -> tuple[object, Callable[[object], object]]:
    """The part of a result a change acts on is the whole of it."""
    return value, lambda new: new


@dataclass(frozen=True)
class Gated:
    """What a gate needs to know about one call."""

    name: str  # the name the model sees
    arguments: dict[str, object]
    tool: Callable[..., object]  # the team's function, whose return type a mock is built from
    failure: type[Exception] = RuntimeError  # what the caller treats as a tool failure
    during: Callable[[], AbstractContextManager[object]] = nullcontext  # around the real call
    shape: Callable[[object], object] = lambda value: value  # a mock, as the caller expects it
    # The part of a result an append or replace acts on, and how to put the changed part
    # back. A tool that returns (content, artifact) changes the content only.
    content: Callable[[object], tuple[object, Callable[[object], object]]] = whole
    # Why an empty fault cannot be carried out on this tool whatever its type, and the fix.
    no_empty: str | None = None


Describe = Callable[[tuple[Any, ...], dict[str, Any]], Gated | None]


def apply(state: RunState, step: Step, gated: Gated) -> object:
    """Carry out a decision that does not run the real tool."""
    decision = step.decision
    if decision.action == "refused":
        # The message names the two fixes, so it belongs in the run table.
        state.fail(step, state.refusal(step), mistake=decision.reason == "unlisted")
    if decision.action == "fault":
        assert decision.fault is not None
        fault = state.plan.faults[decision.fault]
        if fault.do == "empty":
            return emptied(state, step, gated)
        try:
            error = injected(state, fault, step.call.name, gated.failure)
        except Exception as exc:  # a bad raises is a mistake in the test, not the agent's failure
            state.fail(
                step,
                ScrimmageMockError(f"the fault cannot raise {fault.raises}: {exc}"),
                mistake=True,
                cause=exc,
            )
        try:
            state.fail(step, error)
        finally:
            del error  # else it names a traceback that names this frame, and so itself
    value = build_mock(state, step, gated)
    state.finish(step, result=value)
    return value


class NoEmpty:
    """A declared return type with no empty value."""


NO_EMPTY = NoEmpty()
EMPTY: dict[object, Callable[[], object]] = {str: str, list: list, dict: dict, tuple: tuple}


def empty_of(declared: object) -> object:
    """The empty value of a declared return type: "", [], {} or (); None when the type allows
    None or says nothing; NO_EMPTY for any other type, such as a model or a number."""
    found: set[object] = set()
    for member in changes.members(declared):
        if member in (None, changes.NONE, typing.Any, object) or isinstance(member, typing.TypeVar):
            return None
        found.add(typing.get_origin(member) or member)
    if len(found) == 1 and (kind := next(iter(found))) in EMPTY:
        return EMPTY[kind]()
    return NO_EMPTY


def emptied(state: RunState, step: Step, gated: Gated) -> object:
    """empty: the tool's empty value, and the tool never runs. A tool with no empty value is a
    mistake in the test: the call fails at once, and the fault is not counted as fired."""
    why = gated.no_empty
    if why is None:
        declared = changes.declared_return(gated.tool)
        value = NO_EMPTY if isinstance(declared, changes.Unresolved) else empty_of(declared)
        if not isinstance(value, NoEmpty):
            state.finish(step, result=value)
            return value
        if isinstance(declared, changes.Unresolved):
            why = (
                f"declares a return type Scrimmage cannot resolve ({declared.text!r}); mock "
                "it, or import the type at run time"
            )
        else:
            shown = getattr(declared, "__name__", None) or str(declared)
            why = (
                f"is declared to return {shown}, which has no empty value; mock it with the "
                "fields of an empty result, or fault the web request inside it"
            )
    state.unchanged(step, why)
    # Not a fired fault, and not real or mock either: the tool never ran.
    step.decision = engine.Decision("refused", reason=engine.MISTAKE_REFUSAL)
    state.fail(step, ScrimmageMockError(f"{step.call.label} {why}"), mistake=True)


def build_mock(state: RunState, step: Step, gated: Gated) -> object:
    """The mock's value as the caller expects it. A mock that does not fit is a mistake."""
    assert step.decision.mock is not None
    mock = state.plan.mocks[step.decision.mock]
    try:
        return gated.shape(mocks.build(mock.returns, mock.type, gated.tool, state.allowed))
    except ScrimmageMockError as exc:
        state.finish(step, error=exc, mistake=str(exc))
        raise


def carried_end(
    state: RunState, step: Step, gated: Gated, value: object, mistake: str | None = None
) -> object:
    """The end of a call under a carried fault: the value, changed if the fault is a change
    and the value can take it. A value it cannot take goes back as it is, and the line says
    why, so the call is not counted as a fired fault."""
    if step.unchanged is not None:  # the fault could not be carried out at all
        state.finish(step, result=value, mistake=mistake)
        return value
    assert step.decision.fault is not None
    fault = state.plan.faults[step.decision.fault]
    if slow_seconds(fault.do) is not None:
        state.finish(step, result=value)
        return value
    part, put = gated.content(value)
    declared = None if gated.content is not whole else changes.declared_return(gated.tool)
    done = changes.change(part, fault, declared)
    if isinstance(done, changes.Unchangeable):
        state.unchanged(step, done.why)
        state.finish(step, result=value, mistake=done.message(fault, step.call.label))
        return value
    state.changed(step, done)
    state.finish(step, result=value)
    return put(done.value)


def ended(state: RunState, step: Step, gated: Gated, result: object) -> None:
    """Write a real call's line. A recording run keeps what a mock of it needs, or why not."""
    if state.recording:
        step.extra["recorded"] = state.recorded(
            lambda: capture.tool_value(
                result, gated.arguments, gated.tool, gated.shape, state.allowed
            )
        )
    state.finish(step, result=result)


def carry_sync(
    state: RunState,
    step: Step,
    gated: Gated,
    tool: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> object:
    """A call under a carried fault: wait if the fault is slow, then run the tool or build
    the mock as the baseline would, then change the value if the fault is a change."""
    assert step.decision.fault is not None
    seconds = slow_seconds(state.plan.faults[step.decision.fault].do)
    if seconds is not None:
        try:
            delay.wait_sync(state, step, seconds)
        except BaseException as exc:
            state.finish(step, error=exc)
            raise
    if step.decision.under == "mock":
        return carried_end(state, step, gated, build_mock(state, step, gated))
    try:
        with inside(step), gated.during():
            value = tool(*args, **kwargs)
    except BaseException as exc:
        state.finish(step, error=exc)
        raise
    return carried_end(state, step, gated, value)


async def carry_async(
    state: RunState,
    step: Step,
    gated: Gated,
    tool: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> object:
    """carry_sync for an async tool. The wait is on the event loop, in steps, so a deadline
    the caller sets with asyncio.wait_for cancels it and the tool never runs."""
    assert step.decision.fault is not None
    seconds = slow_seconds(state.plan.faults[step.decision.fault].do)
    mistake = None
    sleep = delay.asyncio_sleep() if seconds is not None else None
    if seconds is not None and sleep is None:
        state.unchanged(step, "slow on an async tool needs asyncio")
        mistake = "slow on an async tool needs asyncio, not trio; the call was not slowed"
    elif seconds is not None and sleep is not None:
        try:
            await delay.wait_steps(state, step, seconds, sleep)
        except BaseException as exc:
            state.finish(step, error=exc)
            raise
    if step.decision.under == "mock":
        return carried_end(state, step, gated, build_mock(state, step, gated), mistake)
    try:
        with inside(step), gated.during():
            value = await tool(*args, **kwargs)
    except BaseException as exc:
        state.finish(step, error=exc)
        raise
    return carried_end(state, step, gated, value, mistake)


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
            if step.decision.under is not None:
                return await carry_async(state, step, gated, tool, args, kwargs)
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
        if step.decision.under is not None:
            return carry_sync(state, step, gated, tool, args, kwargs)
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


class Ungated(Exception):
    """In a service with no run: the tool is left as it is, and the process is broken."""


def cannot_gate(name: str, message: str) -> NoReturn:
    """A listed tool no gate can reach. Under a run the run is refused and the import
    fails. A service that imports it with no run stops before it listens; once listening,
    it is marked broken, so no run request is served and plain requests are untouched."""
    run = current()
    process = states.process()
    if run is None and process is not None and process.service:
        process.structure("scrimmage", message, process.stop)
        raise Ungated(message)
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
        try:
            swap_one(module, names, name, renamed, listed)
        except Ungated:
            continue  # a service that listens already: its run requests are refused


def swap_one(
    module: ModuleType,
    names: list[str],
    name: str,
    renamed: dict[str, str] | None,
    listed: str | None,
) -> None:
    class_name, dot, method = name.partition(".")
    tool = getattr(module, class_name, None)
    if listed is not None and tool is None:
        return
    path = f"{listed or module.__name__}:{name}"
    seen = (renamed or {}).get(path, method or name)
    if dot:
        # A method listed under its own name keeps it, even if it is an alias of another.
        mine = {n.partition(".")[2] for n in names if n.partition(".")[0] == class_name}
        swap_method(tool, path, method, seen, mine - {method})
        return
    if hasattr(tool, "invoke") and hasattr(tool, "_run"):
        run = current()
        process = run.process if run is not None else states.process()
        if process is not None:  # a framework's tool object: only its adapter can vouch for it
            process.listed.discard(seen)
        return
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
