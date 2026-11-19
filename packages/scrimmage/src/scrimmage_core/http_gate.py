# pyright: standard
"""The gate at a client's single attempt, Client._send_single_request, sync and async.

httpx and httpx2 share this function. The openai library sends every attempt through
httpx2's, and a tool's own web requests go through either. It sits below the client's
retries, redirects and response hooks, so an injected 429 or 503 meets them exactly as a
real one does, and each hop of a redirect is decided on its own. The host comes from the
request's own URL, so a proxy or a mounted transport changes nothing. The SDK never
imports either library: the hook patches one when the agent imports it.
"""

import atexit
import contextlib
import dataclasses
import datetime
import errno
import hashlib
import http
import inspect
import json
import math
import os
import random
import socket
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from types import ModuleType
from typing import TYPE_CHECKING, Any, NoReturn, Protocol, cast

from scrimmage_core import changes, credentials, delay, hooks, threads
from scrimmage_core import probe as probing
from scrimmage_core import provider_openai as openai_table
from scrimmage_core.budget import ASK_MAX_S, Held, Shortfall
from scrimmage_core.changes import Changed, Unchangeable
from scrimmage_core.client import ControlError
from scrimmage_core.config import BUILT_REPLIES, CONNECTION, REPLY_FAULTS, Fault, slow_seconds
from scrimmage_core.decide import (
    MISTAKE_REFUSAL,
    READ_METHODS,
    Call,
    Decision,
    names,
    schedule,
)
from scrimmage_core.errors import ScrimmageMockError, ScrimmageRefused
from scrimmage_core.judge import normal
from scrimmage_core.labels import shown_name
from scrimmage_core.plan import Plan
from scrimmage_core.redact import redact, redacted
from scrimmage_core.state import (
    BLOB_CAP,
    IN_PROCESS_CALL,
    BlobSink,
    Outside,
    ProcessState,
    RunState,
    Step,
    blob,
    carried,
    current,
    error_text,
    in_service,
    model_path_hint,
)

if TYPE_CHECKING:
    import asyncio

# The minor versions this release was tested with, per library.
TESTED = {"httpx": ((0, 28), (0, 28)), "httpx2": ((2, 16), (2, 16))}
GATED = "__scrimmage_gate__"
# What the gate builds faults and mocks from, in the library itself.
BUILT_FROM = ("Response", "ByteStream", "ReadTimeout", "ConnectError", "ReadError")
# What bad body sends a web request: a list cut off inside a string, so it never parses.
BROKEN_WEB_BODY = b'{"data": [{"id": 1, "name": "'
# The transport library each one raises its connection errors from.
CORE = {"httpx": "httpcore", "httpx2": "httpcore2"}
CHAR_PER_TOKEN = 4  # the rough size of a token, for estimates only
PRICE_KEY_CAP = 300  # a longer name is not shown whole as a price key to paste
BUDGET_WAIT_S = 30.0  # the longest a call waits for calls in flight to free the budget
WAIT_STEP_S = 0.25  # how soon a waiting call asks again when the runner sends no hint
JITTER = random.Random()  # its own, so the team's program keeps its random sequence
if hasattr(os, "register_at_fork"):  # forked children do not ask in step
    os.register_at_fork(after_in_child=JITTER.seed)
# The openai library numbers each attempt of a request; 0 is the first.
RETRY_HEADER = "x-stainless-retry-count"
UNMARKED = (
    "this model call's library does not mark its retries, so requests N counted each attempt "
    "as a request; use first N or always"
)
RUN_MARGIN_S = 10.0  # a wait on other runs ends this long before the run's time limit
SHOWN_HOLDS = 5  # holds a refusal names
WEB_CAP = changes.WEB_CAP  # a reply body over this is not read to be changed
# A refusal the team must act on: the message reaches the run table.
MISTAKE_REASONS = (
    "no_price",
    "unknown_tool_type",
    "unseen_tool",
    "hosted",
    "web_write",
    "unreadable_body",
    "unreadable_model",
    "redirect_off_host",
    "unsupported_loop",
    "unlisted",
    "ambiguous_label",
)
# Of those, the refusals the test's setup caused, not the agent: a later reader says so.
SETUP_REASONS = (
    "hosted",
    "unreadable_body",
    "unreadable_model",
    "redirect_off_host",
    "unsupported_loop",
)
# The MCP endpoint a transport is sending to, set only while it sends. Those messages were
# decided at the MCP gate or are the connection's own, so the web gate does not decide them
# again. It is the whole endpoint, not a flag: an event hook or an auth flow runs inside the
# same send, and its requests are still web requests.
MCP_TRANSPORT: ContextVar[tuple[object, ...] | None] = carried(
    ContextVar("scrimmage_mcp_transport", default=None)
)


class Reserver(Protocol):
    def reserve(
        self, run: str, usd: float, label: str = "", ticket: int | None = None
    ) -> int | Shortfall: ...

    def release(self, ticket: int) -> None: ...


class Context:
    """What the gate keeps for the process: what it knows, the way to the runner, the sender.

    It holds no run. Each request reads its run when it is made, so in a service, where one
    process serves many runs at once, each call is decided by its own run's plan.
    """

    def __init__(
        self,
        owner: ProcessState | RunState,
        client: Reserver,
        sender: BlobSink | None,
        stop: Callable[[str], None],
    ) -> None:
        # A run's own process, when a run is given: a function or a script has one of each.
        self.process = owner.process if isinstance(owner, RunState) else owner
        self.client = client
        self.sender = sender
        self.stop = stop  # ends the program: a library that cannot be gated
        self.faults = True  # false when the library has lost what faults are built from
        self.root = probing.here()  # the folder the probe's paths are looked for under

    def bound(self, run: RunState) -> "Bound":
        # A run's side-folder items go with its own lines; a run made with none uses ours.
        sender = run.blobs if run.blobs is not None else self.sender
        return Bound(run, self.client, sender, self.faults, self.root)

    def close_open(self) -> None:
        """At exit: a reply the agent never finished reading is still a call that was made."""
        run = self.process.current()
        if run is not None:
            run.close_open()


@dataclasses.dataclass(frozen=True)
class Bound:
    """One gated request's view: its own run, read once when the request was made."""

    run: RunState
    client: Reserver
    sender: BlobSink | None
    faults: bool
    root: str

    @property
    def open(self) -> set[Callable[[bool], None]]:
        return self.run.open  # replies not read to their end

    @property
    def model_paths(self) -> set[str]:
        return self.run.model_paths  # model paths run as web requests, noted once each


def params(function: object) -> list[str] | None:
    try:
        return list(inspect.signature(function).parameters)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def gatable(module: ModuleType) -> bool:
    """The functions the gate replaces exist and take the arguments it expects."""
    for name in ("Client", "AsyncClient"):
        send = getattr(getattr(module, name, None), "_send_single_request", None)
        if send is None or params(send) != ["self", "request"]:
            return False
    # Without its stream types a reply cannot be watched, so it could not be budgeted.
    return all(hasattr(module, name) for name in ("SyncByteStream", "AsyncByteStream"))


def version(module: ModuleType) -> tuple[int, int] | None:
    parts = str(getattr(module, "__version__", "")).split(".")
    try:
        return int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        return None


def install(
    process: ProcessState, client: Reserver, sender: BlobSink | None, stop: Callable[[str], None]
) -> Context:
    ctx = Context(process, client, sender, stop)
    atexit.register(ctx.close_open)  # registered after the sender's flush, so it runs first
    for library in TESTED:
        hooks.on_import(library, lambda module, name=library: patch(module, ctx, name))
    return ctx


def patch(module: ModuleType, ctx: Context, library: str = "httpx2") -> None:
    named = f"{library} {getattr(module, '__version__', '?')}"
    if not gatable(module):
        message = (
            f"{named} does not have the functions Scrimmage gates, so its calls cannot be "
            "checked or budgeted, and the program is stopped"
        )
        ctx.process.structure(library, message, ctx.stop)
        return
    if not all(hasattr(module, n) for n in BUILT_FROM):
        ctx.process.notice(
            "structure",
            library,
            f"{named} lacks what Scrimmage builds faults and mocks from; refusals and the "
            "budget still apply, those do not",
        )
        ctx.faults = False
    ctx.process.newer_version(library, version(module), TESTED[library])
    sync_send = module.Client._send_single_request
    async_send = module.AsyncClient._send_single_request
    if getattr(sync_send, GATED, False):
        return

    def sync_gate(self: Any, request: Any) -> Any:
        prepared = prepare(ctx, request, f"thread-{threading.get_ident()}", self)
        if prepared is None:
            with to_this_process(self, request):
                return sync_send(self, request)
        bound, step, extra = prepared
        change = None
        mode = step.decision.action
        if step.decision.under is not None:  # a carried fault: the call goes as the baseline's
            mode = step.decision.under
            change = slowed_sync(bound, module, step, request)
        if mode == "fault":
            return fault(bound, module, self, step, request, extra, sync=True)
        if mode == "mock":
            return mocked(bound, module, self, step, request, extra, change)
        reservation = reserve(bound, step, request, extra)
        try:
            response = sync_send(self, request)
        except BaseException as exc:
            failed(bound, module, step, request, extra, reservation, exc)
            raise
        target = off_host(bound, step, request, response)
        if target is not None:
            with contextlib.suppress(Exception):  # the refusal says why it was dropped
                response.close()
            redirected_away(bound, step, request, extra, reservation, target)
        if change is not None:
            return changed_sync(
                bound, module, self, step, request, response, extra, reservation, change
            )
        capture(bound, module, step, request, response, extra, reservation, sync=True)
        return response

    async def async_gate(self: Any, request: Any) -> Any:
        # A program on an asyncio loop has imported asyncio. Without it the task is None,
        # as under trio, and the SDK does not pay for the import.
        current_task = getattr(sys.modules.get("asyncio"), "current_task", None)
        try:
            task = current_task() if current_task is not None else None
        except RuntimeError:  # no asyncio loop: trio, or something else
            task = None
        who = f"task-{id(task)}" if task is not None else f"thread-{threading.get_ident()}"
        prepared = prepare(ctx, request, who, self)
        if prepared is None:
            with to_this_process(self, request):
                return await async_send(self, request)
        bound, step, extra = prepared
        if task is None and step.call.kind == "model":
            bound.run.refuse(step, "unsupported_loop")
            refuse(
                bound.run,
                step,
                "this model call runs outside asyncio, such as under trio, which Scrimmage "
                "does not support yet, so it could not be budgeted; run the agent on asyncio",
            )
        change = None
        mode = step.decision.action
        if step.decision.under is not None:  # a carried fault: the call goes as the baseline's
            mode = step.decision.under
            change = await slowed_async(bound, module, step, request, extra)
        if mode == "fault":
            return fault(bound, module, self, step, request, extra, sync=False)
        if mode == "mock":
            return mocked(bound, module, self, step, request, extra, change)
        reservation = None
        if step.call.kind == "model":
            reservation = await reserve_off_the_loop(bound, step, request, extra)
        try:
            response = await async_send(self, request)
        except BaseException as exc:
            failed(bound, module, step, request, extra, reservation, exc)
            raise
        target = off_host(bound, step, request, response)
        if target is not None:
            cancel = await close_keeping_cancel(response)
            try:
                redirected_away(bound, step, request, extra, reservation, target)
            except ScrimmageRefused as refusal:
                raise (cancel or refusal) from None
        if change is not None:
            return await changed_async(
                bound, module, self, step, request, response, extra, reservation, change
            )
        capture(bound, module, step, request, response, extra, reservation, sync=False)
        return response

    setattr(sync_gate, GATED, True)
    setattr(async_gate, GATED, True)
    module.Client._send_single_request = sync_gate
    module.AsyncClient._send_single_request = async_gate


def body_of(request: Any) -> bytes | None:
    """The request's body, or None for a streamed one, which has not been read yet."""
    try:
        return bytes(request.content)
    except Exception:
        pass
    # A redirect carries its body as the library's ByteStream: in memory, safe to read.
    held = getattr(request.stream, "_stream", None)  # httpx and httpx2 ByteStream._stream
    return held if isinstance(held, bytes) else None


def provider_of(ctx: Bound, host: str) -> str | None:
    named = next((h.provider for h in ctx.run.plan.model_hosts if h.host == host), None)
    return named or ("openai" if host in openai_table.HOSTS else None)


def host_of(url: Any) -> str:
    """The host as sent, so an international name is its xn-- form."""
    return bytes(url.raw_host).decode("ascii").lower()


def path_of(url: Any) -> str:
    """The path as sent, without the query, so /a%2Fb stays apart from /a/b."""
    return bytes(url.raw_path).decode("ascii").partition("?")[0]


def endpoint_of(url: Any) -> tuple[object, ...]:
    """Scheme, host, port and path, which a transport's own requests all share."""
    return url.scheme, host_of(url), url.port, path_of(url)


def describe(ctx: Bound, request: Any, who: str, body: bytes) -> Call:
    """The call this request is: a model call, or a web request to its own host."""
    host = host_of(request.url)
    provider = provider_of(ctx, host)
    method = str(request.method).upper()
    if provider is None and ctx.run.plan.probe and openai_table.op_for(request.url.path, method):
        provider = "openai"  # a gateway not named yet: the probe is how it gets found
    op = openai_table.op_for(str(request.url.path), method) if provider is not None else None
    if provider is None or op is None:
        return Call("http", f"http:{host}", path_of(request.url), provider, method)
    marked = RETRY_HEADER in request.headers
    try:
        retry = max(0, int(request.headers.get(RETRY_HEADER, "0")))
    except ValueError:
        retry = 0
    label = f"model:{provider}" if op == "chat" else f"model:{provider}/{op}"
    key = f"{who}:{hashlib.sha256(body).hexdigest()}"
    return Call("model", label, op, provider, method, retry, key, marked=marked)


# Transports that hand a request to an app in this process, as a test client does. A mock
# transport is not one: its answer stands in for a server elsewhere.
IN_PROCESS = (
    ("httpx2", "ASGITransport"),
    ("httpx2", "WSGITransport"),
    ("httpx", "ASGITransport"),
    ("httpx", "WSGITransport"),
    ("starlette.testclient", "_TestClientTransport"),
)


def in_process(client: Any, request: Any) -> bool:
    """The client sends this request to an app in this process: it leaves the process for
    nowhere, so outside any request it writes nothing, and that app's own gates decide it.
    Only these exact classes: a subclass could send anywhere."""
    pick = getattr(client, "_transport_for_url", None)
    try:
        transport = pick(request.url) if callable(pick) else None
    except Exception:  # a client this version does not know: decided as any other request
        return False
    kinds = [getattr(sys.modules.get(module), name, None) for module, name in IN_PROCESS]
    return type(transport) in kinds


@contextlib.contextmanager
def to_this_process(client: Any, request: Any) -> Iterator[None]:
    """A request to an app in this process: the app runs under its caller's slot, so code
    outside any request stays outside, never a fresh plain request."""
    if not in_service() or not in_process(client, request):
        yield
        return
    token = IN_PROCESS_CALL.set(True)
    try:
        yield
    finally:
        IN_PROCESS_CALL.reset(token)


def prepare(
    process_ctx: Context, request: Any, who: str, client: Any = None
) -> tuple[Bound, Step, dict[str, object]] | None:
    """Decide the request; None with no run. Raises ScrimmageRefused after recording one.

    The run is read here, once per request, and every step after reads that one."""
    run = current()
    if run is None or MCP_TRANSPORT.get() == endpoint_of(request.url):
        return None
    if isinstance(run, Outside) and in_process(client, request):
        return None
    ctx = process_ctx.bound(run)
    body = body_of(request)
    call = describe(ctx, request, who, body or b"")
    fields = openai_table.request_fields(body) if body and call.kind == "model" else None
    if call.kind == "model":
        call = dataclasses.replace(call, model=openai_table.model_name(fields))
    # The method and path only: headers, cookies and a query string are never recorded.
    extra: dict[str, object] = {
        "method": call.method,
        "path": path_of(request.url),
        "args": blob(body or b"", ctx.sender, scrub=run.secrets),
    }
    if call.provider is not None:
        extra["provider"] = call.provider
    if call.kind == "model":
        extra.update(model=call.model or "", retry=call.retry)
        if run.injected and fields is not None and carries(run.injected, fields):
            extra["carries_injected"] = True
        extra["stream"] = (fields or {}).get("stream") is True
    step = run.decide(call, {})
    step.extra = extra
    if call.kind == "model" and not call.marked and counts_requests(run.plan, call):
        extra["mistake"] = UNMARKED
    if not ctx.faults and step.decision.action in ("fault", "mock"):
        without_faults(run, step)
    where = f"{call.method} {path_of(request.url)}"
    if step.decision.action == "refused":
        refuse(run, step, where)
    if step.decision.action == "real" and call.kind == "http":
        model_path_notice(ctx, call, where)
    if call.kind == "model" and run.plan.probe:
        stop_probe(ctx, run, step, fields, request.url)
    if call.kind == "model" and call.name == "chat":
        if fields is None:
            run.refuse(step, "unreadable_body")
            refuse(
                run,
                step,
                f"{where} has a body Scrimmage cannot read, such as one sent from a generator or "
                "a compressed one, so the tools it offers cannot be checked; send it as plain "
                "JSON",
            )
        check_tools(ctx, run, step, fields, str(request.url.path))
    if call.kind == "model" and step.decision.action == "fault":
        reply_checks(run, step, fields or {}, str(request.url.path))
    if call.kind == "model" and call.name == "chat" and fields is not None:
        if openai_table.is_responses(str(request.url.path)):
            # Never marked: OpenAI takes an item id it never made and reads the item as sent,
            # and a request that looks up a faulted reply is a mistake, so no 400 is known to
            # follow a fault here. One that does stays an infrastructure error.
            chain_check(run, step, fields)
        else:
            # Decided as the request is sent: one sent before a fault fired holds nothing.
            step.carries_reply_fault = run.carries_reply_fault(call.label, fields)
    return ctx, step, extra


def model_path_notice(ctx: Bound, call: Call, where: str) -> None:
    """A POST to a model's path ran for real as a web request: real lists its host. It is then
    never budgeted, nor faulted by model name. Said once per path, as a run notice."""
    hint = model_path_hint(call.label, where)
    if hint is None or where in ctx.model_paths:
        return
    ctx.model_paths.add(where)
    host = call.label.removeprefix("http:")
    ctx.run.notice(
        "model_path",
        "scrimmage",
        redact(
            f"{call.label} {where} ran for real as a web request, so it was never budgeted or "
            f'faulted by model name; if it is a model call, add {{host = "{host}", provider = '
            '"openai"} to [agent] model_hosts'
        ),
    )


def counts_requests(plan: Plan, call: Call) -> bool:
    """Whether a requests N fault names this call."""
    return any(names(f, call) and schedule(f.when)[0] == "requests" for f in plan.faults)


def carries(injected: list[str], fields: dict[str, object]) -> bool:
    """Whether a chat request holds a text a fault put in a result. Only the strings of the
    parsed body are searched, each message's content as text or as parts, compared as a rule
    compares text; the raw body would hold the text escaped. A heuristic."""
    messages = fields.get("messages")
    if not isinstance(messages, list):
        return False
    wanted = [normal(text) for text in injected]
    for message in cast("list[object]", messages):
        if not isinstance(message, dict):
            continue
        content = cast("dict[str, object]", message).get("content")
        parts = cast("list[object]", content) if isinstance(content, list) else [content]
        for part in parts:
            text = (
                part
                if isinstance(part, str)
                else cast("dict[str, object]", part).get("text")
                if isinstance(part, dict)
                else None
            )
            if isinstance(text, str) and any(w in normal(text) for w in wanted):
                return True
    return False


def stop_probe(
    ctx: Bound, run: RunState, step: Step, fields: dict[str, object] | None, url: Any
) -> NoReturn:
    """Note the tools this first model request offers, refuse it unsent, and end the program.

    The note is put before the refusal, so the runner has it when the refusal stops the run.
    A body that cannot be read offers tools no one can name, which is said, not guessed.
    """
    host, path, method = host_of(url), path_of(url), str(step.call.method)
    with contextlib.suppress(Exception):  # the probe only reads, so it fails no call
        run.recheck_tools()
    names = [] if fields is None else openai_table.offered(str(url.path), fields).agent
    tools = [
        {
            "name": redact(name),
            "paths": probing.paths_of(name, ctx.root),
            "adapter": name in run.adapted,
        }
        for name in dict.fromkeys(names)
    ]
    extra: dict[str, object] = {
        "tools": tools,
        "model_request": {
            "host": redact(host),
            "method": method,
            "path": redact(path),
            "provider": str(step.call.provider),
        },
        "stopped_at": redact(f"{step.call.label} {method} {path}"),
    }
    if fields is None:
        extra["unreadable"] = True
    run.notice("probe", "scrimmage", f"the probe stopped at {method} {path}", extra=extra)
    run.refuse(step, "probe")
    # In a probe, finishing the step ends the program.
    run.fail(step, run.refusal(step, redact(f"{method} {path}")))


def without_faults(run: RunState, step: Step) -> None:
    """The library lost what faults and mocks are built from. A read or a model call runs
    for real, because the safety rule does not stop it; a write never does."""
    if step.call.kind == "http" and step.call.method not in READ_METHODS:
        run.refuse(step, "web_write")
    else:
        step.decision = Decision("real")


def refuse(run: RunState, step: Step, detail: str) -> NoReturn:
    if step.decision.reason in SETUP_REASONS:
        step.extra["by_config"] = True  # the test's setup caused this refusal, not the agent
    run.fail(
        step,
        run.refusal(step, redact(detail)),
        mistake=step.decision.reason in MISTAKE_REASONS,
    )


def check_tools(
    ctx: Bound, run: RunState, step: Step, fields: dict[str, object], path: str
) -> None:
    """Hosted writes, unknown tool types and tools no gate sees: all stop before sending."""
    tools = openai_table.offered(path, fields)
    if tools.hosted_reads:
        step.extra["hosted_reads"] = tools.hosted_reads
    if tools.unknown:
        kinds = ", ".join(sorted(set(tools.unknown)))
        run.refuse(step, "unknown_tool_type")
        refuse(
            run,
            step,
            f"the model request offers a tool of type {kinds}, which Scrimmage does not "
            "know, so the test stops; a newer Scrimmage may know it",
        )
    for label in tools.hosted_writes:
        if f"hosted:mcp/{label}" not in run.plan.real:
            run.refuse(step, "hosted")
            refuse(
                run,
                step,
                f"the model request offers the hosted MCP server {label!r}, which runs at "
                f'the provider and can change data; add "hosted:mcp/{label}" to [agent] real '
                "if it is safe",
            )
    run.recheck_tools()
    seen = run.seen
    unseen = sorted(name for name in set(tools.agent) if name not in seen)
    if unseen:
        why = "; ".join(run.blocked[name] for name in unseen if name in run.blocked)
        fix = (
            'add it to [agent] tools as "package.module:function" or, for a method, '
            '"package.module:Class.method", or as {path, name} when '
            "the model sees another name"
        )
        step.extra["unseen_tools"] = unseen
        run.refuse(step, "unseen_tool")
        refuse(
            run,
            step,
            f"the model would be offered {', '.join(unseen)}, which no gate sees, so a call to "
            f"it could run for real unchecked; {why or fix}",
        )


def reply_checks(run: RunState, step: Step, fields: dict[str, object], path: str) -> None:
    """Before anything is sent: a reply fault this request cannot take is a mistake in the
    test, and unknown tool on a request where the model could not pick a tool of its own is
    left as it is: no tools, tool_choice "none" or a named tool. The call then goes on as in
    the baseline."""
    word = run.plan.faults[step.decision.fault or 0].do
    if word not in REPLY_FAULTS:
        return
    if step.extra.get("stream") is True:
        not_carried_out(
            run,
            step,
            f"is a streamed request, and {word} changes a reply that is not streamed; turn "
            "streaming off for this model, or use timeout or a status on this one",
        )
    if word == "unknown tool" and openai_table.is_responses(path):
        not_carried_out(
            run,
            step,
            "is a Responses call, and a call to a tool no one offered is seen on Chat "
            "Completions only; fault a chat call",
        )
    if openai_table.is_responses(path) and fields.get("conversation") is not None:
        not_carried_out(
            run,
            step,
            f"is a Responses call in a conversation OpenAI keeps, and {word} would leave the "
            f"real reply, or none, in it for the next turn; {IN_INPUT}",
        )
    if word == "unknown tool" and (why := openai_table.no_free_tool_call(path, fields)):
        run.unchanged(step, why)
        step.decision = Decision("real")


IN_INPUT = "send the conversation in input instead, as the OpenAI Agents SDK does by default"


def chain_check(run: RunState, step: Step, fields: dict[str, object]) -> None:
    """Before anything is sent: a Responses request that asks OpenAI to look up a reply or
    an item a fault changed or built, as previous_response_id or an item_reference, is a
    mistake in the test. Its id is made up, so OpenAI holds nothing under it, and the model
    could not build the next turn from what the agent got."""
    found = run.chains_to_a_fault(fields)
    if found is None:
        return
    why = (
        f"asks OpenAI to look up {found}, a reply or an item a fault changed or built, and "
        f"OpenAI holds nothing under that id; {IN_INPUT}"
    )
    if step.decision.fault is not None:
        not_carried_out(run, step, why)
    step.decision = Decision("refused", reason=MISTAKE_REFUSAL)
    run.fail(step, ScrimmageMockError(f"{step.call.label} {why}"), mistake=True)


def built_reply(
    ctx: Bound,
    module: ModuleType,
    client: Any,
    step: Step,
    request: Any,
    extra: dict[str, object],
    word: str,
) -> Any:
    """refuse or unknown tool: the whole 200 reply, made from a real one. Nothing is sent, so
    the counts are an estimate and the line costs nothing."""
    body = body_of(request) or b""
    fields = openai_table.request_fields(body) or {}
    tokens = len(body) // CHAR_PER_TOKEN
    path = str(request.url.path)
    responses = openai_table.is_responses(path)
    if word == "refuse" and responses:
        built = openai_table.response_refusal_reply(fields, tokens)
    elif word == "refuse":
        built = openai_table.refusal_reply(fields, tokens)
    else:  # reply_checks let it through only on chat, with a tool offered
        tool = openai_table.tool_call_tools(path, fields)[0]
        built = openai_table.unknown_tool_reply(fields, tokens, tool)
    headers = {"content-type": "application/json", "x-request-id": "req_scrimmage_injected"}
    faulted(ctx, step, fields, built, responses=responses, ids_only=word == "unknown tool")
    return reply(ctx, module, client, step, request, extra, (200, headers, built))


def faulted(
    ctx: Bound,
    step: Step,
    fields: dict[str, object],
    gave: bytes,
    *,
    responses: bool,
    ids_only: bool = False,
) -> None:
    """Keep what a fired fault in a model's reply gave the agent: on Responses its ids,
    which a later request may not chain to; on chat what a later request built from it
    carries."""
    if responses:
        ctx.run.add_faulted_reply(openai_table.response_ids(gave))
    else:
        ctx.run.add_reply_fault_markers(
            step.call.label, openai_table.given(fields, gave, ids_only=ids_only)
        )


def fault(
    ctx: Bound,
    module: ModuleType,
    client: Any,
    step: Step,
    request: Any,
    extra: dict[str, object],
    *,
    sync: bool,
) -> Any:
    """Build the failure the library would see from a real outage. Nothing is sent."""
    run = ctx.run
    plan_fault = run.plan.faults[step.decision.fault or 0]
    # A carried fault lets the call happen, so it never comes here to be parsed as a status.
    assert plan_fault.do in (
        "timeout", "empty", "bad body", "context too long", *CONNECTION, *BUILT_REPLIES
    ) or plan_fault.do.startswith("status "), plan_fault.do  # fmt: skip
    if plan_fault.do == "timeout":
        run.fail(step, module.ReadTimeout("The read operation timed out", request=request))
    if plan_fault.do in CONNECTION:
        run.fail(step, connection_failure(module, plan_fault.do, request, sync=sync))
    if plan_fault.do == "empty":  # a broken backend's reply: no body, and nothing says its type
        return reply(ctx, module, client, step, request, extra, (200, {}, b""))
    if plan_fault.do == "bad body":
        return bad_body(ctx, module, client, step, request, extra)
    if plan_fault.do in BUILT_REPLIES:
        return built_reply(ctx, module, client, step, request, extra, plan_fault.do)
    if plan_fault.do == "context too long":
        if openai_table.is_responses(str(request.url.path)):
            not_carried_out(
                run,
                step,
                "is a Responses call, and context too long sends Chat Completions' error; "
                "fault a chat call, or use a status on this one",
            )
        headers = {"content-type": "application/json", "x-request-id": "req_scrimmage_injected"}
        body = openai_table.context_too_long_body()
        return reply(ctx, module, client, step, request, extra, (400, headers, body))
    status = int(plan_fault.do.split()[1])
    if step.call.kind == "model":
        body = openai_table.error_body(status)
        kind = openai_table.error_type(status)
        headers = {"content-type": kind, "x-request-id": "req_scrimmage_injected"}
    else:
        body = reason_phrase(status).encode()
        headers = {"content-type": "text/plain; charset=utf-8"}
    if plan_fault.retry_after is not None:  # whole seconds, the header's delay form
        headers["retry-after"] = str(plan_fault.retry_after)
    return reply(ctx, module, client, step, request, extra, (status, headers, body))


def bad_body(
    ctx: Bound, module: ModuleType, client: Any, step: Step, request: Any, extra: dict[str, object]
) -> Any:
    """A 200 that says it is JSON and is cut short, so it does not parse. A streamed model
    request would need a broken stream, a fault this version does not build: a mistake in
    the test, raised at once and not counted as fired."""
    run = ctx.run
    if step.call.kind == "model" and extra.get("stream") is True:
        not_carried_out(
            run,
            step,
            "is a streamed request, and bad body breaks a reply that is not streamed; fault "
            "a request without stream, or use timeout or a status on this one",
        )
    if step.call.kind == "model":
        headers = {"content-type": "application/json", "x-request-id": "req_scrimmage_injected"}
        body = openai_table.broken_body(str(request.url.path), step.call.name)
    else:
        headers = {"content-type": "application/json; charset=utf-8"}
        body = BROKEN_WEB_BODY
    return reply(ctx, module, client, step, request, extra, (200, headers, body))


def not_carried_out(run: RunState, step: Step, why: str) -> NoReturn:
    """A fault this call cannot take is a mistake in the test, raised at once. It is not a
    fired fault, and not real either: nothing was sent."""
    run.unchanged(step, why)
    step.decision = Decision("refused", reason=MISTAKE_REFUSAL)
    run.fail(step, ScrimmageMockError(f"{step.call.label} {why}"), mistake=True)


def connection_failure(module: ModuleType, word: str, request: Any, *, sync: bool) -> Exception:
    """The library's error for a connection that failed, chained as a real one is.

    httpx raises its error from httpcore's (httpx2 from httpcore2's), and httpcore raises
    its own from None, so the OSError is its suppressed context. On the async path anyio
    sits between: a refused connection is an OSError "All connection attempts failed" raised
    from the ConnectionRefusedError, and a reset is a BrokenResourceError raised from the
    ConnectionResetError. The errno comes from the platform's constants, which differ by OS."""
    # Loaded by the library's own transport, which a real connection failure comes from. A
    # client on a transport of its own, which never loads it, gets no middle link.
    core = sys.modules.get(CORE[module.__name__])
    os_error: OSError
    if word == "dns":
        os_error = socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        below: BaseException = os_error
        kind = "ConnectError"
    elif word == "refused":
        os_error = ConnectionRefusedError(errno.ECONNREFUSED, os.strerror(errno.ECONNREFUSED))
        attempts = OSError("All connection attempts failed")  # raised after its except ended
        below = os_error if sync else chained(attempts, cause=os_error, context=None)
        kind = "ConnectError"
    else:
        os_error = ConnectionResetError(errno.ECONNRESET, os.strerror(errno.ECONNRESET))
        # anyio's own context here, an IndexError from its buffer, hides behind the cause and
        # is not copied. Without anyio, as under trio, the link is left out.
        broken = getattr(sys.modules.get("anyio"), "BrokenResourceError", None)
        below = os_error if sync or broken is None else chained(broken(), os_error, None)
        kind = "ReadError"
    message = "" if below is not os_error and kind == "ReadError" else str(below)
    error = getattr(module, kind)(message, request=request)
    if core is None:
        return chained(error, cause=below, context=below)
    # httpcore's error is raised from None inside the except that caught the one below.
    middle = chained(getattr(core, kind)(message), cause=None, context=below)
    return chained(error, cause=middle, context=middle)


def chained(
    error: Exception, cause: BaseException | None, context: BaseException | None
) -> Exception:
    """The error as a raise leaves it: from cause, or from None, with that context."""
    error.__cause__ = cause
    error.__context__ = context
    error.__suppress_context__ = True
    return error


def reason_phrase(status: int) -> str:
    try:
        return http.HTTPStatus(status).phrase
    except ValueError:
        return f"Error {status}"


def reply(
    ctx: Bound,
    module: ModuleType,
    client: Any,
    step: Step,
    request: Any,
    extra: dict[str, object],
    answer: tuple[int, dict[str, str], bytes],
    original: bytes | None = None,
) -> Any:
    """Record and build a reply the gate made, left unread as a real one is, so iter_raw
    works on it. Like a real server's, a HEAD reply states its length and has no body.
    original: the body the line records, when the agent gets another, a changed one."""
    status, headers, body = answer
    sent = b"" if request.method == "HEAD" else body
    recorded = sent if original is None or request.method == "HEAD" else original
    extra.update(status=status, result=blob(recorded, ctx.sender, scrub=ctx.run.secrets))
    ctx.run.finish(step)
    if status >= 200 and status not in (204, 304):
        headers = {**headers, "content-length": str(len(body))}
    response = module.Response(
        status,
        headers=headers,
        stream=module.ByteStream(sent),
        request=request,
        default_encoding=client._default_encoding,  # httpx and httpx2 BaseClient
    )
    response.elapsed = datetime.timedelta(0)  # a real reply has it; code may read it
    return response


def mocked(
    ctx: Bound,
    module: ModuleType,
    client: Any,
    step: Step,
    request: Any,
    extra: dict[str, object],
    change: Fault | None = None,
) -> Any:
    """Build the test's fixed reply. Nothing is sent. change: the fault that changes the
    reply on its way to the agent, while the line records the mock's own."""
    mock = ctx.run.plan.mocks[step.decision.mock or 0]
    headers = dict(mock.headers)
    kind = None
    if mock.json is not None:
        body = json.dumps(mock.json).encode()
        kind = "application/json"
    else:
        body = (mock.text or "").encode()
        if mock.text is not None:
            kind = "text/plain; charset=utf-8"
    if kind is not None and "content-type" not in {name.lower() for name in headers}:
        headers["content-type"] = kind
    answer = (mock.status, headers, body)
    original = None
    if change is not None:
        done = changes.change_web(
            mock.status, str(request.method), list(headers.items()), body, change
        )
        if isinstance(done, Unchangeable):
            left_as_it_is(ctx, step, extra, done)
        else:
            ctx.run.changed(step, Changed(None, done.count, done.parts))
            answer, original = (mock.status, dict(done.headers), done.body), body
    response = reply(ctx, module, client, step, request, extra, answer, original)
    client.cookies.extract_cookies(response)  # the step a real reply goes through
    return response


def prices_of(ctx: Bound, model: str) -> openai_table.Rates | None:
    return openai_table.price(model, dict(ctx.run.plan.prices))


def no_price(model: str) -> str:
    """Why a call cannot be budgeted, with a price key the team can paste, if it fits a line."""
    rate = "{input = 0.15, output = 0.60} (dollars per million tokens)"
    off = 'or set max_cost_usd = "none"'
    if not model:
        return (
            "this model call sends no model name, so Scrimmage has no price for it and cannot keep "
            'the budget; send model on the call, or price calls with no name as "" = '
            f"{rate} under [agent.prices], {off}"
        )
    if len(model) > PRICE_KEY_CAP:
        return (
            f"Scrimmage has no price for the model {shown_name(model)}, so it cannot keep the "
            f"budget; the name is too long to show whole here, so add it under [agent.prices] "
            f"from your code, {off}"
        )
    return (
        f"Scrimmage has no price for the model {shown_name(model)}, so it cannot keep the budget; "
        f"add it under [agent.prices], such as {json.dumps(model)} = {rate}, {off}"
    )


def budget_wait(plan: Plan, elapsed: float = 0.0, *, own: bool = True) -> float:
    """How long a call waits for calls in flight to free the budget.

    own: this run's own holds keep the call out, which may be a deadlock, so the wait is short:
    30 s or half the time the run has left. Another run's holds end by its own time limit,
    so the call waits until near its own, leaving time for the refusal to be recorded.
    """
    left = plan.time_limit_s - elapsed
    if own:
        return max(0.0, min(BUDGET_WAIT_S, left / 2))
    return max(0.0, left - min(RUN_MARGIN_S, plan.time_limit_s / 10))


@dataclasses.dataclass(frozen=True)
class Pause:
    """The call must wait this long, then ask again with its ticket."""

    seconds: float
    ticket: int | None


def hold_for(ctx: Bound, step: Step, request: Any, extra: dict[str, object]) -> float | None:
    """What this model call could cost, at worst; None when nothing is reserved for it."""
    plan = ctx.run.plan
    if step.call.kind != "model" or plan.budget_usd is None:
        return None
    if step.call.name in openai_table.FREE_OPS:
        return None
    model = str(extra.get("model", ""))
    rates = prices_of(ctx, model)
    if rates is None:
        ctx.run.refuse(step, "no_price")
        refuse(ctx.run, step, no_price(model))
    body = body_of(request) or b""
    fields = openai_table.request_fields(body) if body else None
    output = openai_table.max_output(fields or {}) or plan.reserve_output_tokens
    return openai_table.cost(len(body) // CHAR_PER_TOKEN, output, rates)


def reserve(ctx: Bound, step: Step, request: Any, extra: dict[str, object]) -> int | None:
    """Reserve what this model call could cost, before it is sent.

    A sync call waits on its own thread, the one that made the call and is blocked on it
    anyway, so no thread anyone else needs is held. An exception that lands in the wait,
    such as a tool timeout's SIGALRM or a KeyboardInterrupt the program goes on after,
    gives the ticket back: the line keeps a place until its caller lets go of it.
    """
    usd = hold_for(ctx, step, request, extra)
    if usd is None:
        return None
    started, ticket = time.monotonic(), None
    try:
        while True:
            got = ask(ctx, step, extra, usd, started, ticket)
            if not isinstance(got, Pause):
                return got
            ticket = got.ticket
            time.sleep(got.seconds)
    except ScrimmageRefused:
        raise  # ask's own refusal, which gave back its ticket and wrote the line
    except BaseException as exc:
        give_up(ctx, ticket)
        abandoned(ctx, step, extra, None, exc)
        raise


def ask(
    ctx: Bound,
    step: Step,
    extra: dict[str, object],
    usd: float,
    started: float,
    ticket: int | None,
) -> int | Pause:
    """Ask the runner once: a reservation, a pause before asking again, or a refusal."""
    plan = ctx.run.plan
    try:
        answer = ctx.client.reserve(plan.run, usd, step.call.label, ticket)
    except ControlError as exc:
        ctx.run.refuse(step, "control_unreachable")
        refuse(ctx.run, step, f"the budget could not be checked, so the call is refused: {exc}")
    if not isinstance(answer, Shortfall):
        return answer
    own = waits_on_itself(answer, plan.run)
    bound = budget_wait(plan, started - ctx.run.began, own=own)
    waited = time.monotonic() - started
    if answer.why == "held" and waited < bound:
        return Pause(min(next_ask_s(answer.retry_s), bound - waited), answer.ticket)
    if answer.ticket is not None:
        ctx.client.release(answer.ticket)
    waited = waited if answer.why == "held" else 0.0
    extra["budget"] = {
        "usd": round(answer.usd, 8),
        "spent": round(answer.spent, 8),
        "held": round(answer.held, 8),
        "limit": answer.limit,
        "waited_s": round(waited, 1),
        "own": own,
        "holds": [
            {"run": h.run, "label": h.label, "usd": round(h.usd, 8), "ended": h.ended}
            for h in answer.holds[:SHOWN_HOLDS]
        ],
    }
    reason = "budget_held" if answer.why == "held" else "budget"
    ctx.run.refuse(step, reason)
    refuse(ctx.run, step, shortfall_text(answer, waited, plan, own))


def next_ask_s(hint: float | None) -> float:
    """How long a held call waits before asking again: the runner's hint, which grows with
    the line ahead of it, or WAIT_STEP_S from a runner that sends none or one out of range.
    Equal jitter, half of it and then a random part of the rest, keeps waiters that started
    together apart."""
    step = WAIT_STEP_S  # also for a zero or NaN hint, which would ask back to back
    if hint is not None and math.isfinite(hint) and 0.0 < hint <= ASK_MAX_S:
        step = hint
    return step / 2 + JITTER.uniform(0.0, step / 2)


def waits_on_itself(short: Shortfall, run: str) -> bool:
    """Even if every other run settled, this run's own holds would keep the call out: it
    may be waiting on itself, such as on a stream it left open."""
    own = sum(h.usd for h in short.holds if h.run == run)
    left = sum(h.usd for h in short.holds if h.ended)
    return short.spent + left + own + short.usd > short.limit


def shortfall_text(short: Shortfall, waited: float, plan: Plan, own: bool) -> str:
    """What the call could cost, what is spent and who holds the rest, and what to change."""
    text = (
        f"it could cost up to ${short.usd:.4f}; ${short.spent:.4f} is spent and "
        f"${short.held:.4f} is held by calls in flight, of a budget of ${short.limit:g}"
    )
    if short.holds:
        named = [hold_text(h, plan.run) for h in short.holds[:SHOWN_HOLDS]]
        more = len(short.holds) - SHOWN_HOLDS
        text += f" ({', '.join(named)}{f' and {more} more' if more > 0 else ''})"
    if short.why == "held":
        text += f"; Scrimmage waited {waited:.0f}s for them to finish"
    if short.why == "left":
        return (
            f"{text}. A run that ended while still holding can never give its hold back, so "
            "waiting cannot help: it was killed during a call. Raise [agent] max_cost_usd, or "
            "find why that run was killed in its log"
        )
    if own:
        return (
            f"{text}. This run's own calls hold the room, so it may be waiting on itself: a "
            "stream left open, or not read to its end, keeps its hold until it is closed. "
            "Close the stream before the next call, or set max_tokens to hold less"
        )
    return (
        f"{text}. A call without max_tokens holds reserve_output_tokens "
        f"({plan.reserve_output_tokens}) at the output price: set max_tokens on the call or "
        "lower [agent] reserve_output_tokens to hold less, lower [agent] parallel, or raise "
        "[agent] max_cost_usd if this is expected"
    )


def hold_text(hold: Held, run: str) -> str:
    where = "this run" if hold.run == run else hold.run
    ended = f", left by {hold.run}, which ended while still holding" if hold.ended else ""
    return f"{hold.label or 'a call'} in {where} ${hold.usd:.4f}{ended}"


async def reserve_off_the_loop(
    ctx: Bound, step: Step, request: Any, extra: dict[str, object]
) -> int | None:
    """Reserve for an async call. Each ask is one short request on a thread; the waits
    between asks are on the loop, so a waiting call holds no thread. asyncio's default
    threads also look up host names, so a held thread could keep a granted call from
    connecting."""
    import asyncio

    usd = hold_for(ctx, step, request, extra)
    if usd is None:
        return None
    started, ticket = time.monotonic(), None
    while True:
        got = await ask_off_the_loop(ctx, step, extra, usd, started, ticket)
        if not isinstance(got, Pause):
            return got
        ticket = got.ticket
        try:
            await asyncio.sleep(got.seconds)
        except asyncio.CancelledError:
            give_up(ctx, ticket)
            abandoned(ctx, step, extra, None)
            raise


async def ask_off_the_loop(
    ctx: Bound,
    step: Step,
    extra: dict[str, object],
    usd: float,
    started: float,
    ticket: int | None,
) -> int | Pause:
    """One ask on a thread, so the loop keeps running while the runner answers.

    If the task is cancelled meanwhile, the thread still gets its answer, with nobody left
    to use it. Whichever of the two finishes last gives back the hold or the ticket. A
    cancel can also land while the work waits for a free thread, so it never runs: the
    call was already counted, so the task writes its line instead.
    """
    import asyncio

    lock = threading.Lock()
    cancelled: list[bool] = []
    started_work: list[bool] = []
    done: list[int | Pause] = []

    def let_go(got: int | Pause | None) -> None:
        if isinstance(got, Pause):
            give_up(ctx, got.ticket)
        abandoned(ctx, step, extra, got if isinstance(got, int) else None)

    def work() -> int | Pause:
        with lock:
            if cancelled:
                return Pause(0.0, ticket)
            started_work.append(True)
        got = ask(ctx, step, extra, usd, started, ticket)
        with lock:
            done.append(got)
            abandon = bool(cancelled)
        if abandon:
            let_go(got)
        return got

    try:
        # Loaded by a service's start only, so a function or a script never imports it.
        pool = getattr(sys.modules.get("scrimmage_core.executor"), "EXECUTOR", None)
        if pool is None or not in_service():
            return await asyncio.to_thread(work)
        # A service's own executor, never the app's, and only with a control slot free:
        # with none, the call waits on the loop and asks again, holding no thread.
        client = cast(Any, ctx.client)
        if not client.try_slot():
            return Pause(next_ask_s(None), ticket)
        return await asyncio.wrap_future(pool.submit(lambda: client.run_held(work)))
    except asyncio.CancelledError:
        with lock:
            cancelled.append(True)
            finished = list(done)
            waiting = not started_work
        if finished or waiting:
            let_go(finished[0] if finished else Pause(0.0, ticket))
        raise


def give_up(ctx: Bound, ticket: int | None) -> None:
    """Hand back a ticket off the caller's thread or loop. If the release is lost, the
    ledger drops the ticket once it has not been asked for in budget.LOST_S."""
    if ticket is not None:
        threads.start(threading.Thread(target=ctx.client.release, args=(ticket,), daemon=True))


def abandoned(
    ctx: Bound,
    step: Step,
    extra: dict[str, object],
    reservation: int | None,
    error: BaseException | None = None,
) -> None:
    """The call was cancelled or interrupted before it was sent, so it cost nothing."""
    extra["cost_usd"] = 0.0
    if error is None:
        import asyncio  # only a cancelled call lands here, and its program has imported it

        error = asyncio.CancelledError("cancelled before it was sent")
    ctx.run.finish(step, error=error)
    settle(ctx, reservation, 0.0)


def off_host(ctx: Bound, step: Step, request: Any, response: Any) -> str | None:
    """The host a redirect would take this model call to, if that is not a model host.

    This refuses even a client that would not follow the redirect. The gate cannot tell,
    and a call wrongly refused is a visible mistake; one wrongly let through is unbudgeted.
    """
    if step.call.kind != "model" or not response.has_redirect_location:
        return None
    try:
        host = host_of(request.url.join(response.headers["location"]))
    except Exception:  # not a usable address: the library raises its own error on it
        return None
    return None if provider_of(ctx, host) else host


async def close_keeping_cancel(response: Any) -> "asyncio.CancelledError | None":
    """Close a reply being refused. A cancel or a failed close must not lose its line or
    settlement, so a cancel is handed back to raise once those are written."""
    import asyncio

    try:
        await response.aclose()
    except asyncio.CancelledError as cancel:
        return cancel
    except Exception:  # the reply is being dropped, and the refusal says why
        return None
    return None


def redirected_away(
    ctx: Bound,
    step: Step,
    request: Any,
    extra: dict[str, object],
    reservation: int | None,
    host: str,
) -> NoReturn:
    """Stop here: past this hop the gate would not see the call, so nothing could price it."""
    rates = prices_of(ctx, str(extra.get("model", "")))
    tokens = len(body_of(request) or b"") // CHAR_PER_TOKEN
    usd = openai_table.cost(tokens, 0, rates) if rates else None
    extra.update(cost_usd=usd, cost_estimated=usd is not None)
    settle(ctx, reservation, usd or 0.0)
    ctx.run.refuse(step, "redirect_off_host")
    refuse(
        ctx.run,
        step,
        f"the model host redirected {step.call.method} {request.url.path} to {host}, which is "
        "not a model host, so that call could not be budgeted or checked; if it is one, add "
        "it under [agent] model_hosts",
    )


def failed(
    ctx: Bound,
    module: ModuleType,
    step: Step,
    request: Any,
    extra: dict[str, object],
    reservation: int | None,
    error: BaseException,
) -> None:
    """The call got no reply. If it never left, it costs nothing; if it did, it may cost."""
    if step.call.kind != "model":
        ctx.run.finish(step, error=error)
        return
    never_sent = (getattr(module, "ConnectError", ()), getattr(module, "ConnectTimeout", ()))
    rates = prices_of(ctx, str(extra.get("model", "")))
    usd: float | None = 0.0
    if not isinstance(error, never_sent):
        tokens = len(body_of(request) or b"") // CHAR_PER_TOKEN
        usd = openai_table.cost(tokens, 0, rates) if rates else None
        extra["cost_estimated"] = usd is not None
    extra["cost_usd"] = usd
    ctx.run.finish(step, error=error)
    settle(ctx, reservation, usd or 0.0)


def settle(ctx: Bound, reservation: int | None, usd: float) -> None:
    """Replace the reservation, off the agent's thread or loop: the sender delivers it."""
    if reservation is not None and ctx.sender is not None:
        ctx.sender.put_settle(reservation, usd)


def timeout_of(request: Any) -> float | None:
    """The read timeout the request carries, which a slow server would run into."""
    timeout = request.extensions.get("timeout") or {}
    value = timeout.get("read")
    return None if value is None else float(value)


def slow_fault(ctx: Bound, step: Step) -> tuple[Fault, float | None]:
    fault = ctx.run.plan.faults[step.decision.fault or 0]
    return fault, slow_seconds(fault.do)


def slowed_sync(ctx: Bound, module: ModuleType, step: Step, request: Any) -> Fault | None:
    """Under a carried fault, before the request is sent: wait if the fault is slow. Returns
    the fault if it changes the reply. A wait cut short ends the call, with its line."""
    fault, seconds = slow_fault(ctx, step)
    if seconds is None:
        return fault

    def timed_out() -> BaseException:
        return module.ReadTimeout("The read operation timed out", request=request)

    try:
        delay.wait_sync(
            ctx.run, step, seconds, read_timeout=timeout_of(request), timed_out=timed_out
        )
    except BaseException as exc:
        ctx.run.finish(step, error=exc)
        raise
    return None


async def slowed_async(
    ctx: Bound, module: ModuleType, step: Step, request: Any, extra: dict[str, object]
) -> Fault | None:
    """slowed_sync on the event loop, in steps, so the caller's own deadline cancels the wait."""
    fault, seconds = slow_fault(ctx, step)
    if seconds is None:
        return fault
    sleep = delay.asyncio_sleep()
    if sleep is None:
        ctx.run.unchanged(step, "slow on an async web request needs asyncio")
        extra["mistake"] = (
            "slow on an async web request needs asyncio, not trio; the request was not slowed"
        )
        return None

    def timed_out() -> BaseException:
        return module.ReadTimeout("The read operation timed out", request=request)

    try:
        await delay.wait_steps(
            ctx.run, step, seconds, sleep, read_timeout=timeout_of(request), timed_out=timed_out
        )
    except BaseException as exc:
        ctx.run.finish(step, error=exc)
        raise
    return None


def left_as_it_is(ctx: Bound, step: Step, extra: dict[str, object], said: Unchangeable) -> None:
    """The reply cannot take the change: the agent gets it as it is, and the line says why."""
    fault = ctx.run.plan.faults[step.decision.fault or 0]
    ctx.run.unchanged(step, said.why)
    message = said.message(fault, step.call.label)
    if message is not None:
        extra["mistake"] = message


def model_op(step: Step, request: Any) -> str:
    """What a model call is, for a change to its reply: a Responses call is a chat to the
    engine, yet its reply has no choices to change."""
    if openai_table.is_responses(path_of(request.url)):
        return "responses"
    return str(step.call.name)


def decided_by_headers(
    ctx: Bound, step: Step, extra: dict[str, object], request: Any, response: Any
) -> bool:
    """Whether the headers alone rule the change out: a status, a type, an encoding, a size.
    Then the reply goes on untouched, and a stream is never read early."""
    headers = response.headers.multi_items()
    word = ctx.run.plan.faults[step.decision.fault or 0].do
    verdict = (
        changes.chat_kind(response.status_code, model_op(step, request), headers, word)
        if step.call.kind == "model"
        else changes.web_kind(response.status_code, str(request.method), headers)
    )
    if isinstance(verdict, Unchangeable):
        left_as_it_is(ctx, step, extra, verdict)
        return True
    length = response.headers.get("content-length", "")
    if length.isdigit() and int(length) > WEB_CAP:
        left_as_it_is(ctx, step, extra, Unchangeable("the body is over 10 MB", "data"))
        return True
    return False


def rebuilt(
    module: ModuleType,
    client: Any,
    request: Any,
    response: Any,
    headers: Any,
    stream: Any,
    status: int | None = None,
) -> Any:
    """A reply like the real one, with another body and the headers that go with it, and
    another status if one is given."""
    new = module.Response(
        response.status_code if status is None else status,
        headers=headers,
        stream=stream,
        request=request,
        extensions=response.extensions,
        default_encoding=client._default_encoding,  # httpx and httpx2 BaseClient
    )
    try:
        new.elapsed = response.elapsed
    except RuntimeError:  # the real reply is not closed yet, so it has no elapsed time
        new.elapsed = datetime.timedelta(0)
    return new


def changed_reply(
    ctx: Bound,
    module: ModuleType,
    client: Any,
    step: Step,
    request: Any,
    response: Any,
    extra: dict[str, object],
    fault: Fault,
    chunks: list[bytes],
    *,
    loaded: bool,
) -> Any:
    """The reply the agent gets once the whole body is read: changed if it can be, else as it
    was sent. The line records the original as its result, and the agent's beside it. A model
    reply takes the change of the fault's word."""
    body = b"".join(chunks)
    headers = response.headers.multi_items()
    status = response.status_code
    op = model_op(step, request)
    if step.call.kind == "model":
        change = changes.model_change(fault.do, op)
        done = change(status, op, headers, body, fault, decoded=loaded)
    else:
        done = changes.change_web(status, str(request.method), headers, body, fault, decoded=loaded)
    if isinstance(done, Unchangeable):
        left_as_it_is(ctx, step, extra, done)
        headers = response.headers.multi_items()
        if loaded:  # its content is unpacked, so the encoding no longer holds
            kept = [(k, v) for k, v in headers if k.lower() not in changes.BODY_HEADERS]
            headers = [*kept, ("content-length", str(len(body)))]
        return rebuilt(module, client, request, response, headers, module.ByteStream(body))
    reply = blob(done.body, ctx.sender, scrub=ctx.run.secrets)
    ctx.run.changed(step, Changed(None, done.count, done.parts), reply=reply, status=done.status)
    if step.call.kind == "model" and fault.do in REPLY_FAULTS:  # never a model replace
        asked = openai_table.request_fields(body_of(request) or b"") or {}
        faulted(ctx, step, asked, done.body, responses=op == "responses")
    stream = module.ByteStream(done.body)
    return rebuilt(module, client, request, response, done.headers, stream, done.status)


def replaying(
    module: ModuleType, response: Any, chunks: list[bytes], rest: Any, *, sync: bool
) -> Any:
    """A stream that gives the chunks already read, then the rest of the real one. A body over
    the cap goes on to the agent whole this way, with nothing buffered past the cap."""
    base = module.SyncByteStream if sync else module.AsyncByteStream

    class Replay(base):  # type: ignore[valid-type, misc]
        def __iter__(self) -> Iterator[bytes]:
            yield from chunks
            yield from rest

        async def __aiter__(self) -> Any:
            for chunk in chunks:
                yield chunk
            async for chunk in rest:
                yield chunk

        def close(self) -> None:
            response.close()

        async def aclose(self) -> None:
            await response.aclose()

    return Replay()


def changed_sync(
    ctx: Bound,
    module: ModuleType,
    client: Any,
    step: Step,
    request: Any,
    response: Any,
    extra: dict[str, object],
    reservation: int | None,
    fault: Fault,
) -> Any:
    """Read the reply's body, raw, and give the agent a changed copy. The reply is read only
    if its headers allow a change, and only up to the cap."""
    if decided_by_headers(ctx, step, extra, request, response):
        capture(ctx, module, step, request, response, extra, reservation, sync=True)
        return response
    loaded = getattr(response, "_content", None)
    chunks: list[bytes] = [] if loaded is None else [loaded]
    rest = None
    size = 0
    if loaded is None:
        rest = response.iter_raw()
        try:
            for chunk in rest:
                chunks.append(chunk)
                size += len(chunk)
                if size > WEB_CAP:
                    break
            else:
                rest = None
        except BaseException as exc:
            capture(ctx, module, step, request, response, extra, reservation, sync=True,
                    raw=chunks, error=exc)  # fmt: skip
            raise
    if rest is not None:  # over the cap: it goes on whole, and is not changed
        left_as_it_is(ctx, step, extra, Unchangeable("the body is over 10 MB", "data"))
        stream = replaying(module, response, chunks, rest, sync=True)
        new = rebuilt(module, client, request, response, response.headers.multi_items(), stream)
        capture(ctx, module, step, request, new, extra, reservation, sync=True)
        return new
    new = changed_reply(
        ctx,
        module,
        client,
        step,
        request,
        response,
        extra,
        fault,
        chunks,
        loaded=loaded is not None,
    )
    capture(ctx, module, step, request, response, extra, reservation, sync=True, raw=chunks)
    return new


async def changed_async(
    ctx: Bound,
    module: ModuleType,
    client: Any,
    step: Step,
    request: Any,
    response: Any,
    extra: dict[str, object],
    reservation: int | None,
    fault: Fault,
) -> Any:
    """changed_sync for an async client. A cancel while the body is read closes the reply,
    writes the line and goes on up."""
    import asyncio

    if decided_by_headers(ctx, step, extra, request, response):
        capture(ctx, module, step, request, response, extra, reservation, sync=False)
        return response
    loaded = getattr(response, "_content", None)
    chunks: list[bytes] = [] if loaded is None else [loaded]
    rest = None
    size = 0
    if loaded is None:
        rest = response.aiter_raw()
        try:
            async for chunk in rest:
                chunks.append(chunk)
                size += len(chunk)
                if size > WEB_CAP:
                    break
            else:
                rest = None
        except asyncio.CancelledError as cancel:
            await close_keeping_cancel(response)
            capture(ctx, module, step, request, response, extra, reservation, sync=False,
                    raw=chunks, error=cancel)  # fmt: skip
            raise
        except BaseException as exc:
            capture(ctx, module, step, request, response, extra, reservation, sync=False,
                    raw=chunks, error=exc)  # fmt: skip
            raise
    if rest is not None:
        left_as_it_is(ctx, step, extra, Unchangeable("the body is over 10 MB", "data"))
        stream = replaying(module, response, chunks, rest, sync=False)
        new = rebuilt(module, client, request, response, response.headers.multi_items(), stream)
        capture(ctx, module, step, request, new, extra, reservation, sync=False)
        return new
    new = changed_reply(
        ctx,
        module,
        client,
        step,
        request,
        response,
        extra,
        fault,
        chunks,
        loaded=loaded is not None,
    )
    capture(ctx, module, step, request, response, extra, reservation, sync=False, raw=chunks)
    return new


def capture(
    ctx: Bound,
    module: ModuleType,
    step: Step,
    request: Any,
    response: Any,
    extra: dict[str, object],
    reservation: int | None,
    *,
    sync: bool,
    raw: list[bytes] | None = None,
    error: BaseException | None = None,
) -> None:
    """Watch the reply's body go by, unchanged, and record the call when it ends.

    raw: the body was read already, in these chunks, so the line records the original while the
    agent gets another reply. error: the read failed, or was cancelled, and this is why."""
    lock = threading.Lock()
    done: list[bool] = []
    chunks: list[bytes] = []
    size = [0]  # every byte that went by; only the first 10 MB or so are kept

    def keep(chunk: bytes) -> None:
        if size[0] < BLOB_CAP:
            chunks.append(chunk)
        size[0] += len(chunk)

    def finish(complete: bool) -> None:
        with lock:
            if done:
                return
            done.append(True)
        ctx.open.discard(finish)
        if not complete:
            extra["reply_incomplete"] = True
        usd = 0.0
        try:
            usd = record(ctx, step, request, response, extra, b"".join(chunks), size[0])
        except Exception:  # the agent's own call must never fail because of the arithmetic
            extra.update(status=response.status_code, cost_usd=None, cost_estimated=False)
        if after_reply_fault(step, extra):
            extra["after_reply_fault"] = True
        ctx.run.finish(step)
        settle(ctx, reservation, usd)

    if raw is not None:
        for chunk in raw:
            keep(chunk)
        if error is not None:
            extra["error"] = error_text(step.call, error, ctx.run.secrets)
        finish(error is None)
        return
    loaded = getattr(response, "_content", None)  # a reply already in memory has no stream
    if loaded is not None:
        keep(loaded)
        finish(True)
        return

    def broke(error: Exception) -> None:
        extra["error"] = error_text(step.call, error, ctx.run.secrets)
        finish(False)

    ctx.open.add(finish)
    response.stream = wrap(module, response.stream, keep, finish, broke, sync=sync)


def after_reply_fault(step: Step, extra: dict[str, object]) -> bool:
    """A model's real 400 to a chat request that, as it was sent, held what a fault in a
    model's reply gave the agent. The agent built this request from the faulted reply, as
    from a real one, so the 400 is the agent's own result. Any other status, or a request
    that held nothing of it, says nothing about the agent."""
    return (
        step.carries_reply_fault and step.decision.action == "real" and extra.get("status") == 400
    )


def record(
    ctx: Bound,
    step: Step,
    request: Any,
    response: Any,
    extra: dict[str, object],
    raw: bytes,
    size: int,
) -> float:
    """Put the reply, its tokens and its cost on the line. Returns the cost to settle."""
    encoding = str(response.headers.get("content-encoding", ""))
    body = decoded(raw, encoding)
    if step.call.kind != "model":
        saved = blob(body, ctx.sender, total=size, wait=ctx.run.recording, scrub=ctx.run.secrets)
        extra.update(status=response.status_code, result=saved)
        if ctx.run.recording and step.call.provider is None:  # a model host is never recorded
            extra["recorded"] = ctx.run.recorded(
                lambda: web_recorded(request, response, body, encoding)
            )
        return 0.0
    used = openai_table.usage(body, output_required=step.call.name == "chat")
    tokens_in, tokens_out, cached = used.input, used.output, used.cached
    if not used.known:
        tokens_in = len(body_of(request) or b"") // CHAR_PER_TOKEN
        tokens_out = len(body) // CHAR_PER_TOKEN
    free = step.call.name in openai_table.FREE_OPS
    rates = openai_table.Rates(0.0, 0.0) if free else prices_of(ctx, str(extra.get("model", "")))
    usd = openai_table.cost(tokens_in, tokens_out, rates, cached) if rates else None
    extra.update(
        status=response.status_code,
        result=blob(body, ctx.sender, total=size, scrub=ctx.run.secrets),
        tokens={"input": tokens_in, "output": tokens_out, "cached": cached},
        cost_usd=None if usd is None else round(usd, 8),
        cost_estimated=usd is not None and not used.known and not free,
    )
    return usd or 0.0


def web_recorded(request: Any, response: Any, body: bytes, encoding: str) -> dict[str, object]:
    """What a mock of a real web request needs beyond its line, in a recording run.

    The body is the line's result. The query string is never recorded, only its hash, so
    two requests that differ by query alone can be told apart. A path the patterns change
    could never match the real request again.
    """
    host, path = host_of(request.url), path_of(request.url)
    what = credentials.credential(host, path)
    if what is None and credentials.carries_vault_token(request.headers):
        what = "HashiCorp Vault's API, as its token header says"
    if what is None and credentials.vault_reply(body):
        what = "HashiCorp Vault's API, as its reply's envelope says"
    if what is not None:
        return {"code": "credential", "why": f"a request to {what} is never recorded"}
    unpacked = changes.packed_why(encoding) is None
    try:
        found = redacted(body.decode("utf-8"))[1]
    except UnicodeDecodeError:
        unpacked, found = False, 0
    kind = response.headers.get("content-type")
    return {
        "content_type": None if kind is None else redact(str(kind)),
        "utf8": unpacked,
        "query_sha256": hashlib.sha256(bytes(request.url.query)).hexdigest(),
        "path_changed": redact(path) != path,
        "removed": [{"key": None, "rule": "pattern"} for _ in range(found)],
    }


def decoded(raw: bytes, encoding: str) -> bytes:
    """Undo gzip, deflate or br so the token counts can be read. A body that will not unpack,
    such as one already unpacked in memory, stays as it is."""
    body = changes.unpacked(raw, encoding)
    return raw if body is None else body


def wrap(
    module: ModuleType,
    inner: Any,
    keep: Callable[[bytes], None],
    finish: Callable[[bool], None],
    broke: Callable[[Exception], None],
    *,
    sync: bool,
) -> Any:
    """A stream of the library's own kind that passes every chunk through unchanged.

    A connection that drops halfway is the provider failing, so it goes on the record;
    the error still reaches the agent exactly as it was raised.
    """
    base = module.SyncByteStream if sync else module.AsyncByteStream

    class Watched(base):  # type: ignore[valid-type, misc]
        def __iter__(self) -> Iterator[bytes]:
            try:
                for chunk in inner:
                    keep(chunk)
                    yield chunk
            except Exception as exc:
                broke(exc)
                raise
            finish(True)

        async def __aiter__(self) -> Any:
            try:
                async for chunk in inner:
                    keep(chunk)
                    yield chunk
            except Exception as exc:
                broke(exc)
                raise
            finish(True)

        def close(self) -> None:
            try:
                inner.close()
            finally:
                finish(False)

        async def aclose(self) -> None:
            try:
                await inner.aclose()
            finally:
                finish(False)

    return Watched()
