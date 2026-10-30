import pytest

from faultline_lab import ida
from faultline_lab.verdict import parse_verdict, score


@pytest.mark.parametrize(
    "answer, expected",
    [
        ("Cart cannot reach redis.\nROOT CAUSE SERVICE: cart", "cart"),
        ("text\nROOT CAUSE SERVICE: none", "none"),
        ("text\n**ROOT CAUSE SERVICE: product-catalog**", "product-catalog"),
        ("text\nROOT CAUSE SERVICE: `payment`.", "payment"),
        ("text\n- Root cause service:  Checkout \n", "checkout"),
        ("text\nROOT CAUSE SERVICE: <ad>", "ad"),
        ("ROOT CAUSE SERVICE: cart\nROOT CAUSE SERVICE: ad", "ad"),
    ],
)
def test_the_verdict_line_is_read(answer, expected):
    assert parse_verdict(answer) == expected


@pytest.mark.parametrize(
    "answer",
    [
        "",
        "No fault found.",
        "The cause is the cart service.",
        "ROOT CAUSE SERVICE:",
        "ROOT CAUSE SERVICE: none of these",
        "ROOT CAUSE SERVICE: cart and checkout",
        "ROOT CAUSE SERVICE: cart\nROOT CAUSE SERVICE: not sure which",
        "the line ROOT CAUSE SERVICE: cart is mentioned mid-sentence",
    ],
)
def test_no_usable_line_is_no_verdict(answer):
    assert parse_verdict(answer) is None


def test_correct_wrong_and_no_verdict_are_kept_apart():
    assert score("x\nROOT CAUSE SERVICE: cart", "cart").outcome == "correct"
    assert score("x\nROOT CAUSE SERVICE: checkout", "cart").outcome == "wrong"
    assert score("The cart is broken.", "cart").outcome == "no verdict"


def test_the_control_is_correct_only_when_the_agent_says_none():
    assert score("fine\nROOT CAUSE SERVICE: none", None).outcome == "correct"
    assert score("x\nROOT CAUSE SERVICE: cart", None).outcome == "wrong"
    assert score("No fault found.", None).outcome == "no verdict"


def test_naming_a_service_for_a_fault_is_wrong_when_the_key_is_checkout():
    # payment_service_unreachable: AIOpsLab blames checkout, so payment is wrong.
    assert score("x\nROOT CAUSE SERVICE: payment", "checkout").outcome == "wrong"


def test_none_is_wrong_when_a_fault_was_injected():
    assert score("x\nROOT CAUSE SERVICE: none", "cart").outcome == "wrong"


def test_the_prompt_asks_for_the_line_and_stays_general():
    assert "ROOT CAUSE SERVICE: <name>" in ida.SYSTEM_PROMPT
    assert "ROOT CAUSE SERVICE: none" in ida.SYSTEM_PROMPT
    for fault in ("flagd", "feature flag", "cartFailure", "AIOpsLab"):
        assert fault not in ida.SYSTEM_PROMPT
