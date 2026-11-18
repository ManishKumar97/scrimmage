"""empty on a web request, bad body on a web request or a model call, and the faults in a
model's reply, in one process: the reply the code gets, the line, and that nothing is sent,
reserved or fired by mistake."""

import asyncio
import http.server
import json
import re
import threading
import warnings
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import httpx2
import openai
import pydantic
import pytest
from pydantic import SecretStr

from conftest import EXAMPLE_FAULT, Project
from fakes.openai_server import FakeOpenAI
from scrimmage_core import cli, config, http_gate, runner, state, suggest
from scrimmage_core import provider_openai as openai_table
from scrimmage_core.config import Fault, ModelHost
from scrimmage_core.decide import MISTAKE_REFUSAL
from scrimmage_core.errors import ConfigError, ScrimmageMockError
from scrimmage_core.plan import Plan
from scrimmage_core.provider_openai import BROKEN_BODY
from test_connection_faults import (
    LIBRARIES,
    PATH_IDS,
    PATHS,
    WEB,
    Counting,
    gated,
    install,
    never_stopped,
)
from test_http_gate import BODY, URL, Sink
from test_model_faults import sent_only_the_real_calls

__all__ = ["gated"]  # the fixture, used by name

FIXTURES = Path(__file__).parent / "fixtures" / "openai"
EMBEDDING_START = (FIXTURES / "embedding.json").read_bytes()[:60]
RESPONSE_START = (FIXTURES / "response_text.json").read_bytes()[:60]


def got(name: str, sync: bool, reached: list[object], url: str = WEB, **kw: Any) -> Any:
    """One request through that library and path, read to its end, to a transport that keeps
    what reaches it."""
    module: Any = LIBRARIES[name]

    def answer(request: Any) -> Any:
        reached.append(request)
        return module.Response(200, json={"choices": []})

    transport = module.MockTransport(answer)
    method = "POST" if url == URL else "GET"
    if sync:
        return module.Client(transport=transport).request(method, url, **kw)

    async def go() -> Any:
        async with module.AsyncClient(transport=transport) as client:
            return await client.request(method, url, **kw)

    return asyncio.run(go())


def line_of(sink: Sink, kind: str) -> dict[str, object]:
    (line,) = [x for x in sink.lines if x.get("kind") == kind]
    return line


@pytest.mark.design("fault.empty_web")
@pytest.mark.parametrize(("name", "sync"), PATHS, ids=PATH_IDS)
def test_empty_on_a_web_request_is_a_200_with_no_body_and_no_content_type(
    gated: None, name: str, sync: bool
) -> None:
    sink, _ = install("empty")
    reached: list[object] = []
    response = got(name, sync, reached)
    assert reached == [], "the gate sent the request"
    assert response.status_code == 200 and response.content == b""
    assert "content-type" not in response.headers
    assert response.headers["content-length"] == "0"
    line = line_of(sink, "http")
    assert line["action"] == "fault" and line["status"] == 200


@pytest.mark.design("fault.bad_body_web")
@pytest.mark.parametrize(("name", "sync"), PATHS, ids=PATH_IDS)
def test_bad_body_on_a_web_request_is_a_200_json_reply_that_does_not_parse(
    gated: None, name: str, sync: bool
) -> None:
    sink, _ = install("bad body")
    reached: list[object] = []
    response = got(name, sync, reached)
    assert reached == [], "the gate sent the request"
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json; charset=utf-8"
    assert response.content == http_gate.BROKEN_WEB_BODY
    with pytest.raises(json.JSONDecodeError) as caught:
        response.json()
    assert caught.value.pos == len(b'{"data": [{"id": 1, "name": ')  # the open string's quote
    line = line_of(sink, "http")
    assert line["action"] == "fault" and line["status"] == 200


def ask(transport: Any) -> BaseException:
    """One chat call through the openai library on this transport; what it raised."""
    api = openai.OpenAI(api_key="test", http_client=httpx2.Client(transport=transport))
    with pytest.raises(Exception) as caught:
        api.chat.completions.create(
            model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}]
        )
    return caught.value


@pytest.mark.design("fault.bad_body_model")
def test_bad_body_on_a_model_call_reaches_the_agent_as_the_real_one_does(gated: None) -> None:
    """The real one: a provider that answers 200 with the same bytes, with no run."""
    real_sent: list[object] = []
    reached: list[object] = []

    def provider(sent: list[object]) -> httpx2.MockTransport:
        def broken(request: httpx2.Request) -> httpx2.Response:
            sent.append(request)
            headers = {"content-type": "application/json", "x-request-id": "req_1"}
            return httpx2.Response(200, headers=headers, content=BROKEN_BODY)

        return httpx2.MockTransport(broken)

    real_error = ask(provider(real_sent))
    sink, budget = install("bad body", "model")
    fault_error = ask(provider(reached))
    assert type(fault_error) is type(real_error) is json.JSONDecodeError
    assert isinstance(fault_error, json.JSONDecodeError)
    assert isinstance(real_error, json.JSONDecodeError)
    assert fault_error.pos == real_error.pos
    assert len(real_sent) == 1 and reached == [], "the real one was retried, or the fault sent"
    lines = [x for x in sink.lines if x.get("kind") == "model"]
    assert [(x["action"], x.get("status")) for x in lines] == [("fault", 200)]  # no retry
    assert budget.asked == 0 and sink.settles == [] and lines[0].get("cost_usd", 0) == 0


@pytest.mark.design("fault.bad_body_stream")
@pytest.mark.parametrize(("name", "sync"), PATHS, ids=PATH_IDS)
def test_bad_body_on_a_streamed_model_request_is_a_mistake_and_not_fired(
    gated: None, name: str, sync: bool
) -> None:
    sink, budget = install("bad body", "model")
    reached: list[object] = []
    with pytest.raises(ScrimmageMockError, match="is a streamed request"):
        got(name, sync, reached, URL, json={**BODY, "stream": True})
    assert reached == [] and budget.asked == 0
    line = line_of(sink, "model")
    # Not a fired fault, and not a real call the provider failed: nothing was sent.
    assert (line["action"], line["reason"]) == ("refused", MISTAKE_REFUSAL), line
    assert runner.real_error(line) is None
    assert "streamed request" in str(line["mistake"])
    # The suggestion reads it as a mistake in the test, never as a call that ran.
    assert suggest.refused_mistakes([line]) == [line] and suggest.unchanged_lines([line]) == []
    unchanged = line["unchanged"]
    assert isinstance(unchanged, dict) and unchanged["do"] == "bad body"
    assert "streamed request" in str(unchanged["why"])


@pytest.mark.design("fault.bad_body_model")
def test_bad_body_on_a_model_request_that_is_not_streamed_still_fires(gated: None) -> None:
    """stream = false is a plain reply, so the stream check leaves it alone."""
    sink, _ = install("bad body", "model")
    response = got("httpx2", True, [], URL, json={**BODY, "stream": False})
    assert response.content == BROKEN_BODY
    assert response.headers["x-request-id"] == "req_scrimmage_injected"
    assert line_of(sink, "model")["action"] == "fault"


EMBEDDINGS = "https://api.openai.com/v1/embeddings"
RESPONSES = "https://api.openai.com/v1/responses"


@pytest.mark.design("fault.bad_body_model")
@pytest.mark.parametrize(
    ("call", "url", "body", "start"),
    [
        ("model:*/embeddings", EMBEDDINGS, {"model": "e", "input": "hi"}, EMBEDDING_START),
        ("model", RESPONSES, {"model": "gpt-4o-mini", "input": "hi"}, RESPONSE_START),
    ],
    ids=["embeddings", "responses"],
)
def test_bad_body_on_another_op_sends_the_start_of_that_ops_real_reply(
    gated: None, call: str, url: str, body: dict[str, object], start: bytes
) -> None:
    """The bytes are the spec example's own, read from the fixture, so they cannot drift."""
    sink, _ = install("bad body", call)
    reached: list[object] = []
    module: Any = LIBRARIES["httpx2"]
    client = module.Client(transport=module.MockTransport(lambda r: reached.append(r)))
    response = client.post(url, json=body)
    assert reached == [], "the gate sent the request"
    assert response.content == start and len(start) == 60
    with pytest.raises(json.JSONDecodeError):
        response.json()
    assert line_of(sink, "model")["action"] == "fault"


@pytest.mark.design("fault.empty_web")
def test_empty_on_a_model_call_is_refused_at_load() -> None:
    for call in ("model", "model:*/embeddings"):
        with pytest.raises(ConfigError, match="for a model reply that fails to parse, use bad"):
            config.read_fault({"call": call, "do": "empty", "when": "first 1"}, "[[test.fault]]")


@pytest.mark.design("fault.bad_body_kinds")
@pytest.mark.parametrize("call", ["tool:get_order", "mcp:docs/search"])
def test_bad_body_on_a_tool_is_refused_naming_the_web_request(call: str) -> None:
    with pytest.raises(ConfigError, match="fault the web request inside it"):
        config.read_fault({"call": call, "do": "bad body", "when": "first 1"}, "[[test.fault]]")


@pytest.mark.design("fault.empty_web")
def test_empty_and_bad_body_load_on_a_web_request_and_bad_body_on_a_model_call() -> None:
    for call, do in (("http:a.acme", "empty"), ("http:a.acme", "bad body"), ("model", "bad body")):
        fault = config.read_fault({"call": call, "do": do, "when": "first 1"}, "[[test.fault]]")
        assert fault == Fault(call, do, "first 1")


@pytest.mark.design("fault.empty_web")
def test_a_head_request_under_bad_body_gets_no_body(gated: None) -> None:
    sink, _ = install("bad body")
    reached: list[object] = []
    module: Any = LIBRARIES["httpx2"]
    client = module.Client(transport=module.MockTransport(lambda r: reached.append(r)))
    response = client.head(WEB)
    assert reached == [], "the gate sent the request"
    assert response.status_code == 200 and response.content == b""
    assert response.headers["content-length"] == str(len(http_gate.BROKEN_WEB_BODY))
    assert line_of(sink, "http")["status"] == 200


STREAMING_BOT = """\
import sys

import openai

client = openai.OpenAI(max_retries=0)
messages = [{"role": "user", "content": sys.argv[1]}]
stream = client.chat.completions.create(model="gpt-4o-mini", messages=messages, stream=True)
print("".join(c.choices[0].delta.content or "" for c in stream if c.choices))
"""
STREAMED_FAULT = '[[test.fault]]\ncall = "model"\ndo = "bad body"\nwhen = "first 1"\n'


@pytest.mark.design("fault.bad_body_stream")
@pytest.mark.slow
def test_a_run_with_bad_body_on_a_streamed_request_ends_as_a_test_mistake(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """The gate's mistake is neither the provider failing nor a call the agent chose: exit 2,
    no "not injected" error, and the run's suggestion is the test's mistake."""
    (project.root / "bot.py").write_text(STREAMING_BOT)
    project.edit('function = "acme.agent:answer"', 'command = "python bot.py {question}"')
    project.edit("[agent]\n", "[agent]\nruns = 1\n")
    project.edit(EXAMPLE_FAULT, STREAMED_FAULT)
    project.edit('expect   = ["shipped", ["Oct 30", "October 30"]]', 'expect   = ["Hello"]')
    fake = FakeOpenAI(["chat_stream"])  # the streamed answer is "Hello"
    fake.start()
    try:
        result = project.test(fake)
    finally:
        fake.stop()
    said = result.out + result.err
    assert result.code == 2, said
    # The baseline passes, so the table shows the fault row's own result: none, as a mistake.
    table = [x for x in said.splitlines() if x.startswith("order-status")]
    assert len(table) == 2, table
    assert "baseline" in table[0] and table[0].endswith("pass"), table
    assert "fault 1" in table[1] and table[1].endswith("no result"), table
    assert "test mistake: order-status fault 1: no result: the fault never fired" in said
    assert "test mistake: order-status fault 1: run-" in said
    fault = next(lines for lines in result.records() if lines[-1]["row"] == "fault 1")
    [line] = [x for x in fault if x.get("kind") == "model"]
    assert (line["action"], line["reason"]) == ("refused", MISTAKE_REFUSAL), line
    summary = cast("dict[str, Any]", fault[-1]["summary"])
    assert summary["fault_fired"] is False
    assert summary["outcome"] not in ("refused call", "infrastructure error"), summary
    assert "not injected" not in said and "is a streamed request" in said
    folder = next(p for p in (project.root / "scrimmage-runs").iterdir() if p.is_dir())
    capsys.readouterr()
    assert cli.main(["show", str(folder), "--run", str(line["run"]), "--json"]) == 0
    [run] = json.loads(capsys.readouterr().out)["runs"]
    texts = [str(s["text"]) for s in run["suggestions"]]
    assert [s["code"] for s in run["suggestions"]][:1] == ["test_mistake"], texts
    assert not any("ran as it would" in text for text in texts), texts


# The faults in a model's reply. The real side of each pair is a loopback server that sends
# the bytes, with no run, so the openai library and LangChain read them over a real socket.
MODEL = "gpt-4o-mini"
ORDER_TOOLS = ("acme.tools:get_order", "acme.tools:get_stock")
SPECS = [
    {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}
    for name in ("get_order", "get_stock")
]
QUESTION = [{"role": "user", "content": "Where is order 1234?"}]
REAL_ANSWER = (FIXTURES / "real_chat_answer.json").read_bytes()
REAL_TOOL_CALL = (FIXTURES / "real_chat_tool_call.json").read_bytes()
JSON_HEADERS = {"content-type": "application/json", "x-request-id": "req_real"}
REPLY_WORDS = ("garble", "truncate", "unknown tool", "bad tool args", "refuse")
SYNC_IDS = ["sync", "async"]


class Order(pydantic.BaseModel):
    status: str


class Loopback:
    """A model server on 127.0.0.1 that sends one reply, and counts what reaches it."""

    def __init__(self) -> None:
        self.body = REAL_ANSWER
        self.seen: list[str] = []
        loop = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                loop.seen.append(self.path)
                self.send_response(200)
                for name, value in JSON_HEADERS.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(loop.body)))
                self.end_headers()
                self.wfile.write(loop.body)

            def log_message(self, format: str, *args: object) -> None:
                del format, args

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"


@pytest.fixture
def loopback() -> Iterator[Loopback]:
    server = Loopback()
    server.thread.start()
    yield server
    server.server.shutdown()
    server.server.server_close()
    server.thread.join()


def plan_with(word: str | None) -> Plan:
    """A run with this reply fault on every call to the model, or none; the loopback is a
    model host."""
    faults = () if word is None else (Fault("model", word, "always", model=MODEL),)
    return Plan(
        "run-1", "t", "row", (), faults, ORDER_TOOLS, (), 60.0,
        model_hosts=(ModelHost("127.0.0.1", "openai"),), budget_usd=1.0,
    )  # fmt: skip


def gate_on(word: str) -> tuple[Sink, Counting]:
    """That run, with both libraries gated."""
    sink, budget = Sink(), Counting()
    run = state.RunState(plan_with(word), sink, [])
    state.install(run)
    ctx = http_gate.Context(run, budget, sink, never_stopped)
    for name, module in LIBRARIES.items():
        http_gate.patch(module, ctx, name)
    return sink, budget


def completion(server: Loopback, sync: bool, **request: Any) -> tuple[Any, bytes]:
    """One chat call through the openai library: what it made of the reply, and its bytes."""
    kw: dict[str, Any] = {"model": MODEL, "messages": QUESTION, **request}
    if sync:
        api = openai.OpenAI(base_url=server.url, api_key="test", max_retries=0)
        raw = api.chat.completions.with_raw_response.create(**kw)
        return raw.parse(), raw.http_response.content

    async def go() -> tuple[Any, bytes]:
        api = openai.AsyncOpenAI(base_url=server.url, api_key="test", max_retries=0)
        raw = await api.chat.completions.with_raw_response.create(**kw)
        return raw.parse(), raw.http_response.content

    return asyncio.run(go())


def parsed(server: Loopback, sync: bool) -> tuple[Any, bytes]:
    """client.chat.completions.parse with a Pydantic model, as Structured Outputs code calls it."""
    kw: dict[str, Any] = {"model": MODEL, "messages": QUESTION, "response_format": Order}
    if sync:
        api = openai.OpenAI(base_url=server.url, api_key="test", max_retries=0)
        raw = api.chat.completions.with_raw_response.parse(**kw)
        return raw.parse(), raw.http_response.content

    async def go() -> tuple[Any, bytes]:
        api = openai.AsyncOpenAI(base_url=server.url, api_key="test", max_retries=0)
        raw = await api.chat.completions.with_raw_response.parse(**kw)
        return raw.parse(), raw.http_response.content

    return asyncio.run(go())


def structured_by_langchain(server: Loopback, sync: bool) -> BaseException | None:
    """LangChain's with_structured_output on the same server; what it raised."""
    with warnings.catch_warnings():
        # langsmith, which langchain_openai loads, calls an asyncio function Python 3.14
        # deprecates, and this suite turns warnings into errors.
        warnings.filterwarnings(
            "ignore", "'asyncio.iscoroutinefunction' is deprecated", DeprecationWarning
        )
        from langchain_openai import ChatOpenAI

        model = ChatOpenAI(
            model=MODEL, base_url=server.url, api_key=SecretStr("test"), max_retries=0
        )
        chain = model.with_structured_output(Order)
        try:
            if sync:
                chain.invoke("Where is order 1234?")
            else:
                asyncio.run(chain.ainvoke("Where is order 1234?"))
        except Exception as exc:
            return exc
    return None


def fields_of(value: object) -> object:
    """The fields the library set, recursively, with no field it did not know."""
    if isinstance(value, pydantic.BaseModel):
        extra = value.model_extra
        assert not extra, f"{type(value).__name__} has fields it does not know: {extra}"
        return {name: fields_of(getattr(value, name)) for name in sorted(value.model_fields_set)}
    if isinstance(value, list):
        return [fields_of(item) for item in cast("list[object]", value)]
    return None


def as_real(server: Loopback, body: bytes) -> None:
    """The run ends, and the server sends these bytes as a real model would."""
    state.install(None)
    server.body = body
    server.seen.clear()


def model_lines(sink: Sink) -> list[dict[str, object]]:
    return [x for x in sink.lines if x.get("kind") == "model"]


@pytest.mark.design("fault.refuse_plain")
@pytest.mark.parametrize("sync", [True, False], ids=SYNC_IDS)
def test_refuse_on_a_plain_request_is_text_with_a_null_refusal(
    gated: None, loopback: Loopback, sync: bool
) -> None:
    sink, _ = gate_on("refuse")
    reply, _ = completion(loopback, sync)
    assert loopback.seen == [], "the gate sent the request"
    [choice] = reply.choices
    assert choice.message.content == "I'm sorry, I can't assist with that."
    assert choice.message.refusal is None and choice.finish_reason == "stop"
    line = line_of(sink, "model")
    assert (line["action"], line["status"]) == ("fault", 200)


@pytest.mark.design("fault.refuse_structured")
@pytest.mark.parametrize("sync", [True, False], ids=SYNC_IDS)
def test_refuse_on_a_structured_output_request_sets_refusal_and_nulls_content(
    gated: None, loopback: Loopback, sync: bool
) -> None:
    schema = {"name": "order", "schema": {"type": "object"}, "strict": True}
    gate_on("refuse")
    format_ = {"type": "json_schema", "json_schema": schema}
    reply, _ = completion(loopback, sync, response_format=format_)
    assert loopback.seen == []
    [choice] = reply.choices
    assert choice.message.content is None and choice.finish_reason == "stop"
    assert choice.message.refusal == "I'm sorry, I cannot assist with that request."
    # JSON mode is not Structured Outputs: a refusal there is plain text.
    reply, _ = completion(loopback, sync, response_format={"type": "json_object"})
    assert reply.choices[0].message.refusal is None
    assert reply.choices[0].message.content == openai_table.PLAIN_REFUSAL


@pytest.mark.design("proof.real_against_injected_refuse")
@pytest.mark.parametrize("sync", [True, False], ids=SYNC_IDS)
def test_refuse_reaches_parse_as_a_real_refusal_does(
    gated: None, loopback: Loopback, sync: bool
) -> None:
    """parse() gives parsed None and the refusal, and LangChain raises its refusal error, from
    the gate's reply and from the same bytes sent by a server."""
    gate_on("refuse")
    injected, body = parsed(loopback, sync)
    injected_error = structured_by_langchain(loopback, sync)
    assert loopback.seen == [], "the gate sent the request"
    as_real(loopback, body)
    real, _ = parsed(loopback, sync)
    real_error = structured_by_langchain(loopback, sync)
    assert len(loopback.seen) == 2, "the real side never reached the server"
    for reply in (injected, real):
        message = reply.choices[0].message
        assert message.parsed is None and message.refusal == openai_table.STRUCTURED_REFUSAL
    assert injected.model_dump() == real.model_dump()
    assert type(injected_error).__name__ == "OpenAIRefusalError", repr(injected_error)
    assert type(real_error) is type(injected_error)
    assert str(real_error) == str(injected_error) == openai_table.STRUCTURED_REFUSAL


@pytest.mark.design("fault.unknown_tool")
@pytest.mark.parametrize("sync", [True, False], ids=SYNC_IDS)
def test_unknown_tool_calls_multi_tool_use_parallel_naming_the_first_offered_tool(
    gated: None, loopback: Loopback, sync: bool
) -> None:
    sink, _ = gate_on("unknown tool")
    reply, _ = completion(loopback, sync, tools=SPECS)
    assert loopback.seen == [], "the gate sent the request"
    [choice] = reply.choices
    assert choice.finish_reason == "tool_calls" and choice.message.content is None
    [call] = choice.message.tool_calls
    assert call.type == "function" and call.function.name == "multi_tool_use.parallel"
    assert json.loads(call.function.arguments) == {
        "tool_uses": [{"recipient_name": "functions.get_order", "parameters": {}}]
    }
    # New ids in OpenAI's forms, and the request's own model.
    assert re.fullmatch(r"call_[A-Za-z0-9]{24}", call.id), call.id
    assert re.fullmatch(r"chatcmpl-[A-Za-z0-9]{29}", reply.id), reply.id
    assert reply.model == MODEL
    again, _ = completion(loopback, sync, tools=SPECS)
    assert again.id != reply.id and again.choices[0].message.tool_calls[0].id != call.id
    assert [x["action"] for x in model_lines(sink)] == ["fault", "fault"]


@pytest.mark.design("fault.unknown_tool_not_fired")
@pytest.mark.parametrize("sync", [True, False], ids=SYNC_IDS)
def test_unknown_tool_on_a_request_with_no_tools_is_not_fired(
    gated: None, loopback: Loopback, sync: bool
) -> None:
    """OpenAI sends tool calls only when tools are offered, so the call goes on as in the
    baseline: sent, reserved, and the agent gets the real reply."""
    sink, budget = gate_on("unknown tool")
    reply, body = completion(loopback, sync)
    assert loopback.seen == ["/v1/chat/completions"] and body == REAL_ANSWER
    assert reply.choices[0].message.content.startswith("Order 1234 has been shipped")
    assert budget.asked == 1, "the real call was not reserved"
    line = line_of(sink, "model")
    assert line["action"] == "real" and "fault" not in line, line
    assert line["unchanged"] == {
        "do": "unknown tool",
        "why": "the request offers no tools, so a model would call none",
    }
    assert suggest.unchanged_lines([line]) == [line] and suggest.refused_mistakes([line]) == []


BUILT = [("refuse", REAL_ANSWER, {}), ("unknown tool", REAL_TOOL_CALL, {"tools": SPECS})]


@pytest.mark.design("proof.real_against_injected_built_reply")
@pytest.mark.parametrize("sync", [True, False], ids=SYNC_IDS)
@pytest.mark.parametrize(("word", "capture", "asked"), BUILT, ids=["refuse", "unknown tool"])
def test_a_built_reply_parses_into_the_same_fields_as_the_capture_it_came_from(
    gated: None,
    loopback: Loopback,
    word: str,
    capture: bytes,
    asked: dict[str, Any],
    sync: bool,
) -> None:
    """A real gpt-4o-mini reply, sent by a server, and the gate's built one: the openai library
    sets the same fields on both, every one a field it knows."""
    gate_on(word)
    injected, _ = completion(loopback, sync, **asked)
    assert loopback.seen == []
    as_real(loopback, capture)
    real, _ = completion(loopback, sync, **asked)
    assert loopback.seen == ["/v1/chat/completions"]
    assert fields_of(injected) == fields_of(real)
    assert injected.object == real.object and injected.service_tier == real.service_tier


@pytest.mark.design("security.built_reply_sends_nothing")
@pytest.mark.parametrize("sync", [True, False], ids=SYNC_IDS)
@pytest.mark.parametrize(
    ("word", "asked"), [(w, a) for w, _, a in BUILT], ids=["refuse", "unknown tool"]
)
def test_a_built_reply_costs_nothing_and_reserves_nothing(
    gated: None, loopback: Loopback, word: str, asked: dict[str, Any], sync: bool
) -> None:
    sink, budget = gate_on(word)
    reply, _ = completion(loopback, sync, **asked)
    assert loopback.seen == [], "the gate sent the request"
    assert budget.asked == 0 and sink.settles == []
    line = line_of(sink, "model")
    assert line["action"] == "fault" and "cost_usd" not in line and "tokens" not in line
    # The counts on the reply are an estimate of the request and a short answer.
    assert reply.usage.completion_tokens == 10 and reply.usage.prompt_tokens > 0
    # The positive control: the same gate, server and request, in a run with no fault.
    state.install(state.RunState(plan_with(None), sink, []))
    completion(loopback, sync, **asked)
    assert loopback.seen == ["/v1/chat/completions"] and budget.asked == 1


@pytest.mark.design("fault.reply_stream")
@pytest.mark.parametrize("word", REPLY_WORDS)
@pytest.mark.parametrize(("name", "sync"), PATHS, ids=PATH_IDS)
def test_a_reply_fault_on_a_streamed_request_is_a_mistake_and_nothing_is_sent(
    gated: None, loopback: Loopback, word: str, name: str, sync: bool
) -> None:
    """Found from the request's own stream = true, before anything is sent or reserved."""
    sink, budget = gate_on(word)
    module: Any = LIBRARIES[name]
    body = {"model": MODEL, "messages": QUESTION, "tools": SPECS, "stream": True}
    url = f"{loopback.url}/chat/completions"

    async def go() -> None:
        async with module.AsyncClient() as client:
            await client.post(url, json=body)

    with pytest.raises(ScrimmageMockError, match="is a streamed request"):
        if sync:
            module.Client().post(url, json=body)
        else:
            asyncio.run(go())
    assert loopback.seen == [] and budget.asked == 0
    line = line_of(sink, "model")
    assert (line["action"], line["reason"]) == ("refused", MISTAKE_REFUSAL), line
    assert runner.real_error(line) is None
    unchanged = line["unchanged"]
    assert isinstance(unchanged, dict) and unchanged["do"] == word
    assert suggest.refused_mistakes([line]) == [line] and suggest.unchanged_lines([line]) == []


@pytest.mark.design("fault.reply_stream")
def test_a_streamed_call_under_refuse_reaches_the_openai_library_as_the_mistake(
    gated: None, loopback: Loopback
) -> None:
    """The openai library neither retries the mistake nor turns it into a connection error."""
    sink, _ = gate_on("refuse")
    api = openai.OpenAI(base_url=loopback.url, api_key="test", max_retries=2)
    with pytest.raises(ScrimmageMockError):
        api.chat.completions.create(model=MODEL, messages=QUESTION, stream=True)  # type: ignore[arg-type]
    assert loopback.seen == []
    assert [x["action"] for x in model_lines(sink)] == ["refused"]


@pytest.mark.design("fault.reply_responses")
@pytest.mark.parametrize("word", REPLY_WORDS)
def test_a_reply_fault_on_a_responses_call_is_a_mistake_and_nothing_is_sent(
    gated: None, loopback: Loopback, word: str
) -> None:
    sink, budget = gate_on(word)
    with pytest.raises(ScrimmageMockError, match="is a Responses call"):
        httpx2.Client().post(f"{loopback.url}/responses", json={"model": MODEL, "input": "hi"})
    assert loopback.seen == [] and budget.asked == 0
    line = line_of(sink, "model")
    assert (line["action"], line["reason"]) == ("refused", MISTAKE_REFUSAL)


@pytest.mark.design("fault.reply_not_built")
@pytest.mark.parametrize("word", ["garble", "truncate", "bad tool args"])
def test_a_reply_fault_that_changes_the_real_reply_is_not_built_and_sends_nothing(
    gated: None, loopback: Loopback, word: str
) -> None:
    sink, budget = gate_on(word)
    with pytest.raises(ScrimmageMockError, match="is not built yet"):
        completion(loopback, True, tools=SPECS)
    assert loopback.seen == [] and budget.asked == 0
    assert line_of(sink, "model")["reason"] == MISTAKE_REFUSAL


REFUSE_AND_UNKNOWN = """\
[[test.fault]]
call   = "model"
model  = "gpt-4o-mini"
do     = "unknown tool"
when   = "call 1"
forbid = ["never said"]

[[test.fault]]
call   = "model"
model  = "gpt-4o-mini"
do     = "refuse"
when   = "call 2"
forbid = ["never said"]
"""


@pytest.mark.design("security.built_reply_sends_nothing")
@pytest.mark.slow
def test_a_run_under_refuse_and_unknown_tool_sends_only_the_real_calls(
    project: Project, fake_openai: Callable[..., FakeOpenAI]
) -> None:
    """The order bot and the fake provider: one request per real line, so a built reply that
    reached the provider would be one too many. The bot meets each as it would a real one."""
    project.edit("[agent]\n", "[agent]\nruns = 1\n")
    project.edit(EXAMPLE_FAULT, REFUSE_AND_UNKNOWN)
    fake = fake_openai(["acme_get_order", "acme_get_stock", "acme_answer|acme_admits"])
    result = project.test(fake)
    assert result.finished, result.out + result.err
    sent_only_the_real_calls(result, fake)
    rows = {lines[-1]["row"]: lines for lines in result.records()}
    unknown = [x for x in rows["fault 1"] if x.get("kind") == "model"]
    refused = [x for x in rows["fault 2"] if x.get("kind") == "model"]
    assert [x["action"] for x in unknown] == ["fault", "real", "real"]
    assert [x["action"] for x in refused] == ["real", "fault"]
    assert all(not x.get("cost_usd") for x in unknown + refused if x["action"] == "fault")
    # The bot ran the tool the model named, failed, and told the model so.
    told = [m for r in fake.requests for m in r["messages"] if m.get("role") == "tool"]
    assert any("multi_tool_use.parallel" in str(m["content"]) for m in told), told
    summary = rows["fault 2"][-1]["summary"]
    assert isinstance(summary, dict) and summary["answer"] == openai_table.PLAIN_REFUSAL
