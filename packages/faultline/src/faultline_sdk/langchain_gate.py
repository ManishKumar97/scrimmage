# pyright: standard
"""The adapter for LangChain tools, which LangGraph runs.

A tool's run calls its _run or _arun inside LangChain's error handling, so the gate sits
at _run and _arun: an injected failure is handled exactly as a real one raised by the
tool's own code. The team writes no tools list. The SDK never imports LangChain: the hook
patches it when the agent imports it.
"""

import contextvars
import functools
import importlib.metadata
import inspect
from collections.abc import Callable
from types import ModuleType
from typing import Any

from faultline_sdk import hooks, tool_gate
from faultline_sdk.state import RunState, current

LIBRARY = "langchain-core"
MODULE = "langchain_core.tools.base"
TESTED = ((1, 7), (1, 7))  # the minor versions this release was tested with
GATED = "__faultline_gate__"
METHODS = ("_run", "_arun")
PLUMBING = ("run_manager", "config", "callbacks")  # LangChain's, not the tool's input

# The tool whose real call is in progress. _arun often delegates to _run on the same tool,
# and that is one call, not two.
_inside: contextvars.ContextVar[object] = contextvars.ContextVar("faultline_inside")


def install(run: RunState, stop: Callable[[str], None]) -> None:
    hooks.on_import(MODULE, lambda module: patch(module, run, stop))


def gatable(module: ModuleType) -> bool:
    """The functions the gate replaces exist where it expects them."""
    base = getattr(module, "BaseTool", None)
    if base is None or not hasattr(module, "ToolException"):
        return False
    return all(n in vars(base) for n in ("__init__", "__init_subclass__", *METHODS))


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
    subclass_hook = vars(base)["__init_subclass__"].__func__

    def init_subclass(cls: type, **kwargs: Any) -> None:
        subclass_hook(cls, **kwargs)
        gate_class(cls, failure)

    base.__init_subclass__ = classmethod(init_subclass)
    create = base.__init__

    @functools.wraps(create)
    def tracked_init(self: Any, *args: Any, **kwargs: Any) -> None:
        create(self, *args, **kwargs)
        run.seen.add(self.name)  # the model may be offered it, and the gate will see it

    base.__init__ = tracked_init
    for cls in subclasses(base):  # defined before the hook ran
        gate_class(cls, failure)


def subclasses(cls: type) -> list[type]:
    found: list[type] = []
    for sub in cls.__subclasses__():
        found += [sub, *subclasses(sub)]
    return found


def gate_class(cls: type, failure: type[Exception]) -> None:
    for name in METHODS:
        method = vars(cls).get(name)
        if inspect.isfunction(method) and not getattr(method, GATED, False):
            setattr(cls, name, gate(method, failure))


def arguments(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, object]:
    found: dict[str, object] = {k: v for k, v in kwargs.items() if k not in PLUMBING}
    if args:
        found["args"] = list(args)
    return found


def function_of(tool: Any, method: Callable[..., Any]) -> Callable[..., Any]:
    """The team's own function, whose return annotation a mock is built from."""
    return getattr(tool, "func", None) or getattr(tool, "coroutine", None) or method


def gate(method: Callable[..., Any], failure: type[Exception]) -> Callable[..., Any]:
    """A function of the same kind as _run or _arun, sync or async, that follows the plan."""
    if inspect.iscoroutinefunction(method):

        @functools.wraps(method)
        async def async_gate(self: Any, *args: Any, **kwargs: Any) -> Any:
            run = current()
            if run is None or _inside.get(None) is self:
                return await method(self, *args, **kwargs)
            step = run.begin(self.name, arguments(args, kwargs))
            if step.decision.action != "real":
                return tool_gate.apply(run, step, function_of(self, method), failure)
            token = _inside.set(self)
            try:
                result = await method(self, *args, **kwargs)
            except BaseException as exc:
                run.finish(step, error=exc)
                raise
            finally:
                _inside.reset(token)
            run.finish(step, result=result)
            return result

        setattr(async_gate, GATED, True)
        return async_gate

    @functools.wraps(method)
    def sync_gate(self: Any, *args: Any, **kwargs: Any) -> Any:
        run = current()
        if run is None or _inside.get(None) is self:
            return method(self, *args, **kwargs)
        step = run.begin(self.name, arguments(args, kwargs))
        if step.decision.action != "real":
            return tool_gate.apply(run, step, function_of(self, method), failure)
        token = _inside.set(self)
        try:
            result = method(self, *args, **kwargs)
        except BaseException as exc:
            run.finish(step, error=exc)
            raise
        finally:
            _inside.reset(token)
        run.finish(step, result=result)
        return result

    setattr(sync_gate, GATED, True)
    return sync_gate
