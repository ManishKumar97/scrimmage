"""The fake's replies keep the shape of real ones, saved from a real-model check."""

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "openai"


def load(name: str) -> dict[str, object]:
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
