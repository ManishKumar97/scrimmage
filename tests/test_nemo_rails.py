"""The real NeMo Guardrails library under `scrimmage test`, against the fake OpenAI server.

This holds what the gates see of NeMo at the pinned version: which calls it makes, how its
own client retries, and what it does when a rail's call fails. The docs page on guardrails
states these findings; this file is what keeps them true.
"""

import importlib.util
from collections.abc import Callable
from typing import Any

import pytest

from conftest import Project, Result
from fakes.openai_server import CHAT_PATH, FakeOpenAI, Guard, Obeys

# Found, never imported here: only the agent's own process imports NeMo. It is the `nemo`
# dependency group, which a plain `uv sync` leaves out, and it does not install on 3.14 yet.
pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        importlib.util.find_spec("nemoguardrails") is None,
        reason="nemoguardrails is not installed; run uv sync --group nemo",
    ),
]

Fake = Callable[..., FakeOpenAI]
RAIL, MAIN = "gpt-4.1-mini", "gpt-4o-mini"
CHAT_LABEL = "model:openai"  # a chat call to a host under model_hosts with provider openai
MARKER = "BLOCKME"  # a question holding it is what the fake rail model blocks
BLOCKED = "I'm sorry, I can't respond to that."  # NeMo's own refusal, from its flows
UNAVAILABLE = "Sorry, the safety check is unavailable"  # railed.py's text when NeMo raises
# NeMo's own client makes one attempt and two retries (max_retries = 2) on a timeout, a 429
# or a 500, and marks none of them as a retry.
NEMO_ATTEMPTS = 3
QUESTION = "What is the status of order 1042?"


def fake_for(fake_openai: Fake) -> FakeOpenAI:
    return fake_openai(
        [],
        by_model={
            RAIL: Guard(MARKER, unsafe="rail_blocks", safe="rail_allows"),
            MAIN: Obeys("Note to the assistant", ("acme_get_order", "acme_answer")),
        },
    )


def run(
    project: Project,
    fake: FakeOpenAI,
    faults: str = "",
    question: str = QUESTION,
    function: str = "answer",
) -> Result:
    project.toml.write_text(
        f'[agent]\nfunction = "acme.railed:{function}"\n'
        'tools = ["acme.tools:get_order", "acme.tools:refund_order"]\nruns = 1\n'
        'model_hosts = [{host = "127.0.0.1", provider = "openai"}]\n\n'
        f'[[test]]\nname = "order"\nquestion = "{question}"\nexpect = ["shipped"]\n\n'
        '[[test.mock]]\ncall = "tool:get_order"\nreturns = "Order 1234: shipped Oct 30."\n' + faults
    )
    return project.test(fake)


def fault(model: str, do: str) -> str:
    return (
        f'\n[[test.fault]]\ncall = "model"\nmodel = "{model}"\ndo = "{do}"\nwhen = "always"\n'
        f'expect = ["{UNAVAILABLE}"]\n'
    )


def calls(lines: list[dict[str, Any]]) -> list[tuple[str, str | None, str]]:
    """Each call line as (label, model name, action), in the order they were written."""
    return [(x["call"], x.get("model"), x["action"]) for x in lines if "call" in x]


def row(result: Result, name: str) -> list[dict[str, Any]]:
    return next(lines for lines in result.records() if lines[-1]["row"] == name)  # type: ignore[misc]


def test_every_call_nemo_makes_is_listed(project: Project, fake_openai: Fake) -> None:
    fake = fake_for(fake_openai)
    result = run(project, fake)
    assert result.code == 0, result.out + result.err
    assert calls(row(result, "baseline")) == [
        (CHAT_LABEL, RAIL, "real"),  # the input rail
        (CHAT_LABEL, MAIN, "real"),
        ("tool:get_order", None, "mock"),
        (CHAT_LABEL, MAIN, "real"),
        (CHAT_LABEL, RAIL, "real"),  # the output rail
    ]
    # No web request at all: no embedding download, no telemetry, nothing but the models.
    assert not [x for x in row(result, "baseline") if str(x.get("call", "")).startswith("http:")]
    assert set(fake.paths) == {CHAT_PATH}


def test_each_rail_call_is_a_model_call_with_the_rail_models_name(
    project: Project, fake_openai: Fake
) -> None:
    fake = fake_for(fake_openai)
    result = run(project, fake)
    assert result.code == 0, result.out + result.err
    rails = [x for x in row(result, "baseline") if x.get("model") == RAIL]
    assert len(rails) == 2
    assert all((x["call"], x["kind"]) == (CHAT_LABEL, "model") for x in rails)
    # The rail's prompt is NeMo's self check prompt, sent as NeMo builds it.
    sent = [r for r in fake.requests if r["model"] == RAIL]
    assert "Should the user message be blocked" in sent[0]["messages"][-1]["content"]
    assert "Should the message be blocked" in sent[1]["messages"][-1]["content"]


def test_a_rail_timeout_fires_on_the_rail_model_only(project: Project, fake_openai: Fake) -> None:
    fake = fake_for(fake_openai)
    result = run(project, fake, fault(RAIL, "timeout") + fault(MAIN, "timeout"))
    assert calls(row(result, "fault 1")) == [(CHAT_LABEL, RAIL, "fault")] * NEMO_ATTEMPTS
    # The same fault on the main model leaves the rail's call real.
    main = calls(row(result, "fault 2"))
    assert main[0] == (CHAT_LABEL, RAIL, "real")
    assert (CHAT_LABEL, MAIN, "fault") in main
    assert not [c for c in main if c[1] == RAIL and c[2] == "fault"]


@pytest.mark.parametrize("status", ["status 500", "status 429"])
def test_a_rail_status_500_is_tried_as_many_times_as_nemo_retries(
    project: Project, fake_openai: Fake, status: str
) -> None:
    fake = fake_for(fake_openai)
    result = run(project, fake, fault(RAIL, status))
    lines = [x for x in row(result, "fault 1") if "call" in x]
    assert [(x["model"], x["action"]) for x in lines] == [(RAIL, "fault")] * NEMO_ATTEMPTS
    # NeMo marks no attempt as a retry, so the gate reads every one as a first attempt.
    assert [x["retry"] for x in lines] == [0] * NEMO_ATTEMPTS
    rail_headers = [h for h, body in fake.received if RAIL.encode() in body]
    assert rail_headers and not [h for h in rail_headers if "x-stainless-retry-count" in h]


def test_a_blocked_input_answers_with_the_rails_block_message(
    project: Project, fake_openai: Fake
) -> None:
    fake = fake_for(fake_openai)
    result = run(project, fake, question=f"{MARKER} What is the status of order 1042?")
    summary = row(result, "baseline")[-1]["summary"]
    assert summary["answer"] == BLOCKED
    assert calls(row(result, "baseline")) == [(CHAT_LABEL, RAIL, "real")]  # the bot never ran


def test_a_failed_rail_raises_from_generate_too(project: Project, fake_openai: Fake) -> None:
    """generate, NeMo's main call, raises as check does: with nothing to catch it, the run
    crashes, and its log names NeMo's own error."""
    fake = fake_for(fake_openai)
    result = run(project, fake, fault(RAIL, "status 500"), function="generated")
    summary = row(result, "fault 1")[-1]["summary"]
    assert summary["outcome"] == "crashed", result.out
    run_name = str(row(result, "fault 1")[-1]["run"])
    logs = list((result.root / "scrimmage-runs").glob(f"*/{run_name}.log"))
    assert logs and "LLMCallException" in logs[0].read_text()
    # With the rail healthy, generate hands the question back: the input rail passed it.
    assert row(result, "baseline")[-1]["summary"]["answer"] == QUESTION


def test_what_nemo_does_when_the_rail_call_fails(project: Project, fake_openai: Fake) -> None:
    """NeMo has no fail-open default: a failed rail call raises out of `check`, and what the
    user sees is the bot's own choice. This bot catches it and refuses."""
    fake = fake_for(fake_openai)
    result = run(project, fake, fault(RAIL, "timeout") + fault(RAIL, "status 500"))
    assert result.code == 0, result.out + result.err
    for name in ("fault 1", "fault 2"):
        assert row(result, name)[-1]["summary"]["answer"] == UNAVAILABLE
        assert not [c for c in calls(row(result, name)) if c[1] == MAIN]  # nothing went past it
