"""The report's text, byte for byte, as main printed it before the report was split."""

import io
from collections.abc import Callable

import pytest

from report_cases import CASES, FOLDER, GOLDEN, WEB_MISS
from scrimmage_core.runner import RunResult, report


@pytest.mark.design("json.text_unchanged")
@pytest.mark.parametrize("name", sorted(CASES))
def test_the_text_is_byte_for_byte_what_it_was_before_the_split(name: str) -> None:
    case = CASES[name]()
    out = io.StringIO()
    code = report(case.config, case.results, FOLDER, out, case.runner)  # type: ignore[arg-type]
    assert f"exit {code}\n" + out.getvalue() == (GOLDEN / f"{name}.txt").read_text()


def test_every_golden_file_has_its_case() -> None:
    assert {p.stem for p in GOLDEN.glob("*.txt")} == set(CASES)


def printed(case_name: str, mutate: Callable[[list[RunResult]], None]) -> str:
    case = CASES[case_name]()
    mutate(case.results)
    out = io.StringIO()
    report(case.config, case.results, FOLDER, out, case.runner)  # type: ignore[arg-type]
    return out.getvalue()


MISSES = [
    {
        "call": "tool:get_order",
        "action": "real",
        "reason": "arguments",
        "seen": '{"order_id": "1"}',
        "method": None,
        "path": None,
        "nearest": {
            "mock": 0,
            "args": '{"order_id": "2"}',
            "method": None,
            "path": None,
            "differs": ["order_id"],
            "differs_left_out": 0,
        },
    },
    {**WEB_MISS, "action": "refused"},
    {
        "call": "tool:refund_order",
        "action": "refused",
        "reason": "no_mock",
        "seen": '{"order_id": <str>}',
        "method": None,
        "path": None,
        "nearest": None,
    },
    {
        "call": "tool:get_stock",
        "action": "real",
        "reason": "unreadable",
        "seen": None,
        "method": None,
        "path": None,
        "nearest": None,
    },
]


def test_a_miss_line_is_labelled_and_says_nothing_is_wrong() -> None:
    def add(results: list[RunResult]) -> None:
        results[0].misses = [dict(m) for m in MISSES]

    out = printed("passing", add)
    lines = [line for line in out.splitlines() if line.startswith("warning: ")]
    assert len(lines) == 4
    guesses = [line for line in lines if "nearest mock" in line]
    assert len(guesses) == 2 and all("(heuristic: " in line for line in guesses)
    assert not any(word in out.lower() for word in ("wrong", "incorrect", "mistake in"))
    assert any("was refused" in line for line in lines) and any(
        "ran for real" in line for line in lines
    )


def test_a_failing_rule_line_points_at_the_miss() -> None:
    def add(results: list[RunResult]) -> None:
        broken = next(r for r in results if r.run == "run-baseline-1")
        broken.misses = [dict(MISSES[0])]

    out = printed("failing", add)
    assert "(1 of these runs had a mock miss; see the warnings)" in out
    assert "(1 of these runs had a mock miss" not in printed("failing", lambda _: None)
