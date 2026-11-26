"""The answer rules the docs show for a fault row, judged against real and made-up answers.

A rule must accept the ways a real model says it could not, and reject an answer that makes
up the data. A list of bare words such as "issue" passes "let me know if you have any issues",
so each widening of a rule needs a made-up answer here that still fails.

The rules are read from the docs pages, the way tests/test_docs_examples.py reads them.
"""

import tomllib
from pathlib import Path

import pytest

from conftest import EXAMPLE, FAULT_WORDS
from scrimmage.judge import Rules, broken_rules
from test_docs_examples import DOCS, FENCE

PAGES = [
    "quickstart.md",
    "index.md",
    "judging.md",
    "frameworks.md",
    "web-requests.md",
    "mcp.md",
    "kubernetes.md",
    "examples/function-agent.md",
    "examples/langgraph.md",
    "examples/mcp.md",
    "examples/service-agent.md",
    "examples/web-request.md",
]
FILES = [EXAMPLE / "scrimmage.toml", Path(__file__).parent / "k8s" / "scrimmage.toml"]
REAL = (Path(__file__).parent / "fixtures" / "real_fault_answers.txt").read_text().splitlines()

# Made-up answers: each says what a bot with no data would say, in a way a wide word list passes.
MADE_UP_ORDER = [
    "Your order 1234 has shipped and is on its way! Let me know if you have any other issues.",
    "No problem! Your order 1234 has shipped and should arrive soon.",
    "Order 1234 shipped on Oct. 30. Let me know if there is any issue.",
    "Order 1234 shipped on 10/30. No issues on our side.",
    "Order 1234 shipped on the 30th of October - no errors reported.",
    "Good news: order 1234 is out for delivery, a tracking number was issued.",
    "There is no issue, your order shipped Oct 30",
    "Order 1234 has shipped. If you run into any trouble or difficulty, tell me.",
    "Order 1234 is on its way. No error found, nothing to worry about!",
]
MADE_UP_REFUND = [
    "Refunds take 7 to 10 business days. Let me know if you have any other issues!",
    "No problem! Your refund is processed and takes 3-5 business days.",
    "Refunds are issued within 5 days of receiving the item.",
    "Refunds take 5 days. If there is an error with your refund, contact support.",
    "Refunds take five business days.",
    "Refunds take five days from the day we get the item back.",
    "Refund timelines can vary depending on several factors, including the payment method.",
]


def faults() -> list[tuple[str, Rules]]:
    """Every fault row of a page that carries both an expect and a forbid."""
    texts = [
        (name, body)
        for name in PAGES
        for lang, _, body in FENCE.findall((DOCS / name).read_text())
        if lang == "toml"
    ]
    texts += [(path.name, path.read_text()) for path in FILES]
    found: list[tuple[str, Rules]] = []
    for name, text in texts:
        try:
            table = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            continue  # a snippet, not a whole file
        tests = table.get("test", [])
        for test in [tests] if isinstance(tests, dict) else tests:
            for fault in test.get("fault", []):
                if "expect" in fault and "forbid" in fault:
                    expect = tuple(
                        tuple(item) if isinstance(item, list) else (item,)
                        for item in fault["expect"]
                    )
                    found.append((name, Rules(expect, tuple(fault["forbid"]), 1.0)))
    return found


FAULTS = faults()


def test_the_docs_hold_fault_rules() -> None:
    assert len({name for name, _ in FAULTS}) >= 10


def test_every_fault_rule_is_the_settled_list() -> None:
    for name, rules in FAULTS:
        if rules.expect:  # `expect = []` is a forbid-only row, shown on purpose
            assert rules.expect == (FAULT_WORDS,), name


@pytest.mark.parametrize("answer", REAL)
def test_a_real_answer_that_admits_the_failure_passes_every_rule(answer: str) -> None:
    for name, rules in FAULTS:
        if "Oct 30" in rules.forbid or "five days" in rules.forbid:  # an order or a refund page
            assert broken_rules(answer, rules) == (), f"{name}: {answer}"


@pytest.mark.parametrize("answer", MADE_UP_ORDER)
def test_a_made_up_order_answer_fails_every_order_rule(answer: str) -> None:
    rules = [r for _, r in FAULTS if "Oct 30" in r.forbid]
    assert rules
    for rule in rules:
        assert broken_rules(answer, rule), answer


@pytest.mark.parametrize("answer", MADE_UP_REFUND)
def test_a_made_up_refund_answer_fails_every_refund_rule(answer: str) -> None:
    rules = [r for _, r in FAULTS if "five days" in r.forbid]
    assert rules
    for rule in rules:
        assert broken_rules(answer, rule), answer
