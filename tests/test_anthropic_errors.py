"""The faults that need no real reply, on an Anthropic call: a status and a body that does not
parse, and the faults that are not done yet, which refuse and name Anthropic.

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
from scrimmage.errors import ScrimmageMockError
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


@pytest.mark.parametrize("word", ["replace", "garble", "truncate", "refuse", "unknown tool"])
def test_a_fault_that_is_not_done_yet_refuses_and_says_so_and_sends_nothing(word: str) -> None:
    kw = {"text": "NEW"} if word == "replace" else {}
    run, provider, client = call(word, **kw)
    if word == "replace":
        # A replace changes the real reply, so the call is sent and the reply left as it was.
        ask(client)
        assert run.line.get("mistake") or run.line.get("unchanged")
        assert "not yet for Anthropic" in json.dumps(run.line)
        return
    with pytest.raises(anthropic.APIConnectionError) as caught:
        ask(client)
    # The library wraps what the gate raised, as it would a connection that failed.
    assert isinstance(caught.value.__cause__, ScrimmageMockError)
    assert "not yet for Anthropic" in str(caught.value.__cause__)
    assert provider.sent == [], "nothing was sent"
    assert run.line["reason"] == "mistake"


def test_bad_tool_args_is_a_mistake_at_the_call_and_says_why_it_can_never_be_built() -> None:
    run, provider, client = call("bad tool args")
    with pytest.raises(anthropic.APIConnectionError) as caught:
        ask(client)
    said = str(caught.value.__cause__)
    assert "JSON object on the wire" in said
    assert "not yet" not in said
    assert provider.sent == [], "nothing was sent"
    assert run.line["reason"] == "mistake"

