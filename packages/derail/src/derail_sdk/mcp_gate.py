# pyright: standard
"""The gate for MCP tools, at ClientSession.send_request.

The official client sends every tool call through this one function, on every transport,
and clients built on it, such as FastMCP's, reach it through call_tool. The SDK never
imports the library: the hook patches it when the agent imports it.

Over streamable HTTP each message is also a web request. The transport's own requests
carry a mark, so a message is decided here and never again at the web gate.
"""

import copy
import functools
import importlib.metadata
import inspect
import json
from collections.abc import Callable, Coroutine
from types import ModuleType
from typing import Any, NoReturn

from derail_sdk import hooks
from derail_sdk.decide import Call
from derail_sdk.http_gate import MCP_TRANSPORT, host_of, params, refuse
from derail_sdk.state import BlobSink, RunState, Step, blob, current

SESSION = "mcp.client.session"
TRANSPORT = "mcp.client.streamable_http"
TESTED = ((2, 3), (2, 3))  # the oldest and newest minor versions this release was tested with
GATED = "__derail_gate__"
SEND_ARGUMENTS = [
    "self",
    "request",
    "result_type",
    "request_read_timeout_seconds",
    "metadata",
    "progress_callback",
]
# The protocol's requests that are not tool calls. Anything else is decided as a tool call,
# so a method this release does not know is refused, never passed.
PASSES = (
    "initialize",
    "ping",
    "server/discover",
    "subscriptions/listen",
    "tools/list",
    "prompts/list",
    "prompts/get",
    "resources/list",
    "resources/templates/list",
    "resources/read",
    "resources/subscribe",
    "resources/unsubscribe",
    "completion/complete",
    "logging/setLevel",
)
# Every method of the transport that sends a web request.
WEB_METHODS = (
    "_handle_post_request",
    "_handle_resumption_request",
    "handle_get_stream",
    "terminate_session",
)


class Gate:
    """What this process has learned about its MCP servers."""

    def __init__(self, run: RunState, sender: BlobSink | None, stop: Callable[[str], None]) -> None:
        self.run = run
        self.sender = sender
        self.stop = stop  # ends the program: a library that cannot be gated
        self.servers: dict[str, set[str]] = {}  # tool -> the labels that have it
        self.listed: set[str] = set()  # labels seen in a tools/list reply
        self.shaped: set[str] = set()  # labels whose listing declares an output schema
        self.read_only: set[str] = set()  # labels the server marks as read-only

    def learn(self, server: str | None, reply: Any) -> None:
        """Read one tools/list reply. A tool it names is one this gate sees."""
        for tool in reply.model_dump(by_alias=True, mode="json", exclude_none=True)["tools"]:
            name = tool["name"]
            label = call_for(server, name).label
            self.servers.setdefault(name, set()).add(label)
            self.run.adapted.add(name)
            self.listed.add(label)
            if "outputSchema" in tool:
                self.shaped.add(label)
            if tool.get("annotations", {}).get("readOnlyHint") is True:
                self.read_only.add(label)

    def ambiguous(self, call: Call) -> list[str]:
        """The labels of a short name the plan uses, if two servers have that tool."""
        plan = self.run.plan
        named = [*plan.real, *(entry.call for entry in (*plan.mocks, *plan.faults))]
        labels = self.servers.get(call.name, set())
        return sorted(labels) if call.short in named and len(labels) > 1 else []


def call_for(server: str | None, tool: str) -> Call:
    short = f"mcp:{tool}"
    # A server of the 2026 era may give no name, so only the short label can name it.
    return Call("mcp", f"mcp:{server}/{tool}" if server else short, tool, short=short)


def server_of(session: Any) -> str | None:
    info = session.server_info
    return info.name if info is not None else None


def names_in(function: object) -> tuple[str, ...]:
    """The attributes and functions a function's own code refers to."""
    return getattr(getattr(function, "__code__", None), "co_names", ())


def gatable(module: ModuleType) -> bool:
    """A mock must never become a real call, so the check reads what the gate relies on:
    call_tool goes through send_request, and send_request reaches the server through the
    session's dispatcher, which the gate replaces for a mock or a fault."""
    session = getattr(module, "ClientSession", None)
    send = getattr(session, "send_request", None)
    if send is None or params(send) != SEND_ARGUMENTS:
        return False
    if "send_request" not in names_in(getattr(session, "call_tool", None)):
        return False
    if not {"_dispatcher", "send_raw_request"} <= set(names_in(send)):
        return False
    timeout = getattr(getattr(module, "types", None), "REQUEST_TIMEOUT", None)
    return timeout is not None and hasattr(module, "MCPError")


def web_gatable(module: ModuleType) -> bool:
    transport = getattr(module, "StreamableHTTPTransport", None)
    methods = [getattr(transport, name, None) for name in WEB_METHODS]
    return all(inspect.iscoroutinefunction(method) for method in methods) and hasattr(
        getattr(module, "httpx2", None), "URL"
    )


def version() -> tuple[int, int] | None:
    try:
        parts = importlib.metadata.version("mcp").split(".")
        return int(parts[0]), int(parts[1])
    except (importlib.metadata.PackageNotFoundError, ValueError, IndexError):
        return None


def broken(gate: Gate) -> None:
    named = f"mcp {'.'.join(map(str, version() or ('?',)))}"
    message = (
        f"{named} does not have the functions Derail gates, so its tool calls cannot be "
        "checked, and the program is stopped"
    )
    gate.run.notice("structure", "mcp", message)
    gate.stop(message)


def install(run: RunState, sender: BlobSink | None, stop: Callable[[str], None]) -> None:
    gate = Gate(run, sender, stop)
    hooks.on_import(SESSION, lambda module: patch(module, gate))
    hooks.on_import(TRANSPORT, lambda module: patch_transport(module, gate))


def patch(module: ModuleType, gate: Gate) -> None:
    if not gatable(module):
        broken(gate)
        return
    found = version()
    oldest, newest = TESTED
    if found is not None and found > newest:
        gate.run.notice(
            "newer_version",
            "mcp",
            f"mcp {found[0]}.{found[1]} is newer than the versions this release was tested "
            f"with ({oldest[0]}.{oldest[1]} to {newest[0]}.{newest[1]})",
        )
    send = module.ClientSession.send_request
    if getattr(send, GATED, False):
        return

    async def send_gate(self: Any, request: Any, result_type: Any, *args: Any, **kw: Any) -> Any:
        run = current()
        if run is None:
            return await send(self, request, result_type, *args, **kw)
        data = request.model_dump(by_alias=True, mode="json", exclude_none=True)
        method = data["method"]
        server = server_of(self)
        if method in PASSES:
            reply = await send(self, request, result_type, *args, **kw)
            if method == "tools/list":
                gate.learn(server, reply)
            return reply
        fields = data.get("params", {})
        tool_call = method == "tools/call"
        call = call_for(server, fields["name"] if tool_call else method)
        step = decided(gate, run, call, fields.get("arguments") if tool_call else fields)
        if step.decision.action == "real":
            return await carried(gate, run, step, send(self, request, result_type, *args, **kw))
        if step.decision.action == "fault":
            reply = fault_reply(module, run, step, method)
        else:
            reply = await mock_reply(gate, run, step, self)
        return await carried(
            gate, run, step, send(stand_in(self, reply), request, result_type, *args, **kw)
        )

    setattr(send_gate, GATED, True)
    module.ClientSession.send_request = send_gate


def patch_transport(module: ModuleType, gate: Gate) -> None:
    """Mark every web request the transport sends, whoever caused it.

    The GET stream, the DELETE at close and a reply to the server pass through no
    send_request, so the mark sits on the transport's own methods.
    """
    if not web_gatable(module):
        broken(gate)
        return
    transport = module.StreamableHTTPTransport
    if getattr(transport.terminate_session, GATED, False):
        return

    def marked(method: Callable[..., Coroutine[Any, Any, Any]]) -> Callable[..., Any]:
        @functools.wraps(method)
        async def web_gate(self: Any, *args: Any, **kw: Any) -> Any:
            token = MCP_TRANSPORT.set(host_of(module.httpx2.URL(self.url)))
            try:
                return await method(self, *args, **kw)
            finally:
                MCP_TRANSPORT.reset(token)

        setattr(web_gate, GATED, True)
        return web_gate

    for name in WEB_METHODS:
        setattr(transport, name, marked(getattr(transport, name)))


def decided(gate: Gate, run: RunState, call: Call, arguments: object) -> Step:
    """Count and decide one call. Raises DerailRefused after recording a refusal."""
    labels = gate.servers.setdefault(call.name, set())
    labels.add(call.label)
    step = run.decide(call, {})
    step.extra["args"] = record(gate, arguments)
    if step.decision.action == "refused":
        refuse(run, step, read_only_note(gate, call))
    two = gate.ambiguous(call)
    if two:
        stop_ambiguous(run, step, two)
    return step


def stop_ambiguous(run: RunState, step: Step, labels: list[str]) -> NoReturn:
    run.refuse(step, "ambiguous_label")
    refuse(
        run,
        step,
        f"{step.call.short} matches a tool on more than one server, so the test stops; "
        f"name the server, as {' or '.join(labels)}",
    )


def read_only_note(gate: Gate, call: Call) -> str:
    if call.label not in gate.read_only:
        return ""
    return (
        " The server marks this tool as read-only, but that is only a hint, so it is not trusted."
    )


def record(gate: Gate, value: object) -> dict[str, object]:
    """Arguments and results go through blob, which removes secrets."""
    return blob(json.dumps(value, default=repr, sort_keys=True).encode(), gate.sender)


async def carried(gate: Gate, run: RunState, step: Step, sent: Coroutine[Any, Any, Any]) -> Any:
    """Wait for the reply, real or injected, and write the call's line."""
    try:
        reply = await sent
    except BaseException as exc:
        run.finish(step, error=exc)
        raise
    wire = reply.model_dump(by_alias=True, mode="json", exclude_none=True)
    step.extra["result"] = record(gate, wire)
    run.finish(step)
    return reply


class StandIn:
    """Takes the place of a session's dispatcher: nothing is sent, and the reply goes up
    through the library's own send_request, as a server's reply does."""

    def __init__(self, reply: dict[str, object]) -> None:
        self.reply = reply

    async def send_raw_request(self, *_: object) -> Any:
        return self.reply


def stand_in(session: Any, reply: dict[str, object]) -> Any:
    """A copy, as other tasks are using the session. send_request only reads it."""
    replica = copy.copy(session)
    replica._dispatcher = StandIn(reply)  # mcp ClientSession
    return replica


def tool_result(content: list[dict[str, str]], **fields: object) -> dict[str, object]:
    """A result as a server sends it. The 2026 protocol requires resultType."""
    return {"content": content, "resultType": "complete", **fields}


def fault_reply(module: ModuleType, run: RunState, step: Step, method: str) -> dict[str, object]:
    """What a server's failure looks like to the client. Raises for a timeout."""
    fault = run.plan.faults[step.decision.fault or 0]
    if fault.do == "timeout":
        code = module.types.REQUEST_TIMEOUT
        error = module.MCPError(code=code, message=f"Request {method!r} timed out")
        run.finish(step, error=error)
        raise error
    if fault.do == "empty":
        return tool_result([])
    text = f"Error executing tool {step.call.name}"
    return tool_result([{"type": "text", "text": text}], isError=True)


async def mock_reply(gate: Gate, run: RunState, step: Step, session: Any) -> dict[str, object]:
    """A real tool result. Its text is the value, and a declared shape fills the structured
    result, so a value of the wrong shape fails call_tool's own check."""
    value = run.plan.mocks[step.decision.mock or 0].returns
    if step.call.label not in gate.listed:
        try:
            await session.list_tools()  # as call_tool's own check does, for a tool it has not seen
        except BaseException as exc:
            run.finish(step, error=exc)
            raise
    text = value if isinstance(value, str) else json.dumps(value)
    reply = tool_result([{"type": "text", "text": text}])
    if step.call.label in gate.shaped:
        reply["structuredContent"] = value
    return reply
