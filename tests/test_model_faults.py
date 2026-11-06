"""Model faults through the real openai library, against the fake server."""

import json
import shutil
from collections.abc import Callable

import pytest

from conftest import ACME, Project, Result
from fakes.openai_server import FakeOpenAI
from faultline_sdk import config
from faultline_sdk.runner import Runner

Fake = Callable[..., FakeOpenAI]
TOOL_FAULT = '[[test.fault]]\ncall = "tool:get_order"\ndo   = "timeout"\n'


def use(project: Project, function: str, fault: str = "", extra: str = "") -> None:
    """One run per row of this function, with this fault as the only fault."""
    project.edit('function = "acme.agent:answer"', f'function = "{function}"\nruns = 1{extra}')
    project.edit(TOOL_FAULT, fault)


def model_fault(do: str, when: str, call: str = "model") -> str:
    return f'[[test.fault]]\ncall = "{call}"\ndo   = "{do}"\nwhen = "{when}"\n'


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
    assert result.code == 0, result.out + result.err
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
    shutil.rmtree(project.root / "faultline-runs")
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


@pytest.mark.design("model.embeddings_labelled_real")
def test_an_embedding_runs_for_real_and_is_labelled(project: Project, fake_openai: Fake) -> None:
    use(project, "acme.rag:answer")
    result = project.test(fake_openai(ACME, embedding="embedding"))
    first = next(x for x in result.records()[0] if x.get("kind") == "model")
    assert (first["call"], first["action"]) == ("model:openai/embeddings", "real")
    assert first["tokens"] == {"input": 8, "output": 0}


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
    blobs = next((project.root / "faultline-runs").glob("*/blobs"))
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
    assert "does not have the functions Faultline gates" in result.out
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
