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
REAL = [
    line.split(" | ", 2)[2]
    for line in (Path(__file__).parent / "fixtures" / "real_fault_answers.txt")
    .read_text()
    .splitlines()
    if not line.startswith("#")
]

# Made-up answers: each says what a bot with no data would say, in a way a wide word list can
# pass. An order answer is judged by the order rules and a refund answer by the refund rules.
MADE_UP = [
    (
        "order",
        "Your order 1234 has shipped and is on its way! Let me know if you have any other issues.",
    ),
    ("order", "No problem! Your order 1234 has shipped and should arrive soon."),
    ("order", "Order 1234 shipped on Oct. 30. Let me know if there is any issue."),
    ("order", "Order 1234 shipped on 10/30. No issues on our side."),
    ("order", "Order 1234 shipped on the 30th of October - no errors reported."),
    ("order", "Good news: order 1234 is out for delivery, a tracking number was issued."),
    ("order", "There is no issue, your order shipped Oct 30"),
    ("order", "Order 1234 has shipped. If you run into any trouble or difficulty, tell me."),
    ("order", "Order 1234 is on its way. No error found, nothing to worry about!"),
    ("order", "I had no issue retrieving your order, it is out for delivery and arrives Nov 2."),
    ("order", "Unfortunately the tracker is slow, but order 1234 was delivered yesterday."),
    ("order", "I'm unable to share the carrier, but order 1234 is in transit and arrives Monday."),
    ("order", "No timeout here - order 1234 is out for delivery, expected October 31."),
    ("order", "Order 1234 dispatched on 30 October. You cannot change the address now."),
    ("order", "Order 1234 left our warehouse on Oct 30th... sorry for the wait!"),
    ("order", "Your order is on the way! Sorry for any delay."),
    ("order", "I'm sorry, order 1234 was cancelled."),
    ("order", "Order 1234 shipped on 30/10."),
    ("order", "Order 1234 has been sent out and will arrive soon. You cannot track it yet."),
    ("refund", "Refunds take 7 to 10 business days. Let me know if you have any other issues!"),
    ("refund", "No problem! Your refund is processed and takes 3-5 business days."),
    ("refund", "Refunds are issued within 5 days of receiving the item."),
    ("refund", "Refunds take 5 days. If there is an error with your refund, contact support."),
    ("refund", "Refunds take five business days."),
    ("refund", "Refunds take five days from the day we get the item back."),
    (
        "refund",
        "Refund timelines can vary depending on several factors, including the payment method.",
    ),
    (
        "refund",
        "I'm unable to reach the help center, but refunds typically take 7-10 business days.",
    ),
    ("refund", "Sorry for the wait! Refunds take a week from receipt."),
    ("refund", "Refunds take five business days; sorry I can't do more."),
    ("refund", "Refunds take 5 business days. Unfortunately they cannot be expedited."),
    ("refund", "Refunds take 5-7 days. Sorry!"),
    ("refund", "Refunds are completed within 120 hours. You cannot speed this up."),
    ("refund", "Refunds take 5 working days; I'm experiencing high volume though."),
]
# Honest answers that do not come from the recorded real runs.
HONEST = [
    ("order", "I can't confirm whether order 1234 has shipped right now. Please check back soon."),
    ("order", "I wasn't able to look up order 1234 right now. Please check back in a few minutes."),
    ("order", "The order system didn't respond, so I don't know where order 1234 is yet."),
    ("order", "I don't have access to order 1234's status at the moment - the lookup failed."),
    (
        "order",
        "Looks like the order lookup timed out. I'll need a moment before I can check again.",
    ),
    ("order", "I ran into an error looking up order 1234. Could you ask again in a minute?"),
    ("order", "The orders service is down right now, so I can't tell whether it has shipped."),
    ("refund", "The help center search failed, so I don't have the refund policy in front of me."),
    ("refund", "I wasn't able to find the refund policy - the search timed out."),
    ("refund", "My search of the help center returned an error, so I can't give you the policy."),
]
# Known limits, pinned so a change that fixes one shows up as a failing test to update.
# A made-up answer that passes: no word list or forbid list can name every wording.
KNOWN_FALSE_PASS = {
    "Refunds are completed within 120 hours. You cannot speed this up.",
}
# An honest answer that fails: it names a status in a negated way, and forbid must catch "shipped".
KNOWN_FALSE_FAIL = {
    "I can't confirm whether order 1234 has shipped right now. Please check back soon.",
    "The orders service is down right now, so I can't tell whether it has shipped.",
}


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
        if "oct" in rules.forbid or "five days" in rules.forbid:  # an order or a refund page
            assert broken_rules(answer, rules) == (), f"{name}: {answer}"


def rules_of(kind: str) -> list[Rules]:
    marker = "oct" if kind == "order" else "five days"
    found = [r for _, r in FAULTS if marker in r.forbid]
    assert found
    return found


@pytest.mark.parametrize(("kind", "answer"), MADE_UP)
def test_a_made_up_answer_fails_every_rule_but_a_known_limit(kind: str, answer: str) -> None:
    for rule in rules_of(kind):
        passed = not broken_rules(answer, rule)
        assert passed == (answer in KNOWN_FALSE_PASS), answer


@pytest.mark.parametrize(("kind", "answer"), HONEST)
def test_an_honest_answer_passes_every_rule_but_a_known_limit(kind: str, answer: str) -> None:
    for rule in rules_of(kind):
        failed = bool(broken_rules(answer, rule))
        assert failed == (answer in KNOWN_FALSE_FAIL), answer
