"""The faults that need no real reply, on an Anthropic call: a status and a body that does not
parse, and the fault that never can be built, which refuses and names Anthropic.

The anthropic library reads each as it would a real one: the class of the error follows the
status, the body's type is Anthropic's own, and the request id header is there."""

import json
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest

from model_gate import Gate, Provider, anthropic_client
from scrimmage import provider_anthropic, state
from scrimmage.config import Fault
from scrimmage.prices import Rates

FIXTURES = Path(__file__).parent / "fixtures" / "anthropic"
MODEL = "claude-test"


@pytest.fixture(autouse=True)
def gated() -> Any:
    yield
    state.install(None)


def call(word: str, **kw: Any) -> tuple[Gate, Provider, anthropic.Anthropic]:
    run = Gate(Fault("model", word, "always", **kw), hosts=(), prices=((MODEL, Rates(3.0, 15.0)),))
    provider = Provider(run.events, lambda r: httpx2.Response(200, content=b"{}"))
    return run, provider, anthropic_client(provider)


def ask(client: anthropic.Anthropic) -> anthropic.types.Message:
    return client.messages.create(
        model=MODEL, max_tokens=5, messages=[{"role": "user", "content": "hi"}]
    )


@pytest.mark.parametrize(
    ("status", "error", "kind"),
    [
        (400, anthropic.BadRequestError, "invalid_request_error"),
        (401, anthropic.AuthenticationError, "authentication_error"),
        (403, anthropic.PermissionDeniedError, "permission_error"),
        (404, anthropic.NotFoundError, "not_found_error"),
        (409, anthropic.ConflictError, "conflict_error"),
        (413, anthropic.ContentTooLargeError, "request_too_large"),
        (429, anthropic.RateLimitError, "rate_limit_error"),
        (500, anthropic.InternalServerError, "api_error"),
        (529, anthropic.OverloadedError, "overloaded_error"),
    ],
)
def test_a_status_fault_is_the_error_the_library_raises_for_that_status(
    status: int, error: type[Exception], kind: str
) -> None:
    run, provider, client = call(f"status {status}")
    with pytest.raises(error) as caught:
        ask(client)
    assert provider.sent == [], "the faulted request never reached the provider"
    raised: Any = caught.value
    assert raised.status_code == status
    assert raised.body["error"]["type"] == kind
    assert raised.body["type"] == "error"
    assert raised.request_id == "req_scrimmage_injected", "the request-id header is there"
    assert run.line["status"] == status


def test_a_status_fault_carries_its_retry_after() -> None:
    run = Gate(
        Fault("model", "status 529", "always", retry_after=7),
        hosts=(),
        prices=((MODEL, Rates(3.0, 15.0)),),
    )
    provider = Provider(run.events, lambda r: httpx2.Response(200, content=b"{}"))
    client = anthropic_client(provider)
    with pytest.raises(anthropic.OverloadedError) as caught:
        ask(client)
    assert caught.value.response.headers["retry-after"] == "7"


def test_the_injected_401_has_the_shape_of_the_real_one() -> None:
    """Every key and every value but the request id, which is the injected reply's own."""
    real = json.loads((FIXTURES / "real_error_401.json").read_text())
    injected = json.loads(provider_anthropic.error_body(401))
    assert set(injected) == set(real["body"])
    assert injected["type"] == real["body"]["type"]
    assert injected["error"] == real["body"]["error"]
    assert provider_anthropic.error_content_type(401) == real["headers"]["content-type"]
    assert injected["request_id"].startswith("req_")


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (402, "billing_error"),
        (413, "request_too_large"),
        (504, "timeout_error"),
        (418, "invalid_request_error"),
        (503, "api_error"),
    ],
)
def test_the_error_type_follows_the_status_and_any_other_status_falls_back(
    status: int, kind: str
) -> None:
    body = json.loads(provider_anthropic.error_body(status))
    assert body["error"]["type"] == kind
    assert body["error"]["message"]


def test_a_bad_body_is_the_start_of_a_real_reply_and_does_not_parse() -> None:
    run, provider, client = call("bad body")
    with pytest.raises(Exception) as caught:  # the library fails on the JSON it cannot read
        ask(client)
    assert provider.sent == []
    assert run.line["status"] == 200
    cut = provider_anthropic.broken_body("/v1/messages", "chat")
    assert len(cut) == 60
    with pytest.raises(ValueError):
        json.loads(cut)
    real = (FIXTURES / "real_answer.json").read_bytes()
    assert real.startswith(cut), "it is the start of a real reply, byte for byte"
    assert caught.value is not None


def test_a_prompt_over_the_context_window_is_a_400_invalid_request() -> None:
    run, provider, client = call("context too long")
    with pytest.raises(anthropic.BadRequestError) as caught:
        ask(client)
    assert provider.sent == []
    body: Any = caught.value.body
    assert body["error"]["type"] == "invalid_request_error"
    real = json.loads((FIXTURES / "real_context_too_long.json").read_text())
    assert body["error"]["message"] == real["body"]["error"]["message"]
    assert run.line["status"] == 400


def test_bad_tool_args_is_a_mistake_at_the_call_and_says_why_it_can_never_be_built() -> None:
    run, provider, client = call("bad tool args")
    with pytest.raises(anthropic.APIConnectionError) as caught:
        ask(client)
    said = str(caught.value.__cause__)
    assert "input is a JSON object" in said
    assert "not yet" not in said
    assert provider.sent == [], "nothing was sent"
    assert run.line["reason"] == "mistake"


def test_a_streamed_bad_tool_args_gets_the_reason_that_holds_not_turn_streaming_off() -> None:
    """Turning streaming off would not help: the reason that holds for every reply comes first."""
    run, provider, client = call("bad tool args")
    with pytest.raises(anthropic.APIConnectionError) as caught:
        client.messages.create(
            model=MODEL, max_tokens=5, messages=[{"role": "user", "content": "hi"}], stream=True
        )
    said = str(caught.value.__cause__)
    assert "input is a JSON object" in said
    assert "turn streaming off" not in said
    assert provider.sent == [], "nothing was sent"
    assert run.line["reason"] == "mistake"


# The library's default is 2 retries, so 3 attempts in one create(). The gate counts attempts, so
# first N faults the first N attempts of the agent's calls together, retries included.
REAL_ANSWER = (FIXTURES / "real_answer.json").read_bytes()


class Sleeps:
    """The waits the library chose between attempts, kept and never slept, so the test is fast.
    Only the sleep is replaced: the library still works the wait out from the reply's headers
    and its own backoff, and the attempts go through the gate as in a real run."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.waits: list[float] = []
        original = anthropic.Anthropic._calculate_retry_timeout  # pyright: ignore[reportPrivateUsage]

        def sleep(
            self_: Any, *, retries_taken: int, max_retries: int, options: Any, response: Any
        ) -> None:
            remaining = max_retries - retries_taken
            headers = response.headers if response else None
            self.waits.append(original(self_, remaining, options, headers))

        monkeypatch.setattr(anthropic.Anthropic, "_sleep_for_retry", sleep)


def retrying(
    when: str, monkeypatch: pytest.MonkeyPatch, **kw: Any
) -> tuple[Gate, Provider, anthropic.Anthropic, Sleeps]:
    sleeps = Sleeps(monkeypatch)
    run = Gate(
        Fault("model", "status 529", when, **kw), hosts=(), prices=((MODEL, Rates(3.0, 15.0)),)
    )
    provider = Provider(run.events, lambda r: httpx2.Response(200, content=REAL_ANSWER))
    # The library's own default, which the docs' arithmetic is about: 3 attempts a call.
    assert anthropic.DEFAULT_MAX_RETRIES == 2
    client = anthropic_client(provider, max_retries=anthropic.DEFAULT_MAX_RETRIES)
    return run, provider, client, sleeps


def attempts(run: Gate) -> list[int]:
    return [x["status"] for x in run.sink.lines if x.get("kind") == "model"]


def test_first_2_is_got_past_by_the_libraries_own_two_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, provider, client, sleeps = retrying("first 2", monkeypatch)
    assert ask(client).stop_reason == "end_turn"
    assert attempts(run) == [529, 529, 200], "the third attempt is the second retry"
    assert len(provider.sent) == 1
    first, second = sleeps.waits
    assert 0.375 <= first <= 0.5, "0.5 s backoff with up to 25% jitter"
    assert 0.75 <= second <= 1.0, "doubled"


def test_first_3_fails_every_attempt_of_one_call_and_the_agents_next_call_gets_past_on_the_4th(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, provider, client, sleeps = retrying("first 3", monkeypatch)
    with pytest.raises(anthropic.OverloadedError):
        ask(client)
    assert attempts(run) == [529, 529, 529], "1 attempt and 2 retries, all faulted"
    assert provider.sent == []
    assert ask(client).stop_reason == "end_turn", "the agent's own second call"
    assert attempts(run) == [529, 529, 529, 200], "the 4th attempt gets through"
    assert len(provider.sent) == 1
    assert len(sleeps.waits) == 2, "the second call needed no retry"


def test_first_4_still_fails_the_first_call_and_costs_the_second_a_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, _provider, client, sleeps = retrying("first 4", monkeypatch)
    with pytest.raises(anthropic.OverloadedError):
        ask(client)
    assert ask(client).stop_reason == "end_turn"
    assert attempts(run) == [529, 529, 529, 529, 200], "the second call's first attempt is faulted"
    assert len(sleeps.waits) == 3


def test_first_6_fails_both_calls_of_an_agent_that_tries_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, provider, client, _sleeps = retrying("first 6", monkeypatch)
    for _ in range(2):
        with pytest.raises(anthropic.OverloadedError):
            ask(client)
    assert attempts(run) == [529] * 6
    assert provider.sent == []


def test_a_retry_after_on_the_fault_is_the_wait_the_library_takes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run, _provider, client, sleeps = retrying("first 2", monkeypatch, retry_after=3)
    ask(client)
    assert sleeps.waits == [3.0, 3.0], "the header replaces the library's backoff"
