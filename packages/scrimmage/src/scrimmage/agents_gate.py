# pyright: standard, reportPrivateUsage=true
"""The adapter for the OpenAI Agents SDK's function tools and handoffs.

`@function_tool` captures the team's function when it decorates it, so a swap of the module
attribute never sees a call. Every run reaches a function tool through the SDK's own invoker,
which runs the tool inside the tool's failure-error function. The gate sits there: an
injected failure reaches the model as a real failure of that tool does, by the SDK's default
text or the team's own function. The team writes no tools list. The SDK never imports the
Agents SDK: the hook patches it when the agent imports it.

A handoff runs no I/O, so it is seen and recorded and never refused or faulted.
"""

import dataclasses
import functools
import inspect
import json
import weakref
from collections.abc import Callable
from types import ModuleType
from typing import Any

from scrimmage import hooks, tool_gate
from scrimmage.decide import Call
from scrimmage.errors import ScrimmageRefused
from scrimmage.http_gate import params
from scrimmage.state import ProcessState, RunState

LIBRARY = "openai-agents"
TOOL_MODULE = "agents.tool"
HANDOFF_MODULE = "agents.handoffs"
TESTED = ((0, 20), (0, 26))  # the minor versions this release was tested with
INVOKER = "_FailureHandlingFunctionToolInvoker"
# The team's function, kept on the SDK's invoker implementation: a mock is typed from it.
WRAPPED = "__agents_function_tool_wrapped_callable__"
RAW = "__scrimmage_raw_invoker__"  # on a gate: the SDK's own implementation it wraps
MCP = "mcp"  # the value of agents.tool.ToolOriginType for a tool made from an MCP server's tool


def install(process: ProcessState, stop: Callable[[str], None]) -> None:
    hooks.on_import(TOOL_MODULE, lambda module: patch_tools(module, process, stop))
    hooks.on_import(HANDOFF_MODULE, lambda module: patch_handoffs(module, process, stop))


def missing_in_tools(module: ModuleType) -> str | None:
    """The first thing the gate relies on that this module lacks, or None."""
    invoker = getattr(module, INVOKER, None)
    tool = getattr(module, "FunctionTool", None)
    if not isinstance(invoker, type):
        return INVOKER
    if not isinstance(tool, type) or "on_invoke_tool" not in field_names(tool):
        return "FunctionTool.on_invoke_tool"
    init = (params(vars(invoker).get("__init__")) or [])[:3]
    if init != ["self", "invoke_tool_impl", "on_handled_error"]:
        return f"{INVOKER}.__init__(invoke_tool_impl, on_handled_error)"
    call = vars(invoker).get("__call__")
    # The failure handling must still call the attribute the gate stands in for.
    if "_invoke_tool_impl" not in getattr(getattr(call, "__code__", None), "co_names", ()):
        return f"{INVOKER}._invoke_tool_impl"
    if params(vars(tool).get("__post_init__")) != ["self"]:
        return "FunctionTool.__post_init__"
    return None


def missing_in_handoffs(module: ModuleType) -> str | None:
    handoff = getattr(module, "Handoff", None)
    if not isinstance(handoff, type):
        return "Handoff"
    if "tool_name" not in field_names(handoff):
        return "Handoff.tool_name"
    if "on_invoke_handoff" not in field_names(handoff):
        return "Handoff.on_invoke_handoff"
    return None


def field_names(cls: type) -> set[str]:
    return {f.name for f in dataclasses.fields(cls)} if dataclasses.is_dataclass(cls) else set()


def version() -> tuple[int, int] | None:
    try:
        # Not at import: the SDK starts without it. A team's stray module of the same name
        # as one this pulls in can raise, and that must not crash the run.
        import importlib.metadata

        parts = importlib.metadata.version(LIBRARY).split(".")
        return int(parts[0]), int(parts[1])
    except Exception:
        return None


def broken(process: ProcessState, stop: Callable[[str], None], missing: str) -> None:
    (oldest, newest) = TESTED
    message = (
        f"{LIBRARY} {found_version()} does not have {missing}, which Scrimmage gates (it was "
        f"tested with {oldest[0]}.{oldest[1]} to {newest[0]}.{newest[1]}), so its tools cannot "
        "be checked, and the program is stopped"
    )
    process.structure(LIBRARY, message, stop)


def found_version() -> str:
    found = version()
    return "?" if found is None else f"{found[0]}.{found[1]}"


def patch_tools(
    module: ModuleType, owner: ProcessState | RunState, stop: Callable[[str], None]
) -> None:
    """owner: the process, or a run's own process; the adapter keeps no run."""
    process = owner.process if isinstance(owner, RunState) else owner
    if (missing := missing_in_tools(module)) is not None:
        broken(process, stop, missing)
        return
    process.newer_version(LIBRARY, version(), TESTED)
    invoker = getattr(module, INVOKER)
    tool_class = module.FunctionTool
    if getattr(invoker, RAW, False):
        return
    made = invoker.__init__
    created = tool_class.__post_init__
    # By id: a dataclass with eq has no hash, so a WeakSet cannot hold one.
    tools: weakref.WeakValueDictionary[int, Any] = weakref.WeakValueDictionary()

    @functools.wraps(made)
    def gated_init(self: Any, invoke_tool_impl: Any, *args: Any, **kwargs: Any) -> None:
        # The SDK rebinds an invoker to a copied tool with the same implementation: gate its
        # own, so each gate reads the tool of its own invoker.
        raw = getattr(invoke_tool_impl, RAW, invoke_tool_impl)
        made(self, gate_impl(raw, lambda: self._function_tool), *args, **kwargs)

    @functools.wraps(created)
    def tracked_post_init(self: Any) -> None:
        created(self)
        with process.lock:
            tools[id(self)] = self
        vet(self)

    def vet(tool: Any) -> str | None:
        """Record whether a call to this tool reaches the gate. Says why not, if it does not."""
        reason = skips_gate(tool, invoker)
        if reason is None:
            process.adapted.add(tool.name)  # offered to the model, it will reach the gate
        else:
            process.blocked[tool.name] = reason
        return reason

    def recheck() -> None:
        with process.lock:
            live = list(tools.values())
        for tool in live:
            vet(tool)

    process.rechecks.append(recheck)
    setattr(invoker, RAW, True)
    invoker.__init__ = gated_init
    tool_class.__post_init__ = tracked_post_init


def skips_gate(tool: Any, invoker: type) -> str | None:
    """Why a call to this tool could run without passing the gate, if one could."""
    own = tool.on_invoke_tool
    if not isinstance(own, invoker):
        return (
            f"{tool.name} cannot be gated, because its on_invoke_tool is the team's own "
            "function and not the Agents SDK's"
        )
    if not tool_gate.is_gate(getattr(own, "_invoke_tool_impl", None)):
        return f"{tool.name} cannot be gated, because its _invoke_tool_impl was replaced"
    return None


def parsed(text: object) -> dict[str, object]:
    """The call's arguments: the SDK passes the model's JSON text. Text that is no JSON object
    is kept as it came, under a name no mock's args can name."""
    try:
        value = json.loads(text) if isinstance(text, str) and text else {}
    except ValueError:
        return {"<unparsed arguments>": str(text)}
    return value if isinstance(value, dict) else {"<unparsed arguments>": str(text)}


def from_mcp(tool: Any) -> bool:
    """A tool made from an MCP server's tool: the MCP gate decides its call."""
    if getattr(tool, "_mcp_tool_binding", None) is not None:
        return True
    origin = getattr(tool, "_tool_origin", None)
    return str(getattr(getattr(origin, "type", None), "value", "")) == MCP


def awaiting(impl: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(impl)
    async def awaited(*args: Any, **kwargs: Any) -> Any:
        return await impl(*args, **kwargs)

    return awaited


def gate_impl(impl: Callable[..., Any], own: Callable[[], Any]) -> Callable[..., Any]:
    """A gate in place of the invoker's implementation, so it runs inside the failure handling.

    own: the FunctionTool this invoker belongs to, once the SDK has bound it.
    """
    function = getattr(impl, WRAPPED, None) or impl
    original = impl
    if not inspect.iscoroutinefunction(impl):  # a callable object the SDK awaits all the same
        impl = awaiting(impl)

    def describe(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tool_gate.Gated | None:
        context, text = args[0], args[1]
        tool = own()
        if tool is not None and from_mcp(tool):
            return None  # the MCP gate sees the call of the server's tool, which is this one
        name = tool.name if tool is not None else getattr(context, "tool_name", None)
        if not isinstance(name, str) or not name:
            raise ScrimmageRefused("an Agents SDK tool was called with no name, so it is refused")
        return tool_gate.Gated(name, parsed(text), function, RuntimeError)

    gated = tool_gate.gate_with(impl, describe)
    setattr(gated, RAW, original)
    return gated


def patch_handoffs(
    module: ModuleType, owner: ProcessState | RunState, stop: Callable[[str], None]
) -> None:
    process = owner.process if isinstance(owner, RunState) else owner
    if (missing := missing_in_handoffs(module)) is not None:
        broken(process, stop, missing)
        return
    handoff = module.Handoff
    made = handoff.__init__
    if getattr(made, RAW, False):
        return

    @functools.wraps(made)
    def seen_init(self: Any, *args: Any, **kwargs: Any) -> None:
        made(self, *args, **kwargs)
        # A handoff is a function tool to the model: its name is one a gate sees.
        process.adapted.add(self.tool_name)
        self.on_invoke_handoff = recorded(self.on_invoke_handoff, process, self)

    setattr(seen_init, RAW, True)
    handoff.__init__ = seen_init


def recorded(invoke: Callable[..., Any], process: ProcessState, handoff: Any) -> Callable[..., Any]:
    """The handoff's own invoker, leaving a line for each switch. It decides nothing: a
    handoff runs no code of the team's that touches the world, and a refusal would break
    every agent that has one."""
    if getattr(invoke, RAW, False):
        return invoke

    @functools.wraps(invoke)
    async def switch(*args: Any, **kwargs: Any) -> Any:
        run = process.current()
        if run is None:  # code outside any run, or a plain request to a service
            return await invoke(*args, **kwargs)
        name = handoff.tool_name
        step = run.passed(Call("tool", f"handoff:{name}", name))
        try:
            result = await invoke(*args, **kwargs)
        except BaseException as exc:
            run.finish(step, error=exc)
            raise
        run.finish(step)
        return result

    setattr(switch, RAW, True)
    return switch
