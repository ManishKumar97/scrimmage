# pyright: standard
"""The gate at httpx2's single attempt, Client._send_single_request, sync and async.

The openai library sends every attempt of every request through it, below its own
retries, so an injected 429 is retried by the library exactly like a real one. Every
hop of a redirect passes here too, so each is decided on its own. The SDK never imports
httpx2: the hook patches it when the agent imports it.
"""

import asyncio
import atexit
import datetime
import hashlib
import inspect
import threading
import zlib
from collections.abc import Callable, Iterator
from types import ModuleType
from typing import Any, NoReturn, Protocol

from faultline_sdk import hooks
from faultline_sdk import provider_openai as openai_table
from faultline_sdk.client import ControlError
from faultline_sdk.decide import Call, Decision
from faultline_sdk.redact import redact
from faultline_sdk.state import BLOB_CAP, BlobSink, RunState, Step, blob, current

LIBRARY = "httpx2"
TESTED = ((2, 16), (2, 16))  # the minor versions this release was tested with
GATED = "__faultline_gate__"
CHAR_PER_TOKEN = 4  # the rough size of a token, for estimates only
# A refusal the team must act on: the message reaches the run table.
MISTAKE_REASONS = (
    "no_price",
    "unknown_tool_type",
    "unseen_tool",
    "hosted",
    "model_host_write",
    "unreadable_body",
    "redirect_off_host",
    "unsupported_loop",
)


class Reserver(Protocol):
    def reserve(self, run: str, usd: float) -> int | None: ...


class Context:
    """What every gated request needs: the run, the way to the runner, the sender."""

    def __init__(
        self,
        run: RunState,
        client: Reserver,
        sender: BlobSink | None,
        stop: Callable[[str], None],
    ) -> None:
        self.run = run
        self.client = client
        self.sender = sender
        self.stop = stop  # ends the program: a library that cannot be gated
        self.faults = True  # false when the library has lost what faults are built from
        self.seen = {path.rpartition(":")[2] for path in run.plan.tools}
        self.open: set[Callable[[bool], None]] = set()  # replies not read to their end

    def close_open(self) -> None:
        """At exit: a reply the agent never finished reading is still a call that was made."""
        for finish in list(self.open):
            finish(False)


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
    run: RunState, client: Reserver, sender: BlobSink | None, stop: Callable[[str], None]
) -> None:
    ctx = Context(run, client, sender, stop)
    atexit.register(ctx.close_open)  # registered after the sender's flush, so it runs first
    hooks.on_import(LIBRARY, lambda module: patch(module, ctx))


def patch(module: ModuleType, ctx: Context) -> None:
    named = f"{LIBRARY} {getattr(module, '__version__', '?')}"
    if not gatable(module):
        message = (
            f"{named} does not have the functions Faultline gates, so its calls cannot be "
            "checked or budgeted, and the program is stopped"
        )
        ctx.run.notice("structure", LIBRARY, message)
        ctx.stop(message)
        return
    if not all(hasattr(module, n) for n in ("Response", "ReadTimeout")):
        ctx.run.notice(
            "structure",
            LIBRARY,
            f"{named} lacks what Faultline builds faults from; refusals and the budget "
            "still apply, faults do not",
        )
        ctx.faults = False
    found = version(module)
    if found is not None and found > TESTED[1]:
        ctx.run.notice(
            "newer_version",
            LIBRARY,
            f"{LIBRARY} {module.__version__} is newer than the versions this release was "
            f"tested with ({TESTED[0][0]}.{TESTED[0][1]} to {TESTED[1][0]}.{TESTED[1][1]})",
        )
    sync_send = module.Client._send_single_request
    async_send = module.AsyncClient._send_single_request
    if getattr(sync_send, GATED, False):
        return

    def sync_gate(self: Any, request: Any) -> Any:
        prepared = prepare(ctx, request, f"thread-{threading.get_ident()}")
        if prepared is None:
            return sync_send(self, request)
        step, extra = prepared
        if step.decision.action == "fault":
            return fault(ctx, module, step, request, extra)
        reservation = reserve(ctx, step, request, extra)
        try:
            response = sync_send(self, request)
        except BaseException as exc:
            failed(ctx, module, step, request, extra, reservation, exc)
            raise
        target = off_host(ctx, request, response)
        if target is not None:
            response.close()
            redirected_away(ctx, step, request, extra, reservation, target)
        capture(ctx, module, step, request, response, extra, reservation, sync=True)
        return response

    async def async_gate(self: Any, request: Any) -> Any:
        try:
            task = asyncio.current_task()
        except RuntimeError:  # no asyncio loop: trio, or something else
            task = None
        who = f"task-{id(task)}" if task is not None else f"thread-{threading.get_ident()}"
        prepared = prepare(ctx, request, who)
        if prepared is None:
            return await async_send(self, request)
        step, extra = prepared
        if task is None:
            ctx.run.refuse(step, "unsupported_loop")
            refuse(
                ctx.run,
                step,
                "this model call runs outside asyncio, such as under trio, which Faultline "
                "does not support yet, so it could not be budgeted; run the agent on asyncio",
            )
        if step.decision.action == "fault":
            return fault(ctx, module, step, request, extra)
        reservation = await reserve_off_the_loop(ctx, step, request, extra)
        try:
            response = await async_send(self, request)
        except BaseException as exc:
            failed(ctx, module, step, request, extra, reservation, exc)
            raise
        target = off_host(ctx, request, response)
        if target is not None:
            await response.aclose()
            redirected_away(ctx, step, request, extra, reservation, target)
        capture(ctx, module, step, request, response, extra, reservation, sync=False)
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
    # A redirect carries its body as httpx2's ByteStream: still in memory, safe to read.
    held = getattr(request.stream, "_stream", None)  # httpx2 ByteStream._stream
    return held if isinstance(held, bytes) else None


def provider_of(ctx: Context, host: str) -> str | None:
    named = next((h.provider for h in ctx.run.plan.model_hosts if h.host == host), None)
    return named or ("openai" if host in openai_table.HOSTS else None)


def describe(ctx: Context, request: Any, who: str, body: bytes) -> Call | None:
    """The call this request is, or None for a host that is not a model host."""
    host = str(request.url.host).lower()
    provider = provider_of(ctx, host)
    if provider is None:
        return None
    path = str(request.url.path)
    method = str(request.method).upper()
    op = openai_table.op_for(path, method)
    if op is None:
        return Call("http", f"http:{host}", path, provider, method)
    try:
        retry = max(0, int(request.headers.get("x-stainless-retry-count", "0")))
    except ValueError:
        retry = 0
    label = f"model:{provider}" if op == "chat" else f"model:{provider}/{op}"
    key = f"{who}:{hashlib.sha256(body).hexdigest()}"
    return Call("model", label, op, provider, method, retry, key)


def prepare(ctx: Context, request: Any, who: str) -> tuple[Step, dict[str, object]] | None:
    """Decide the request. Raises FaultlineRefused after recording a refusal."""
    run = current()
    if run is None:
        return None
    body = body_of(request)
    call = describe(ctx, request, who, body or b"")
    if call is None:
        return None
    fields = openai_table.request_fields(body) if body else None
    extra: dict[str, object] = {
        "provider": call.provider,
        "method": call.method,
        "path": str(request.url.path),
        "args": blob(body or b"", ctx.sender),
    }
    if call.kind == "model":
        extra.update(model=str((fields or {}).get("model", "")), retry=call.retry)
        extra["stream"] = (fields or {}).get("stream") is True
    step = run.decide(call, {})
    step.extra = extra
    if not ctx.faults and step.decision.action == "fault":
        step.decision = Decision("real")
    # The method and path only: a query string can hold a key, and this text is recorded.
    where = f"{call.method} {request.url.path}"
    if step.decision.action == "refused":
        refuse(run, step, where)
    if call.kind == "model" and call.name == "chat":
        if fields is None:
            run.refuse(step, "unreadable_body")
            refuse(
                run,
                step,
                f"{where} has a body Faultline cannot read, such as a stream or a compressed "
                "one, so the tools it offers cannot be checked; send it as plain JSON",
            )
        check_tools(ctx, run, step, fields, str(request.url.path))
    if call.kind == "http":
        run.finish(step, result=None)  # a read to a model host: recorded, then sent
        return None
    return step, extra


def refuse(run: RunState, step: Step, detail: str) -> NoReturn:
    error = run.refusal(step, detail)
    mistake = str(error) if step.decision.reason in MISTAKE_REASONS else None
    run.finish(step, error=error, mistake=mistake)
    raise error


def check_tools(
    ctx: Context, run: RunState, step: Step, fields: dict[str, object], path: str
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
            f"the model request offers a tool of type {kinds}, which Faultline does not "
            "know, so the test stops; a newer Faultline may know it",
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
    unseen = sorted(name for name in set(tools.agent) if name not in ctx.seen)
    if unseen:
        step.extra["unseen_tools"] = unseen
        run.refuse(step, "unseen_tool")
        refuse(
            run,
            step,
            f"the model would be offered {', '.join(unseen)}, which no gate sees, so a call to "
            "it could run for real unchecked; add it to [agent] tools, such as "
            f'"<module>:{unseen[0]}"',
        )


def fault(
    ctx: Context, module: ModuleType, step: Step, request: Any, extra: dict[str, object]
) -> Any:
    """Build the failure the library would see from a real outage. Nothing is sent."""
    run = ctx.run
    plan_fault = run.plan.faults[step.decision.fault or 0]
    if plan_fault.do == "timeout":
        error = module.ReadTimeout("The read operation timed out", request=request)
        run.finish(step, error=error)
        raise error
    status = int(plan_fault.do.split()[1])
    body = openai_table.error_body(status)
    extra.update(status=status, result=blob(body, ctx.sender))
    run.finish(step)
    headers = {"content-type": "application/json", "x-request-id": "req_faultline_injected"}
    response = module.Response(status, headers=headers, content=body, request=request)
    response.elapsed = datetime.timedelta(0)  # a real reply has it; code may read it
    return response


def prices_of(ctx: Context, model: str) -> tuple[float, float] | None:
    team = {m: (p.input, p.output) for m, p in ctx.run.plan.prices}
    return openai_table.price(model, team)


def reserve(ctx: Context, step: Step, request: Any, extra: dict[str, object]) -> int | None:
    """Reserve what this model call could cost, before it is sent."""
    plan = ctx.run.plan
    if plan.budget_usd is None or step.call.name in openai_table.FREE_OPS:
        return None
    model = str(extra.get("model", ""))
    rates = prices_of(ctx, model)
    if rates is None:
        ctx.run.refuse(step, "no_price")
        refuse(
            ctx.run,
            step,
            f"Faultline has no price for the model {model!r}, so it cannot keep the budget; "
            f"add it under [agent.prices], such as {model} = {{input = 0.15, output = 0.60}} "
            '(dollars per million tokens), or set max_cost_usd = "none"',
        )
    body = body_of(request) or b""
    fields = openai_table.request_fields(body) if body else None
    output = openai_table.max_output(fields or {}) or plan.reserve_output_tokens
    usd = openai_table.cost(len(body) // CHAR_PER_TOKEN, output, rates)
    try:
        reservation = ctx.client.reserve(plan.run, usd)
    except ControlError as exc:
        ctx.run.refuse(step, "control_unreachable")
        refuse(ctx.run, step, f"the budget could not be checked, so the call is refused: {exc}")
    if reservation is None:
        ctx.run.refuse(step, "budget")
        refuse(
            ctx.run,
            step,
            f"raise [agent] max_cost_usd above ${plan.budget_usd:g} if this is expected",
        )
    return reservation


async def reserve_off_the_loop(
    ctx: Context, step: Step, request: Any, extra: dict[str, object]
) -> int | None:
    """Reserve on a thread, so the loop keeps running while the runner answers.

    If the task is cancelled meanwhile, the thread still gets its reservation, with nobody
    left to use it. Whichever of the two finishes last gives it back.
    """
    lock = threading.Lock()
    cancelled: list[bool] = []
    held: list[int | None] = []

    def work() -> int | None:
        reservation = reserve(ctx, step, request, extra)
        with lock:
            held.append(reservation)
            abandon = bool(cancelled)
        if abandon:
            abandoned(ctx, step, extra, reservation)
        return reservation

    try:
        return await asyncio.to_thread(work)
    except asyncio.CancelledError:
        with lock:
            cancelled.append(True)
            done = list(held)
        if done:
            abandoned(ctx, step, extra, done[0])
        raise


def abandoned(ctx: Context, step: Step, extra: dict[str, object], reservation: int | None) -> None:
    """The call was cancelled before it was sent, so it cost nothing."""
    extra["cost_usd"] = 0.0
    ctx.run.finish(step, error=asyncio.CancelledError("cancelled before it was sent"))
    settle(ctx, reservation, 0.0)


def off_host(ctx: Context, request: Any, response: Any) -> str | None:
    """The host a redirect would take this model call to, if that is not a model host."""
    if not response.has_redirect_location:
        return None
    host = str(request.url.join(response.headers["location"]).host).lower()
    return None if provider_of(ctx, host) else host


def redirected_away(
    ctx: Context,
    step: Step,
    request: Any,
    extra: dict[str, object],
    reservation: int | None,
    host: str,
) -> NoReturn:
    """Stop here: past this hop the gate would not see the call, so nothing could price it."""
    rates = prices_of(ctx, str(extra.get("model", "")))
    usd = (
        openai_table.cost(len(body_of(request) or b"") // CHAR_PER_TOKEN, 0, rates)
        if rates
        else 0.0
    )
    settle(ctx, reservation, usd)
    ctx.run.refuse(step, "redirect_off_host")
    refuse(
        ctx.run,
        step,
        f"the model host redirected {step.call.method} {request.url.path} to {host}, which is "
        "not a model host, so that call could not be budgeted or checked; if it is one, add "
        "it under [agent] model_hosts",
    )


def failed(
    ctx: Context,
    module: ModuleType,
    step: Step,
    request: Any,
    extra: dict[str, object],
    reservation: int | None,
    error: BaseException,
) -> None:
    """The call got no reply. If it never left, it costs nothing; if it did, it may cost."""
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


def settle(ctx: Context, reservation: int | None, usd: float) -> None:
    """Replace the reservation, off the agent's thread or loop: the sender delivers it."""
    if reservation is not None and ctx.sender is not None:
        ctx.sender.put_settle(reservation, usd)


def capture(
    ctx: Context,
    module: ModuleType,
    step: Step,
    request: Any,
    response: Any,
    extra: dict[str, object],
    reservation: int | None,
    *,
    sync: bool,
) -> None:
    """Watch the reply's body go by, unchanged, and record the call when it ends."""
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
        ctx.run.finish(step)
        settle(ctx, reservation, usd)

    loaded = getattr(response, "_content", None)  # a reply already in memory has no stream
    if loaded is not None:
        keep(loaded)
        finish(True)
        return

    def broke(error: Exception) -> None:
        extra["error"] = redact(f"{type(error).__name__}: {error}")
        finish(False)

    ctx.open.add(finish)
    response.stream = wrap(module, response.stream, keep, finish, broke, sync=sync)


def record(
    ctx: Context,
    step: Step,
    request: Any,
    response: Any,
    extra: dict[str, object],
    raw: bytes,
    size: int,
) -> float:
    """Put the reply, its tokens and its cost on the line. Returns the cost to settle."""
    body = decoded(raw, str(response.headers.get("content-encoding", "")))
    used = openai_table.usage(body, output_required=step.call.name == "chat")
    tokens_in, tokens_out = used.input, used.output
    if not used.known:
        tokens_in = len(body_of(request) or b"") // CHAR_PER_TOKEN
        tokens_out = len(body) // CHAR_PER_TOKEN
    free = step.call.name in openai_table.FREE_OPS
    rates = (0.0, 0.0) if free else prices_of(ctx, str(extra.get("model", "")))
    usd = openai_table.cost(tokens_in, tokens_out, rates) if rates else None
    extra.update(
        status=response.status_code,
        result=blob(body, ctx.sender, total=size),
        tokens={"input": tokens_in, "output": tokens_out},
        cost_usd=None if usd is None else round(usd, 8),
        cost_estimated=usd is not None and not used.known and not free,
    )
    return usd or 0.0


def decoded(raw: bytes, encoding: str) -> bytes:
    """Undo gzip or deflate so the token counts can be read; anything else stays as is."""
    try:
        if encoding.strip() == "gzip":
            return zlib.decompress(raw, 16 + zlib.MAX_WBITS)
        if encoding.strip() == "deflate":
            return zlib.decompress(raw)
    except zlib.error:
        return raw
    return raw


def wrap(
    module: ModuleType,
    inner: Any,
    keep: Callable[[bytes], None],
    finish: Callable[[bool], None],
    broke: Callable[[Exception], None],
    *,
    sync: bool,
) -> Any:
    """A stream of httpx2's own kind that passes every chunk through unchanged.

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
