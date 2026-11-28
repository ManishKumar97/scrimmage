"""The fake's replies keep the shape of real ones, saved from a real-model check."""

import json
import re
from pathlib import Path
from typing import Any

import httpx2
import pytest

from test_connection_faults import gated, install
from test_http_gate import BODY

__all__ = ["gated"]  # the fixture, used by name

FIXTURES = Path(__file__).parent / "fixtures" / "openai"


def load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def missing(fake: object, real: object, where: str = "") -> list[str]:
    """Fields the fake sends that a real reply does not have."""
    if isinstance(fake, dict) and isinstance(real, dict):
        return [
            problem
            for key, value in fake.items()
            for problem in (
                missing(value, real[key], f"{where}.{key}") if key in real else [f"{where}.{key}"]
            )
        ]
    if isinstance(fake, list) and isinstance(real, list) and fake and real:
        return missing(fake[0], real[0], f"{where}[0]")
    return []


@pytest.mark.design("fixtures.match_real_replies")
@pytest.mark.parametrize(
    ("fake", "real"),
    [
        ("acme_get_order", "real_chat_tool_call"),
        ("acme_answer", "real_chat_answer"),
        ("embedding", "real_embedding"),
    ],
)
def test_every_field_the_fake_sends_is_in_a_real_reply(fake: str, real: str) -> None:
    assert missing(load(fake), load(real)) == []


# A masked key in a 401 message: its start, the stars, and its last four characters.
MASKED_KEY = re.compile(r"sk-[A-Za-z0-9_-]*?\*+[A-Za-z0-9]{4}")


def scrubbed(body: dict[str, Any]) -> dict[str, Any]:
    """The body with the key fragment in its message replaced, so two keys compare equal."""
    error = dict(body["error"])
    error["message"] = MASKED_KEY.sub("sk-(key)", error["message"])
    return {**body, "error": error}


@pytest.mark.design("fixtures.match_real_errors")
@pytest.mark.parametrize(
    ("do", "real"),
    [("status 401", "real_error_401"), ("context too long", "real_error_context_length")],
)
def test_the_injected_error_bodies_match_the_captured_ones(gated: None, do: str, real: str) -> None:
    """The status, the content type and the whole body: message, type, param and code, and
    the 401's top-level status. Only the key fragment in the 401 message may differ."""
    want = load(real)
    install(do, "model")
    reached: list[object] = []

    def provider(request: httpx2.Request) -> httpx2.Response:
        reached.append(request)
        return httpx2.Response(200, json={})

    client = httpx2.Client(transport=httpx2.MockTransport(provider))
    got = client.post("https://api.openai.com/v1/chat/completions", json=BODY)
    assert reached == [], "the faulted request reached the provider"
    assert got.status_code == want["status"]
    assert got.headers["content-type"] == want["headers"]["content-type"]
    assert scrubbed(got.json()) == scrubbed(want["body"])
    if do == "status 401":  # the scrub found a masked key in each, and hid only that
        for message in (got.json()["error"]["message"], want["body"]["error"]["message"]):
            assert MASKED_KEY.search(message), message


RESPONSES_URL = "https://api.openai.com/v1/responses"
RESPONSES_BODY = {"model": "gpt-4o-mini", "input": "hi"}


def shape(error: dict[str, Any]) -> dict[str, Any]:
    """An error object's shape: its keys, and its type. Message, code and param name the
    request that failed, so they are not part of it."""
    return {"keys": list(error), "type": error["type"]}


def send_responses() -> httpx2.Response:
    transport = httpx2.MockTransport(lambda _: httpx2.Response(200, json={}))
    return httpx2.Client(transport=transport).post(RESPONSES_URL, json=RESPONSES_BODY)


@pytest.mark.design("fixtures.match_real_errors")
def test_a_responses_401_is_the_chat_completions_401(gated: None) -> None:
    """Captured 28 November 2026: OpenAI's Responses 401 is the Chat Completions 401, down
    to the text/plain content type and the top-level status. So `status 401` sends one
    body on both paths."""
    want, chat = load("real_response_error_401"), load("real_error_401")
    assert want["status"] == chat["status"] == 401
    assert want["headers"] == chat["headers"]
    assert scrubbed(want["body"]) == scrubbed(chat["body"])
    install("status 401", "model")
    got = send_responses()
    assert got.status_code == 401
    assert got.headers["content-type"] == want["headers"]["content-type"]
    assert scrubbed(got.json()) == scrubbed(want["body"])


@pytest.mark.design("fixtures.match_real_errors")
def test_a_responses_context_error_is_not_the_chat_completions_one(gated: None) -> None:
    """Captured 28 November 2026: the two APIs word it differently, so `context too long`
    sends the Responses reply on a Responses path, and the chat one on a chat path."""
    want, chat = load("real_response_error_context_length"), load("real_error_context_length")
    assert want["body"]["error"]["param"] == "input"
    assert chat["body"]["error"]["param"] == "messages"
    assert want["body"] != chat["body"]
    install("context too long", "model")
    got = send_responses()
    assert got.status_code == want["status"] == 400
    assert got.headers["content-type"] == want["headers"]["content-type"]
    assert got.json() == want["body"]


@pytest.mark.design("fixtures.match_real_errors")
@pytest.mark.parametrize(
    ("status", "real"), [(404, "real_response_error_404"), (400, "real_response_error_400")]
)
def test_a_responses_404_and_400_have_the_shape_of_the_injected_error(
    gated: None, status: int, real: str
) -> None:
    """Captured 28 November 2026. The same four fields and the same type as the body
    `status N` sends. The message and the code name the failed request (its model, its
    parameter), so a generic status cannot match them and they are not compared."""
    want = load(real)
    assert want["status"] == status
    install(f"status {status}", "model")
    got = send_responses()
    assert got.status_code == status
    assert got.headers["content-type"] == want["headers"]["content-type"]
    assert shape(got.json()["error"]) == shape(want["body"]["error"])
    assert set(want["body"]) == {"error"}
