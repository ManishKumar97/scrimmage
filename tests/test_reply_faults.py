"""empty on a web request and bad body on a web request or a model call, in one process: the
reply the code gets, the line, and that nothing is sent, reserved or fired by mistake."""

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx2
import openai
import pytest

from scrimmage_core import config, http_gate
from scrimmage_core.config import Fault
from scrimmage_core.errors import ConfigError, ScrimmageMockError
from scrimmage_core.provider_openai import BROKEN_BODY
from test_connection_faults import LIBRARIES, PATH_IDS, PATHS, WEB, gated, install
from test_http_gate import BODY, URL, Sink

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
    assert line["action"] != "fault", "a fault that was not carried out counted as fired"
    assert "streamed request" in str(line["mistake"])
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
