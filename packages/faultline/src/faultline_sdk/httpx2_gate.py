# pyright: standard
"""The gate at httpx2's single attempt, Client._send_single_request, sync and async.

The openai library sends every attempt of every request through it, below its own
retries, so an injected 429 is retried by the library exactly like a real one. The SDK
never imports httpx2: the hook patches it when the agent imports it, and the gate finds
httpx2's own classes through the request it is handed.
"""

import asyncio
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
from faultline_sdk.decide import Call
from faultline_sdk.state import BLOB_CAP, BlobSink, RunState, Step, blob, current

LIBRARY = "httpx2"
TESTED = ((2, 16), (2, 16))  # the minor versions this release was tested with
HANDLED = "faultline_handled"  # a later gate lets a request with this mark straight through
STOP_TEST = ("no_price", "unknown_tool_type", "unseen_tool")
CHAR_PER_TOKEN = 4  # the rough size of a token, for estimates only


class Reserver(Protocol):
    def reserve(self, run: str, usd: float) -> int | None: ...


class Context:
    """What every gated request needs: the run, the way to the runner, the side folder."""

    def __init__(self, run: RunState, client: Reserver, blobs: BlobSink | None) -> None:
        self.run = run
        self.client = client
        self.blobs = blobs
        self.faults = True  # false when the library failed the structural check
        self.seen = {path.rpartition(":")[2] for path in run.plan.tools}


def structure_ok(module: ModuleType) -> bool:
    """The functions the gate replaces exist and take the arguments it expects."""
    for name in ("Client", "AsyncClient"):
        cls = getattr(module, name, None)
        send = getattr(cls, "_send_single_request", None)
        if send is None or list(inspect.signature(send).parameters) != ["self", "request"]:
            return False
    return all(hasattr(module, n) for n in ("Response", "ReadTimeout", "SyncByteStream"))


def version(module: ModuleType) -> tuple[int, int] | None:
    parts = str(getattr(module, "__version__", "")).split(".")
    try:
        return int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        return None


def install(run: RunState, client: Reserver, blobs: BlobSink | None) -> None:
    hooks.on_import(LIBRARY, lambda module: patch(module, Context(run, client, blobs)))


def patch(module: ModuleType, ctx: Context) -> None:
    if not structure_ok(module):
        ctx.run.notice(
            "structure",
            LIBRARY,
            f"{LIBRARY} {getattr(module, '__version__', '?')} does not have the functions "
            "Faultline gates; its model calls get no faults, and writes are still refused",
        )
        ctx.faults = False
        if not hasattr(getattr(module, "Client", None), "_send_single_request"):
            return
    found = version(module)
    if found is not None and found > TESTED[1]:
        ctx.run.notice(
            "newer_version",
            LIBRARY,
            f"{LIBRARY} {module.__version__} is newer than the versions this release was "
            f"tested with ({TESTED[0][0]}.{TESTED[0][1]} to {TESTED[1][0]}.{TESTED[1][1]})",
        )
    sync_send = module.Client._send_single_request
    async_send = getattr(getattr(module, "AsyncClient", None), "_send_single_request", None)
    if getattr(sync_send, HANDLED, False):
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
            failed(ctx, step, reservation, exc)
            raise
        capture(ctx, module, step, request, response, extra, reservation, sync=True)
        return response

    async def async_gate(self: Any, request: Any) -> Any:
        task = asyncio.current_task()
        who = f"task-{id(task)}" if task is not None else f"thread-{threading.get_ident()}"
        assert async_send is not None
        prepared = prepare(ctx, request, who)
        if prepared is None:
            return await async_send(self, request)
        step, extra = prepared
        if step.decision.action == "fault":
            return fault(ctx, module, step, request, extra)
        # The reservation is a request to the runner; wait for it without blocking the loop.
        reservation = await asyncio.to_thread(reserve, ctx, step, request, extra)
        try:
            response = await async_send(self, request)
        except BaseException as exc:
            failed(ctx, step, reservation, exc)
            raise
        capture(ctx, module, step, request, response, extra, reservation, sync=False)
        return response

    setattr(sync_gate, HANDLED, True)
    module.Client._send_single_request = sync_gate
    if async_send is not None:
        setattr(async_gate, HANDLED, True)
        module.AsyncClient._send_single_request = async_gate


def body_of(request: Any) -> bytes:
    try:
        return bytes(request.content)
    except Exception:  # a streamed upload has no content to read yet
        return b""


def describe(ctx: Context, request: Any, who: str, body: bytes) -> Call | None:
    """The call this request is, or None for a host that is not a model host."""
    host = str(request.url.host).lower()
    provider = next((h.provider for h in ctx.run.plan.model_hosts if h.host == host), None)
    if provider is None and host in openai_table.HOSTS:
        provider = "openai"
    if provider is None:
        return None
    path = str(request.url.path)
    method = str(request.method).upper()
    op = openai_table.op_for(path)
    if op is None:
        return Call("http", f"http:{host}", path, provider, method)
    try:
        retry = int(request.headers.get("x-stainless-retry-count", "0"))
    except ValueError:
        retry = 0
    label = f"model:{provider}" if op == "chat" else f"model:{provider}/{op}"
    key = f"{who}:{hashlib.sha256(body).hexdigest()}"
    return Call("model", label, op, provider, method, retry, key)


def prepare(ctx: Context, request: Any, who: str) -> tuple[Step, dict[str, object]] | None:
    """Decide the request. Raises FaultlineRefused after recording a refusal."""
    run = current()
    if run is None or request.extensions.get(HANDLED):
        return None
    body = body_of(request)
    call = describe(ctx, request, who, body)
    if call is None:
        return None
    request.extensions[HANDLED] = True
    fields = openai_table.request_fields(body)
    extra: dict[str, object] = {
        "provider": call.provider,
        "method": call.method,
        "path": str(request.url.path),
        "args": blob(body, ctx.blobs),
    }
    if call.kind == "model":
        extra.update(model=str(fields.get("model", "")), retry=call.retry)
        extra["stream"] = fields.get("stream") is True
    step = run.decide(call, {})
    step.extra = extra
    if not ctx.faults and step.decision.action == "fault":
        step.decision = type(step.decision)("real")
    if step.decision.action == "refused":
        refuse(run, step, f"{call.method} {request.url}")
    if call.kind == "model" and call.name == "chat":
        check_tools(ctx, run, step, call, fields, str(request.url.path))
    if call.kind == "http":
        run.finish(step, result=None)  # a read to a model host: recorded, then sent
        return None
    return step, extra


def refuse(run: RunState, step: Step, detail: str) -> NoReturn:
    error = run.refusal(step, detail)
    mistake = str(error) if step.decision.reason in STOP_TEST else None
    run.finish(step, error=error, mistake=mistake)
    raise error


def check_tools(
    ctx: Context,
    run: RunState,
    step: Step,
    call: Call,
    fields: dict[str, object],
    path: str,
) -> None:
    """Hosted writes, unknown tool types and tools no gate sees."""
    tools = openai_table.offered(path, fields)
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
    if unseen and run.unseen and call.retry == 0:
        names = ", ".join(run.unseen)
        run.refuse(step, "unseen_tool")
        refuse(
            run,
            step,
            f"the model is offered {names}, which no gate sees, so a call to it could run "
            "for real unchecked; add it to [agent] tools, such as "
            f'"<module>:{run.unseen[0]}"',
        )
    if unseen:
        run.unseen = unseen
        step.extra["unseen_tools"] = unseen


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
    extra.update(status=status, result=blob(body, ctx.blobs))
    run.finish(step)
    headers = {"content-type": "application/json", "x-request-id": "req_faultline_injected"}
    return module.Response(status, headers=headers, content=body, request=request)


def prices_of(ctx: Context, model: str) -> tuple[float, float] | None:
    team = {m: (p.input, p.output) for m, p in ctx.run.plan.prices}
    return openai_table.price(model, team)


def reserve(ctx: Context, step: Step, request: Any, extra: dict[str, object]) -> int | None:
    """Reserve what this model call could cost, before it is sent."""
    plan = ctx.run.plan
    if plan.budget_usd is None:
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
    assert rates is not None
    fields = openai_table.request_fields(body_of(request))
    output = openai_table.max_output(fields) or plan.reserve_output_tokens
    usd = openai_table.cost(len(body_of(request)) // CHAR_PER_TOKEN, output, rates)
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


def failed(ctx: Context, step: Step, reservation: int | None, error: BaseException) -> None:
    """The call never got a reply: record it and give the reservation back."""
    ctx.run.finish(step, error=error)
    if reservation is not None:
        post_settle(ctx, reservation, 0.0)


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
    done = threading.Event()
    chunks: list[bytes] = []
    size = [0]

    def keep(chunk: bytes) -> None:
        if size[0] < BLOB_CAP:
            chunks.append(chunk)
            size[0] += len(chunk)

    def finish(complete: bool) -> None:
        if done.is_set():
            return
        done.set()
        raw = b"".join(chunks)
        body = decoded(raw, str(response.headers.get("content-encoding", "")))
        used = openai_table.usage(body)
        rates = prices_of(ctx, str(extra.get("model", "")))
        tokens_in, tokens_out, estimated = used.input, used.output, not used.known
        if estimated:
            tokens_in = len(body_of(request)) // CHAR_PER_TOKEN
            tokens_out = len(body) // CHAR_PER_TOKEN
        usd = openai_table.cost(tokens_in, tokens_out, rates) if rates else 0.0
        extra.update(
            status=response.status_code,
            result=blob(body, ctx.blobs),
            tokens={"input": tokens_in, "output": tokens_out},
            cost_usd=round(usd, 8),
            cost_estimated=estimated,
        )
        if not complete:
            extra["reply_incomplete"] = True
        ctx.run.finish(step)
        if reservation is not None:
            post_settle(ctx, reservation, usd)

    loaded = getattr(response, "_content", None)  # a reply already in memory has no stream
    if loaded is not None:
        keep(loaded)
        finish(True)
        return
    response.stream = wrap(module, response.stream, keep, finish, sync=sync)


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


def post_settle(ctx: Context, reservation: int, usd: float) -> None:
    """Settle off the agent's thread or loop; the sender thread delivers it."""
    payload: dict[str, object] = {"run": ctx.run.plan.run, "reservation": reservation, "usd": usd}
    if ctx.blobs is not None:
        ctx.blobs.put_post("/v1/budget/settle", payload)


def wrap(
    module: ModuleType,
    inner: Any,
    keep: Callable[[bytes], None],
    finish: Callable[[bool], None],
    *,
    sync: bool,
) -> Any:
    """A stream of httpx2's own kind that passes every chunk through unchanged."""
    base = module.SyncByteStream if sync else module.AsyncByteStream

    class Watched(base):  # type: ignore[valid-type, misc]
        def __iter__(self) -> Iterator[bytes]:
            for chunk in inner:
                keep(chunk)
                yield chunk
            finish(True)

        async def __aiter__(self) -> Any:
            async for chunk in inner:
                keep(chunk)
                yield chunk
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
