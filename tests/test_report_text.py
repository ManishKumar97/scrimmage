"""The report's text, byte for byte, as main printed it before the report was split."""

import io
from collections.abc import Callable

import pytest

from report_cases import CASES, FOLDER, GOLDEN, WEB_MISS
from scrimmage.runner import RunResult, report


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


def ungated(library: str, host: str, method: str) -> dict[str, object]:
    return {"notice": "ungated", "library": library, "host": host, "method": method}


def test_a_test_whose_runs_sent_ungated_requests_gets_one_warning_line() -> None:
    def add(results: list[RunResult]) -> None:
        for r in results[:3]:  # the baseline's three runs
            r.notices = [ungated("requests", "api.acme.com", "POST")]
        results[3].notices = [ungated("urllib3", "files.acme.com", "GET")]

    out = printed("passing", add)
    [line] = [x for x in out.splitlines() if "does not gate" in x]
    assert line.startswith("warning: in test 'order-status' 4 runs sent web requests that ")
    assert "requests to api.acme.com (POST), urllib3 to files.acme.com (GET)" in line
    assert "a write there ran for real" in line
    assert line.endswith("/limits/calls/#web-requests-and-recorded-mocks")


def test_a_read_through_an_ungated_client_is_not_called_a_write() -> None:
    def add(results: list[RunResult]) -> None:
        results[0].notices = [ungated("aiohttp", "api.acme.com", "GET")]

    [line] = [x for x in printed("passing", add).splitlines() if "does not gate" in x]
    assert "1 run sent" in line and "those requests were not gated" in line
    assert "ran for real" not in line


def test_the_ungated_line_changes_neither_the_table_nor_the_exit_code() -> None:
    def add(results: list[RunResult]) -> None:
        results[0].notices = [ungated("requests", "api.acme.com", "POST")]

    with_notice = printed("failing", add)
    plain = printed("failing", lambda _: None)
    assert [x for x in with_notice.splitlines() if "does not gate" not in x] == plain.splitlines()
