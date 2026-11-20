"""empty on a web request, bad body on a web request or a model call, and the faults in a
model's reply, in one process: the reply the code gets, the line, and that nothing is sent,
reserved or fired by mistake."""

import asyncio
import dataclasses
import gzip
import http.server
import json
import re
import threading
import warnings
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import httpx2
import jsonschema
import openai
import pydantic
import pytest
from pydantic import SecretStr

from conftest import EXAMPLE_FAULT, Project
from fakes.openai_server import FakeOpenAI
from scrimmage_core import changes, cli, config, http_gate, runner, show, state, suggest
from scrimmage_core import provider_openai as openai_table
from scrimmage_core.config import Fault, ModelHost
from scrimmage_core.decide import MISTAKE_REFUSAL
from scrimmage_core.errors import ConfigError, ScrimmageMockError
from scrimmage_core.plan import Plan
from scrimmage_core.provider_openai import BROKEN_BODY
from scrimmage_core.redact import REMOVED
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
        self.status = 200
        self.headers = dict(JSON_HEADERS)
        self.seen: list[str] = []
        loop = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                loop.seen.append(self.path)
                self.send_response(loop.status)
                for name, value in loop.headers.items():
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
    return gate_plan(plan_with(word))


def gate_plan(plan: Plan) -> tuple[Sink, Counting]:
    """This plan's run, with both libraries gated."""
    sink, budget = Sink(), Counting()
    run = state.RunState(plan, sink, [])
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


def as_real(server: Loopback, body: bytes, status: int = 200) -> None:
    """The run ends, and the server sends these bytes, with this status, as a real model
    would."""
    state.install(None)
    server.body, server.status = body, status
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


@pytest.mark.design("fault.refuse_parse")
@pytest.mark.parametrize("sync", [True, False], ids=SYNC_IDS)
def test_a_structured_refuse_reaches_parse_and_langchain_as_a_refusal(
    gated: None, loopback: Loopback, sync: bool
) -> None:
    """parse() gives parsed None and the refusal, and LangChain raises its refusal error. The
    gate's bytes are sent again by a server with no run, so the gate's own reply object is
    shown to be what a socket gives. No real refusal was captured: the form is the guide's."""
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


# Requests where the model could not pick a tool of its own, so no real reply has one it
# was never offered.
NO_FREE_CHOICE = [
    ({}, "the request offers no tools, so a model would call none"),
    (
        {"tools": SPECS, "tool_choice": "none"},
        'the request sets tool_choice "none", so a model would call no tool',
    ),
    (
        {"tools": SPECS, "tool_choice": {"type": "function", "function": {"name": "get_stock"}}},
        "the request's tool_choice names the tool to call, so a model would call that one",
    ),
    (
        {"functions": [{"name": "get_order", "parameters": {"type": "object"}}]},
        "the request offers tools in the old functions field only, which a model answers "
        "with function_call, not tool_calls",
    ),
]


@pytest.mark.design("fault.unknown_tool_not_fired")
@pytest.mark.parametrize(
    ("asked", "why"), NO_FREE_CHOICE, ids=["no tools", "none", "named", "functions"]
)
def test_unknown_tool_where_the_model_cannot_pick_a_tool_is_not_fired(
    gated: None, loopback: Loopback, asked: dict[str, Any], why: str
) -> None:
    sink, budget = gate_on("unknown tool")
    _, body = completion(loopback, True, **asked)
    assert loopback.seen == ["/v1/chat/completions"] and body == REAL_ANSWER
    assert budget.asked == 1
    line = line_of(sink, "model")
    assert line["action"] == "real" and line["unchanged"] == {"do": "unknown tool", "why": why}


@pytest.mark.design("fault.unknown_tool")
@pytest.mark.parametrize(
    "asked",
    [{"tool_choice": "auto"}, {"tool_choice": "required"}, {"parallel_tool_calls": False}],
    ids=["auto", "required", "no parallel calls"],
)
def test_unknown_tool_fires_where_the_model_picks_its_own_tool(
    gated: None, loopback: Loopback, asked: dict[str, Any]
) -> None:
    """parallel_tool_calls false is left firing: no source shows either way."""
    sink, _ = gate_on("unknown tool")
    reply, _ = completion(loopback, True, tools=SPECS, **asked)
    assert loopback.seen == []
    assert reply.choices[0].message.tool_calls[0].function.name == "multi_tool_use.parallel"
    assert line_of(sink, "model")["action"] == "fault"


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


# The Responses API. A real gpt-4o-mini answer, saved on 18 November 2026.
REAL_RESPONSE = json.loads((FIXTURES / "real_response_answer.json").read_text())["body"]
RESPONSE_SPECS = [
    {"type": "function", "name": name, "parameters": {"type": "object"}}
    for name in ("get_order", "get_stock")
]


def responses_api(server: Loopback, sync: bool, how: str = "create", **request: Any) -> Any:
    """One Responses call through the openai library, create or parse: what it made of the
    reply."""
    kw: dict[str, Any] = {"model": MODEL, "input": "Where is order 1234?", **request}
    if sync:
        api = openai.OpenAI(base_url=server.url, api_key="test", max_retries=0)
        return getattr(api.responses, how)(**kw)

    async def go() -> Any:
        api = openai.AsyncOpenAI(base_url=server.url, api_key="test", max_retries=0)
        return await getattr(api.responses, how)(**kw)

    return asyncio.run(go())


def response_fields(value: object) -> object:
    """The fields the library set, recursively, and the names of those it has no field for:
    a real Responses reply holds a few, such as billing."""
    if isinstance(value, pydantic.BaseModel):
        known = {name: response_fields(getattr(value, name)) for name in value.model_fields_set}
        return {**known, "unknown": sorted(value.model_extra or {})}
    if isinstance(value, list):
        return [response_fields(item) for item in cast("list[object]", value)]
    return None


def but_tools(fields: object) -> dict[str, object]:
    """A reply's field set without tools, which echoes each request's own."""
    return {**cast("dict[str, object]", fields), "tools": None}


@pytest.mark.design("fault.refuse_responses")
@pytest.mark.parametrize("sync", [True, False], ids=SYNC_IDS)
def test_refuse_on_a_responses_structured_request_is_a_refusal_content_part(
    gated: None, loopback: Loopback, sync: bool
) -> None:
    """responses.parse with a Pydantic model asks for Structured Outputs: the reply's message
    holds a refusal part, so output_parsed is None and output_text is empty, as on a real
    refusal. Nothing is sent or reserved, and the line has no cost."""
    sink, budget = gate_on("refuse")
    got = responses_api(loopback, sync, "parse", text_format=Order)
    assert loopback.seen == [] and budget.asked == 0
    [item] = got.output
    [part] = item.content
    assert (part.type, part.refusal) == ("refusal", openai_table.STRUCTURED_REFUSAL)
    assert got.output_parsed is None and got.output_text == ""
    assert (got.status, item.status, got.model) == ("completed", "completed", MODEL)
    assert re.fullmatch(r"resp_[0-9a-f]{50}", got.id) and re.fullmatch(r"msg_[0-9a-f]{50}", item.id)
    assert got.text.format.type == "json_schema"  # the request's own format, echoed
    line = line_of(sink, "model")
    assert line["action"] == "fault" and "cost_usd" not in line and budget.asked == 0


@pytest.mark.design("fault.refuse_responses")
@pytest.mark.parametrize("sync", [True, False], ids=SYNC_IDS)
def test_refuse_on_a_plain_responses_request_is_text_with_the_real_replys_fields(
    gated: None, loopback: Loopback, sync: bool
) -> None:
    """A plain request's refusal is ordinary output text. The built reply sets the same fields
    as the real answer it was made from, those the library has no field for included."""
    gate_on("refuse")
    got = responses_api(loopback, sync)
    assert loopback.seen == []
    assert got.output_text == openai_table.PLAIN_REFUSAL
    as_real(loopback, json.dumps(REAL_RESPONSE).encode())
    real = responses_api(loopback, sync)
    assert but_tools(response_fields(got)) == but_tools(response_fields(real))
    assert got.id != real.id and got.usage.output_tokens == openai_table.BUILT_OUTPUT_TOKENS


@pytest.mark.design("fault.refuse_responses")
def test_a_built_responses_refusal_repeats_the_requests_own_fields(
    gated: None, loopback: Loopback
) -> None:
    """A real reply repeats what the request set; text keeps the verbosity a reply adds."""
    gate_on("refuse")
    got = responses_api(
        loopback,
        True,
        store=False,
        instructions="Be brief.",
        temperature=0.2,
        top_p=0.5,
        metadata={"row": "7"},
        tool_choice="required",
        tools=RESPONSE_SPECS,
        text={"format": {"type": "text"}},
        reasoning={"effort": "low"},
    )
    assert (got.store, got.instructions, got.temperature, got.top_p) == (
        False, "Be brief.", 0.2, 0.5,
    )  # fmt: skip
    assert (got.metadata, got.tool_choice) == ({"row": "7"}, "required")
    assert got.text.verbosity == "medium" and got.reasoning.effort == "low"
    assert got.reasoning.summary is None
    gate_on("refuse")
    plain = responses_api(loopback, True)
    assert (plain.store, plain.instructions, plain.temperature) == (True, None, 1.0)


@pytest.mark.design("fault.unknown_tool_responses")
@pytest.mark.parametrize("sync", [True, False], ids=SYNC_IDS)
def test_unknown_tool_on_responses_is_a_mistake_and_nothing_is_sent(
    gated: None, loopback: Loopback, sync: bool
) -> None:
    sink, budget = gate_on("unknown tool")
    with pytest.raises(ScrimmageMockError, match="is a Responses call, and a call to a tool"):
        responses_api(loopback, sync, tools=RESPONSE_SPECS)
    assert loopback.seen == [] and budget.asked == 0
    line = line_of(sink, "model")
    assert (line["action"], line["reason"]) == ("refused", MISTAKE_REFUSAL)
    assert "fault" not in line


def plan_on_call_1(word: str) -> Plan:
    """A run with this reply fault on the first model call only."""
    plan = plan_with(word)
    return dataclasses.replace(plan, faults=(dataclasses.replace(plan.faults[0], when="call 1"),))


@pytest.mark.design("fault.responses_chain")
@pytest.mark.parametrize("sync", [True, False], ids=SYNC_IDS)
@pytest.mark.parametrize(("word", "sent"), [("garble", 1), ("refuse", 0)], ids=["changed", "built"])
def test_a_request_chained_to_a_changed_reply_is_a_mistake_and_not_sent(
    gated: None, loopback: Loopback, word: str, sent: int, sync: bool
) -> None:
    """A changed or built reply has a made-up id, so OpenAI holds nothing under it: a request
    chained to it could not build the next turn from what the agent got."""
    sink, budget = gate_plan(plan_on_call_1(word))
    loopback.body = json.dumps(REAL_RESPONSE).encode()
    first = responses_api(loopback, sync)
    assert len(loopback.seen) == budget.asked == sent
    assert first.id != REAL_RESPONSE["id"]
    with pytest.raises(ScrimmageMockError, match="asks OpenAI to look up resp_"):
        responses_api(loopback, sync, input="And order 5678?", previous_response_id=first.id)
    assert len(loopback.seen) == budget.asked == sent  # the chained request is never sent
    faulted, chained = model_lines(sink)
    assert faulted["action"] == "fault"
    assert (chained["action"], chained["reason"]) == ("refused", MISTAKE_REFUSAL)
    assert f"look up {first.id}," in str(chained["mistake"])
    assert str(chained["mistake"]).endswith(
        "send the conversation in input instead, as the OpenAI Agents SDK does by default"
    )


@pytest.mark.design("fault.responses_chain")
@pytest.mark.parametrize("sync", [True, False], ids=SYNC_IDS)
@pytest.mark.parametrize(("word", "sent"), [("garble", 1), ("refuse", 0)], ids=["changed", "built"])
def test_an_item_reference_to_a_faulted_item_is_a_mistake_and_not_sent(
    gated: None, loopback: Loopback, word: str, sent: int, sync: bool
) -> None:
    """OpenAI reads an item_reference from the item it stored, and holds none under a faulted
    item's made-up id. A reference to another item goes on."""
    sink, budget = gate_plan(plan_on_call_1(word))
    loopback.body = json.dumps(REAL_RESPONSE).encode()
    first = responses_api(loopback, sync)
    [item] = first.output
    asked = [{"type": "item_reference", "id": item.id}, {"role": "user", "content": "More?"}]
    with pytest.raises(ScrimmageMockError, match=f"asks OpenAI to look up {item.id},"):
        responses_api(loopback, sync, input=asked)
    assert len(loopback.seen) == budget.asked == sent
    other = [{"type": "item_reference", "id": "msg_" + "2" * 50}, asked[1]]
    responses_api(loopback, sync, input=other)
    assert len(loopback.seen) == sent + 1
    assert [x["action"] for x in model_lines(sink)] == ["fault", "refused", "real"]


@pytest.mark.design("fault.responses_conversation")
@pytest.mark.parametrize("sync", [True, False], ids=SYNC_IDS)
@pytest.mark.parametrize("word", ["garble", "refuse"])
def test_a_reply_fault_in_a_responses_conversation_is_a_mistake_and_nothing_is_sent(
    gated: None, loopback: Loopback, word: str, sync: bool
) -> None:
    """OpenAI adds each reply to a conversation it keeps: the real one after a changed reply,
    none after a built one. The next turn would be built from what the agent never got."""
    sink, budget = gate_on(word)
    with pytest.raises(ScrimmageMockError, match="is a Responses call in a conversation"):
        responses_api(loopback, sync, conversation="conv_" + "3" * 50)
    assert loopback.seen == [] and budget.asked == 0
    line = line_of(sink, "model")
    assert (line["action"], line["reason"]) == ("refused", MISTAKE_REFUSAL)
    assert "fault" not in line and "send the conversation in input" in str(line["mistake"])


@pytest.mark.design("fault.responses_chain")
@pytest.mark.parametrize("sync", [True, False], ids=SYNC_IDS)
def test_a_request_chained_to_a_real_reply_goes_on(
    gated: None, loopback: Loopback, sync: bool
) -> None:
    """After a fault changed one reply, a request chained to another, real, reply is sent as
    in the baseline, and so is one chained to a reply from before the run."""
    sink, _ = gate_plan(plan_on_call_1("garble"))
    loopback.body = json.dumps(REAL_RESPONSE).encode()
    faulted = responses_api(loopback, sync)
    other = {**REAL_RESPONSE, "id": "resp_" + "0" * 50}
    loopback.body = json.dumps(other).encode()
    real = responses_api(loopback, sync, input="And order 5678?")
    assert real.id == other["id"] != faulted.id
    after = responses_api(loopback, sync, input="Thanks.", previous_response_id=real.id)
    earlier = responses_api(loopback, sync, input="Hi.", previous_response_id="resp_" + "1" * 50)
    assert after.output_text == earlier.output_text == "Order 1234 has been shipped."
    assert loopback.seen == ["/v1/responses"] * 4
    assert [x["action"] for x in model_lines(sink)] == ["fault", "real", "real", "real"]


# The three that change the real reply: each word, the real reply it changes, and what the
# request offers.
THAI = "สวัสดี"
RECORD = json.loads((Path(http_gate.__file__).parent / "schemas" / "record.v1.json").read_text())
CARRIED = [
    ("garble", REAL_ANSWER, {}),
    ("truncate", REAL_ANSWER, {}),
    ("truncate", REAL_TOOL_CALL, {"tools": SPECS}),
    ("bad tool args", REAL_TOOL_CALL, {"tools": SPECS}),
]
CARRIED_IDS = ["garble", "truncate", "truncate a tool call", "bad tool args"]
REFUSAL = json.dumps(
    {
        **json.loads(REAL_ANSWER),
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": None, "refusal": "I can't."},
                "logprobs": None,
                "finish_reason": "stop",
            }
        ],
    }
).encode()


def agent_got(server: Loopback, sync: bool, **request: Any) -> tuple[int, bytes, Any]:
    """The status, the bytes and the headers of the reply the openai library read, also when
    it raised on the status."""
    kw: dict[str, Any] = {"model": MODEL, "messages": QUESTION, **request}

    def through(raw: Any) -> tuple[int, bytes, Any]:
        response = raw.http_response
        return response.status_code, response.content, response.headers

    try:
        if sync:
            api = openai.OpenAI(base_url=server.url, api_key="test", max_retries=0)
            return through(api.chat.completions.with_raw_response.create(**kw))

        async def go() -> tuple[int, bytes, Any]:
            api = openai.AsyncOpenAI(base_url=server.url, api_key="test", max_retries=0)
            return through(await api.chat.completions.with_raw_response.create(**kw))

        return asyncio.run(go())
    except openai.APIStatusError as error:
        response = error.response
        return response.status_code, response.content, response.headers


def real_cost(reply: bytes) -> float:
    used = json.loads(reply)["usage"]
    rates = openai_table.price(MODEL, {})
    assert rates is not None
    return openai_table.cost(used["prompt_tokens"], used["completion_tokens"], rates)


@pytest.mark.design("fault.reply_not_fired")
@pytest.mark.parametrize(
    ("word", "body", "why"),
    [
        ("garble", REAL_TOOL_CALL, "the reply has only tool calls and no text"),
        ("bad tool args", REAL_ANSWER, "the reply has no tool call with arguments to break"),
        ("truncate", REFUSAL, "the reply is a refusal with no text"),
    ],
    ids=["garble on tool calls", "bad tool args on text", "truncate on a refusal"],
)
def test_a_reply_with_nothing_to_change_is_not_fired_and_says_why(
    gated: None, loopback: Loopback, word: str, body: bytes, why: str
) -> None:
    """The agent gets the real reply byte for byte, and the run is left out of the row."""
    sink, budget = gate_on(word)
    loopback.body = body
    status, got_bytes, _ = agent_got(loopback, True, tools=SPECS)
    assert (status, got_bytes) == (200, body)
    assert loopback.seen == ["/v1/chat/completions"] and budget.asked == 1
    line = line_of(sink, "model")
    assert line["action"] == "real" and "fault" not in line and "changed" not in line, line
    assert line["unchanged"] == {"do": word, "why": why}
    assert suggest.unchanged_lines([line]) == [line] and suggest.refused_mistakes([line]) == []
    assert "mistake" not in line


def written(sink: Sink, folder: Path) -> Path:
    """The side-folder items written out as a records folder holds them, for show."""
    (folder / "blobs").mkdir()
    for sha, data in sink.blobs.items():
        (folder / "blobs" / sha).write_bytes(data)
    return folder


@pytest.mark.design("record.changed_reply")
@pytest.mark.parametrize("sync", [True, False], ids=SYNC_IDS)
@pytest.mark.parametrize(("word", "real", "asked"), CARRIED, ids=CARRIED_IDS)
def test_a_changed_reply_line_keeps_the_real_reply_and_the_one_the_agent_got(
    gated: None,
    loopback: Loopback,
    tmp_path: Path,
    word: str,
    real: bytes,
    asked: dict[str, Any],
    sync: bool,
) -> None:
    sink, _ = gate_on(word)
    loopback.body = real
    status, got_bytes, headers = agent_got(loopback, sync, **asked)
    assert got_bytes != real and headers["x-request-id"] == "req_real"
    line = line_of(sink, "model")
    assert (line["action"], line["under"], line["status"]) == ("fault", "real", 200)
    changed = cast("dict[str, Any]", line["changed"])
    result = cast("dict[str, Any]", line["result"])
    assert sink.blobs[result["sha256"]] == real  # the real reply, as the model sent it
    assert sink.blobs[changed["reply"]["sha256"]] == got_bytes  # the one the agent read
    assert "text" not in changed  # a fault in a model's reply has no text of its own
    jsonschema.Draft202012Validator(RECORD).validate(line)
    assert changed.get("status") == (None if status == 200 else status)
    # show prints both, cut to fit its column: what the model said, then what the agent got.
    shown = show.came_back(line, written(sink, tmp_path))
    real_start, agent_part = {
        ("garble", 200): (
            "the model said Or...",
            f"; garble gave the agent: Order 1234 has been {THAI}",
        ),
        ("truncate", 200): (
            "the model said Or...",
            "; truncate gave the agent: Order 1234 has been",
        ),
        ("truncate", 400): (
            "the model called get_order; truncate gave the agent: a 400 error",
            "",
        ),
        ("bad tool args", 200): (
            'the model said {"ord',
            '; bad tool args gave the agent: {"order_id":"1234"',
        ),
    }[word, status]
    assert shown.startswith(real_start) and agent_part in shown, shown
    if status == 400:  # nothing cut: the whole line fits
        assert shown == real_start, shown


@pytest.mark.design("budget.carried_reply_fault")
@pytest.mark.parametrize("sync", [True, False], ids=SYNC_IDS)
@pytest.mark.parametrize(("word", "real", "asked"), CARRIED, ids=CARRIED_IDS)
def test_a_carried_reply_fault_reserves_and_bills_as_the_baseline(
    gated: None,
    loopback: Loopback,
    word: str,
    real: bytes,
    asked: dict[str, Any],
    sync: bool,
) -> None:
    """The real call is sent and billed: reserved once before it is sent, settled once at the
    real reply's usage, as in a run with no fault."""
    loopback.body = real
    sink, budget = gate_on(word)
    lines: list[dict[str, object]] = []
    settles: list[list[float]] = []
    for plan in (plan_with(word), plan_with(None)):  # the same gate, server and request
        state.install(state.RunState(plan, sink, []))
        loopback.seen.clear()
        sink.lines.clear()
        sink.settles.clear()
        budget.asked = 0
        agent_got(loopback, sync, **asked)
        assert loopback.seen == ["/v1/chat/completions"], "the real call was not sent once"
        assert budget.asked == 1, "the real call was not reserved once"
        lines.append(line_of(sink, "model"))
        settles.append([usd for _, usd in sink.settles])
    faulted, baseline = lines
    assert settles[0] == settles[1] == [pytest.approx(real_cost(real))]
    assert faulted["tokens"] == baseline["tokens"]
    assert faulted["cost_usd"] == baseline["cost_usd"] == round(real_cost(real), 8)
    assert faulted["cost_estimated"] is baseline["cost_estimated"] is False
    assert faulted["action"] == "fault" and baseline["action"] == "real"


SK = "sk-proj-" + "a1B2c3D4e5F6g7H8i9J0"  # a key by its pattern
SCRUB = "acme-vault-pass-7781"  # a value the plan's scrub names


def kept_secrets(sink: Sink, line: dict[str, object]) -> list[str]:
    """Which of the two secrets the line, its real reply or the agent's reply still holds."""
    changed = cast("dict[str, Any]", line["changed"])
    result = cast("dict[str, Any]", line["result"])
    held = [
        json.dumps(line).encode(),
        sink.blobs[result["sha256"]],
        sink.blobs[changed["reply"]["sha256"]],
    ]
    return [s for s in (SK, SCRUB) if any(s.encode() in data for data in held)]


@pytest.mark.design("record.changed_reply_redacted")
@pytest.mark.parametrize("sync", [True, False], ids=SYNC_IDS)
def test_a_changed_model_reply_is_redacted_as_the_result_is(
    gated: None, loopback: Loopback, sync: bool
) -> None:
    """The agent reads both secrets; neither reply on the record holds them."""
    real = json.loads(REAL_ANSWER)
    said = f"Key {SK} and {SCRUB} were in the note for order 1234."
    real["choices"][0]["message"]["content"] = said
    loopback.body = json.dumps(real).encode()
    sink, _ = gate_plan(dataclasses.replace(plan_with("garble"), scrub=(SCRUB,)))
    reply, _ = completion(loopback, sync)
    text = reply.choices[0].message.content
    assert SK in text and SCRUB in text and THAI in text  # garble hit the fifth word only
    line = line_of(sink, "model")
    leaked = kept_secrets(sink, line)
    assert not leaked, "a secret is on the record"
    reply_blob = sink.blobs[cast("dict[str, Any]", line["changed"])["reply"]["sha256"]]
    assert reply_blob.count(REMOVED.encode()) == 2


@pytest.mark.design("record.changed_reply_redacted")
@pytest.mark.parametrize(("name", "sync"), PATHS, ids=PATH_IDS)
def test_a_changed_web_reply_is_redacted_as_the_result_is(
    gated: None, name: str, sync: bool
) -> None:
    fault = Fault("http:inventory.acme.internal", "replace", "always", text="12", find="none")
    plan = Plan(
        "run-1", "t", "row", (), (fault,), (), (), 60.0, scrub=(SCRUB,),
    )  # fmt: skip
    sink, _ = gate_plan(plan)
    module: Any = LIBRARIES[name]
    body = {"sku": "SKU-42", "left": "none", "key": SK, "note": SCRUB}

    def answer(request: Any) -> Any:
        return module.Response(200, json=body)

    transport = module.MockTransport(answer)
    if sync:
        response = module.Client(transport=transport).get(WEB)
    else:

        async def go() -> Any:
            async with module.AsyncClient(transport=transport) as client:
                return await client.get(WEB)

        response = asyncio.run(go())
    assert response.json() == {**body, "left": "12"}  # the agent reads both secrets
    line = line_of(sink, "http")
    assert line["action"] == "fault"
    leaked = kept_secrets(sink, line)
    assert not leaked, "a secret is on the record"


ENCODED = [
    pytest.param("gzip", id="gzip"),
    pytest.param(
        "br",
        id="br",
        marks=[
            pytest.mark.needs_brotli,
            pytest.mark.skipif(
                changes.brotli_module() is None,
                reason="br needs brotli or brotlicffi 1.2 or later; run with uv run --with brotli",
            ),
        ],
    ),
]
REAL_BR = (FIXTURES / "real_chat_answer_br.bin").read_bytes()  # as OpenAI sent it, in br


@pytest.mark.design("fault.reply_encoded")
@pytest.mark.parametrize("sync", [True, False], ids=SYNC_IDS)
@pytest.mark.parametrize("encoding", ENCODED)
def test_a_gzip_and_a_br_reply_are_changed_and_sent_unencoded(
    gated: None, loopback: Loopback, encoding: str, sync: bool
) -> None:
    """The br reply is a real one, as OpenAI sent it to a client that offers br. The agent's
    copy has no content-encoding, so its client reads it as plain JSON."""
    if encoding == "gzip":
        plain = REAL_ANSWER
        loopback.body = gzip.compress(REAL_ANSWER)
    else:
        brotli = changes.brotli_module()
        assert brotli is not None
        plain = brotli.decompress(REAL_BR)
        loopback.body = REAL_BR
    loopback.headers = {**JSON_HEADERS, "content-encoding": encoding}
    sink, _ = gate_on("garble")
    reply, got_bytes = completion(loopback, sync)
    assert THAI in reply.choices[0].message.content
    line = line_of(sink, "model")
    assert sink.blobs[cast("dict[str, Any]", line["result"])["sha256"]] == plain
    changed = cast("dict[str, Any]", line["changed"])
    assert sink.blobs[changed["reply"]["sha256"]] == got_bytes
    used = json.loads(plain)["usage"]
    assert line["tokens"] == {
        "input": used["prompt_tokens"],
        "output": used["completion_tokens"],
        "cached": 0,
    }
    _, _, headers = agent_got(loopback, sync)
    assert "content-encoding" not in headers
    assert headers["content-length"] == str(len(got_bytes))


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
