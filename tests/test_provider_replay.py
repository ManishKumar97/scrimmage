"""The OpenAI model calls of tests/provider_replay.py, through the real gate and the fake
OpenAI server, give the bytes that origin/main gave before the provider seam: the same
replies, lines, holds and settled costs. A change on purpose regenerates the golden file with
`python tests/provider_replay.py`, and the diff is the review."""

import json

import pytest

import provider_replay


def test_openai_calls_replay_byte_for_byte_as_before_the_provider_seam() -> None:
    # The golden file keeps the one measured time it was captured with; only the comparison
    # ignores it, as it ignores every clock reading.
    saved = json.loads(provider_replay.GOLDEN.read_text())
    golden = {name: provider_replay.settled(run) for name, run in saved.items()}
    found = json.loads(provider_replay.dumps(provider_replay.capture()))
    assert list(found) == list(golden), "the scenarios changed"
    changed = [name for name in golden if found[name] != golden[name]]
    assert not changed, f"these calls no longer replay as before: {changed}"


def test_the_replay_is_deterministic() -> None:
    once = provider_replay.dumps(provider_replay.capture(lambda s: "fault" in s.name))
    twice = provider_replay.dumps(provider_replay.capture(lambda s: "fault" in s.name))
    assert once == twice


@pytest.mark.parametrize("name", ["chat chat_answer", "fault refuse chat", "marker chat"])
def test_the_replay_keeps_what_it_is_meant_to_pin(name: str) -> None:
    golden = json.loads(provider_replay.GOLDEN.read_text())[name]
    assert golden["lines"], "the call left no line"


def test_a_slowed_call_waited_at_least_its_delay() -> None:
    """slowed_ms is measured, so the replay ignores it; here it is held to its floor."""
    provider_replay.MEASURED.clear()
    provider_replay.capture(lambda s: s.name == "fault slow")
    assert provider_replay.MEASURED["slowed_ms"], "the slowed call recorded no wait"
    assert all(ms >= 10 for ms in provider_replay.MEASURED["slowed_ms"])  # slow 0.01s
