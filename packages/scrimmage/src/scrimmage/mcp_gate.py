# pyright: standard, reportPrivateUsage=true
"""The gate for MCP tools, at ClientSession.send_request.

The official client sends every tool call through this one function, on every transport,
and clients built on it, such as FastMCP's, reach it through call_tool. The SDK never
imports the library: the hook patches it when the agent imports it.

Over streamable HTTP each message is also a web request. The transport's own requests
carry a mark, so a message is decided here and never again at the web gate.
"""

import contextlib
import copy
import json
import sys
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from contextvars import ContextVar
from dataclasses import dataclass
from types import ModuleType
from typing import Any

from scrimmage import capture, changes, delay, hooks, mocks
from scrimmage.config import slow_seconds
from scrimmage.decide import Call
from scrimmage.errors import ScrimmageMockError
from scrimmage.http_gate import MCP_TRANSPORT, endpoint_of, params, refuse
from scrimmage.process_gate import EXEMPT
from scrimmage.redact import redact
from scrimmage.state import BlobSink, ProcessState, RunState, Step, blob, current
from scrimmage.state import carried as carried_var

SESSION = "mcp.client.session"
TRANSPORT = "mcp.client.streamable_http"
STDIO = "mcp.client.stdio"
STARTS_THE_SERVER = "_create_platform_compatible_process"
LIBRARY = "mcp"  # the PyPI project the gate patches
TESTED = ((2, 3), (2, 3))  # the oldest and newest minor versions this release was tested with
GATED = "__scrimmage_gate__"
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
# Of those, the ones a server can act on with data, whatever the protocol says. They still
# pass, because a test could not run without them, but each one leaves a line.
TRACED = ("resources/read", "prompts/get", "completion/complete", "resources/subscribe")
# The real call in flight here. A client that proxies, such as FastMCP's for several servers,
# makes a second MCP call inside the first, which the gate decides by itself.
FORWARDING: ContextVar[Step | None] = carried_var(
    ContextVar("scrimmage_mcp_forwarding", default=None)
)
# The functions the transport opens a web request with, and where each takes the address.
OPENERS = {"stream_within_origin": 2, "sse_within_origin": 1}


class Gate:
    """What this process has learned about its MCP servers. It holds no run: a tools/list
    made at startup, before any run, is learned for every later run."""

    def __init__(
        self, owner: ProcessState | RunState, sender: BlobSink | None, stop: Callable[[str], None]
    ) -> None:
        # A run's own process, when a run is given: a function or a script has one of each.
        self.process = owner.process if isinstance(owner, RunState) else owner
        self.sender = sender
        self.stop = stop  # ends the program: a library that cannot be gated
        self.servers: dict[str, set[str]] = {}  # tool -> the labels that have it
        self.tools: dict[str, set[str]] = {}  # label -> the tools it can be read as
        self.listed: set[str] = set()  # labels seen in a tools/list reply
        self.shaped: set[str] = set()  # labels whose listing declares an output schema
        self.schemas: dict[str, dict[str, Any]] = {}  # label -> that output schema
        self.read_only: set[str] = set()  # labels the server marks as read-only
        # Per run: each label the gate listed its server for, and why it is not a tool there.
        self.asked: weakref.WeakKeyDictionary[RunState, dict[str, str | None]] = (
            weakref.WeakKeyDictionary()
        )

    def learn(self, server: str | None, reply: Any, run: RunState | None = None) -> None:
        """Read one tools/list reply, with or without a run. A tool it names is one this
        gate sees, in the process, so every run sees it."""
        process = self.process if run is None else run.process
        for tool in reply.model_dump(by_alias=True, mode="json", exclude_none=True)["tools"]:
            name = tool["name"]
            label = call_for(server, name).label
            self.note(name, label)
            process.adapted.add(name)
            self.listed.add(label)
            if "outputSchema" in tool:
                self.shaped.add(label)
                self.schemas[label] = tool["outputSchema"]
            else:
                self.shaped.discard(label)
                self.schemas.pop(label, None)
            if tool.get("annotations", {}).get("readOnlyHint") is True:
                self.read_only.add(label)

    def note(self, tool: str, label: str) -> None:
        self.servers.setdefault(tool, set()).add(label)
        self.tools.setdefault(label, set()).add(tool)

    def ambiguous(self, call: Call, run: RunState) -> str | None:
        """Why a label the plan uses could mean two things, if it could."""
        plan = run.plan
        named = [*plan.real, *(entry.call for entry in (*plan.mocks, *plan.faults))]
        labels = self.servers.get(call.name, set())
        if call.short in named and len(labels) > 1:
            return (
                f"{call.short} matches a tool on more than one server, so the test stops; "
                f"name the server, as {' or '.join(sorted(labels))}"
            )
        tools = self.tools.get(call.label, set())
        if call.label in named and len(tools) > 1:
            return (
                f"{call.label} could mean any of the tools {', '.join(sorted(tools))}, because "
                "a server name and a tool name can both hold a slash, so the test stops"
            )
        return None


def call_for(server: str | None, tool: str) -> Call:
    """The label names the server. A server of the 2026 era may give no name, so then only
    the tool can. A tool with a slash gets no short label, as it would read as server/tool,
    and on a server with no name its label starts with the slash, for the same reason."""
    bare = f"mcp:{tool}"
    short = None if "/" in tool else bare
    label = f"mcp:{server or ''}/{tool}" if server or "/" in tool else bare
    return Call("mcp", label, tool, short=short)


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
    if not (hasattr(session, "server_info") and hasattr(session, "list_tools")):
        return False
    if not {"_dispatcher", "send_raw_request"} <= set(names_in(send)):
        return False
    timeout = getattr(getattr(module, "types", None), "REQUEST_TIMEOUT", None)
    return timeout is not None and hasattr(module, "MCPError")


def web_gatable(module: ModuleType) -> bool:
    senders = (*OPENERS, "request_within_origin")
    return all(callable(getattr(module, name, None)) for name in senders) and hasattr(
        getattr(module, "httpx2", None), "URL"
    )


def version() -> tuple[int, int] | None:
    import importlib.metadata  # not at import: the SDK starts without it

    try:
        parts = importlib.metadata.version(LIBRARY).split(".")
        return int(parts[0]), int(parts[1])
    except (importlib.metadata.PackageNotFoundError, ValueError, IndexError):
        return None


def broken(gate: Gate) -> None:
    """Importing the library is enough: from then on a tool call could not be checked."""
    named = f"mcp {'.'.join(map(str, version() or ('?',)))}"
    tested = ".".join(map(str, TESTED[1]))
    message = (
        f"{named} does not have the functions Scrimmage gates. Scrimmage supports mcp 2.x, tested "
        f"with {tested}, so the program is stopped, even if it never calls an MCP tool. "
        "Install mcp 2.x in the test environment, or remove mcp from it"
    )
    gate.process.structure("mcp", message, gate.stop)


def install(process: ProcessState, sender: BlobSink | None, stop: Callable[[str], None]) -> None:
    gate = Gate(process, sender, stop)
    hooks.on_import(SESSION, lambda module: patch(module, gate))
    hooks.on_import(TRANSPORT, lambda module: patch_transport(module, gate))
    hooks.on_import(STDIO, patch_stdio)


def patch_stdio(module: ModuleType) -> None:
    """This gate decides what the server is asked, so starting it is not a blind child, even
    with the small environment the library gives it by default."""
    start = getattr(module, STARTS_THE_SERVER)

    async def start_exempt(*args: Any, **kwargs: Any) -> Any:
        token = EXEMPT.set(True)
        try:
            return await start(*args, **kwargs)
        finally:
            EXEMPT.reset(token)

    setattr(module, STARTS_THE_SERVER, start_exempt)


def patch(module: ModuleType, gate: Gate) -> None:
    if not gatable(module):
        broken(gate)
        return
    gate.process.newer_version(LIBRARY, version(), TESTED)
    send = module.ClientSession.send_request
    if getattr(send, GATED, False):
        return

    async def send_gate(self: Any, request: Any, result_type: Any, *args: Any, **kw: Any) -> Any:
        run = current()
        if run is None:
            reply = await send(self, request, result_type, *args, **kw)
            # A service lists its tools at startup, with no run; every run must see them.
            if getattr(request, "method", None) == "tools/list":
                gate.learn(server_of(self), reply)
            return reply
        data = request.model_dump(by_alias=True, mode="json", exclude_none=True)
        method = data["method"]
        server = server_of(self)
        if method in TRACED:
            step = run.passed(call_for(server, method))
            step.extra["args"] = record(gate, run, data.get("params", {}))
            return await carried(gate, run, step, send(self, request, result_type, *args, **kw))
        if method in PASSES:
            reply = await send(self, request, result_type, *args, **kw)
            if method == "tools/list":
                gate.learn(server, reply, run)
            return reply
        fields = data.get("params", {})
        tool_call = method == "tools/call"
        call = call_for(server, fields["name"] if tool_call else method)
        arguments = fields.get("arguments") if tool_call else fields
        step = decided(gate, run, call, arguments)
        if step.decision.under is not None:
            sending = (request, result_type, args, kw)
            return await carry(module, gate, run, step, self, sending, send)
        if step.decision.action == "real":
            token = FORWARDING.set(step)
            keep = Kept(self, arguments, result_type) if run.recording and tool_call else None
            try:
                sent = send(self, request, result_type, *args, **kw)
                return await carried(gate, run, step, sent, keep)
            finally:
                FORWARDING.reset(token)
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
    """Mark each web request the transport sends, for as long as it is being sent.

    The GET stream, the DELETE at close and a reply to the server pass through no
    send_request, so the mark sits where the transport sends. It ends before the reply is
    read: a message received here carries this task's context to the callback that handles
    it, and that callback's requests are the agent's own.
    """
    if not web_gatable(module):
        broken(gate)
        return
    if getattr(module.request_within_origin, GATED, False):
        return
    url_type = module.httpx2.URL

    def opened(opener: Callable[..., Any], place: int) -> Callable[..., Any]:
        @contextlib.asynccontextmanager
        async def web_gate(*args: Any, **kw: Any) -> AsyncIterator[Any]:
            token = MCP_TRANSPORT.set(endpoint_of(url_type(args[place])))
            try:
                async with opener(*args, **kw) as reply:
                    MCP_TRANSPORT.reset(token)
                    token = None
                    yield reply
            finally:
                if token is not None:
                    MCP_TRANSPORT.reset(token)

        return web_gate

    send = module.request_within_origin

    async def request_gate(client: Any, method: str, url: Any, **kw: Any) -> Any:
        token = MCP_TRANSPORT.set(endpoint_of(url_type(url)))
        try:
            return await send(client, method, url, **kw)
        finally:
            MCP_TRANSPORT.reset(token)

    for name, place in OPENERS.items():
        setattr(module, name, opened(getattr(module, name), place))
    setattr(request_gate, GATED, True)
    setattr(module, "request_within_origin", request_gate)  # noqa: B010


def decided(gate: Gate, run: RunState, call: Call, arguments: object) -> Step:
    """Count and decide one call. Raises ScrimmageRefused after recording a refusal."""
    gate.note(call.name, call.label)
    step = run.decide(run.with_args(call, arguments if arguments is not None else {}), {})
    forwarding = FORWARDING.get()
    if forwarding is not None:  # its real says only that it was passed on, so the line says so
        forwarding.extra["forwarded_to"] = call.label
    step.extra["args"] = record(gate, run, arguments)
    if step.decision.action == "refused":
        refuse(run, step, read_only_note(gate, call))
    why = gate.ambiguous(call, run)
    if why is not None:
        run.refuse(step, "ambiguous_label")
        refuse(run, step, why)
    return step


def read_only_note(gate: Gate, call: Call) -> str:
    if call.label not in gate.read_only:
        return ""
    return (
        " The server marks this tool as read-only, but that is only a hint, so it is not trusted."
    )


def record(gate: Gate, run: RunState, value: object) -> dict[str, object]:
    """Arguments and results go through blob, which removes secrets."""
    sink = run.blobs if run.blobs is not None else gate.sender  # the run's own side folder
    return blob(json.dumps(value, default=repr, sort_keys=True).encode(), sink, scrub=run.secrets)


@dataclass(frozen=True)
class Kept:
    """What a recording run needs to save a real tool call's result."""

    session: Any
    arguments: object
    result_type: Any


async def carried(
    gate: Gate,
    run: RunState,
    step: Step,
    sent: Coroutine[Any, Any, Any],
    keep: Kept | None = None,
    mistake: str | None = None,
) -> Any:
    """Wait for the reply, real or injected, and write the call's line.

    A recording run then saves the result. The call has succeeded by then, so a cancel that
    arrives while it is saved is raised only after the line says it succeeded.
    """
    import asyncio

    try:
        reply = await sent
        step.extra["result"] = record(gate, run, wire_form(reply))
    except BaseException as exc:
        run.finish(step, error=exc)
        raise
    cancel = None
    if keep is not None:
        try:
            found, unknown = await saved(gate, run, step, keep, reply)
            mistake = mistake or unknown
        except asyncio.CancelledError as exc:
            found, cancel = capture.unsaved(capture.UNSAVABLE, "the run was cancelled"), exc
        except Exception as exc:  # recording only observes the call, so it never fails it
            found = capture.scrimmage_error(exc)
        step.extra["recorded"] = run.recorded(lambda: found)
    run.finish(step, mistake=mistake)
    if cancel is not None:
        try:
            raise cancel
        finally:
            del cancel  # else it names a traceback that names this frame, and so itself
    return reply


async def saved(
    gate: Gate, run: RunState, step: Step, keep: Kept, reply: Any
) -> tuple[dict[str, object], str | None]:
    """C.J3.34: the result is saved only if a mock built from it gives the same result. Also
    the mistake to write on the call's line, when the server's own listing lacks the tool.

    The mock is built as scrimmage test builds it, which lists the server's tools first when
    the agent never did, so this does too. A failed result needs no listing: it is never
    saved, so it sends nothing more to the server.
    """
    real = wire_form(reply)
    failed = capture.failed_result(real)
    if failed is not None:
        return failed, None
    try:
        why = await unlisted(gate, run, step, keep.session)
    except Exception as exc:
        return capture.unsaved(
            capture.UNSAVABLE, f"listing the server's tools failed: {type(exc).__name__}"
        ), None
    if why is not None:
        mistake = f"{why}, so a mock of it would be a mistake. Fix the call, or remove it"
        return capture.unsaved(capture.UNSAVABLE, mistake), mistake
    shaped = step.call.label in gate.shaped

    def rebuild(value: object) -> object:
        # The library reads every reply with this pydantic TypeAdapter, as it reads a mock's.
        return wire_form(keep.result_type.validate_python(mock_result(value, shaped)))

    return capture.mcp_value(real, keep.arguments, rebuild), None


def asyncio_free_sleep() -> Callable[[float], Awaitable[None]]:
    """anyio's sleep, which the MCP library depends on, so it is loaded whenever this gate runs.
    It waits on asyncio and on trio alike. Not imported here: the SDK imports only the
    standard library."""
    return sys.modules["anyio"].sleep  # type: ignore[no-any-return]


def read_timeout(session: Any, args: tuple[Any, ...], kw: dict[str, Any]) -> float | None:
    """The read timeout the caller gave this request, or its session's own, if any."""
    given = args[0] if args else kw.get("request_read_timeout_seconds")
    return given if given is not None else getattr(session, "_session_read_timeout_seconds", None)


UNCHECKED = changes.Unchangeable("the output schema could not be checked", "data")


def schema_problem(
    session: Any, gate: Gate, label: str, name: str, raw: dict[str, Any]
) -> str | changes.Unchangeable | None:
    """Why the changed structured content breaks the tool's output schema, if it does. It runs
    the validator the client runs after the call, which would otherwise find this only once
    the line said the fault fired."""
    schema = gate.schemas.get(label)
    if schema is None or not isinstance(raw.get("structuredContent"), dict):
        return None
    # A schema is held and cannot be checked: the call is not fired, so no line says it was.
    build = getattr(session, "_output_schema_validator", None)
    if build is None:
        return UNCHECKED
    try:
        validator = build(name, schema)
        errors = list(validator.iter_errors(raw["structuredContent"]))
    except Exception:
        return UNCHECKED
    if not errors:
        return None
    try:
        best = sys.modules["jsonschema.exceptions"].best_match(errors) or errors[0]
    except (KeyError, AttributeError):
        best = errors[0]
    parts = getattr(best, "absolute_schema_path", ())
    where = ".".join(str(part) for part in parts) or "the schema root"
    shown = redact(repr(getattr(best, "instance", None)))[:80]
    path = str(getattr(best, "json_path", "$"))
    return (
        f"makes structuredContent{path.removeprefix('$')} {shown}, which the tool's "
        f"outputSchema does not allow at {where}"
    )


Sending = tuple[Any, Any, tuple[Any, ...], dict[str, Any]]  # request, result type, args, kwargs
Send = Callable[..., Coroutine[Any, Any, Any]]


async def carry(
    module: ModuleType,
    gate: Gate,
    run: RunState,
    step: Step,
    session: Any,
    sending: Sending,
    send: Send,
) -> Any:
    """A call under a carried fault: wait if the fault is slow, then answer as the baseline
    would, with a real call or a mock, and change the result if the fault is a change."""
    request, result_type, args, kw = sending
    assert step.decision.fault is not None
    fault = run.plan.faults[step.decision.fault]
    method = request.model_dump(by_alias=True, mode="json", exclude_none=True)["method"]
    seconds = slow_seconds(fault.do)
    if seconds is not None:

        def timed_out() -> BaseException:
            code = module.types.REQUEST_TIMEOUT
            return module.MCPError(code=code, message=f"Request {method!r} timed out")

        try:
            await delay.wait_steps(
                run,
                step,
                seconds,
                asyncio_free_sleep(),
                read_timeout=read_timeout(session, args, kw),
                timed_out=timed_out,
            )
        except BaseException as exc:
            run.finish(step, error=exc)
            raise
        return await beneath(gate, run, step, session, sending, send, None)
    label = step.call.label
    if step.decision.under == "real" and label not in gate.listed:
        try:
            await session.list_tools()  # its output schema, as call_tool's own check needs it
        except BaseException as exc:
            run.finish(step, error=exc)
            raise
    if label in gate.shaped and (fault.do == "append" or fault.find is None):
        why = "always sends structured content"
        run.unchanged(step, f"the tool declares an outputSchema, so it {why}")
        mistake = (
            f"{label} declares an outputSchema, so it {why} and {fault.do} has no place to put "
            "text; use replace with find on one of its string values"
        )
        return await beneath(gate, run, step, session, sending, send, mistake)

    def change(raw: dict[str, Any]) -> tuple[dict[str, Any], changes.Result]:
        done = changes.change_mcp_result(raw, fault)
        if isinstance(done, changes.Unchangeable):
            return raw, done
        new = as_dict(done.value)
        broke = schema_problem(session, gate, label, step.call.name, new)
        if isinstance(broke, changes.Unchangeable):
            return raw, broke
        if broke is not None:
            return raw, changes.Unchangeable(
                f"{fault.do} on {label} {broke}; choose text the schema accepts", "schema"
            )
        return new, done

    done: changes.Result | None
    if step.decision.under == "mock":
        raw = await mock_reply(gate, run, step, session)
        shown, done = change(raw)
        step.extra["result"] = record(gate, run, parsed_form(result_type, raw))
        try:
            reply = await send(stand_in(session, shown), request, result_type, *args, **kw)
        except BaseException as exc:
            run.finish(step, error=exc)
            raise
    else:
        changing = Changing(session._dispatcher, change)
        token = FORWARDING.set(step)
        try:
            reply = await send(replica(session, changing), request, result_type, *args, **kw)
        except BaseException as exc:
            run.finish(step, error=exc)
            raise
        finally:
            FORWARDING.reset(token)
        done = changing.result
        step.extra["result"] = record(gate, run, parsed_form(result_type, changing.original))
    if isinstance(done, changes.Changed):
        run.changed(step, done)
        run.finish(step)
    else:
        assert isinstance(done, changes.Unchangeable)
        run.unchanged(step, done.why)
        run.finish(step, mistake=done.message(fault, label))
    return reply


async def beneath(
    gate: Gate,
    run: RunState,
    step: Step,
    session: Any,
    sending: Sending,
    send: Send,
    mistake: str | None,
) -> Any:
    """The call as the baseline makes it, for a fault that changes nothing: real, or a mock."""
    request, result_type, args, kw = sending
    if step.decision.under == "mock":
        raw = await mock_reply(gate, run, step, session)
        return await carried(
            gate,
            run,
            step,
            send(stand_in(session, raw), request, result_type, *args, **kw),
            mistake=mistake,
        )
    token = FORWARDING.set(step)
    try:
        sent = send(session, request, result_type, *args, **kw)
        return await carried(gate, run, step, sent, mistake=mistake)
    finally:
        FORWARDING.reset(token)


def as_dict(value: object) -> dict[str, Any]:
    assert isinstance(value, dict)
    return value  # type: ignore[return-value]


def parsed_form(result_type: Any, raw: dict[str, Any] | None) -> object:
    """A raw reply as the line shows a reply: parsed as the library parses it, then written as
    JSON. A reply the library cannot parse is shown as it is."""
    if raw is None:
        return None
    try:
        if hasattr(result_type, "validate_python"):
            return wire_form(result_type.validate_python(raw, by_name=False))
        return wire_form(result_type.model_validate(raw, by_name=False))
    except Exception:
        return raw


class Changing:
    """Takes the place of a session's dispatcher for a real call under append or replace: the
    request goes to the real dispatcher, and the raw reply is changed on its way to the
    library, which then reads it as it reads a server's. The reply as the server sent it is
    kept, for the line."""

    def __init__(
        self,
        real: Any,
        change: Callable[[dict[str, Any]], tuple[dict[str, Any], changes.Result]],
    ) -> None:
        self.real = real
        self.change = change
        self.original: dict[str, Any] | None = None
        self.result: changes.Result | None = None

    async def send_raw_request(self, method: str, params: Any, opts: Any) -> Any:
        raw = await self.real.send_raw_request(method, params, opts)
        self.original = copy.deepcopy(raw)
        shown, self.result = self.change(raw)
        return shown


def replica(session: Any, dispatcher: Any) -> Any:
    """A copy, as other tasks are using the session. send_request only reads it."""
    copied = copy.copy(session)
    copied._dispatcher = dispatcher  # mcp ClientSession
    return copied


def wire_form(reply: Any) -> object:
    """A result as JSON. The call has happened, so nothing here may lose its line."""
    try:
        return reply.model_dump(by_alias=True, mode="json", exclude_none=True)
    except Exception:
        return repr(reply)


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
        run.fail(step, module.MCPError(code=code, message=f"Request {method!r} timed out"))
    if fault.do == "empty":
        return tool_result([])
    text = f"Error executing tool {step.call.name}"
    return tool_result([{"type": "text", "text": text}], isError=True)


async def mock_reply(gate: Gate, run: RunState, step: Step, session: Any) -> dict[str, object]:
    """A real tool result. Its text is the value, and a declared shape fills the structured
    result, so a value of the wrong shape fails call_tool's own check."""
    value = mocks.fresh(run.plan.mocks[step.decision.mock or 0].returns)
    try:
        why = await unlisted(gate, run, step, session)
    except BaseException as exc:
        run.finish(step, error=exc)
        raise
    if why is not None:
        run.fail(
            step, ScrimmageMockError(f"{why}. Fix the mock's call, or remove it"), mistake=True
        )
    return mock_result(value, step.call.label in gate.shaped)


SHOWN_TOOLS = 10  # a mistake names this many of the server's tools, then how many more


async def unlisted(gate: Gate, run: RunState, step: Step, session: Any) -> str | None:
    """Why the call's tool is not one its server has, when the server's own listing says so.

    A tool the gate has not seen listed is listed, as call_tool's own check does, once per
    label in a run. Only a complete listing is proof: one with a next page says nothing, and
    so does a reply that is not a listing, such as one a client of its own returns.
    """
    label = step.call.label
    asked = gate.asked.setdefault(run, {})
    if label not in gate.listed and label not in asked:
        listing = await session.list_tools()
        tools: object = getattr(listing, "tools", None)
        found = [getattr(tool, "name", None) for tool in tools] if isinstance(tools, list) else []
        names = [name for name in found if isinstance(name, str)]
        proof = (
            isinstance(tools, list)
            and len(names) == len(found)
            and getattr(listing, "next_cursor", "unknown") is None
        )
        unknown = proof and label not in gate.listed
        asked[label] = lacking(step.call, server_of(session), names) if unknown else None
    return None if label in gate.listed else asked.get(label)


def lacking(call: Call, server: str | None, tools: list[str]) -> str:
    """The mistake's first part: the label, and the tools the server does have."""
    lister = f"server {server}" if server else "the server"
    names = sorted(tools)
    more = len(names) - SHOWN_TOOLS
    shown = ", ".join(names[:SHOWN_TOOLS]) + (f" and {more} more" if more > 0 else "")
    has = f"its tools are {shown}" if names else "it lists no tools"
    return f"{call.label} is not a tool {lister} lists; {has}"


def mock_result(value: object, shaped: bool) -> dict[str, object]:
    """The result a mock of this value gives, as a server would send it."""
    text = value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False)
    reply = tool_result([{"type": "text", "text": text}])
    if shaped:
        reply["structuredContent"] = value
    return reply
