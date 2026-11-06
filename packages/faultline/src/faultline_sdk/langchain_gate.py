# pyright: standard
"""The adapter for LangChain tools, which LangGraph runs.

A tool's run calls its _run or _arun inside LangChain's error handling, so the gate sits
at _run and _arun: an injected failure is handled exactly as a real one raised by the
tool's own code. The team writes no tools list. The SDK never imports LangChain: the hook
patches it when the agent imports it.
"""

import contextlib
import contextvars
import functools
import importlib.metadata
import inspect
import typing
from collections.abc import Callable, Iterator
from types import ModuleType
from typing import Any

from faultline_sdk import hooks, tool_gate
from faultline_sdk.errors import FaultlineMockError, FaultlineRefused
from faultline_sdk.httpx2_gate import params
from faultline_sdk.state import RunState, current

LIBRARY = "langchain-core"
MODULE = "langchain_core.tools.base"
TESTED = ((1, 7), (1, 7))  # the minor versions this release was tested with
METHODS = ("_run", "_arun")
ENTRIES = ("run", "arun")
CALLED = ("run", "arun", "invoke", "ainvoke")
# LangChain's own, passed beside the tool's input. Told apart by type, since a tool can
# have an input of the same name or call its config anything.
PLUMBING = ("RunnableConfig", "CallbackManagerForToolRun", "AsyncCallbackManagerForToolRun")

# The tool whose real call is in progress. _arun often delegates to _run on the same tool,
# and that is one call, not two.
_inside: contextvars.ContextVar[object] = contextvars.ContextVar("faultline_inside")


def install(run: RunState, stop: Callable[[str], None]) -> None:
    hooks.on_import(MODULE, lambda module: patch(module, run, stop))


def gatable(module: ModuleType) -> bool:
    """The functions the gate relies on exist and take the arguments it expects."""
    base = getattr(module, "BaseTool", None)
    if base is None or not hasattr(module, "ToolException"):
        return False
    if params(vars(base).get("__init__")) != ["self", "kwargs"]:
        return False
    if any(params(vars(base).get(n)) != ["self", "args", "kwargs"] for n in METHODS):
        return False
    return all((params(vars(base).get(n)) or [])[1:2] == ["tool_input"] for n in ENTRIES)


def version() -> tuple[int, int] | None:
    try:
        parts = importlib.metadata.version(LIBRARY).split(".")
        return int(parts[0]), int(parts[1])
    except (importlib.metadata.PackageNotFoundError, ValueError, IndexError):
        return None


def patch(module: ModuleType, run: RunState, stop: Callable[[str], None]) -> None:
    if not gatable(module):
        message = (
            f"{LIBRARY} does not have the functions Faultline gates, so its tools cannot be "
            "checked, and the program is stopped"
        )
        run.notice("structure", LIBRARY, message)
        stop(message)
        return
    found = version()
    if found is not None and found > TESTED[1]:
        run.notice(
            "newer_version",
            LIBRARY,
            f"{LIBRARY} {found[0]}.{found[1]} is newer than the versions this release was "
            f"tested with ({TESTED[0][0]}.{TESTED[0][1]} to {TESTED[1][0]}.{TESTED[1][1]})",
        )
    base = module.BaseTool
    failure = module.ToolException
    create = base.__init__

    @functools.wraps(create)
    def tracked_init(self: Any, *args: Any, **kwargs: Any) -> None:
        create(self, *args, **kwargs)
        if gate_tool(type(self), failure) and library_runs_it(type(self), base):
            run.adapted.add(self.name)  # offered to the model, it will reach a gate

    base.__init__ = tracked_init
    for name in ("run", "arun"):
        setattr(base, name, checked(vars(base)[name], failure))


def owner_of(cls: type, name: str) -> type:
    return next(c for c in cls.__mro__ if name in vars(c))


def gate_tool(cls: type, failure: type[Exception]) -> bool:
    """Gate what this class really runs, wherever it comes from.

    False if one of them is not a plain function, so no gate can sit there.
    """
    gatable_tool = True
    for name in METHODS:
        owner = owner_of(cls, name)
        method = vars(owner)[name]
        if not inspect.isfunction(method):
            gatable_tool = False
        elif not tool_gate.is_gate(method):
            setattr(owner, name, gate(method, failure))
    return gatable_tool


def library_runs_it(cls: type, base: type) -> bool:
    """Calls go through the library's own entry points, so they reach the gate."""
    package = base.__module__.split(".")[0]
    return all(owner_of(cls, n).__module__.split(".")[0] == package for n in CALLED)


def checked(entry: Callable[..., Any], failure: type[Exception]) -> Callable[..., Any]:
    """Gate a tool again as it is called, for _run assigned after the tool was made.

    This only checks: an injected failure is raised at the gate, inside the error handling.
    """

    def check(tool: Any) -> None:
        run = current()
        if run is not None and not gate_tool(type(tool), failure):
            message = f"{tool.name} cannot be gated, because its _run is not a plain function"
            run.cannot_gate(tool.name, message)
            raise FaultlineRefused(message)

    if inspect.iscoroutinefunction(entry):

        @functools.wraps(entry)
        async def async_checked(self: Any, *args: Any, **kwargs: Any) -> Any:
            check(self)
            return await entry(self, *args, **kwargs)

        return async_checked

    @functools.wraps(entry)
    def sync_checked(self: Any, *args: Any, **kwargs: Any) -> Any:
        check(self)
        return entry(self, *args, **kwargs)

    return sync_checked


def plumbing_names(function: Callable[..., Any]) -> set[str]:
    """Parameters annotated with one of LangChain's own types."""
    try:
        hints = typing.get_type_hints(function)
    except Exception:  # a forward reference that cannot be resolved
        return set()
    return {
        name
        for name, hint in hints.items()
        if any(kind in repr(hint) or getattr(hint, "__name__", "") == kind for kind in PLUMBING)
    }


def arguments(
    args: tuple[Any, ...], kwargs: dict[str, Any], functions: tuple[Callable[..., Any], ...]
) -> dict[str, object]:
    names: set[str] = set().union(*map(plumbing_names, functions))
    found: dict[str, object] = {
        k: v for k, v in kwargs.items() if k not in names and type(v).__name__ not in PLUMBING
    }
    if args:
        found["args"] = list(args)
    return found


def function_of(tool: Any, method: Callable[..., Any]) -> Callable[..., Any]:
    """The team's own function, whose return annotation a mock is built from."""
    return getattr(tool, "func", None) or getattr(tool, "coroutine", None) or method


def mock_shape(tool: Any, value: object) -> object:
    """A tool with an artifact returns a pair, which TOML writes as a list of two."""
    if getattr(tool, "response_format", "content") != "content_and_artifact":
        return value
    if isinstance(value, list) and len(value) == 2:
        return tuple(value)
    raise FaultlineMockError(
        f"the mock for {tool.name} must be a list of two items: its content and its artifact"
    )


@contextlib.contextmanager
def real_call(tool: object) -> Iterator[None]:
    token = _inside.set(tool)
    try:
        yield
    finally:
        _inside.reset(token)


def gate(method: Callable[..., Any], failure: type[Exception]) -> Callable[..., Any]:
    """A gate at _run or _arun, inside LangChain's error handling."""

    def describe(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tool_gate.Gated | None:
        tool, *rest = args
        if _inside.get(None) is tool:  # _arun delegating to _run on the same tool
            return None
        function = function_of(tool, method)
        return tool_gate.Gated(
            tool.name,
            arguments(tuple(rest), kwargs, (method, function)),
            function,
            failure,
            functools.partial(real_call, tool),
            functools.partial(mock_shape, tool),
        )

    return tool_gate.gate_with(method, describe)
