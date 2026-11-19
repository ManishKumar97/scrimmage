"""A real failure against the injected one: the code that meets it cannot tell.

A real get_order that times out, real refused, reset and unresolvable connections on
httpx and httpx2, sync and async, a real broken JSON reply and a real empty one, and a
real rate limiter's Retry-After."""

import asyncio
import gzip
import http.server
import json
import shutil
import socket
import struct
import threading
import time
import types
import warnings
from collections.abc import Callable, Iterator
from typing import Any, cast

import openai
import openai._base_client as openai_base
import pytest
from pydantic import SecretStr

from conftest import ACME, EXAMPLE_FAULT, Project
from fakes.openai_server import FakeOpenAI
from scrimmage_core import changes, http_gate, state
from scrimmage_core import provider_openai as openai_table
from test_connection_faults import LIBRARIES, PATH_IDS, PATHS, WEB, gated, install, raised
from test_reply_faults import (
    FIXTURES,
    JSON_HEADERS,
    MODEL,
    QUESTION,
    REAL_ANSWER,
    REAL_TOOL_CALL,
    SPECS,
    SYNC_IDS,
    THAI,
    Loopback,
    as_real,
    but_tools,
    completion,
    fields_of,
    gate_on,
    loopback,
    model_lines,
    response_fields,
    responses_api,
)

__all__ = ["gated", "loopback"]  # the fixtures, used by name

# Every test here, or nearly, starts a process or a real scrimmage test run.
pytestmark = pytest.mark.slow

Fake = Callable[[list[str]], FakeOpenAI]


@pytest.fixture
def silent_orders_system() -> Iterator[str]:
    """Accepts connections and never answers, like a hung orders system."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(16)
    yield f"127.0.0.1:{server.getsockname()[1]}"
    server.close()


def told(fake: FakeOpenAI) -> set[str]:
    """What the bot told the model about failed tools: "error: <exception type>"."""
    return {
        ":".join(str(m["content"]).split(":")[:2])
        for request in fake.requests
        for m in request["messages"]
        if m.get("role") == "tool" and str(m["content"]).startswith("error")
    }


@pytest.mark.design("proof.real_against_injected_tool_timeout")
def test_a_real_timeout_and_the_injected_one_reach_the_loop_the_same_way(
    project: Project,
    fake_openai: Fake,
    silent_orders_system: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Injected: the example's fault row, runs = 1.
    project.edit("[agent]\n", "[agent]\nruns = 1\n")
    injected_fake = fake_openai(ACME)
    injected = project.test(injected_fake)
    fault_run = next(lines for lines in injected.records() if lines[-1]["row"] == "fault 1")

    # Real: get_order runs for real against a system that never answers.
    shutil.rmtree(project.root / "scrimmage-runs")
    project.edit(
        'real     = ["tool:check_inventory"]',
        'real     = ["tool:check_inventory", "tool:get_order"]',
    )
    project.edit('[[test.mock]]\ncall    = "tool:get_order"\nreturns = "shipped, Oct 30"\n', "")
    project.edit(EXAMPLE_FAULT, "")
    monkeypatch.setenv("ACME_ORDERS", silent_orders_system)
    real_fake = fake_openai(ACME)
    real = project.test(real_fake)
    (real_run,) = real.records()

    # The same exception type reaches the loop.
    def errors(lines: list[dict[str, object]]) -> list[str]:
        return [str(x["error"]).split(":")[0] for x in lines if x.get("call") == "tool:get_order"]

    assert errors(fault_run) == errors(real_run) == ["TimeoutError"]
    # The loop takes the same branch: it tells the model the tool failed with that type.
    assert told(injected_fake) == told(real_fake) == {"error: TimeoutError"}

    # The records' actions match, apart from real against fault.
    def actions(lines: list[dict[str, object]]) -> list[tuple[object, object]]:
        return [(x["call"], x["action"]) for x in lines[:-1] if x.get("kind") == "tool"]

    assert actions(fault_run) == [
        ("tool:get_order", "fault"),
        ("tool:get_stock", "mock"),
    ]
    assert actions(real_run) == [("tool:get_order", "real"), ("tool:get_stock", "mock")]
    assert real.code == 1 and injected.code == 0


@pytest.fixture
def closed_port() -> str:
    """A loopback port nothing listens on, so a connect to it is refused."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return f"http://127.0.0.1:{port}/stock/SKU-42"


@pytest.fixture
def resetting_server() -> Iterator[str]:
    """Reads each request, then closes with SO_LINGER 0, which sends a TCP RST."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(16)
    server.settimeout(0.1)  # so the loop sees stop without a connection to wake it
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except TimeoutError:
                continue
            conn.settimeout(5)
            conn.recv(65536)
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.getsockname()[1]}/stock/SKU-42"
    stop.set()
    thread.join(5)
    server.close()


def real(name: str, sync: bool, url: str) -> BaseException:
    """The failure the library raises with no run, through a real transport."""
    module: Any = LIBRARIES[name]
    state.install(None)
    with pytest.raises(Exception) as caught:
        if sync:
            module.Client().get(url)
        else:

            async def go() -> None:
                async with module.AsyncClient() as client:
                    await client.get(url)

            asyncio.run(go())
    return caught.value


def injected(word: str, name: str, sync: bool) -> BaseException:
    """The fault on the same library and path, with nothing sent."""
    install(word)
    return raised(name, sync)


def name_of(error: BaseException | None) -> str | None:
    return None if error is None else f"{type(error).__module__}.{type(error).__qualname__}"


def chain(error: BaseException) -> list[tuple[str | None, ...]]:
    """Each link from the top down: its class, the class of its __cause__ and __context__,
    and __suppress_context__. anyio's BrokenResourceError has an IndexError from its own
    buffer as context, hidden behind its cause; the gate does not copy it."""
    links: list[tuple[str | None, ...]] = []
    link: BaseException | None = error
    while link is not None:
        context = link.__context__
        if type(link).__name__ == "BrokenResourceError" and link.__cause__ is not None:
            context = None
        links.append(
            (
                name_of(link),
                name_of(link.__cause__),
                name_of(context),
                str(link.__suppress_context__),
            )
        )
        link = link.__cause__ if link.__cause__ is not None else link.__context__
    return links


def root(error: BaseException) -> BaseException:
    while (below := error.__cause__ or error.__context__) is not None:
        error = below
    return error


def alike(word: str, real_error: BaseException, fault_error: BaseException) -> None:
    """The whole chain has the same shape, and the OSError at its end the same errno."""
    assert chain(fault_error) == chain(real_error), word
    real_root, fault_root = root(real_error), root(fault_error)
    assert isinstance(real_root, OSError) and isinstance(fault_root, OSError)
    assert fault_root.errno == real_root.errno


@pytest.mark.design("proof.real_against_injected_refused")
@pytest.mark.parametrize(("name", "sync"), PATHS, ids=PATH_IDS)
def test_a_real_refused_connection_and_the_injected_one_look_the_same(
    gated: None, closed_port: str, name: str, sync: bool
) -> None:
    real_error = real(name, sync, closed_port)
    fault_error = injected("refused", name, sync)
    alike("refused", real_error, fault_error)
    assert str(fault_error) == str(real_error)


@pytest.mark.design("proof.real_against_injected_reset")
@pytest.mark.parametrize(("name", "sync"), PATHS, ids=PATH_IDS)
def test_a_real_reset_and_the_injected_one_look_the_same(
    gated: None, resetting_server: str, name: str, sync: bool
) -> None:
    real_error = real(name, sync, resetting_server)
    fault_error = injected("reset", name, sync)
    alike("reset", real_error, fault_error)
    assert str(fault_error) == str(real_error)


@pytest.mark.design("proof.real_against_injected_dns")
@pytest.mark.parametrize(("name", "sync"), PATHS, ids=PATH_IDS)
def test_a_real_unresolvable_name_and_the_injected_one_look_the_same(
    gated: None, name: str, sync: bool
) -> None:
    real_error = real(name, sync, "http://no-such-host.invalid/stock/SKU-42")
    if getattr(root(real_error), "errno", None) == socket.EAI_AGAIN:
        pytest.skip("this machine has no resolver: the real lookup failed with EAI_AGAIN")
    fault_error = injected("dns", name, sync)
    alike("dns", real_error, fault_error)
    assert type(root(real_error)) is socket.gaierror
    # The text after the errno comes from the resolver, so only "[Errno N]" is compared.
    assert str(fault_error).partition("] ")[0] == str(real_error).partition("] ")[0]


class Fixed(http.server.BaseHTTPRequestHandler):
    """Answers every request with the reply its server was given, after reading the request."""

    def answer(self) -> None:
        self.rfile.read(int(self.headers.get("content-length") or 0))
        headers, body = cast("FixedServer", self.server).reply
        self.send_response(200)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = answer

    def log_message(self, format: str, *args: object) -> None:
        pass


class FixedServer(http.server.ThreadingHTTPServer):
    reply: tuple[dict[str, str], bytes]


@pytest.fixture
def fixed_server() -> Iterator[Callable[[dict[str, str], bytes], str]]:
    """Starts loopback servers that each send one set reply; gives each one's base URL."""
    servers: list[FixedServer] = []

    def start(headers: dict[str, str], body: bytes) -> str:
        server = FixedServer(("127.0.0.1", 0), Fixed)
        server.reply = (headers, body)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def fetched(name: str, sync: bool, url: str) -> Any:
    """A GET through that library and path, read to its end."""
    module: Any = LIBRARIES[name]
    if sync:
        return module.Client().get(url)

    async def go() -> Any:
        async with module.AsyncClient() as client:
            return await client.get(url)

    return asyncio.run(go())


def parsed(response: Any) -> tuple[str, int]:
    """What reading the reply as JSON raises: its class and pos, never its message, whose
    wording has changed between Python releases."""
    with pytest.raises(json.JSONDecodeError) as caught:
        response.json()
    return type(caught.value).__name__, caught.value.pos


def seen(response: Any) -> tuple[object, ...]:
    """What code reads of a reply, apart from the headers every server adds, such as Date."""
    headers = response.headers
    return (
        response.status_code,
        response.content,
        headers.get("content-type"),
        headers.get("content-length"),
        parsed(response),
    )


def chat(base_url: str) -> BaseException:
    """One chat call through the openai library; what it raised."""
    api = openai.OpenAI(api_key="test", base_url=base_url)
    with pytest.raises(Exception) as caught:
        api.chat.completions.create(
            model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}]
        )
    return caught.value


@pytest.mark.design("proof.real_against_injected_bad_body")
@pytest.mark.parametrize(("name", "sync"), PATHS, ids=PATH_IDS)
def test_a_real_broken_json_reply_and_bad_body_look_the_same(
    gated: None, fixed_server: Callable[[dict[str, str], bytes], str], name: str, sync: bool
) -> None:
    """On a web request on each path, and on a model call through the openai library."""
    web = {"content-type": "application/json; charset=utf-8"}
    real_web = fetched(name, sync, fixed_server(web, http_gate.BROKEN_WEB_BODY) + "/stock/1")
    install("bad body")
    assert seen(fetched(name, sync, WEB)) == seen(real_web)
    if (name, sync) != ("httpx2", True):  # the openai library sends through httpx2's client
        return
    state.install(None)
    model = {"content-type": "application/json", "x-request-id": "req_1"}
    real_model = chat(fixed_server(model, openai_table.BROKEN_BODY) + "/v1")
    sink, _ = install("bad body", "model")
    fault_model = chat("https://api.openai.com/v1")
    assert type(fault_model) is type(real_model) is json.JSONDecodeError
    assert isinstance(fault_model, json.JSONDecodeError)
    assert isinstance(real_model, json.JSONDecodeError)
    assert fault_model.pos == real_model.pos
    # A 200 is never retried, real or injected, so one attempt has a line.
    assert [x["action"] for x in sink.lines if x.get("kind") == "model"] == ["fault"]


@pytest.mark.design("proof.real_against_injected_empty")
@pytest.mark.parametrize(("name", "sync"), PATHS, ids=PATH_IDS)
def test_a_real_empty_reply_and_the_injected_one_look_the_same(
    gated: None, fixed_server: Callable[[dict[str, str], bytes], str], name: str, sync: bool
) -> None:
    real_reply = fetched(name, sync, fixed_server({}, b"") + "/stock/1")
    install("empty")
    assert seen(fetched(name, sync, WEB)) == seen(real_reply)
    assert seen(real_reply)[:4] == (200, b"", None, "0")


class Limited(http.server.BaseHTTPRequestHandler):
    """A rate limiter: every request gets a 429 with its server's Retry-After."""

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("content-length") or 0))
        server = cast("LimitedServer", self.server)
        server.hits += 1
        body = openai_table.error_body(429)
        self.send_response(429)
        self.send_header("content-type", "application/json")
        self.send_header("x-request-id", "req_1")
        self.send_header("retry-after", server.retry_after)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


class LimitedServer(http.server.ThreadingHTTPServer):
    retry_after: str
    hits: int = 0


@pytest.fixture
def waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The openai library's waits between attempts, kept and not slept, so a 30 s wait costs
    nothing. Only the library's own reference to time is replaced; the wait it computes is
    the one it would sleep."""
    kept: list[float] = []
    clock = types.SimpleNamespace(sleep=kept.append, time=time.time)
    monkeypatch.setattr(openai_base, "time", clock)
    return kept


@pytest.mark.design("proof.real_against_injected_retry_after")
@pytest.mark.parametrize("wait", [30, 121])
def test_a_real_retry_after_and_the_injected_one_make_the_library_wait_the_same(
    gated: None, waits: list[float], wait: int
) -> None:
    """At 30 s the library waits 30 s before each of its two retries; over 120 s it does
    not retry at all. Real and injected alike."""
    server = LimitedServer(("127.0.0.1", 0), Limited)
    server.retry_after = str(wait)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        state.install(None)
        url = f"http://127.0.0.1:{server.server_address[1]}/v1"
        real_error = chat(url)
    finally:
        server.shutdown()
        server.server_close()
    real_waits, real_attempts = list(waits), server.hits
    waits.clear()
    sink, _ = install("status 429", "model", retry_after=wait)
    fault_error = chat("https://api.openai.com/v1")
    fault_attempts = len([x for x in sink.lines if x.get("action") == "fault"])
    assert type(fault_error) is type(real_error) is openai.RateLimitError
    assert (waits, fault_attempts) == (real_waits, real_attempts)
    assert (real_waits, real_attempts) == (([float(wait)] * 2, 3) if wait <= 120 else ([], 1))
    assert isinstance(fault_error, openai.RateLimitError)
    assert isinstance(real_error, openai.RateLimitError)
    assert fault_error.response.headers["retry-after"] == real_error.response.headers["retry-after"]


# The faults that change a real model reply, against a real reply of that kind. Both sides go
# through the openai library, and LangChain, over a real socket from the same loopback server.
REAL_TRUNCATED = json.loads((FIXTURES / "real_chat_truncated.json").read_text())
# Two real cuts inside a tool call, one with tool_choice required and one with it left out.
REAL_CUT_CALLS = [
    pytest.param(
        json.loads((FIXTURES / "real_chat_tool_call_truncated.json").read_text()),
        {"tool_choice": "required"},
        id="required",
    ),
    pytest.param(
        json.loads((FIXTURES / "real_chat_tool_call_truncated_auto.json").read_text()),
        {},
        id="auto",
    ),
]


def parse_error(server: Loopback, sync: bool) -> BaseException | None:
    """What client.chat.completions.parse raised on the server's reply, if anything."""
    kw: dict[str, Any] = {"model": MODEL, "messages": QUESTION}
    try:
        if sync:
            openai.OpenAI(
                base_url=server.url, api_key="test", max_retries=0
            ).chat.completions.parse(**kw)
        else:

            async def go() -> None:
                api = openai.AsyncOpenAI(base_url=server.url, api_key="test", max_retries=0)
                await api.chat.completions.parse(**kw)

            asyncio.run(go())
    except Exception as exc:
        return exc
    return None


def by_langchain(server: Loopback, sync: bool, **bound: Any) -> Any:
    """LangChain's ChatOpenAI on the same server, with these tools: the message, or what it
    raised."""
    with warnings.catch_warnings():
        # langsmith, which langchain_openai loads, calls an asyncio function Python 3.14
        # deprecates, and this suite turns warnings into errors.
        warnings.filterwarnings(
            "ignore", "'asyncio.iscoroutinefunction' is deprecated", DeprecationWarning
        )
        from langchain_openai import ChatOpenAI

        model: Any = ChatOpenAI(
            model=MODEL, base_url=server.url, api_key=SecretStr("test"), max_retries=0
        )
        if bound:
            model = model.bind_tools(bound["tools"])
        try:
            if sync:
                return model.invoke("Where is order 1234?")
            return asyncio.run(model.ainvoke("Where is order 1234?"))
        except Exception as exc:
            return exc


def status_error(server: Loopback, sync: bool, **request: Any) -> openai.APIStatusError:
    """The openai library's error on the server's reply, with its default retries: a 400 is
    not retried, real or injected."""
    kw: dict[str, Any] = {"model": MODEL, "messages": QUESTION, **request}
    with pytest.raises(openai.APIStatusError) as caught:
        if sync:
            openai.OpenAI(base_url=server.url, api_key="test").chat.completions.create(**kw)
        else:

            async def go() -> None:
                await openai.AsyncOpenAI(
                    base_url=server.url, api_key="test"
                ).chat.completions.create(**kw)

            asyncio.run(go())
    return caught.value


@pytest.mark.design("proof.real_against_injected_truncate")
@pytest.mark.parametrize("sync", [True, False], ids=SYNC_IDS)
def test_a_real_truncated_reply_and_truncate_parse_the_same(
    gated: None, loopback: Loopback, sync: bool
) -> None:
    """The capture is a real reply cut by max_completion_tokens = 5; the injected one is
    truncate on a real whole answer. parse() raises LengthFinishReasonError on both."""
    gate_on("truncate")
    injected, _ = completion(loopback, sync)
    injected_error = parse_error(loopback, sync)
    injected_chain = by_langchain(loopback, sync)
    assert loopback.seen == ["/v1/chat/completions"] * 3  # the real call is made
    as_real(loopback, json.dumps(REAL_TRUNCATED["body"]).encode(), REAL_TRUNCATED["status"])
    real, _ = completion(loopback, sync)
    real_error = parse_error(loopback, sync)
    real_chain = by_langchain(loopback, sync)
    assert fields_of(injected) == fields_of(real)
    assert injected.choices[0].finish_reason == real.choices[0].finish_reason == "length"
    assert type(injected_error) is type(real_error) is openai.LengthFinishReasonError
    assert isinstance(injected_error, openai.LengthFinishReasonError)
    assert isinstance(real_error, openai.LengthFinishReasonError)
    assert fields_of(injected_error.completion) == fields_of(real_error.completion)
    # LangChain reads both as a message whose finish_reason says it was cut.
    assert type(injected_chain) is type(real_chain)
    assert injected_chain.response_metadata["finish_reason"] == "length"
    assert real_chain.response_metadata["finish_reason"] == "length"


@pytest.mark.design("proof.real_against_injected_truncate_tool_call")
@pytest.mark.parametrize("sync", [True, False], ids=SYNC_IDS)
@pytest.mark.parametrize(("capture", "choice"), REAL_CUT_CALLS)
def test_a_real_truncated_tool_call_and_truncate_parse_the_same(
    gated: None,
    loopback: Loopback,
    capture: dict[str, Any],
    choice: dict[str, str],
    sync: bool,
) -> None:
    """A real limit inside a tool call is OpenAI's 400, saved on 18 and 19 November 2026 with
    tool_choice required and with it left to the model; truncate on
    a real tool call gives the agent the same 400. The openai library raises the same
    BadRequestError on both and does not retry it, and LangChain raises its own subclass of it
    on both."""
    sink, _ = gate_on("truncate")
    loopback.body = REAL_TOOL_CALL
    injected = status_error(loopback, sync, tools=SPECS, **choice)
    injected_chain = by_langchain(loopback, sync, tools=SPECS)
    assert loopback.seen == ["/v1/chat/completions"] * 2, "retried, or never sent"
    assert [x["action"] for x in model_lines(sink)] == ["fault", "fault"]
    as_real(loopback, json.dumps(capture["body"]).encode(), capture["status"])
    real = status_error(loopback, sync, tools=SPECS, **choice)
    real_chain = by_langchain(loopback, sync, tools=SPECS)
    assert loopback.seen == ["/v1/chat/completions"] * 2
    assert type(injected) is type(real) is openai.BadRequestError
    assert (injected.status_code, injected.body, injected.message, injected.code) == (
        real.status_code,
        real.body,
        real.message,
        real.code,
    )
    assert (injected.type, injected.param) == (real.type, real.param)
    assert injected.response.headers["content-type"] == real.response.headers["content-type"]
    assert type(injected_chain) is type(real_chain)  # its own subclass
    assert isinstance(injected_chain, openai.BadRequestError)
    assert str(injected_chain) == str(real_chain)


@pytest.mark.design("proof.real_against_injected_bad_tool_args")
@pytest.mark.parametrize("sync", [True, False], ids=SYNC_IDS)
def test_broken_arguments_parse_as_a_real_reply_of_that_kind(
    gated: None, loopback: Loopback, sync: bool
) -> None:
    """The real side is the real tool call with its arguments broken by hand, as the spec says a
    model may send them. json.loads fails at the same place, and LangChain puts both calls in
    invalid_tool_calls."""
    gate_on("bad tool args")
    loopback.body = REAL_TOOL_CALL
    injected, _ = completion(loopback, sync, tools=SPECS)
    injected_chain = by_langchain(loopback, sync, tools=SPECS)
    whole = b'"arguments": "{\\"order_id\\":\\"1234\\"}"'
    assert REAL_TOOL_CALL.count(whole) == 1
    by_hand = REAL_TOOL_CALL.replace(whole, b'"arguments": "{\\"order_id\\":\\"1234\\""')
    as_real(loopback, by_hand)
    real, _ = completion(loopback, sync, tools=SPECS)
    real_chain = by_langchain(loopback, sync, tools=SPECS)
    assert fields_of(injected) == fields_of(real)
    [injected_call] = injected.choices[0].message.tool_calls
    [real_call] = real.choices[0].message.tool_calls
    assert injected_call.model_dump() == real_call.model_dump()
    errors: list[tuple[str, int]] = []
    for call in (injected_call, real_call):
        with pytest.raises(json.JSONDecodeError) as caught:
            json.loads(call.function.arguments)
        errors.append((caught.value.msg, caught.value.pos))
    assert errors[0] == errors[1]
    assert injected_chain.tool_calls == real_chain.tool_calls == []
    assert injected_chain.invalid_tool_calls == real_chain.invalid_tool_calls
    [invalid] = real_chain.invalid_tool_calls
    assert (invalid["name"], invalid["args"]) == ("get_order", '{"order_id":"1234"')


GARBLED_ENCODINGS = [
    pytest.param("identity", id="plain"),
    pytest.param("gzip", id="gzip"),
    pytest.param(
        "br",
        id="br",
        marks=pytest.mark.skipif(
            changes.brotli_module() is None,
            reason="br needs brotli or brotlicffi 1.2 or later; run with uv run --with brotli",
        ),
    ),
]


@pytest.mark.design("proof.real_against_injected_garble")
@pytest.mark.parametrize("sync", [True, False], ids=SYNC_IDS)
@pytest.mark.parametrize("encoding", GARBLED_ENCODINGS)
def test_a_garbled_reply_parses_into_the_same_fields_as_the_real_one(
    gated: None, loopback: Loopback, encoding: str, sync: bool
) -> None:
    """garble on a real answer, sent plain, in gzip or in br: the openai library sets the same
    fields as on the real reply, and the gate's bytes sent again by a server with no run give
    the same object, so the gate's own reply is what a socket gives."""
    packed = {"identity": lambda b: b, "gzip": gzip.compress}
    pack = packed.get(encoding) or (lambda b: cast(Any, changes.brotli_module()).compress(b))
    loopback.headers = {**JSON_HEADERS, "content-encoding": encoding}
    loopback.body = pack(REAL_ANSWER)
    gate_on("garble")
    injected, sent = completion(loopback, sync)
    injected_chain = by_langchain(loopback, sync)
    assert THAI in injected.choices[0].message.content
    assert THAI in injected_chain.content
    state.install(None)
    real, _ = completion(loopback, sync)  # the real answer, as the model sent it
    assert fields_of(injected) == fields_of(real)
    loopback.headers = dict(JSON_HEADERS)
    as_real(loopback, sent)
    again, _ = completion(loopback, sync)
    assert again.model_dump() == injected.model_dump()
    assert by_langchain(loopback, sync).content == injected_chain.content


# The Responses API: a real reply cut by max_output_tokens = 16, and a real whole answer that
# truncate cuts. Both were saved from gpt-4o-mini on 18 November 2026.
REAL_INCOMPLETE = json.loads((FIXTURES / "real_response_incomplete.json").read_text())
REAL_RESPONSE = json.loads((FIXTURES / "real_response_answer.json").read_text())


@pytest.mark.design("proof.real_against_injected_truncate_responses")
@pytest.mark.parametrize("sync", [True, False], ids=SYNC_IDS)
def test_a_real_incomplete_response_and_truncate_parse_the_same(
    gated: None, loopback: Loopback, sync: bool
) -> None:
    """The capture is a real Responses reply cut by max_output_tokens; the injected one is
    truncate on a real whole answer. The output_text property and incomplete_details agree,
    and usage stays what the model was billed for."""
    gate_on("truncate")
    loopback.body = json.dumps(REAL_RESPONSE["body"]).encode()
    injected = responses_api(loopback, sync)
    assert loopback.seen == ["/v1/responses"]  # the real call is made
    as_real(loopback, json.dumps(REAL_INCOMPLETE["body"]).encode(), REAL_INCOMPLETE["status"])
    real = responses_api(loopback, sync)
    # tools echoes each request's own: the whole answer's offered one, the cut one's none.
    assert but_tools(response_fields(injected)) == but_tools(response_fields(real))
    assert injected.status == real.status == "incomplete"
    assert injected.incomplete_details == real.incomplete_details
    assert injected.incomplete_details.reason == "max_output_tokens"
    assert [i.status for i in injected.output] == [i.status for i in real.output] == ["incomplete"]
    assert injected.completed_at is None and real.completed_at is None
    whole = REAL_RESPONSE["body"]["output"][0]["content"][0]["text"]
    assert isinstance(real.output_text, str) and real.output_text
    assert injected.output_text == "Order 1234 has" and whole.startswith(injected.output_text)
    billed = REAL_RESPONSE["body"]["usage"]
    assert (injected.usage.input_tokens, injected.usage.output_tokens) == (
        billed["input_tokens"],
        billed["output_tokens"],
    )


# A real Responses function call cut by max_output_tokens = 16, with tool_choice required,
# saved on 19 November 2026: a 200 with the arguments cut, not a 400 as on chat.
REAL_CUT_CALL = json.loads((FIXTURES / "real_response_function_call_truncated.json").read_text())
CUT_TOOLS = [
    {
        "type": "function",
        "name": "get_order",
        "description": "Look up an order.",
        "parameters": REAL_CUT_CALL["body"]["tools"][0]["parameters"],
        "strict": True,
    }
]


def whole_call() -> dict[str, Any]:
    """The capture as the call would have come had the limit not landed in it: its arguments
    finished by hand, and the reply and the item completed."""
    body = json.loads(json.dumps(REAL_CUT_CALL["body"]))
    [item] = body["output"]
    item.update(status="completed", arguments=item["arguments"] + '1234"}')
    body.update(status="completed", incomplete_details=None, completed_at=body["created_at"] + 1)
    return body


@pytest.mark.design("proof.real_against_injected_truncate_responses_call")
@pytest.mark.parametrize("sync", [True, False], ids=SYNC_IDS)
def test_a_real_cut_function_call_and_truncate_parse_the_same(
    gated: None, loopback: Loopback, sync: bool
) -> None:
    """truncate on a whole function call gives the agent what OpenAI sent when the limit
    landed in one: the reply and the item incomplete, for max_output_tokens, and arguments
    that do not parse, under the same call_id."""
    whole = whole_call()
    json.loads(whole["output"][0]["arguments"])  # finished by hand, it parses
    gate_on("truncate")
    loopback.body = json.dumps(whole).encode()
    asked: dict[str, Any] = {"tools": CUT_TOOLS, "tool_choice": "required"}
    injected = responses_api(loopback, sync, **asked)
    assert loopback.seen == ["/v1/responses"]
    as_real(loopback, json.dumps(REAL_CUT_CALL["body"]).encode(), REAL_CUT_CALL["status"])
    real = responses_api(loopback, sync, **asked)
    assert response_fields(injected) == response_fields(real)
    assert injected.status == real.status == "incomplete"
    assert injected.incomplete_details == real.incomplete_details
    [injected_call], [real_call] = injected.output, real.output
    assert injected_call.status == real_call.status == "incomplete"
    assert injected_call.call_id == real_call.call_id and injected_call.id != real_call.id
    assert whole["output"][0]["arguments"].startswith(injected_call.arguments)
    for call in (injected_call, real_call):
        with pytest.raises(json.JSONDecodeError):
            json.loads(call.arguments)


def by_langchain_responses(server: Loopback, sync: bool) -> Any:
    """LangChain's ChatOpenAI on the Responses API, on the same server: its message."""
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore", "'asyncio.iscoroutinefunction' is deprecated", DeprecationWarning
        )
        from langchain_openai import ChatOpenAI

        model = ChatOpenAI(
            model=MODEL,
            base_url=server.url,
            api_key=SecretStr("test"),
            max_retries=0,
            use_responses_api=True,
        )
        if sync:
            return model.invoke("Where is order 1234?")
        return asyncio.run(model.ainvoke("Where is order 1234?"))


@pytest.mark.design("proof.real_against_injected_garble_responses")
@pytest.mark.parametrize("sync", [True, False], ids=SYNC_IDS)
def test_a_garbled_response_reads_as_the_real_one_in_the_openai_library_and_langchain(
    gated: None, loopback: Loopback, sync: bool
) -> None:
    """garble on a real Responses answer: the openai library sets the same fields as on the
    real reply, and LangChain's Responses path gives the same kind of message, with the
    stray token in its text."""
    loopback.body = json.dumps(REAL_RESPONSE["body"]).encode()
    gate_on("garble")
    injected = responses_api(loopback, sync)
    injected_chain = by_langchain_responses(loopback, sync)
    assert loopback.seen == ["/v1/responses"] * 2
    state.install(None)
    real = responses_api(loopback, sync)
    real_chain = by_langchain_responses(loopback, sync)
    assert response_fields(injected) == response_fields(real)
    assert injected.output_text == f"Order 1234 has been {THAI}" != real.output_text
    assert type(injected_chain) is type(real_chain)
    assert injected_chain.text == injected.output_text
    assert real_chain.text == real.output_text
    assert injected_chain.id != real_chain.id  # the made-up reply id, as LangChain keeps it
    assert sorted(injected_chain.response_metadata) == sorted(real_chain.response_metadata)
