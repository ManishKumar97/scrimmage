"""The entry gate in the test process, before a real ASGI app, with a real control server.

A request without the run header reaches the app exactly as the server sent it. One with it
reaches the app only once its MAC is checked and its plan claimed, once, with every
x-scrimmage-* header removed. Its record closes record_grace_s after the reply.
"""

import asyncio
import contextvars
import json
import os
import threading
import time
import types
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast

import httpx2
import pytest

from scrimmage_core import asgi_gate, http_gate, protocol, state, tool_gate
from scrimmage_core.client import CAP
from scrimmage_core.config import Mock
from scrimmage_core.plan import Plan
from service_harness import Service, drive, eventually
from service_harness import service as make_service
from test_http_gate import Budget
from test_service_units import Sink, sink_of

seen_scopes: list[dict[str, Any]] = []


def get_order(order_id: str) -> str:
    raise AssertionError("a real order lookup must never run in a test")


GATED = tool_gate.gate(get_order, "get_order")


async def app(scope: Any, receive: Any, send: Any) -> None:
    """Calls the gated tool, then answers with it and with the headers it was sent."""
    seen_scopes.append(scope)
    message = await receive()
    try:
        answer = GATED("1")
    except Exception as exc:  # a refusal, as the agent would see it
        answer = f"{type(exc).__name__}: {exc}"
    body = json.dumps(
        {
            "answer": answer,
            "headers": [[bytes(k).decode(), bytes(v).decode()] for k, v in scope["headers"]],
            "body": bytes(message.get("body", b"")).decode(),
        }
    ).encode()
    await send({"type": "http.response.start", "status": 200, "headers": [(b"x-app", b"1")]})
    await send({"type": "http.response.body", "body": body})


def gated_app() -> Any:
    return asgi_gate.Entry(app)


@pytest.fixture
def svc(tmp_path: Path) -> Iterator[Service]:
    seen_scopes.clear()
    yield from make_service(tmp_path / "records")


def mocked(name: str) -> tuple[Mock, ...]:
    return (Mock("tool:get_order", f"order for {name}", None),)


def run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


@pytest.mark.design("service.runs_apart")
def test_four_parallel_runs_each_see_only_their_plan(svc: Service) -> None:
    names = [svc.open(f"run-{i:012x}", mocks=mocked(f"run-{i:012x}")) for i in range(4)]

    async def four() -> list[Any]:
        return await asyncio.gather(
            *(drive(gated_app(), headers=[svc.header(name)]) for name in names)
        )

    replies = run(four())
    for name, reply in zip(names, replies, strict=True):
        assert reply.status == 200 and json.loads(reply.body)["answer"] == f"order for {name}"
        lines = svc.closed(name)
        calls = [x for x in lines if x.get("call") == "tool:get_order"]
        assert len(calls) == 1 and {x["run"] for x in lines} == {name}


@pytest.mark.design("service.plain_untouched")
def test_plain_requests_reach_the_app_byte_for_byte(svc: Service) -> None:
    """A thousand plain requests, some with x-scrimmage-* names but no run header, between
    four runs, under a slot leaked into the server's own context: each reaches the app with
    the server's own scope object, and the same reply the app gives without Scrimmage."""
    extra = [(b"x-scrimmage-time", b"1"), (b"x-scrimmage-nonce", b"n"), (b"accept", b"*/*")]

    def scope_for(i: int) -> dict[str, Any]:
        return {
            "type": "http",
            "method": "POST",
            "path": "/chat",
            "raw_path": b"/chat",
            "query_string": b"",
            "headers": list(extra),
            "n": i,
        }

    # The same app with no gate, before any run is open: what it gives without Scrimmage.
    async def alone() -> list[Any]:
        return await asyncio.gather(*(drive(app, scope=scope_for(i)) for i in range(1000)))

    bare = run(alone())
    names = [svc.open(mocks=mocked("x")) for _ in range(4)]
    leaked = contextvars.copy_context()
    stale = state.RunState(None, None, [])  # type: ignore[arg-type]
    leaked.run(state.SLOT.set, state.Joined("run-leaked", stale))  # leaked into the server
    scopes = [scope_for(i) for i in range(1000)]
    seen_scopes.clear()

    async def mixed() -> list[Any]:
        plain = [drive(gated_app(), scope=scope) for scope in scopes]
        runs = [drive(gated_app(), headers=[svc.header(n)]) for n in names]
        return await asyncio.gather(*plain[:500], *runs, *plain[500:])

    through = leaked.run(run, mixed())
    gated = [*through[:500], *through[504:]]
    for without, mine in zip(bare, gated, strict=True):
        assert (mine.status, mine.headers, mine.body) == (
            without.status,
            without.headers,
            without.body,
        )
    # Each plain request reached the app with the server's own scope object, not a copy.
    plain_seen = {id(s) for s in seen_scopes if "n" in s}
    assert plain_seen == {id(s) for s in scopes}
    assert all(json.loads(r.body)["answer"] == "order for x" for r in through[500:504])


@pytest.mark.design("service.plain_untouched")
def test_a_plain_request_gets_the_servers_own_scope(svc: Service) -> None:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/echo",
        "raw_path": b"/echo",
        "query_string": b"",
        "headers": [(b"x-scrimmage-time", b"1")],
    }
    run(drive(gated_app(), scope=scope))
    assert seen_scopes == [scope] and seen_scopes[0] is scope


@pytest.mark.design("service.header_removed")
def test_the_run_header_never_reaches_the_app(svc: Service) -> None:
    name = svc.open(mocks=mocked("x"))
    _, value = svc.header(name)
    headers = [(b"X-Scrimmage-Run", value), (b"x-scrimmage-other", b"1"), (b"accept", b"a")]
    reply = run(drive(gated_app(), headers=headers))
    assert reply.status == 200
    sent = json.loads(reply.body)["headers"]
    assert sent == [["accept", "a"]]
    assert "x-scrimmage" not in reply.body.decode().lower()


def refused_code(reply: Any) -> str:
    assert reply.status == 503, reply
    assert reply.headers["content-type"] == "application/json"
    assert json.loads(reply.body) == {"error": reply.headers["x-scrimmage-error"]}
    assert set(reply.headers) == {"content-type", "content-length", "x-scrimmage-error"}
    return reply.headers["x-scrimmage-error"]


@pytest.mark.design("service.no_plan_no_app")
def test_a_run_request_whose_plan_cannot_be_claimed_gets_503_and_never_reaches_the_app(
    svc: Service,
) -> None:
    # A run that closed: its genuine header, sent late.
    late = svc.open()
    svc.server.close_run(late)
    assert refused_code(run(drive(gated_app(), headers=[svc.header(late)]))) == "no-such-run"
    # A plan outside SCRIMMAGE_ALLOW_IMPORTS.
    outside = svc.open(mocks=(Mock("tool:get_order", 1, "other.models:Order"),))
    assert (
        refused_code(run(drive(gated_app(), headers=[svc.header(outside)]))) == "plan-unavailable"
    )
    # A function to build a mock's value, outside it too.
    made = svc.open(mocks=(Mock("tool:get_order", None, None, returns_from="other.mocks:make"),))
    assert refused_code(run(drive(gated_app(), headers=[svc.header(made)]))) == "plan-unavailable"
    # The runner stopped.
    gone = svc.open()
    svc.server.stop()
    assert refused_code(run(drive(gated_app(), headers=[svc.header(gone)]))) == "plan-unavailable"
    assert seen_scopes == []


class Jumped:
    """The SDK client's view of the time module, its wall clock five minutes ahead."""

    def __getattr__(self, name: str) -> Any:
        return getattr(time, name)

    def time(self) -> float:
        return time.time() + 300


def test_a_claim_refused_for_the_clock_gets_503_clock(
    svc: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scrimmage_core import client as control_client

    name = svc.open()
    monkeypatch.setattr(control_client, "time", Jumped())  # the clock jumps after the probe
    assert refused_code(run(drive(gated_app(), headers=[svc.header(name)]))) == "clock"
    assert seen_scopes == []


@pytest.mark.design("service.claimed_once")
def test_a_second_claim_of_a_run_gets_503_claimed(svc: Service) -> None:
    name = svc.open(mocks=mocked("x"), grace=30.0)
    assert run(drive(gated_app(), headers=[svc.header(name)])).status == 200
    assert refused_code(run(drive(gated_app(), headers=[svc.header(name)]))) == "claimed"
    assert len(seen_scopes) == 1


@pytest.mark.design("service.claimer_only")
@pytest.mark.stub_record_lines
def test_lines_from_a_process_that_did_not_claim_are_refused(svc: Service) -> None:
    name = svc.open()
    mine = {"host": "h", "pid": 1, "started": "a"}
    other = {"host": "h", "pid": 2, "started": "b"}
    assert svc.client.claim(name, mine)
    line = {"v": 1, "run": name, "test": "t", "row": "baseline", "seq": 1, "proc": other}
    status, reply = svc.client.request("POST", "/v1/records", {"run": name, "lines": [line]})
    assert (status, reply["error"]) == (409, "not_the_claimer")
    line["proc"] = mine
    status, _ = svc.client.request("POST", "/v1/records", {"run": name, "lines": [line]})
    assert status == 200
    # A service run's plan is claimed, never fetched.
    assert svc.client.request("GET", f"/v1/plans/{name}")[0] == 409


class Counting:
    """Counts the claims the gate makes, and every control request, made from then on."""

    def __init__(self, svc: Service) -> None:
        self.count = 0
        self.every = 0
        real = svc.client.request

        def counted(method: str, path: str, *args: Any, **kwargs: Any) -> Any:
            self.every += 1
            self.count += path == "/v1/claims"
            return real(method, path, *args, **kwargs)

        svc.client.request = counted  # type: ignore[method-assign]


@pytest.mark.design("service.bad_header")
def test_a_bad_or_doubled_run_header_gets_503_with_no_fetch(svc: Service) -> None:
    name = svc.open(mocks=mocked("x"))
    good = svc.header(name)
    counting = Counting(svc)
    for headers in (
        [good, good],
        [(b"x-scrimmage-run", name.encode())],
        [(b"x-scrimmage-run", b"run-NOTHEX0000.0123456789abcdef")],
        [(b"x-scrimmage-run", good[1] + b"0")],
        [(b"x-scrimmage-run", b"probe-0123456789ab.0123456789abcdef")],
    ):
        assert refused_code(run(drive(gated_app(), headers=headers))) == "bad-run-header"
    assert counting.every == 0 and seen_scopes == []


def signed_probe(svc: Service, body: bytes, more: list[tuple[bytes, bytes]]) -> Any:
    """A probe signed as the runner signs it, over an empty body, whatever body follows."""
    signed = protocol.request_headers(svc.token, "POST", "/chat", b"", time.time())
    headers = [
        (b"x-scrimmage-run", b"probe-0123456789ab"),
        (b"x-scrimmage-probe", b"1"),
        *[(k.lower().encode(), v.encode()) for k, v in signed.items()],
        *more,
    ]
    return run(drive(gated_app(), headers=headers, body=body))


@pytest.mark.design("service.startup_report_schema")
def test_the_probes_report_with_a_notice_matches_its_schema(svc: Service) -> None:
    import jsonschema

    import scrimmage_core

    schema = json.loads(
        (Path(scrimmage_core.__file__).parent / "schemas" / "startup-report.v2.json").read_text()
    )
    svc.process.notice("newer_version", "httpx2", "httpx2 2.99.0 is newer than the versions")
    reply = signed_probe(svc, b"", [])
    report = json.loads(reply.body)
    jsonschema.Draft202012Validator(schema).validate(report)
    assert report["notices"] == [
        {
            "notice": "newer_version",
            "library": "httpx2",
            "message": "httpx2 2.99.0 is newer than the versions",
        }
    ]


@pytest.mark.design("service.probe_needs_run_header")
def test_a_probe_header_alone_is_a_plain_request_and_a_signed_probe_gets_the_report(
    svc: Service,
) -> None:
    alone = run(drive(gated_app(), headers=[(b"x-scrimmage-probe", b"1")]))
    assert alone.status == 200 and alone.headers.get("x-app") == "1"
    assert ["x-scrimmage-probe", "1"] in json.loads(alone.body)["headers"]  # untouched
    report = signed_probe(svc, b"", [])
    assert report.status == 200 and report.headers.get("x-scrimmage-probe") == "report"


@pytest.mark.design("service.probe_body_empty")
@pytest.mark.parametrize(
    "declared", [[(b"content-length", b"5")], [(b"transfer-encoding", b"chunked")]]
)
def test_a_probe_that_declares_a_body_goes_to_the_app_unread(
    svc: Service, declared: list[tuple[bytes, bytes]]
) -> None:
    reply = signed_probe(svc, b"hello", declared)
    assert reply.status == 200 and reply.headers.get("x-app") == "1"
    assert json.loads(reply.body)["body"] == "hello"  # the app read it, the gate did not
    assert len(seen_scopes) == 1


@pytest.mark.design("service.header_mac")
def test_a_header_with_a_wrong_mac_gets_503_with_no_control_request(svc: Service) -> None:
    name = svc.open(mocks=mocked("x"))
    counting = Counting(svc)
    mac = protocol.run_mac(svc.token, name)
    wrong = "0" * 16 if mac != "0" * 16 else "1" * 16
    for bad in (wrong, mac[:15] + ("0" if mac[15] != "0" else "1")):
        headers = [(b"x-scrimmage-run", f"{name}.{bad}".encode())]
        assert refused_code(run(drive(gated_app(), headers=headers))) == "bad-run-header"
    assert counting.every == 0 and seen_scopes == []


@pytest.mark.design("service.header_mac")
def test_a_header_made_with_another_token_gets_503(svc: Service) -> None:
    name = svc.open(mocks=mocked("x"))
    header = svc.header(name, token="another-token-" + "0" * 40)
    assert refused_code(run(drive(gated_app(), headers=[header]))) == "bad-run-header"


@pytest.mark.design("service.abandoned")
def test_a_claim_failure_after_the_claim_sends_abandoned_and_the_run_ends_at_once(
    svc: Service,
) -> None:
    name = svc.open(tools=("acme.tools:get_order", "acme.tools:refund_order"))
    assert refused_code(run(drive(gated_app(), headers=[svc.header(name)]))) == "plan-unavailable"
    eventually(lambda: (name, "structure") in svc.refused)
    assert svc.server.runs[name].broken and seen_scopes == []


@pytest.mark.design("service.setup_header")
def test_the_setup_header_passes_the_gate(svc: Service) -> None:
    name = svc.open("setup-0123456789ab", mocks=mocked("setup"))
    assert run(drive(gated_app(), headers=[svc.header(name)])).status == 200
    # A setup- name with a MAC made for the run- name is refused.
    other = svc.open("setup-ba9876543210")
    mac = protocol.run_mac(svc.token, "run-ba9876543210")
    headers = [(b"x-scrimmage-run", f"{other}.{mac}".encode())]
    assert refused_code(run(drive(gated_app(), headers=headers))) == "bad-run-header"


@pytest.mark.design("service.own_executor")
def test_claims_and_budget_asks_never_use_the_default_executor(svc: Service) -> None:
    name = svc.open(mocks=mocked("x"))
    used: list[object] = []

    class Watched(ThreadPoolExecutor):
        def submit(self, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
            used.append(fn)
            return super().submit(fn, *args, **kwargs)

    async def with_default() -> Any:
        asyncio.get_running_loop().set_default_executor(Watched(max_workers=2))
        return await drive(gated_app(), headers=[svc.header(name)])

    assert run(with_default()).status == 200
    assert used == []
    own = [t for t in threading.enumerate() if t.name.startswith("scrimmage-control-")]
    assert len(own) >= CAP and not any(t in state.TAGS for t in own)


@pytest.mark.design("service.claim_waits_on_loop")
def test_a_claim_with_no_free_slot_waits_about_250_ms_then_gets_503(svc: Service) -> None:
    name = svc.open(mocks=mocked("x"))
    for _ in range(CAP):
        assert svc.client.slots.acquire(blocking=False)
    try:

        async def timed() -> tuple[Any, float, int]:
            ticks = [0]

            async def tick() -> None:  # the loop keeps running while the claim waits
                while True:
                    ticks[0] += 1
                    await asyncio.sleep(0.01)

            ticker = asyncio.create_task(tick())
            began = time.monotonic()
            reply = await drive(gated_app(), headers=[svc.header(name)])
            ticker.cancel()
            return reply, time.monotonic() - began, ticks[0]

        reply, waited, ticks = run(timed())
    finally:
        for _ in range(CAP):
            svc.client.slots.release()
    assert refused_code(reply) == "plan-unavailable"
    assert 0.2 <= waited < 1.0 and ticks >= 10
    assert svc.client.load_since(0)[0] == 1  # turned away once, counted once


@pytest.mark.design("service.nonce_queue")
def test_the_nonce_store_drops_expired_entries_without_a_full_sweep() -> None:
    store = protocol.NonceStore()
    for i in range(10_000):
        assert store.first_use(f"n{i}", now=float(i) / 100)
    assert not store.first_use("n9999", now=100.0)
    # Each call drops only the expired entries at the front, then looks once.
    late = 100.0 + protocol.NONCE_KEEP_S
    assert store.first_use("fresh", now=late)
    assert len(store._order) < 10_000 and "n0" not in store._seen
    began = time.perf_counter()
    for i in range(10_000):
        store.first_use(f"m{i}", now=late)
    assert time.perf_counter() - began < 1.0


@pytest.mark.design("service.control_load")
def test_the_last_batch_counts_control_busy_and_control_p99_ms(svc: Service) -> None:
    real_handle = svc.server.handle

    def slow(*args: Any) -> Any:
        time.sleep(0.3)  # a control server under load: each request is held 300 ms
        return real_handle(*args)

    svc.server.handle = slow  # type: ignore[method-assign]
    names = [svc.open(mocks=mocked("x")) for _ in range(CAP + 1)]

    async def nine() -> list[Any]:  # a ninth claim beside eight: no slot frees in 250 ms
        return await asyncio.gather(*(drive(gated_app(), headers=[svc.header(n)]) for n in names))

    replies = run(nine())
    assert sorted(r.status for r in replies) == [200] * CAP + [503]
    claimed = [n for n, r in zip(names, replies, strict=True) if r.status == 200]
    for name in claimed:
        svc.closed(name)
    loaded = [svc.server.runs[n] for n in claimed]
    assert all((r.control_busy or 0) >= 1 for r in loaded)
    # Each request was held 300 ms, so every round trip, and its p99, is at least that.
    assert all((r.control_p99_ms or 0) >= 300 for r in loaded)
    svc.server.handle = real_handle  # type: ignore[method-assign]
    quiet = svc.open(mocks=mocked("x"))
    assert run(drive(gated_app(), headers=[svc.header(quiet)])).status == 200
    svc.closed(quiet)
    calm = svc.server.runs[quiet]
    # Measured, and never refused for no free slot. Its time is the box's, not a bound.
    assert calm.control_busy == 0 and calm.control_p99_ms is not None


@pytest.mark.design("service.grace")
def test_a_call_in_the_grace_window_is_in_the_record_and_one_after_is_refused(
    svc: Service,
) -> None:
    name = svc.open(mocks=mocked("x"), grace=1.0)
    holder: list[state.RunState] = []

    async def remember(scope: Any, receive: Any, send: Any) -> None:
        current = state.current()
        assert current is not None
        holder.append(current)
        await app(scope, receive, send)

    assert run(drive(asgi_gate.Entry(remember), headers=[svc.header(name)])).status == 200
    context = contextvars.copy_context()
    context.run(state.SLOT.set, state.Joined(name, holder[0]))
    # Well after the reply, still inside the 1 s grace: a record closed at the reply would
    # have closed by now. A time is what this checks, so the wait is a time.
    threading.Event().wait(0.3)
    assert context.run(GATED, "2") == "order for x"  # in the grace window: the run's own
    lines = svc.closed(name)
    assert sum(1 for x in lines if x.get("call") == "tool:get_order") == 2
    with pytest.raises(Exception, match="has ended"):
        context.run(GATED, "3")


@pytest.mark.design("service.closed_before_last_batch")
def test_a_call_made_while_the_last_batch_is_sent_is_refused(
    svc: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scrimmage_core.client import RunSender

    name = svc.open(mocks=mocked("x"))
    holder: list[state.RunState] = []
    during: list[str] = []
    close = RunSender.close

    async def remember(scope: Any, receive: Any, send: Any) -> None:
        holder.append(cast(state.RunState, state.current()))
        await app(scope, receive, send)

    def sending(self: RunSender) -> None:
        context = contextvars.copy_context()
        context.run(state.SLOT.set, state.Joined(name, holder[0]))
        try:
            during.append(context.run(GATED, "late"))
        except Exception as exc:  # the refusal the agent would see
            during.append(str(exc))
        close(self)

    monkeypatch.setattr(RunSender, "close", sending)
    assert run(drive(asgi_gate.Entry(remember), headers=[svc.header(name)])).status == 200
    lines = svc.closed(name)
    assert len(during) == 1 and "has ended" in during[0]
    calls = [x["action"] for x in lines if x.get("call") == "tool:get_order"]
    assert calls == ["mock", "refused"]  # the late call is refused, and still recorded


@pytest.mark.design("service.orphan_reported")
def test_a_forced_lost_plan_is_an_orphan_its_write_is_refused_and_the_run_is_an_infrastructure_error(  # noqa: E501  # the design's name
    svc: Service,
) -> None:
    import threading

    from scrimmage_core import runner

    name = svc.open(mocks=mocked("x"), grace=0.3)
    told: list[str] = []

    async def loses_it(scope: Any, receive: Any, send: Any) -> None:
        def worker() -> None:
            try:
                contextvars.Context().run(GATED, "1")  # the plan's context is lost here
            except Exception as exc:
                told.append(str(exc))

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        await app(scope, receive, send)

    assert run(drive(asgi_gate.Entry(loses_it), headers=[svc.header(name)])).status == 200
    lines = svc.closed(name)
    assert "outside that run's context" in told[0]
    assert any(x.get("reason") == "orphan" for x in lines)
    assert runner.context_lost(lines)


@pytest.mark.design("service.gates_once")
def test_both_gates_present_handle_a_request_once(svc: Service) -> None:
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from scrimmage_core.asgi_gate import patch_starlette

    async def chat(request: Any) -> Any:
        return JSONResponse({"answer": GATED("1")})

    patch_starlette(__import__("starlette.applications", fromlist=["x"]), svc.process)
    inner = Starlette(routes=[Route("/chat", chat, methods=["POST"])])
    name = svc.open(mocks=mocked("both"))
    counting = Counting(svc)
    reply = run(drive(asgi_gate.Entry(inner), headers=[svc.header(name)]))
    assert reply.status == 200 and json.loads(reply.body)["answer"] == "order for both"
    assert counting.count == 1  # one claim: the inner gate passed the call on as it was


@pytest.mark.design("service.starlette_alone")
def test_starlette_gate_alone_under_testclient(svc: Service) -> None:
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from scrimmage_core.asgi_gate import patch_starlette

    async def chat(request: Any) -> Any:
        try:
            answer = GATED("1")
        except AssertionError as real:  # the real tool ran: untouched, as for a plain request
            answer = str(real)
        except Exception as refused:  # the outside rules refused it
            answer = str(refused)
        return JSONResponse({"answer": answer, "headers": dict(request.headers)})

    patch_starlette(__import__("starlette.applications", fromlist=["x"]), svc.process)
    # The web gate too, as in a service: the test client's own requests go to the app in
    # this process, so they are no web write; the app then runs as its caller, here code
    # outside any request.
    http_gate.patch(httpx2, http_gate.Context(svc.process, Budget(), None, svc.process.stop))
    web = Starlette(routes=[Route("/chat", chat, methods=["POST"])])
    name = svc.open(mocks=mocked("alone"))
    header, value = svc.header(name)
    with TestClient(web) as client:
        got = client.post("/chat", headers={header.decode(): value.decode()})
        eventually(lambda: not svc.process.open_runs())  # the run's record has closed
        alone = client.post("/chat")  # no run open: untouched, the real tool runs
        plan = Plan("run-0123456789ab", "t", "baseline", (), (), (), (), 60.0, service=True)
        held = state.RunState(plan, Sink(), [], process=svc.process)
        svc.process.register(held)  # a run open in this process while the next request goes
        outside = client.post("/chat")
        svc.process.unregister(held)
    assert got.json()["answer"] == "order for alone"
    assert not any(k.startswith("x-scrimmage") for k in got.json()["headers"])
    assert "must never run" in alone.json()["answer"]
    # With a run open, the app runs as its caller: the outside rules refuse its write, and
    # the open run is told. The test client's own request is no web write.
    assert "outside any request" in outside.json()["answer"]
    assert [(x["call"], x["reason"]) for x in sink_of(held).lines] == [("tool:get_order", "orphan")]


@pytest.mark.design("service.token_file_popped")
def test_only_a_process_that_serves_the_app_pops_the_token_path_and_says_it_is_active(
    svc: Service, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    monkeypatch.setenv("SCRIMMAGE_TOKEN_FILE", "/run/token")
    asgi_gate.active("starlette")  # a supervisor imports starlette and loads no app
    asgi_gate.patch_starlette(__import__("starlette.applications", fromlist=["x"]), svc.process)

    async def ok(request: Any) -> Any:
        return PlainTextResponse("ok")

    web = Starlette(routes=[Route("/ok", ok)])
    assert os.environ["SCRIMMAGE_TOKEN_FILE"] == "/run/token"
    assert "active in service mode" not in capsys.readouterr().err
    with TestClient(web) as client:  # its first call serves the app
        assert client.get("/ok").text == "ok"
        assert client.get("/ok").text == "ok"
    assert "SCRIMMAGE_TOKEN_FILE" not in os.environ
    assert capsys.readouterr().err.count("active in service mode") == 1


@pytest.mark.design("service.token_file_popped")
def test_uvicorn_loading_the_app_pops_the_token_path_and_says_it_is_active(
    svc: Service, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Config:  # uvicorn's, as far as the gate reads it
        http = "auto"
        http2 = False

        def load(self) -> None:
            self.loaded_app = app

    module = types.ModuleType("uvicorn.config")
    module.Config = Config  # type: ignore[attr-defined]
    asgi_gate.patch_uvicorn(module, svc.process)
    monkeypatch.setenv("SCRIMMAGE_TOKEN_FILE", "/run/token")
    config = Config()
    config.load()
    assert isinstance(config.loaded_app, asgi_gate.Entry)
    assert "SCRIMMAGE_TOKEN_FILE" not in os.environ
    assert capsys.readouterr().err.count("active in service mode") == 1


@pytest.mark.design("service.closes_own_threads")
def test_closes_waiting_for_every_slot_never_hold_up_the_claims_that_hold_them(
    svc: Service,
) -> None:
    from scrimmage_core.client import RunSender

    closing, claiming = [svc.open() for _ in range(CAP)], [svc.open() for _ in range(CAP)]
    for name in closing:
        svc.client.claim(name, {"pid": 1})
    go, entered, closed = threading.Event(), threading.Semaphore(0), []

    def close(name: str) -> None:
        entered.release()
        go.wait(10)
        sender = RunSender(svc.client, name, {"pid": 1})
        sender.plan_of = lambda: {"run": name}  # a claimed run's last batch names its plan
        sender.close()  # waits for a slot on its thread
        closed.append(name)

    for name in closing:
        svc.pool.timers.later(0, lambda n=name: close(n))
    for _ in closing:
        assert entered.acquire(timeout=10)  # every close is on a thread of its own, waiting
    # Claims arrive on the loop: each takes a slot, then hands its work to the claim threads.
    assert all(svc.client.try_slot(count=False) for _ in claiming)
    claims = [
        svc.pool.submit(lambda n=name: svc.client.run_held(lambda: svc.client.claim(n, {})))
        for name in claiming
    ]
    go.set()
    for claim in claims:
        claim.result(10)
    eventually(lambda: len(closed) == CAP, within=10)
    assert all(svc.server.runs[name].made for name in closing)  # each last batch arrived


@pytest.mark.design("service.closes_own_threads")
def test_a_claim_thread_that_asks_for_a_slot_it_does_not_hold_fails_at_once(
    svc: Service,
) -> None:
    from scrimmage_core.client import ControlError

    name = svc.open()
    with pytest.raises(ControlError, match="did not hold"):
        svc.pool.submit(lambda: svc.client.claim(name, {})).result(10)


@pytest.mark.design("service.closes_own_threads")
def test_a_budget_release_on_a_claim_thread_is_sent_from_a_thread_that_may_wait(
    svc: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[tuple[str, object]] = []
    request = svc.client.request

    def watched(method: str, path: str, payload: object = None, **kw: Any) -> Any:
        sent.append((threading.current_thread().name, payload))
        return request(method, path, payload, **kw)

    monkeypatch.setattr(svc.client, "request", watched)
    svc.pool.submit(lambda: svc.client.release(7)).result(10)
    eventually(lambda: sent)
    assert sent[0][1] == {"ticket": 7} and not sent[0][0].startswith("scrimmage-control")


@pytest.mark.design("service.websocket")
def test_a_websocket_with_the_run_header_is_closed(svc: Service) -> None:
    name = svc.open()
    reply = run(drive(gated_app(), headers=[svc.header(name)], scope_type="websocket"))
    assert reply.messages == [{"type": "websocket.close", "code": 1008}]
    assert seen_scopes == []


@pytest.mark.design("service.forked_worker_stops")
def test_a_forked_worker_such_as_gunicorns_stops_at_its_first_serve(svc: Service) -> None:
    svc.gate.proc = {**svc.gate.proc, "pid": os.getpid() + 1}  # as a fork sees its parent's
    asgi_gate.serving()
    assert svc.stopped and "a fork of the one Scrimmage started in" in svc.stopped[0]


@pytest.mark.design("service.in_process_runs_as_caller")
def test_an_app_in_this_process_runs_as_its_caller_and_only_exact_transports_are_in_process(
    svc: Service,
) -> None:
    http_gate.patch(httpx2, http_gate.Context(svc.process, Budget(), None, svc.process.stop))
    wrote: list[str] = []

    def write_order(order: str) -> str:
        wrote.append(order)
        return f"wrote {order}"

    write = tool_gate.gate(write_order, "write_order")

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        try:
            body = write("1").encode()
        except Exception as exc:  # the refusal, as the app would see it
            body = str(exc).encode()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": body})

    class Forwarding(httpx2.ASGITransport):
        """A subclass, which could send anywhere: not taken as in process."""

    async def post(transport: Any) -> str:
        async with httpx2.AsyncClient(transport=transport) as web:
            return (await web.post("http://app.test/orders")).text

    gated = asgi_gate.Entry(inner)
    plan = Plan("run-0123456789ab", "t", "baseline", (), (), (), (), 60.0, service=True)
    held = state.RunState(plan, Sink(), [], process=svc.process)
    svc.process.register(held)
    try:
        outside = contextvars.Context()
        # Code outside any request: its in-process request is no web write, and the app
        # runs as that code, so the app's write meets the outside rules.
        replied = outside.run(asyncio.run, post(httpx2.ASGITransport(gated)))
        assert "outside any request" in replied and wrote == []
        with pytest.raises(Exception, match="outside any request"):
            outside.run(asyncio.run, post(Forwarding(gated)))
        assert [x["call"] for x in sink_of(held).lines] == ["tool:write_order", "http:app.test"]
        # A run's own in-process request is decided by its plan, as any web write is.
        inside = contextvars.copy_context()
        inside.run(state.SLOT.set, state.Joined(held.name, held))
        with pytest.raises(Exception, match=r"http:app\.test"):
            inside.run(asyncio.run, post(httpx2.ASGITransport(gated)))
        assert wrote == []
    finally:
        svc.process.unregister(held)


@pytest.mark.design("service.starlette_runs_as_caller")
def test_a_hand_made_call_to_the_app_runs_as_its_caller_where_uvicorns_gate_is(
    svc: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With uvicorn's gate installed, every request it serves has its slot set, so a call
    that reaches Starlette's gate without one was made by code in this process: outside any
    request, the app's write meets the outside rules, never a fresh plain request."""
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    from scrimmage_core.asgi_gate import patch_starlette

    wrote: list[str] = []

    def write_order(order: str) -> str:
        wrote.append(order)
        return f"wrote {order}"

    write = tool_gate.gate(write_order, "write_order")

    async def direct(request: Any) -> Any:
        try:
            return PlainTextResponse(write("1"))
        except Exception as exc:  # the refusal, as the app would see it
            return PlainTextResponse(str(exc))

    patch_starlette(__import__("starlette.applications", fromlist=["x"]), svc.process)
    web = Starlette(routes=[Route("/direct", direct, methods=["POST"])])
    monkeypatch.setattr(svc.gate, "gates", [*svc.gate.gates, "asgi"])  # as uvicorn's load does
    plan = Plan("run-0123456789ab", "t", "baseline", (), (), (), (), 60.0, service=True)
    held = state.RunState(plan, Sink(), [], process=svc.process)
    svc.process.register(held)
    try:
        reply = contextvars.Context().run(run, drive(web, path="/direct"))
        assert "outside any request" in reply.body.decode() and wrote == []
        assert [x["call"] for x in sink_of(held).lines] == ["tool:write_order"]
    finally:
        svc.process.unregister(held)


@pytest.mark.design("service.runs_apart")
def test_a_slow_claim_counts_in_the_runs_age(svc: Service, monkeypatch: pytest.MonkeyPatch) -> None:
    """The run's age starts when the request for its plan starts, so the time the claim takes
    is in it. A wait on another run's hold that is cut by the age then never ends past the
    runner's limit. Counted after the claim, the run would look 0.5 s younger."""
    name = svc.open()
    real_claim = svc.client.claim

    def slow_claim(run: str, proc: object, *, held: bool = False) -> object:
        plan = real_claim(run, proc, held=held)  # the runner's age is read here
        time.sleep(0.5)  # then the reply is slow to arrive
        return plan

    made: list[state.RunState] = []

    def spy(*args: Any, **kwargs: Any) -> state.RunState:
        made.append(state.RunState(*args, **kwargs))
        return made[-1]

    monkeypatch.setattr(svc.client, "claim", slow_claim)
    monkeypatch.setattr(asgi_gate, "RunState", spy)
    before = time.monotonic()
    reply = run(drive(gated_app(), headers=[svc.header(name)]))
    assert reply.status == 200
    assert len(made) == 1
    assert made[0].began <= before + 0.2, made[0].began - before
