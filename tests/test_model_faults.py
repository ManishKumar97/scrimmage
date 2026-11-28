"""Model faults through the real openai library, against the fake server."""

import asyncio
import datetime
import json
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2
import openai
import pytest

from conftest import ACME, EXAMPLE_FAULT, Project, Result
from fakes.openai_server import FakeOpenAI
from scrimmage import config
from scrimmage import runner as runner_module
from scrimmage.errors import ConfigError
from scrimmage.runner import Runner
from test_connection_faults import gated, install
from test_http_gate import Sink

__all__ = ["gated"]  # the fixture, used by name

# Every test here, or nearly, starts a process or a real scrimmage test run.
pytestmark = pytest.mark.slow

Fake = Callable[..., FakeOpenAI]
TOOL_FAULT = EXAMPLE_FAULT


def use(project: Project, function: str, fault: str = "", extra: str = "") -> None:
    """One run per row of this function, with this fault as the only fault."""
    project.edit('function = "acme.agent:answer"', f'function = "{function}"\nruns = 1{extra}')
    project.edit(TOOL_FAULT, fault)


def model_fault(do: str, when: str, call: str = "model") -> str:
    block = f'[[test.fault]]\ncall = "{call}"\ndo   = "{do}"\nwhen = "{when}"\n'
    return f'{block}forbid = ["never said"]\n'


def row(result: Result, name: str) -> list[dict[str, object]]:
    return next(lines for lines in result.records() if lines[-1]["row"] == name)


def calls(lines: list[dict[str, object]]) -> list[tuple[object, object, object]]:
    return [(x["call"], x["action"], x.get("retry")) for x in lines if x.get("kind") == "model"]


def answer(lines: list[dict[str, object]]) -> object:
    summary = lines[-1]["summary"]
    assert isinstance(summary, dict)
    return summary["answer"]


@pytest.mark.design("model.status_429_first_3")
def test_first_3_outlasts_the_librarys_retries_and_the_agent_sees_rate_limit_error(
    project: Project, fake_openai: Fake
) -> None:
    use(project, "acme.rag:answer", model_fault("status 429", "first 3"))
    fake = fake_openai(ACME, embedding="embedding")
    result = project.test(fake)
    assert result.finished, result.out + result.err
    lines = row(result, "fault 1")
    assert calls(lines) == [
        ("model:openai/embeddings", "real", 0),
        ("model:openai", "fault", 0),
        ("model:openai", "fault", 1),  # the library's own retries meet the gate
        ("model:openai", "fault", 2),
    ]
    assert answer(lines) == "error: RateLimitError"
    # The fake saw the baseline's calls and the embedding only: a fault sends nothing.
    assert fake.paths.count("/v1/chat/completions") == 3


@pytest.mark.design("proof.real_against_injected_429")
@pytest.mark.parametrize(
    ("function", "good", "bad"),
    [
        ("acme.rag:answer_streamed", "chat_stream", "429:error_429"),
        ("acme.rag:answer_twice", "chat_answer", "429:error_429"),
    ],
    ids=["streaming path", "plain path"],
)
def test_a_real_429_and_the_injected_one_end_the_same_way(
    project: Project, fake_openai: Fake, function: str, good: str, bad: str
) -> None:
    # Real: the server itself answers 429 on every attempt. The run counts as a real
    # model error, so it is an infrastructure error, but the agent's view is what matters.
    use(project, function)
    real_fake = fake_openai([bad])
    real = project.test(real_fake)
    real_lines = real.records()[0]
    attempts = real_fake.paths.count("/v1/chat/completions")

    # Injected: the same run, with the gate answering every attempt.
    shutil.rmtree(project.root / "scrimmage-runs")
    project.toml.write_text(project.toml.read_text() + model_fault("status 429", "always"))
    injected = project.test(fake_openai([good]))
    injected_lines = row(injected, "fault 1")

    assert answer(injected_lines) == answer(real_lines)
    assert "RateLimitError" in str(answer(real_lines))
    # The same number of attempts, so the library retried the same way.
    faults = [x for x in injected_lines if x.get("action") == "fault"]
    assert len(faults) == attempts
    assert real.code == 3  # a 429 nobody injected is the provider's, not the agent's
    assert "not injected" in real.out


@pytest.mark.design("model.fault_can_end")
def test_a_fault_on_the_first_attempt_only_is_absorbed_by_the_librarys_retry(
    project: Project, fake_openai: Fake
) -> None:
    use(project, "acme.rag:answer", model_fault("status 429", "first 1"))
    result = project.test(fake_openai(ACME, embedding="embedding"))
    lines = row(result, "fault 1")
    assert [x[1] for x in calls(lines)[1:3]] == ["fault", "real"]
    assert "shipped" in str(answer(lines))


def hi(api: openai.OpenAI) -> openai.APIStatusError:
    """One chat call; the status error it raised."""
    with pytest.raises(openai.APIStatusError) as caught:
        api.chat.completions.create(
            model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}]
        )
    return caught.value


@pytest.mark.design("model.retry_after_header")
def test_retry_after_sends_the_header_with_the_429(gated: None) -> None:
    """The header a rate limiter sends, in seconds, the form RFC 9110 gives. The openai
    library reads its own retry-after-ms first, so sending both would make one of them
    the wait; only retry-after is sent."""
    for do, error in (
        ("status 429", openai.RateLimitError),
        ("status 503", openai.InternalServerError),
    ):
        sink, budget = install(do, "model", retry_after=30)
        api = openai.OpenAI(api_key="test", max_retries=0)  # one attempt: the reply itself
        raised = hi(api)
        assert type(raised) is error
        assert raised.response.status_code == int(do.split()[1])
        assert raised.response.headers["retry-after"] == "30"
        assert "retry-after-ms" not in raised.response.headers
        [line] = [x for x in sink.lines if x.get("kind") == "model"]
        assert line["fault"] == {"do": do, "when": "always", "n": 1, "retry_after": 30}
        assert budget.asked == 0  # nothing reserved: nothing was sent
    install("status 429", "model")
    assert "retry-after" not in hi(openai.OpenAI(api_key="test", max_retries=0)).response.headers


def sent_only_the_real_calls(result: Result, fake: FakeOpenAI) -> None:
    """The fake provider got one request per real model line, in every row, so a faulted
    attempt that reached it would be one too many."""
    real = [
        str(x["call"])
        for lines in result.records()
        for x in lines
        if x.get("kind") == "model" and x.get("action") == "real"
    ]
    names = {"/v1/embeddings": "model:openai/embeddings", "/v1/chat/completions": "model:openai"}
    assert sorted(names[p] for p in fake.paths) == sorted(real), (fake.paths, real)


def started(line: dict[str, object]) -> datetime.datetime:
    return datetime.datetime.fromisoformat(str(line["start"]).replace("Z", "+00:00"))


@pytest.mark.design("model.retry_after_waits")
def test_the_openai_library_waits_retry_after_before_its_retry(
    project: Project, fake_openai: Fake
) -> None:
    """The one real wait in this file, 2 s: the library's own first backoff is at most
    0.5 s, so a gap of 2 s or more is the header's."""
    wait = model_fault("status 429", "first 1") + "retry_after = 2\n"
    use(project, "acme.rag:answer", wait)
    fake = fake_openai(ACME, embedding="embedding")
    result = project.test(fake)
    assert result.finished, result.out + result.err
    sent_only_the_real_calls(result, fake)
    lines = row(result, "fault 1")
    chats = [x for x in lines if x.get("call") == "model:openai"]
    assert [(x["action"], x["retry"]) for x in chats[:2]] == [("fault", 0), ("real", 1)]
    gap = (started(chats[1]) - started(chats[0])).total_seconds()
    assert 2.0 <= gap < 30, gap  # the ceiling is loose: a loaded machine is slow, not wrong
    assert "shipped" in str(answer(lines))


@pytest.mark.design("model.retry_after_over_120")
def test_a_retry_after_over_120_seconds_stops_the_openai_library_retrying(
    project: Project, fake_openai: Fake
) -> None:
    """openai 3.x does not retry when the server asks for more than two minutes. No wait
    happens, so the test is fast; without the header the library would retry at once."""
    wait = model_fault("status 429", "first 1") + "retry_after = 121\n"
    use(project, "acme.rag:answer", wait, extra="\nrun_timeout = 200")
    begun = time.monotonic()
    fake = fake_openai(ACME, embedding="embedding")
    result = project.test(fake)
    assert result.finished, result.out + result.err
    sent_only_the_real_calls(result, fake)
    lines = row(result, "fault 1")
    assert calls(lines) == [("model:openai/embeddings", "real", 0), ("model:openai", "fault", 0)]
    assert answer(lines) == "error: RateLimitError"
    assert time.monotonic() - begun < 120  # it did not wait


@pytest.mark.design("model.embeddings_labelled_real")
def test_an_embedding_runs_for_real_and_is_labelled(project: Project, fake_openai: Fake) -> None:
    use(project, "acme.rag:answer")
    result = project.test(fake_openai(ACME, embedding="embedding"))
    first = next(x for x in result.records()[0] if x.get("kind") == "model")
    assert (first["call"], first["action"]) == ("model:openai/embeddings", "real")
    assert first["tokens"] == {"input": 8, "output": 0, "cached": 0}


@pytest.mark.design("model.fault_on_embeddings")
def test_a_fault_can_name_the_embedding_and_leaves_chat_alone(
    project: Project, fake_openai: Fake
) -> None:
    use(project, "acme.rag:answer", model_fault("status 500", "always", "model:*/embeddings"))
    result = project.test(fake_openai(ACME, embedding="embedding"))
    lines = row(result, "fault 1")
    assert {x[0] for x in calls(lines)} == {"model:openai/embeddings"}
    assert answer(lines) == "error: InternalServerError"


@pytest.mark.design("model.timeout")
def test_a_timeout_fault_raises_the_librarys_own_timeout(
    project: Project, fake_openai: Fake
) -> None:
    use(project, "acme.rag:answer", model_fault("timeout", "first 3"))
    result = project.test(fake_openai(ACME, embedding="embedding"))
    lines = row(result, "fault 1")
    assert answer(lines) == "error: APITimeoutError"
    fault = next(x for x in lines if x.get("action") == "fault")
    assert str(fault["error"]).startswith("ReadTimeout")


@pytest.mark.design("model.status_other")
@pytest.mark.parametrize(
    ("status", "error"), [(500, "InternalServerError"), (400, "BadRequestError")]
)
def test_other_statuses_reach_the_agent_as_the_librarys_own_errors(
    project: Project, fake_openai: Fake, status: int, error: str
) -> None:
    use(project, "acme.rag:answer", model_fault(f"status {status}", "always"))
    result = project.test(fake_openai(ACME, embedding="embedding"))
    assert answer(row(result, "fault 1")) == f"error: {error}"


@pytest.mark.design("schedule.requests_end_to_end")
def test_requests_1_fails_one_of_two_parallel_calls_and_all_its_retries(
    project: Project, fake_openai: Fake
) -> None:
    use(project, "acme.rag:answer_twice", model_fault("status 429", "requests 1"))
    fake = fake_openai(["chat_answer"])
    result = project.test(fake)
    lines = row(result, "fault 1")
    actions = [x[1] for x in calls(lines)]
    assert actions.count("fault") == 3  # the first attempt and its two retries
    assert actions.count("real") == 1  # the other request
    assert "error: RateLimitError" in str(answer(lines))
    assert "Hello" in str(answer(lines))


@pytest.mark.design("model.host_write_refused")
def test_a_call_that_creates_something_at_the_provider_is_refused(
    project: Project, fake_openai: Fake
) -> None:
    use(project, "acme.rag:answer_tuning", extra='\nmax_cost_usd = "none"')
    result = project.test(fake_openai([]))
    refused = [x for x in result.records()[0] if x.get("action") == "refused"]
    assert refused[0]["call"] == "http:127.0.0.1" and refused[0]["reason"] == "web_write"
    assert result.code == 2


@pytest.mark.design("model.real_error_is_infrastructure")
def test_a_real_error_the_gate_did_not_inject_is_infrastructure(
    project: Project, fake_openai: Fake
) -> None:
    use(project, "acme.rag:answer_twice")
    result = project.test(fake_openai(["500:error_500"]))
    assert result.code == 3
    assert {s["outcome"] for s in result.summaries()} == {"infrastructure error"}


@pytest.mark.design("record.model_side_folder")
def test_the_model_request_and_reply_are_kept_in_the_side_folder(
    project: Project, fake_openai: Fake
) -> None:
    use(project, "acme.rag:answer")
    result = project.test(fake_openai(ACME, embedding="embedding"))
    line = next(x for x in result.records()[0] if x.get("call") == "model:openai")
    blobs = next((project.root / "scrimmage-runs").glob("*/blobs"))
    request, reply = line["args"], line["result"]
    assert isinstance(request, dict) and isinstance(reply, dict)
    sent = json.loads((blobs / str(request["sha256"])).read_text())
    assert sent["messages"][0]["content"] == "Where is order 1234?"
    assert "get_order" in (blobs / str(reply["sha256"])).read_text()


@pytest.mark.design("model.transport_error_is_infrastructure")
def test_a_real_connection_failure_the_agent_swallows_is_infrastructure(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    use(project, "acme.rag:answer_twice")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    result = project.test()
    assert result.code == 3, result.out
    assert "failed with ConnectError, not injected" in result.out


@pytest.mark.design("gate.structural_stops_end_to_end")
def test_a_library_that_changed_stops_the_test_at_once(project: Project) -> None:
    (project.root / "httpx2.py").write_text('__version__ = "9.0.0"\n')
    project.edit(
        'function = "acme.agent:answer"',
        'function = "acme.slow:use_httpx2"\nruns = 3\nparallel = 1\nmax_cost_usd = "none"',
    )
    result = project.test()
    assert result.code == 3, result.out
    assert "does not have the functions Scrimmage gates" in result.out
    assert len(result.records()) == 1  # no other run started
    assert result.summaries()[0]["outcome"] == "infrastructure error"


def test_no_run_starts_after_a_stop_however_late_the_servers_thread_is(project: Project) -> None:
    (project.root / "httpx2.py").write_text('__version__ = "9.0.0"\n')
    project.edit(
        'function = "acme.agent:answer"',
        'function = "acme.slow:use_httpx2"\nruns = 3\nparallel = 1\nmax_cost_usd = "none"',
    )
    project.monkeypatch.chdir(project.root)
    runner = Runner(config.load(project.toml))
    runner.server.on_refused = lambda run, reason: None  # that thread has not run yet
    assert len(runner.run_all()) == 1


def test_no_run_starts_while_a_stop_is_still_stopping_a_program(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    (project.root / "httpx2.py").write_text('__version__ = "9.0.0"\n')
    project.edit(
        'function = "acme.agent:answer"',
        'function = "acme.slow:use_httpx2"\nruns = 3\nparallel = 1\nmax_cost_usd = "none"',
    )
    project.monkeypatch.chdir(project.root)
    stop_group = runner_module.stop_group

    def slow_stop(proc: subprocess.Popen[bytes]) -> None:
        time.sleep(1)
        stop_group(proc)

    monkeypatch.setattr(runner_module, "stop_group", slow_stop)
    assert len(Runner(config.load(project.toml)).run_all()) == 1


@pytest.mark.design("gate.structural_notice_fails_the_test")
def test_a_library_that_cannot_build_faults_fails_a_run_that_ended_cleanly(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    (project.root / "httpx2.py").write_text(
        "class Client:\n    def _send_single_request(self, request): ...\n\n\n"
        "class AsyncClient:\n    async def _send_single_request(self, request): ...\n\n\n"
        "class SyncByteStream: ...\n\n\nclass AsyncByteStream: ...\n"
    )
    project.edit(
        'function = "acme.agent:answer"',
        'function = "acme.slow:use_httpx2"\nruns = 1\nmax_cost_usd = "none"',
    )
    project.edit(TOOL_FAULT, "")
    monkeypatch.setattr(Runner, "_refused", lambda self, run, reason: None)  # let it finish
    result = project.test()
    assert result.summaries()[0]["outcome"] == "infrastructure error"
    assert result.code == 3


# A client that retries a failed request once and marks nothing, as NeMo Guardrails' does.
UNMARKED_CLIENT = """import os

import httpx


def answer(question):
    for _ in range(2):
        reply = httpx.post(
            os.environ["OPENAI_BASE_URL"] + "/chat/completions",
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": question}]},
            headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"]},
        )
        if reply.status_code < 500:
            return reply.json()["choices"][0]["message"]["content"]
    return "the model is down"
"""


def unmarked(project: Project, when: str) -> None:
    (project.root / "unmarked.py").write_text(UNMARKED_CLIENT)
    project.toml.write_text(
        '[agent]\nfunction = "unmarked:answer"\nruns = 1\n'
        'model_hosts = [{host = "127.0.0.1", provider = "openai"}]\n\n'
        '[[test]]\nname = "t"\nquestion = "Say hello"\nexpect = ["Hello"]\n\n'
        f'[[test.fault]]\ncall = "model"\ndo = "status 500"\nwhen = "{when}"\n'
        'expect = ["down"]\n'
    )


def test_requests_n_on_a_client_that_marks_no_retries_is_a_test_mistake(
    project: Project, fake_openai: Fake
) -> None:
    unmarked(project, "requests 1")
    result = project.test(fake_openai(["chat_answer"]))
    assert result.code == 2, result.out + result.err
    assert "does not mark its retries, so requests N counted each attempt as a request" in (
        result.out
    )
    assert "use first N or always" in result.out
    # The call is decided as before: the retry counts as request 2, so it runs for real.
    assert calls(row(result, "fault 1")) == [
        ("model:openai", "fault", 0),
        ("model:openai", "real", 0),
    ]


def test_first_n_on_that_client_counts_attempts(project: Project, fake_openai: Fake) -> None:
    unmarked(project, "first 2")
    result = project.test(fake_openai(["chat_answer"]))
    assert result.code == 0, result.out + result.err
    assert calls(row(result, "fault 1")) == [("model:openai", "fault", 0)] * 2
    assert answer(row(result, "fault 1")) == "the model is down"
    assert "does not mark its retries" not in result.out


FIXTURES = Path(__file__).parent / "fixtures" / "openai"
SYNC = {"argvalues": [True, False], "ids": ["sync", "async"]}


def captured(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def raised_by(sync: bool, reached: list[object], *, responses: bool = False) -> BaseException:
    """One chat or Responses call through the openai library, sync or async, to a provider
    that keeps every request that reaches it; what the call raised."""

    def provider(request: httpx2.Request) -> httpx2.Response:
        reached.append(request)
        return httpx2.Response(200, json={})

    transport = httpx2.MockTransport(provider)

    def create(api: Any) -> Any:
        if responses:
            return api.responses.create(model="gpt-4o-mini", input="hi")
        return api.chat.completions.create(
            model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}]
        )

    async def go() -> None:
        async with httpx2.AsyncClient(transport=transport) as http:
            await create(openai.AsyncOpenAI(api_key="test", http_client=http))

    with pytest.raises(Exception) as caught:
        if sync:
            create(openai.OpenAI(api_key="test", http_client=httpx2.Client(transport=transport)))
        else:
            asyncio.run(go())
    return caught.value


def model_lines(sink: Sink) -> list[dict[str, object]]:
    return [x for x in sink.lines if x.get("kind") == "model"]


@pytest.mark.design("model.context_too_long")
@pytest.mark.parametrize("sync", **SYNC)
def test_context_too_long_raises_bad_request_with_the_real_code(gated: None, sync: bool) -> None:
    real = captured("real_error_context_length")["body"]["error"]
    sink, budget = install("context too long", "model")
    reached: list[object] = []
    error = raised_by(sync, reached)
    assert type(error) is openai.BadRequestError
    assert error.code == "context_length_exceeded" == real["code"]
    assert error.param == "messages" == real["param"]
    assert error.type == real["type"]
    assert error.message == f"Error code: 400 - {real['message']}"
    assert reached == [], "the faulted request reached the provider"
    # One attempt: the library does not retry a 400.
    assert [(x["action"], x.get("status")) for x in model_lines(sink)] == [("fault", 400)]
    assert budget.asked == 0 and sink.settles == []


@pytest.mark.design("model.context_too_long")
@pytest.mark.parametrize("sync", **SYNC)
def test_context_too_long_on_a_responses_call_raises_the_real_responses_error(
    gated: None, sync: bool
) -> None:
    real = captured("real_response_error_context_length")["body"]["error"]
    chat = captured("real_error_context_length")["body"]["error"]
    assert real["param"] == "input" != chat["param"], "Responses names its own parameter"
    sink, budget = install("context too long", "model")
    reached: list[object] = []
    error = raised_by(sync, reached, responses=True)
    assert type(error) is openai.BadRequestError
    assert error.code == "context_length_exceeded" == real["code"]
    assert error.param == "input" == real["param"]
    assert error.type == real["type"]
    assert error.message == f"Error code: 400 - {real['message']}"
    assert reached == [], "the faulted request reached the provider"
    # Fired, once: the library does not retry a 400.
    assert [(x["action"], x.get("status")) for x in model_lines(sink)] == [("fault", 400)]
    assert budget.asked == 0 and sink.settles == []


@pytest.mark.design("model.context_too_long")
@pytest.mark.parametrize(
    "call",
    [
        "http:inventory.acme.internal",
        "model:*/embeddings",
        "model:openai/moderations",
        "tool:get_order",
        "mcp:docs/search",
    ],
)
def test_context_too_long_is_refused_on_a_web_request_and_a_read_call(call: str) -> None:
    table: dict[str, object] = {"call": call, "do": "context too long", "when": "first 1"}
    with pytest.raises(ConfigError, match=r"'context too long' works only on a chat model call"):
        config.read_fault(table, "[[test.fault]] 1")
    for chat in ("model", "model:openai"):
        assert config.read_fault({**table, "call": chat}, "x").do == "context too long"


@pytest.mark.design("model.status_401_auth")
@pytest.mark.parametrize("sync", **SYNC)
def test_status_401_is_the_real_authentication_error(gated: None, sync: bool) -> None:
    real = captured("real_error_401")
    sink, budget = install("status 401", "model")
    reached: list[object] = []
    error = raised_by(sync, reached)
    assert type(error) is openai.AuthenticationError
    assert error.code == "invalid_api_key" == real["body"]["error"]["code"]
    assert error.type == "invalid_request_error" and error.param is None
    assert error.message.startswith("Error code: 401 - Incorrect API key provided: sk-proj-")
    assert error.response.headers["content-type"] == real["headers"]["content-type"]
    assert reached == [], "the faulted request reached the provider"
    # One attempt: the library does not retry a 401.
    assert [(x["action"], x.get("status")) for x in model_lines(sink)] == [("fault", 401)]
    assert budget.asked == 0 and sink.settles == []


@pytest.mark.design("model.context_too_long")
def test_a_run_under_context_too_long_sends_the_provider_only_the_real_calls(
    project: Project, fake_openai: Fake
) -> None:
    use(project, "acme.rag:answer", model_fault("context too long", "first 1"))
    fake = fake_openai(ACME, embedding="embedding")
    result = project.test(fake)
    assert result.finished, result.out + result.err
    sent_only_the_real_calls(result, fake)
    lines = row(result, "fault 1")
    assert calls(lines) == [("model:openai/embeddings", "real", 0), ("model:openai", "fault", 0)]
    assert answer(lines) == "error: BadRequestError"
