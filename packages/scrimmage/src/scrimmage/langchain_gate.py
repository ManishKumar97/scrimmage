# pyright: standard, reportPrivateUsage=true
"""The adapter for LangChain tools, which LangGraph runs.

A tool's run calls its _run or _arun inside LangChain's error handling, so the gate sits
at _run and _arun: an injected failure is handled exactly as a real one raised by the
tool's own code. The team writes no tools list. The SDK never imports LangChain: the hook
patches it when the agent imports it.
"""

import ast
import contextlib
import contextvars
import functools
import inspect
import re
import threading
import typing
import weakref
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from types import FunctionType, ModuleType
from typing import Any, cast

from scrimmage import changes, hooks, tool_gate
from scrimmage.errors import ScrimmageMockError, ScrimmageRefused
from scrimmage.http_gate import params
from scrimmage.state import ProcessState, RunState, carried, in_service

LIBRARY = "langchain-core"
MODULE = "langchain_core.tools.base"
TESTED = ((1, 8), (1, 8))  # the minor versions this release was tested with
METHODS = ("_run", "_arun")
ENTRIES = ("run", "arun")
# Overrides that are allowed. The gates sit at _run and _arun. model_post_init runs once as
# a tool is made, and the check reads what it leaves. as_tool only builds another tool, and a
# published tool overrides it. Of 237 published tool classes, these and __init__ were all
# that any of them overrode.
EXEMPT = ("_run", "_arun", "model_post_init", "as_tool")
# LangChain's own, passed beside the tool's input. Told apart by type, since a tool can
# have an input of the same name or call its config anything.
PLUMBING = ("RunnableConfig", "CallbackManagerForToolRun", "AsyncCallbackManagerForToolRun")

# The tool whose real call is in progress. _arun often delegates to _run on the same tool,
# and that is one call, not two.
_inside: contextvars.ContextVar[object] = carried(contextvars.ContextVar("scrimmage_inside"))


def install(process: ProcessState, stop: Callable[[str], None]) -> None:
    hooks.on_import(MODULE, lambda module: patch(module, process, stop))


def gatable(module: ModuleType) -> bool:
    """The functions the gate relies on exist and take the arguments it expects."""
    base = getattr(module, "BaseTool", None)
    if base is None or not hasattr(module, "ToolException"):
        return False
    if params(vars(base).get("__init__")) != ["self", "kwargs"]:
        return False
    if not isinstance(vars(base).get("__init_subclass__"), classmethod):
        return False
    if any(params(vars(base).get(n)) != ["self", "args", "kwargs"] for n in METHODS):
        return False
    return all((params(vars(base).get(n)) or [])[1:2] == ["tool_input"] for n in ENTRIES)


def version() -> tuple[int, int] | None:
    try:
        # Not at import: the SDK starts without it. A team's stray module of the same name
        # as one this pulls in can raise, and that must not crash the run.
        import importlib.metadata

        parts = importlib.metadata.version(LIBRARY).split(".")
        return int(parts[0]), int(parts[1])
    except Exception:
        return None


def patch(module: ModuleType, owner: ProcessState | RunState, stop: Callable[[str], None]) -> None:
    """owner: the process, or a run's own process; the adapter keeps no run."""
    process = owner.process if isinstance(owner, RunState) else owner
    if not gatable(module):
        message = (
            f"{LIBRARY} does not have the functions Scrimmage gates, so its tools cannot be "
            "checked, and the program is stopped"
        )
        process.structure(LIBRARY, message, stop)
        return
    process.newer_version(LIBRARY, version(), TESTED)
    base = module.BaseTool
    failure = module.ToolException
    create = base.__init__
    library = Library(base, entry_points(base), failure, process)
    made = vars(base)["__init_subclass__"].__func__
    # By id: a pydantic model has no hash, so a WeakSet cannot hold one.
    tools: weakref.WeakValueDictionary[int, Any] = weakref.WeakValueDictionary()
    lock = threading.Lock()

    @functools.wraps(create)
    def tracked_init(self: Any, *args: Any, **kwargs: Any) -> None:
        create(self, *args, **kwargs)
        track(self)

    def track(tool: Any) -> None:
        with lock:
            tools[id(tool)] = tool
        vet(tool)

    def tracked_subclass(cls: type, **kwargs: Any) -> None:
        made(cls, **kwargs)
        guard_class(cls, library)  # model_construct makes a tool with no __init__

    def vet(tool: Any) -> str | None:
        """Record whether a call to this tool reaches a gate. Says why not, if it does not."""
        reason = skips_gate(tool, library)
        if reason is None:
            process.adapted.add(tool.name)  # offered to the model, it will reach a gate
        else:
            process.blocked[tool.name] = reason
        return reason

    def recheck() -> None:
        with lock:
            live = list(tools.values())
        for tool in live:
            vet(tool)

    process.rechecks.append(recheck)
    base.__init__ = tracked_init
    if "model_copy" in vars(base):  # a copy has no __init__; older versions lack this method
        copied = vars(base)["model_copy"]

        @functools.wraps(copied)
        def tracked_copy(self: Any, *args: Any, **kwargs: Any) -> Any:
            tool = copied(self, *args, **kwargs)
            track(tool)
            return tool

        base.model_copy = tracked_copy
    base.__init_subclass__ = classmethod(tracked_subclass)
    for name in ENTRIES:
        setattr(base, name, checked(vars(base)[name], process, vet))


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


def package_of(cls: type) -> str:
    return cls.__module__.split(".")[0]


def entry_points(base: type) -> tuple[str, ...]:
    """What a call could go through: every function on the library's tool classes.

    A rule and not a list, so a function a later version adds is covered. __call__ is by
    hand: the library defines none, and a class that adds one makes the tool callable.
    """
    library = [c for c in base.__mro__ if package_of(c) == package_of(base)]
    names = {
        name
        for cls in library
        for name, value in vars(cls).items()
        if inspect.isfunction(value) and not name.startswith("__")
    }
    return ("__call__", *sorted(names - set(EXEMPT)))


@dataclass(frozen=True)
class Library:
    """What the checks need to know about the library and this process."""

    base: type
    entries: tuple[str, ...]
    failure: type[Exception]
    process: ProcessState


def overrides(cls: type, library: Library) -> list[str]:
    """The entry points this class replaces. The library's own all reach the gate."""

    def replaced(name: str) -> bool:
        owner = next((c for c in cls.__mro__ if name in vars(c)), None)
        if owner is None or package_of(owner) == package_of(library.base):
            return False
        value = vars(owner)[name]  # a field's default, still here as a class is made, is neither
        return callable(value) or hasattr(value, "__get__")

    return [name for name in library.entries if replaced(name)]


def override_problem(tool: Any, name: str) -> str:
    return (
        f"{tool.name} cannot be gated, because its class overrides {name}; "
        "move the work into _run or _arun"
    )


def refusal(process: ProcessState, name: str, original: object) -> Callable[..., Any]:
    """Stands in for an override that may never reach a gate, so its code never runs in a run.

    In a service a plain request, or code outside any request, gets the override itself:
    Scrimmage changes nothing outside a test's runs."""

    def refuse(tool: Any, *args: Any, **kwargs: Any) -> Any:
        run = process.current()
        if run is None and in_service():
            getter = getattr(original, "__get__", None)
            own = getter(tool, type(tool)) if getter is not None else original
            return cast("Callable[..., Any]", own)(*args, **kwargs)
        message = override_problem(tool, name)
        if run is not None:
            run.cannot_gate(tool.name, message)
        raise ScrimmageRefused(message)

    return tool_gate.mark(refuse)


def guard_class(cls: type, library: Library) -> tuple[bool, list[str]]:
    """Gate what this class runs and refuse what it replaces, before any other check.

    Says whether the gates could go in, and which entry points the class replaces.
    """
    gated = gate_tool(cls, library.failure)
    replaced = overrides(cls, library)
    for name in replaced:
        # On this class, not the owner: the owner can be a mixin other classes use too.
        if not tool_gate.is_gate(getattr(cls, name)):
            original = inspect.getattr_static(cls, name)
            setattr(cls, name, refusal(library.process, name, original))
    return gated, replaced


def skips_gate(tool: Any, library: Library) -> str | None:
    """Why a call to this tool could run without passing a gate, if one could."""
    gated, replaced = guard_class(type(tool), library)
    if not gated:
        return f"{tool.name} cannot be gated, because its _run is not a plain function"
    own = [n for n in (*METHODS, *library.entries) if callable(vars(tool).get(n))]
    if own:  # the class's gate never sees a method set on the tool itself
        return f"{tool.name} cannot be gated, because {own[0]} is set on the tool itself"
    if replaced:
        return override_problem(tool, replaced[0])
    return None


def checked(
    entry: Callable[..., Any], process: ProcessState, vet: Callable[[Any], str | None]
) -> Callable[..., Any]:
    """Gate a tool again as it is called, for _run assigned after the tool was made.

    This only checks: an injected failure is raised at the gate, inside the error handling.
    In a service, a call with no run is only noted: nothing outside a test's runs is refused.
    """

    def check(tool: Any) -> None:
        reason = vet(tool)
        if reason is None:
            return
        run = process.current()
        if run is None and in_service():
            return
        if run is not None:
            run.cannot_gate(tool.name, reason)
        raise ScrimmageRefused(reason)

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


def owning_class(qualname: str) -> str:
    """The nearest class around a function, read from its __qualname__; "" when none."""
    parts = qualname.split(".")[:-1]  # the last part is the function's own name
    for index in range(len(parts) - 1, -1, -1):
        after = parts[index + 1] if index + 1 < len(parts) else ""
        if parts[index] != "<locals>" and after != "<locals>":
            return parts[index]
    return ""


def mangled(name: str, owner: str) -> str:
    """The name the compiler gives a parameter such as __sku inside a class."""
    cls = owner.lstrip("_")
    if cls and name.startswith("__") and not name.endswith("__"):
        return f"_{cls}{name}"
    return name


def type_words(hint: object) -> list[str]:
    """The names in a resolved type, leaving out the values inside a Literal."""
    if typing.get_origin(hint) is typing.Literal:
        return []
    args = typing.get_args(hint)
    if not args:
        return re.findall(r"[A-Za-z_]\w*", repr(hint))
    words = re.findall(r"[A-Za-z_]\w*", repr(typing.get_origin(hint)))
    for arg in args:
        words += [w for a in (arg if isinstance(arg, list) else [arg]) for w in type_words(a)]
    return words


def written_in_source(function: object) -> dict[str, str]:
    """Each parameter's annotation as written in the function's source.

    None when there is no readable source, or when the source no longer matches the code that ran.
    """
    try:
        source = inspect.getsource(function)  # pyright: ignore
        indented = source[:1].isspace()
        tree = ast.parse(("if 1:\n" if indented else "") + source)
    except (OSError, TypeError, SyntaxError):
        return {}
    body = tree.body
    if indented and body and isinstance(body[0], ast.If):
        body = body[0].body
    node = body[0] if body else None
    if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        return {}
    found = node.args
    every = [*found.posonlyargs, *found.args, *found.kwonlyargs]
    real = changes.unwrapped(function)
    code = getattr(real, "__code__", None)
    owner = owning_class(getattr(real, "__qualname__", ""))
    if code is None or [mangled(a.arg, owner) for a in every] != list(
        code.co_varnames[: code.co_argcount + code.co_kwonlyargcount]
    ):  # the file changed after it was imported
        return {}
    return {a.arg: ast.unparse(a.annotation) for a in every if a.annotation is not None}


def plumbing_names(function: Callable[..., Any]) -> set[str]:
    """Parameters annotated with one of LangChain's own types.

    Each annotation is resolved alone, so a type that exists only under TYPE_CHECKING hides
    nothing else. One that cannot be resolved is matched by its written text.
    """
    target = changes.annotated(function)
    try:
        written = changes.annotations_of(target)
    except Exception:  # on 3.14 one annotation that raises, such as 1/0, hides the rest
        return {
            name
            for name, text in written_in_source(target).items()
            if any(word in PLUMBING for word in re.findall(r"[A-Za-z_]\w*", text))
        }
    space = cast("dict[str, object]", getattr(changes.unwrapped(target), "__globals__", None) or {})
    names: set[str] = set()
    for name, annotation in written.items():
        if name == "return":
            continue
        stand_in = FunctionType(changes.borrowed.__code__, space)
        stand_in.__annotations__ = {name: annotation}
        try:
            hint = typing.get_type_hints(stand_in)[name]
            found = getattr(hint, "__name__", "") in PLUMBING or any(
                word in PLUMBING for word in type_words(hint)
            )
        except Exception:  # a name it cannot find, or text that is no type
            text = getattr(annotation, "__forward_arg__", None) or str(annotation)
            found = any(word in PLUMBING for word in re.findall(r"[A-Za-z_]\w*", text))
        if found:
            names.add(name)
    return names


def arguments(
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    functions: tuple[Callable[..., Any], ...],
    injected: frozenset[str],
) -> dict[str, object]:
    names: set[str] = set(injected).union(*map(plumbing_names, functions))
    found: dict[str, object] = {
        k: v for k, v in kwargs.items() if k not in names and type(v).__name__ not in PLUMBING
    }
    if args and not injected:  # a positional value has no name, so it could be an injected one
        found["args"] = list(args)
    return found


def withheld(tool: Any) -> frozenset[str]:
    """Arguments LangChain fills in itself and never shows the model.

    Its own list reads only signatures, so one declared on an args_schema is found by
    comparing that schema with what the model gets. A dict schema has no such fields.
    """
    names = set[str](getattr(tool, "_injected_args_keys", ()))
    try:
        offered = getattr(getattr(tool, "tool_call_schema", None), "model_fields", None)
        if offered is not None:
            names |= set(tool.get_input_schema().model_fields) - set(offered)
    except Exception:  # a parameter type that exists only for the type checker: no schema
        pass
    return frozenset(names)


def function_of(tool: Any, method: Callable[..., Any]) -> Callable[..., Any]:
    """The team's own function, whose return annotation a mock is built from."""
    return getattr(tool, "func", None) or getattr(tool, "coroutine", None) or method


def mock_shape(tool: Any, value: object) -> object:
    """A tool with an artifact returns a pair, which TOML writes as a list of two."""
    if getattr(tool, "response_format", "content") != "content_and_artifact":
        return value
    if isinstance(value, list) and len(value) == 2:
        return tuple(value)
    raise ScrimmageMockError(
        f"the mock for {tool.name} must be a list of two items: its content and its artifact"
    )


NO_EMPTY_PAIR = (
    "is a LangChain tool with response_format content_and_artifact, so empty cannot tell "
    'its empty result; mock it with an empty pair, such as returns = ["", {}]'
)


def artifact_content(value: object) -> tuple[object, Callable[[object], object]]:
    """A tool with an artifact returns (content, artifact). Only the content goes to the
    model, so it is all that an append or replace changes; the artifact is put back as it was."""
    if isinstance(value, tuple) and len(cast("tuple[object, ...]", value)) == 2:
        pair = cast("tuple[object, object]", value)
        return pair[0], lambda new: (new, pair[1])
    return tool_gate.whole(value)


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
        paired = getattr(tool, "response_format", "content") == "content_and_artifact"
        return tool_gate.Gated(
            tool.name,
            arguments(tuple(rest), kwargs, (method, function), withheld(tool)),
            function,
            failure,
            functools.partial(real_call, tool),
            functools.partial(mock_shape, tool),
            artifact_content if paired else tool_gate.whole,
            NO_EMPTY_PAIR if paired else None,
        )

    return tool_gate.gate_with(method, describe)
