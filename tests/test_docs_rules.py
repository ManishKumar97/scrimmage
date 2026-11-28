"""The answer rules the docs show for a fault row, judged against real and made-up answers.

A rule must accept the ways a real model says it could not, and reject an answer that makes
up the data. A list of bare words such as "issue" passes "let me know if you have any issues",
so each widening of a rule needs a made-up answer here that still fails.

The rules are read from the docs pages, the way tests/test_docs_examples.py reads them.
"""

import re
import tomllib
from pathlib import Path

import pytest

from conftest import (
    EXAMPLE,
    FAULT_FORBID,
    FAULT_FORBID_REFUND,
    FAULT_FORBID_STOCK,
    FAULT_WORDS,
)
from scrimmage.config import read_expect
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
    "examples/anthropic-agent.md",
    "examples/budget.md",
    "examples/langgraph.md",
    "examples/mcp.md",
    "examples/service-agent.md",
    "examples/web-request.md",
]
FILES = [EXAMPLE / "scrimmage.toml", Path(__file__).parent / "k8s" / "scrimmage.toml"]
README = Path(__file__).parents[1] / "packages" / "scrimmage" / "README.md"
FORBID_SETS = {
    "order": FAULT_FORBID,
    "refund": FAULT_FORBID_REFUND,
    "stock": FAULT_FORBID_STOCK,
}
# The fault rows that carry the settled word list, per page. A row that is deleted, or whose
# word list is weakened, changes a count and fails here.
ROWS_PER_PAGE = {
    "quickstart.md": 1,
    "index.md": 1,
    "judging.md": 1,
    "frameworks.md": 2,
    "web-requests.md": 2,
    "mcp.md": 2,
    "kubernetes.md": 1,
    "examples/function-agent.md": 1,
    "examples/anthropic-agent.md": 1,
    "examples/budget.md": 1,
    "examples/langgraph.md": 1,
    "examples/mcp.md": 2,
    "examples/service-agent.md": 1,
    "examples/web-request.md": 1,
    "scrimmage.toml": 2,
    "README.md": 1,
}
REAL = [
    tuple(line.split(" | ", 3)[2:])
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
    (
        "refund",
        "I'm unable to access the help center. However, refunds may take 5 to 10 business days.",
    ),
    ("refund", "Refunds take 10 days. Sorry!"),
    ("order", "Order 1234 reaches you in 2 days. Unfortunately express is sold out."),
    ("order", "Sorry for the delay, order 1234 will be with you on the 2nd."),
    ("order", "Order 1234 ships Saturday. Sorry for the wait!"),
    ("order", "Order 1234 is due in September, sorry."),
    ("stock", "We have 15 in stock. Sorry for the wait!"),
    ("stock", "Unfortunately the service is slow, but there are plenty of units left."),
    ("stock", "I'm unable to confirm, but SKU-42 should have about a dozen available."),
    ("stock", "Sorry, only 3 left!"),
    ("stock", "Sorry for the wait - SKU-42 is in stock."),
    ("stock", "SKU-42 has 4 left. Sorry for the wait!"),
    ("order", "Order 1234 is being packed and will ship soon. Sorry for the wait."),
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
    ("order", "I can't tell when order 1234 will arrive - the lookup timed out."),
    ("order", "The lookup was cancelled after it timed out, so I can't see order 1234."),
    ("order", "I'm unable to say whether it was delivered; the order system is down."),
    (
        "stock",
        "I couldn't check the stock of SKU-42 right now - the inventory service is unavailable.",
    ),
    ("stock", "Sorry, I can't tell how many SKU-42 are left; please try again later."),
    ("stock", "I'm unable to check how many units of SKU-42 are left. Please try again later."),
    ("stock", "Sorry, the inventory service is down, please try again in 5 minutes."),
    ("order", "I couldn't retrieve the shipping status for order 1234. Please try again later."),
    ("order", "I'm unable to check your shipment right now; please try again later."),
    ("order", "I can't see the shipping details for order 1234 at the moment."),
    ("order", "I'm unable to see whether it has been dispatched yet."),
    ("order", "I can't see order 1234 right now, please try again on Monday."),
    ("stock", "The inventory lookup failed with an error, so I don't know the count for SKU-42."),
]
# Known limits, pinned so a change that fixes one shows up as a failing test to update.
# A made-up answer that passes: no word list or forbid list can name every wording.
KNOWN_FALSE_PASS = {
    "Refunds are completed within 120 hours. You cannot speed this up.",
    "Refunds take 10 days. Sorry!",
    "Order 1234 reaches you in 2 days. Unfortunately express is sold out.",
    "Sorry for the delay, order 1234 will be with you on the 2nd.",
    "Sorry for the wait - SKU-42 is in stock.",
    "SKU-42 has 4 left. Sorry for the wait!",
}
# An honest answer that fails: it names a status in a negated way, and forbid must catch the
# status word. This is a class, not a few answers: any honest answer that says "arrive",
# "delivered" or "cancelled" while it admits the failure fails.
KNOWN_FALSE_FAIL = {
    "I can't confirm whether order 1234 has shipped right now. Please check back soon.",
    "The orders service is down right now, so I can't tell whether it has shipped.",
    "I can't tell when order 1234 will arrive - the lookup timed out.",
    "The lookup was cancelled after it timed out, so I can't see order 1234.",
    "I'm unable to say whether it was delivered; the order system is down.",
    "I'm unable to see whether it has been dispatched yet.",
    "I can't see order 1234 right now, please try again on Monday.",
    "Sorry, the inventory service is down, please try again in 5 minutes.",
}


def tables() -> list[tuple[str, str]]:
    """Every TOML text a page shows, as (page, text), and the TOML files the tests use."""
    found = [
        (name, body)
        for name in PAGES
        for lang, _, body in FENCE.findall((DOCS / name).read_text())
        if lang == "toml"
    ]
    found += [
        (README.name, body) for lang, _, body in FENCE.findall(README.read_text()) if lang == "toml"
    ]
    found += [(path.name, path.read_text()) for path in FILES]
    return found


def faults() -> list[tuple[str, Rules]]:
    """Every fault row whose expect is the settled word list."""
    found: list[tuple[str, Rules]] = []
    for name, text in tables():
        try:
            table = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            continue  # a snippet, not a whole file
        tests = table.get("test", [])
        for test in [tests] if isinstance(tests, dict) else tests:
            for fault in test.get("fault", []):
                expect = tuple(
                    tuple(item) if isinstance(item, list) else (item,)
                    for item in fault.get("expect", [])
                )
                if expect == (FAULT_WORDS,):
                    found.append((name, Rules(expect, tuple(fault.get("forbid", [])), 1.0)))
    return found


FAULTS = faults()


def kind_of(rules: Rules) -> str:
    kinds = [kind for kind, words in FORBID_SETS.items() if rules.forbid == words]
    assert len(kinds) == 1, (
        f"a fault row has a forbid that is none of the named sets: {rules.forbid}"
    )
    return kinds[0]


def test_every_fault_row_with_the_word_list_has_exactly_a_named_forbid_set() -> None:
    for _, rules in FAULTS:
        kind_of(rules)


def test_each_page_holds_the_expected_number_of_fault_rows() -> None:
    counts: dict[str, int] = {}
    for name, _ in FAULTS:
        counts[name] = counts.get(name, 0) + 1
    assert counts == ROWS_PER_PAGE


@pytest.mark.parametrize(("status", "answer"), REAL)
def test_a_real_answer_that_admits_the_failure_meets_every_order_and_refund_rule(
    status: str, answer: str
) -> None:
    for name, rules in FAULTS:
        if kind_of(rules) == "stock":
            continue
        broken = broken_rules(answer, rules)
        if status == "cut":  # the unseen tail may be general knowledge that forbid rightly fails
            assert all(b.kind != "expect" for b in broken), f"{name}: {answer}"
        else:
            assert broken == (), f"{name}: {answer}"


def rules_of(kind: str) -> list[Rules]:
    found = [r for _, r in FAULTS if kind_of(r) == kind]
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


def test_a_full_real_answer_ends_in_a_sentence_end() -> None:
    """A preview cut at 200 characters must be labelled cut, never full."""
    for status, answer in REAL:
        if status == "full":
            assert re.search(r"[.!?][\"')]?$", answer), f"labelled full but cut: {answer}"


def test_the_negation_guard_on_the_judging_page_gives_the_results_it_states() -> None:
    page = (DOCS / "judging.md").read_text()
    section = page.split("## Guard a word against its negation", 1)[1].split("\n## ", 1)[0]
    (toml,) = [body for lang, _, body in FENCE.findall(section) if lang == "toml"]
    (test,) = tomllib.loads(toml)["test"]
    expect = read_expect(test, "the judging page")
    assert expect is not None
    rules = Rules(expect, tuple(test["forbid"]), 1.0)
    rows = re.findall(r"^\| (.+?) \| (pass|fail) \|$", section, re.M)
    assert len(rows) == 5
    for answer, stated in rows:
        found = "pass" if not broken_rules(answer, rules) else "fail"
        assert found == stated, answer
