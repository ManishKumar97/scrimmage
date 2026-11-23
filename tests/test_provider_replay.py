"""The OpenAI model calls of tests/provider_replay.py, through the real gate and a fake or
scripted OpenAI server, give the bytes the golden file holds: the same replies, lines, holds
and settled costs. The golden file was captured on the source of the provider seam's first
phase, before the seam was completed. A change on purpose regenerates the golden file with
`python tests/provider_replay.py`, and the diff is the review."""

import json
from typing import Any

import pytest

import provider_replay


def test_openai_calls_replay_byte_for_byte_as_before_the_provider_seam() -> None:
    # The golden file keeps the one measured time it was captured with; only the comparison
    # ignores it, as it ignores every clock reading.
    saved = json.loads(provider_replay.GOLDEN.read_text())
    golden: dict[str, Any] = {n: provider_replay.settled(run) for n, run in saved.items()}
    found: dict[str, Any] = json.loads(provider_replay.dumps(provider_replay.capture()))
    assert list(found) == list(golden), "the scenarios changed"
    changed = {
        name: [key for key in golden[name] if found[name][key] != golden[name][key]]
        for name in golden
        if found[name] != golden[name]
    }
    assert not changed, f"these calls no longer replay as before, by scenario and field: {changed}"


def test_the_anthropic_scenarios_are_added_after_every_openai_one() -> None:
    """The OpenAI scenarios keep their order and their place, so a new provider's scenarios
    are additions to the golden file and never change what was captured before."""
    names = list(json.loads(provider_replay.GOLDEN.read_text()))
    first = next(i for i, name in enumerate(names) if name.startswith("anthropic "))
    assert all(n.startswith("anthropic ") for n in names[first:])
    assert not any(n.startswith("anthropic ") for n in names[:first])
    assert len(names[:first]) >= 674, "an OpenAI scenario was dropped"


def test_the_replay_is_deterministic() -> None:
    once = provider_replay.dumps(provider_replay.capture(lambda s: "fault" in s.name))
    twice = provider_replay.dumps(provider_replay.capture(lambda s: "fault" in s.name))
    assert once == twice


PINNED = [
    "chat chat_answer",
    "fault refuse chat",
    "marker chat",
    # The paths the first capture was blind to.
    "httpx2 async: chat answer",
    "httpx: chat answer",
    "openai: chat answer",
    "openai async: fault garble responses",
    "httpx2: redirect off host priced",
    "httpx2 async: redirect off host priced",
    "httpx2: connect error",
    "httpx2: ReadError after sending",
    "retry fault requests 1",
    "budget shortfall chat tiny",
    "reply tier 'scale' chat",
    "host listed twice",
    "stream cut",
    "stream gzip",
    "packed gzip fault garble",
    "gateway cost header high custom",
    "cache writes responses split short",
    "injected text one string",
    # Anthropic's Messages API, added after the OpenAI scenarios, which are unchanged above.
    "anthropic messages doc_cached",
    "anthropic messages doc_stream_cached",
    "anthropic hold cache control 1h",
    "anthropic hold speed fast",
    "anthropic fault status 529",
    "anthropic anthropic: retry fault",
    "anthropic stream cut",
    "anthropic fault refuse not yet",
    "anthropic host listed for both",
    "anthropic messages count tokens",
]


@pytest.mark.parametrize("name", PINNED)
def test_the_replay_keeps_what_it_is_meant_to_pin(name: str) -> None:
    golden = json.loads(provider_replay.GOLDEN.read_text())[name]
    assert golden["lines"], "the call left no line"


def test_a_slowed_call_waited_at_least_its_delay() -> None:
    """slowed_ms is measured, so the replay ignores it; here it is held to its floor."""
    provider_replay.MEASURED.clear()
    provider_replay.capture(lambda s: s.name == "fault slow")
    assert provider_replay.MEASURED["slowed_ms"], "the slowed call recorded no wait"
    assert all(ms >= 10 for ms in provider_replay.MEASURED["slowed_ms"])  # slow 0.01s
