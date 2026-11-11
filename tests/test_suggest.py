"""Suggestions: rules over a failing run's record, each a heuristic that cites its lines."""

import builtins
import itertools
import os
import re
import socket
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from scrimmage_core import judge, outcomes, runner, suggest

Doc = dict[str, object]
Combo = tuple[str, str, str | None, frozenset[str], bool, bool]  # outcome, verdict, left_out,
# broken kinds, fault row (else baseline), fired
PROC: Doc = {"host": "h", "pid": 100, "started": "2026-11-11T10:15:00+00:00"}
OTHER_PROC: Doc = {"host": "h", "pid": 200, "started": "2026-11-11T10:15:01+00:00"}
RULES = judge.Rules(expect=(("needle",),), forbid=("bad",), pass_rate=1.0)
ANSWERS = ("", "x", "needle", "bad", "needle bad")  # empty, expect, pass, both, forbid
UNJUDGED = runner.NOT_JUDGED.left_out
FAULT_ROW: Doc = {"fault": {"call": "tool:get_order", "do": "timeout"}}
BASELINE_ROW: Doc = {"fault": None}
REFUSALS = ("unlisted", "web_write", "blind_child", "other_python", "structure")
# Rules about the whole run: what they read is the run's summary, not a call line.
SUMMARY_ONLY = {
    "not_agent",
    "fault_not_fired",
    "crash_on_baseline",
    "timeout_on_baseline",
    "rule_on_baseline",
}
BAD_WORDS = re.compile(r"\b(because|wrong)\b", re.IGNORECASE)


def line(seq: int, call: str = "model:openai", action: str = "real", **extra: object) -> Doc:
    return {
        "seq": seq,
        "proc": PROC,
        "call": call,
        "kind": "tool",
        "action": action,
        "args": {"sha256": "aa"},
        **extra,
    }


def fault_line(seq: int = 2) -> Doc:
    return line(seq, "tool:get_order", "fault", fault={"do": "timeout", "when": "always", "n": 1})


def run_doc(
    outcome: str,
    judged: str,
    left_out: str | None = None,
    kinds: frozenset[str] | set[str] = frozenset(),
    row: str = "fault 1",
) -> Doc:
    return {
        "run": "run-3f9a1c40b2e1",
        "test": "order-status",
        "row": row,
        "outcome": outcome,
        "judged": judged,
        "left_out": left_out,
        "broken": [{"kind": k, "text": k} for k in sorted(kinds)],
    }


def lines_for(combo: Combo) -> list[Doc]:
    """The record lines a run of this shape leaves."""
    outcome, _, left_out, kinds, _, fired = combo
    lines = [line(1)]
    if fired:
        lines.append(fault_line(2))
    if judge.REFUSED in kinds or outcome == suggest.REFUSED_CALL:
        lines.append(line(3, "tool:other", "refused"))
    if left_out == judge.TEST_MISTAKE:
        lines.append(
            line(4, "tool:get_order", "real", mistake="the mock for get_order has no match")
        )
    return lines


def main_of(combo: Combo) -> suggest.Suggestion | suggest.NoRule:
    outcome, verdict, left_out, kinds, fault, _ = combo
    row = "fault 1" if fault else "baseline"
    return suggest.main(
        run_doc(outcome, verdict, left_out, kinds, row),
        lines_for(combo),
        FAULT_ROW if fault else BASELINE_ROW,
    )


# --- the combinations ---------------------------------------------------------------------


def produced() -> set[Combo]:
    """Every combination the judge gives, found by running the judge, not by listing."""
    found: set[Combo] = set()
    for outcome, fault, fired, mistake, reason, answer in itertools.product(
        outcomes.OUTCOMES, (False, True), (False, True), (False, True), REFUSALS, ANSWERS
    ):
        flag = runner.fault_fired(fault, int(fired))  # the runner's own: None on a baseline
        if bool(flag) != fired:
            continue  # a baseline never fires
        exercised = runner.is_exercised(flag, outcome)
        chose = runner.chose_refused(fault, reason)
        refused = "tool:other" if chose and outcome == suggest.REFUSED_CALL else None
        judgement = judge.judge_run(outcome, answer, exercised, RULES, mistake, refused)
        kinds = frozenset(b.kind for b in judgement.broken)
        found.add((outcome, judgement.verdict, judgement.left_out, kinds, fault, fired))
    return found


def every_combo() -> Iterator[Combo]:
    left_outs = {None, UNJUDGED, *(c[2] for c in produced())}
    kind_sets = [
        frozenset(s)
        for n in range(len(judge.KINDS) + 1)
        for s in itertools.combinations(judge.KINDS, n)
    ]
    yield from itertools.product(
        outcomes.OUTCOMES,
        judge.RUN_VERDICTS,
        sorted(left_outs, key=str),
        kind_sets,
        (False, True),
        (False, True),
    )


def failing_kinds(outcome: str) -> list[frozenset[str]]:
    """The broken sets a failing run of this outcome can hold (judge.py judge_run)."""
    return {
        "answered": [  # broken_rules: empty alone, else expect and forbid in any mix
            frozenset({"empty"}),
            frozenset({"expect"}),
            frozenset({"forbid"}),
            frozenset({"expect", "forbid"}),
        ],
        "crashed": [frozenset({"crashed"})],  # `if outcome in ENDED_BADLY: Broken(outcome, ...)`
        "timed out": [frozenset({"timed out"})],
        "refused call": [frozenset({"refused"})],  # `if refused_call is not None: Broken(REFUSED)`
    }.get(outcome, [])


def plain_left_out(left_out: str | None) -> bool:
    return left_out in outcomes.OUTCOMES


# Each exclusion names the line that rules its combinations out. The test below proves the
# list is exact: it rules out every combination the judge does not give, and no other.
EXCLUDED: list[tuple[str, Callable[..., bool]]] = [
    (
        "judge.py RunJudgement.verdict: `if self.left_out is not None: return LEFT_OUT`, "
        "else FAIL or PASS; a run is left out exactly when it has a reason",
        lambda o, v, left, k, f, fi: (v == judge.LEFT_OUT) != (left is not None),
    ),
    (
        "judge.py judge_run: every left_out return is `RunJudgement(left_out=...)`, "
        "with no broken rule",
        lambda o, v, left, k, f, fi: v == judge.LEFT_OUT and bool(k),
    ),
    (
        "judge.py RunJudgement.verdict: `FAIL if self.broken else PASS`",
        lambda o, v, left, k, f, fi: (v == judge.PASS and bool(k)) or (v == judge.FAIL and not k),
    ),
    (
        "runner.py one: `fired = None if row.fault is None else ...`; a baseline has no fault",
        lambda o, v, left, k, f, fi: not f and fi,
    ),
    (
        "judge.py judge_run `if not exercised: left_out=NOT_FIRED` with runner.py "
        "`exercised` (fault_fired is not False, or stopped): a fault row that did not fire "
        "is left out as not fired, unless the test was stopped",
        lambda o, v, left, k, f, fi: (
            (f and not fi and o != "stopped" and left != judge.NOT_FIRED)
            or (left == judge.NOT_FIRED and not (f and not fi and o != "stopped"))
        ),
    ),
    (
        "judge.py judge_run `if mistake and outcome == 'crashed'` after `if not exercised`: "
        "a mistake leaves out a crash that was exercised",
        lambda o, v, left, k, f, fi: (
            left == judge.TEST_MISTAKE and (o != "crashed" or (f and not fi))
        ),
    ),
    (
        "judge.py judge_run `if outcome != 'answered': left_out=outcome` after the crash and "
        "time-out returns: a plain left_out is the run's own outcome, and not those three",
        lambda o, v, left, k, f, fi: (
            plain_left_out(left) and (left != o or o in ("answered", *judge.ENDED_BADLY))
        ),
    ),
    (
        "judge.py judge_run: only `broken_rules(answer, rules)` on an answered run can be empty; "
        "every other outcome returns a broken rule or a reason to leave the run out",
        lambda o, v, left, k, f, fi: v == judge.PASS and o != "answered",
    ),
    (
        "judge.py judge_run: a failing run holds the broken set its outcome gives "
        "(failing_kinds); no other outcome fails",
        lambda o, v, left, k, f, fi: v == judge.FAIL and k not in failing_kinds(o),
    ),
    (
        "runner.py one `chose = row.fault is not None and ...`: only a fault row that fired "
        "is judged refused; a refused call on a baseline is left out",
        lambda o, v, left, k, f, fi: judge.REFUSED in k and not (f and fi),
    ),
    (
        "judge.py judge_run `exercised` is true for a baseline: it is never left out as not fired",
        lambda o, v, left, k, f, fi: not f and left == judge.NOT_FIRED,
    ),
    (
        "runner.py one writes the summary after `result.judgement = judge.judge_run(...)`; "
        "NOT_JUDGED is only the field's default and is never in a record",
        lambda o, v, left, k, f, fi: left == UNJUDGED,
    ),
]
# What `main` answers with no suggestion, on purpose, and why.
NO_RULE: list[tuple[str, Callable[..., bool]]] = [
    ("a pass gets no suggestion", lambda o, v, left, k, f, fi: v == judge.PASS),
    (
        "a fault row left out for a refused call it did not choose: the refusal came from "
        "something other than a call the agent picked, and the run's own lines say which",
        lambda o, v, left, k, f, fi: f and left == suggest.REFUSED_CALL,
    ),
]


def is_excluded(combo: Combo) -> bool:
    return any(rule(*combo) for _, rule in EXCLUDED)


def test_the_exclusions_are_exactly_what_the_judge_cannot_produce() -> None:
    made = produced()
    combos = list(every_combo())
    assert made <= set(combos)
    wrong = [c for c in combos if is_excluded(c) == (c in made)]
    assert not wrong, wrong[:5]
    for why, rule in EXCLUDED:  # an exclusion that rules nothing out is dead weight
        assert any(rule(*c) for c in combos), why
    for why, rule in NO_RULE:
        assert any(rule(*c) for c in made), why


@pytest.mark.design("suggest.every_failing_combination_covered")
def test_every_failing_combination_gets_exactly_one_main_suggestion_or_no_rule() -> None:
    seen = {"suggestion": 0, "no rule": 0}
    codes: set[str] = set()
    for combo in every_combo():
        if is_excluded(combo):
            continue
        got = main_of(combo)  # never raises
        intended = any(rule(*combo) for _, rule in NO_RULE)
        if intended:
            assert isinstance(got, suggest.NoRule), (combo, got)
            assert got.reason, combo
            seen["no rule"] += 1
        else:
            assert isinstance(got, suggest.Suggestion), (combo, got)
            assert got.code in suggest.MAIN_CODES, (combo, got.code)
            codes.add(got.code)
            seen["suggestion"] += 1
    # Every rule is reached by a real combination, but the retry loop: it differs from the
    # timeout after a fault by the count of lines, which is no dimension of a combination.
    # The unchanged fault is told by a field on a line, which is no dimension either.
    assert codes == set(suggest.MAIN_CODES) - {"retry_loop", "fault_not_changed"}
    assert seen["suggestion"] > 0 and seen["no rule"] > 0
    assert len(list(every_combo())) - sum(map(is_excluded, every_combo())) == sum(seen.values())


# --- each rule ------------------------------------------------------------------------------

Case = tuple[Doc, list[Doc], Doc | None]


def case(
    outcome: str,
    judged: str,
    left_out: str | None = None,
    kinds: set[str] | None = None,
    *,
    fault: bool = True,
    lines: list[Doc] | None = None,
) -> Case:
    return (
        run_doc(outcome, judged, left_out, kinds or set(), "fault 1" if fault else "baseline"),
        lines if lines is not None else [line(1), fault_line(2)],
        FAULT_ROW if fault else BASELINE_ROW,
    )


def more_calls(n: int, proc: Doc = PROC) -> list[Doc]:
    return [{**line(3 + i, "tool:get_order"), "proc": proc} for i in range(n)]


WHY = "the reply is image/png, which is not text or JSON"
UNCHANGED = line(2, "tool:get_order", unchanged={"do": "append", "why": WHY})
MISS = {"nearest": None}
# code: the run with its evidence, and the same run with that evidence taken away.
RULE_CASES: dict[str, tuple[Case, Case]] = {
    "test_mistake": (
        case("crashed", "left out", judge.TEST_MISTAKE, lines=[line(1), line(2, mistake="m")]),
        case("crashed", "left out", judge.TEST_MISTAKE, lines=[line(1)]),
    ),
    "not_agent": (
        case("budget reached", "left out", "budget reached"),
        case("answered", "left out", "x"),
    ),
    "fault_not_fired": (
        case("answered", "left out", judge.NOT_FIRED, lines=[line(1)]),
        case("answered", "fail", None, {"forbid"}, lines=[line(1)]),
    ),
    "fault_not_changed": (
        case("answered", "left out", judge.NOT_FIRED, lines=[line(1), UNCHANGED]),
        case("answered", "left out", judge.NOT_FIRED, lines=[line(1)]),
    ),
    "refused_call": (
        case(
            "refused call",
            "fail",
            None,
            {"refused"},
            lines=[fault_line(1), line(2, "x", "refused")],
        ),
        case("refused call", "fail", None, {"refused"}, lines=[fault_line(1)]),
    ),
    "refused_on_baseline": (
        case(
            "refused call", "left out", "refused call", fault=False, lines=[line(1, "x", "refused")]
        ),
        case("refused call", "left out", "refused call", fault=False, lines=[line(1)]),
    ),
    "crash_after_fault": (
        case("crashed", "fail", None, {"crashed"}),
        case("crashed", "fail", None, {"crashed"}, lines=[line(1)]),
    ),
    "crash_on_baseline": (
        case("crashed", "fail", None, {"crashed"}, fault=False),
        case("crashed", "fail", None, {"crashed"}, fault=True, lines=[line(1)]),
    ),
    "retry_loop": (
        case(
            "timed out", "fail", None, {"timed out"}, lines=[line(1), fault_line(2), *more_calls(3)]
        ),
        case(
            "timed out", "fail", None, {"timed out"}, lines=[line(1), fault_line(2), *more_calls(2)]
        ),
    ),
    "timeout_after_fault": (
        case("timed out", "fail", None, {"timed out"}),
        case("timed out", "fail", None, {"timed out"}, lines=[line(1)]),
    ),
    "timeout_on_baseline": (
        case("timed out", "fail", None, {"timed out"}, fault=False),
        case("timed out", "fail", None, {"timed out"}, lines=[line(1)]),
    ),
    "forbid_after_fault": (
        case("answered", "fail", None, {"forbid"}),
        case("answered", "fail", None, {"forbid"}, lines=[line(1)]),
    ),
    "expect_after_fault": (
        case("answered", "fail", None, {"expect"}),
        case("answered", "fail", None, {"expect"}, lines=[line(1)]),
    ),
    "empty_answer": (
        case("answered", "fail", None, {"empty"}),
        case("answered", "pass"),
    ),
    "rule_on_baseline": (
        case("answered", "fail", None, {"forbid"}, fault=False),
        case("answered", "fail", None, {"forbid"}, fault=True, lines=[line(1)]),
    ),
}


@pytest.mark.parametrize("code", sorted(RULE_CASES))
def test_each_rule_fires_on_its_evidence_and_not_without_it(code: str) -> None:
    with_evidence, without = RULE_CASES[code]
    got = suggest.main(*with_evidence)
    assert isinstance(got, suggest.Suggestion) and got.code == code, got
    lacking = suggest.main(*without)
    assert not (isinstance(lacking, suggest.Suggestion) and lacking.code == code), lacking


def test_an_unchanged_fault_quotes_the_gates_reason_and_cites_its_line() -> None:
    got = suggest.main(*RULE_CASES["fault_not_changed"][0])
    assert isinstance(got, suggest.Suggestion) and got.code == "fault_not_changed"
    assert WHY in got.text and got.doc["evidence"] == [{"seq": 2, "proc": PROC}]
    never = suggest.main(*RULE_CASES["fault_not_changed"][1])  # the call was never made
    assert isinstance(never, suggest.Suggestion) and never.code == "fault_not_fired"


def test_every_main_rule_has_a_case() -> None:
    assert set(RULE_CASES) == set(suggest.MAIN_CODES)  # a new rule needs its evidence pair


def test_a_retry_loop_counts_calls_after_the_fault_in_its_own_process() -> None:
    other = more_calls(5, OTHER_PROC)  # another process's calls are not this one's retries
    early = [line(0, "tool:get_order") for _ in range(4)]  # before the fault line
    run, _, row = case("timed out", "fail", None, {"timed out"})
    got = suggest.main(run, [*early, line(1), fault_line(2), *other, *more_calls(3)], row)
    assert isinstance(got, suggest.Suggestion) and got.code == "retry_loop"
    assert "3 times" in got.text
    none = suggest.main(run, [*early, line(1), fault_line(2), *other], row)
    assert isinstance(none, suggest.Suggestion) and none.code == "timeout_after_fault"


@pytest.mark.design("suggest.cites_lines")
def test_every_suggestion_is_labelled_a_heuristic_and_cites_its_lines() -> None:
    checked = 0
    for combo in every_combo():
        if is_excluded(combo):
            continue
        got = main_of(combo)
        if not isinstance(got, suggest.Suggestion):
            continue
        lines = lines_for(combo)
        assert got.text.startswith("suggestion (a heuristic):"), got.text
        doc = got.doc
        assert doc["heuristic"] is True and doc["code"] == got.code
        evidence = cast("list[Doc]", doc["evidence"])
        assert evidence, combo  # what it read
        have = {(x["seq"], str(x["proc"])) for x in lines}
        for e in evidence:
            if e == {"summary": True}:
                assert got.code in SUMMARY_ONLY | {"empty_answer"}, (combo, e)
                continue
            assert (e["seq"], str(e["proc"])) in have, (combo, evidence)  # a line, never filler
            assert isinstance(e["seq"], int) and e["proc"] == PROC
        if got.code in SUMMARY_ONLY:  # these read nothing but the summary
            assert evidence == [{"summary": True}], (combo, evidence)
        checked += 1
    assert checked >= 30  # one per outcome, verdict, kind set, row and fired that can happen
    run, lines, row = RULE_CASES["crash_after_fault"][0]
    chosen = suggest.main(run, lines, row)
    assert isinstance(chosen, suggest.Suggestion)
    assert chosen.doc["evidence"] == [{"seq": 2, "proc": PROC}]  # the fault line, by seq and proc
    # A rule about the whole run cites the fault line too, so a reader finds it from any code.
    for code in ("refused_call", "empty_answer"):
        run, lines, row = RULE_CASES[code][0]
        got = suggest.main(run, lines, row)
        assert isinstance(got, suggest.Suggestion)
        fault_seq = next(x["seq"] for x in lines if x["action"] == "fault")
        assert {"seq": fault_seq, "proc": PROC} in cast("list[Doc]", got.doc["evidence"]), code


def test_a_test_mistake_is_never_read_as_a_crash_after_a_fault() -> None:
    lines = [line(1), fault_line(2), line(3, mistake="the mock for get_order has no match")]
    run = run_doc("crashed", "left out", judge.TEST_MISTAKE)
    got = suggest.main(run, lines, FAULT_ROW)
    assert isinstance(got, suggest.Suggestion) and got.code == "test_mistake"
    assert "the mock for get_order has no match" in got.text
    assert got.doc["evidence"] == [{"seq": 3, "proc": PROC}]


def test_a_timeout_suggestion_never_says_set_a_time_limit() -> None:
    seen = 0
    for code in ("retry_loop", "timeout_after_fault", "timeout_on_baseline"):
        got = suggest.main(*RULE_CASES[code][0])
        assert isinstance(got, suggest.Suggestion)
        assert "time limit" not in got.text.lower() and "set a time" not in got.text.lower()
        seen += 1
    assert seen == 3
    timeout = cast(suggest.Suggestion, suggest.main(*RULE_CASES["timeout_after_fault"][0]))
    assert "after an injected failure in tool:get_order" in timeout.text  # the time went after


def miss_case(action: str) -> tuple[Doc, list[Doc]]:
    run = run_doc("answered", "fail", None, {"forbid"})
    return run, [line(1), fault_line(2), line(3, "http:shop", action, mock_miss=MISS)]


def test_a_miss_suggestion_follows_the_lines_action() -> None:
    run, lines = miss_case("real")
    real = suggest.extras(run, lines)
    assert [s.code for s in real] == ["mock_miss_real"]
    assert "ran for real" in real[0].text and "refused" not in real[0].text
    run, lines = miss_case("refused")
    refused = suggest.extras(run, lines)
    assert [s.code for s in refused] == ["mock_miss_refused"]
    assert "was refused" in refused[0].text and "ran for real" not in refused[0].text
    assert real[0].text != refused[0].text and real[0].doc["evidence"] == [{"seq": 3, "proc": PROC}]
    both = [*lines, line(4, "http:shop", "real", mock_miss=MISS)]
    assert [s.code for s in suggest.extras(run, both)] == [
        "mock_miss_refused"
    ]  # the first, one only
    assert suggest.extras(run, [line(1, "http:shop", "real")]) == []  # no miss, no suggestion
    listed = suggest.for_run(run, lines, FAULT_ROW)
    assert [s.code for s in listed] == ["forbid_after_fault", "mock_miss_refused"]  # main first


@pytest.mark.design("suggest.never_because")
def test_no_suggestion_says_because_or_wrong() -> None:
    texts: list[str] = []
    for combo in every_combo():
        if is_excluded(combo):
            continue
        got = main_of(combo)
        if isinstance(got, suggest.Suggestion):
            texts.append(got.text)
    for action in ("real", "refused"):
        texts += [s.text for s in suggest.extras(*miss_case(action))]
    assert len(set(texts)) >= len(suggest.CODES)
    assert not [t for t in texts if BAD_WORDS.search(t)]


def test_a_pass_gets_no_suggestion() -> None:
    run = run_doc("answered", "pass")
    lines = [line(1), fault_line(2), line(3, "http:shop", "real", mock_miss=MISS)]
    got = suggest.main(run, lines, FAULT_ROW)
    assert isinstance(got, suggest.NoRule) and "pass" in got.reason
    assert suggest.extras(run, lines) == []
    assert suggest.for_run(run, lines, FAULT_ROW) == []


def test_odd_shapes_give_no_rule_and_never_raise() -> None:
    odd: list[Doc] = [
        {},
        {"outcome": None, "judged": None},  # a run with no summary line
        {"outcome": "answered", "judged": "fail", "broken": "x"},
        {"outcome": "answered", "judged": "fail", "broken": [1, None, {"kind": 5}]},
        {"outcome": "answered", "judged": "fail", "broken": [{"kind": "forbid"}], "row": None},
    ]
    for run in odd:
        suggest.for_run(run, [{"seq": "x", "proc": None, "action": "fault", "call": 3}], None)
        suggest.for_run(run, [], {"fault": {}})
    got = suggest.main({"outcome": None, "judged": None}, [], None)
    assert isinstance(got, suggest.NoRule) and "did not end" in got.reason


def test_a_secret_in_a_call_label_is_redacted_in_the_text() -> None:
    key = "sk-" + "a1b2c3d4e5f6a7b8c9d0e1f2a3b4"
    run, lines, row = case("crashed", "fail", None, {"crashed"})
    lines = [line(1), {**fault_line(2), "call": f"tool:get_{key}"}]
    got = suggest.main(run, lines, row)
    assert isinstance(got, suggest.Suggestion) and key not in got.text


def test_suggest_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    def refuse(*args: object, **kwargs: object) -> Any:
        raise AssertionError("suggest touched a file or a socket")

    monkeypatch.setattr(builtins, "open", refuse)
    monkeypatch.setattr(os, "open", refuse)
    monkeypatch.setattr(socket, "socket", refuse)
    for combo in itertools.islice(every_combo(), 0, None, 7):
        if not is_excluded(combo):
            main_of(combo)
    for with_evidence, _ in RULE_CASES.values():
        suggest.for_run(*with_evidence)
    assert list(tmp_path.iterdir()) == []
    source = Path(suggest.__file__).read_text()
    assert "scrimmage.toml" not in source and ".write" not in source


def test_a_retry_loop_cites_the_fault_line_and_each_repeated_call() -> None:
    run, _, row = case("timed out", "fail", None, {"timed out"})
    lines = [line(1), fault_line(2), *more_calls(3)]
    got = suggest.main(run, lines, row)
    assert isinstance(got, suggest.Suggestion) and got.code == "retry_loop"
    assert got.doc["evidence"] == [{"seq": n, "proc": PROC} for n in (2, 3, 4, 5)]


def test_a_loop_over_other_arguments_is_not_a_retry() -> None:
    run, _, row = case("timed out", "fail", None, {"timed out"})
    others = [{**x, "args": {"sha256": f"b{i}"}} for i, x in enumerate(more_calls(5))]
    got = suggest.main(run, [line(1), fault_line(2), *others], row)
    assert isinstance(got, suggest.Suggestion) and got.code == "timeout_after_fault"
    bare = {k: v for k, v in fault_line(2).items() if k != "args"}  # no arguments to compare
    got = suggest.main(run, [line(1), bare, *more_calls(4)], row)
    assert isinstance(got, suggest.Suggestion) and got.code == "timeout_after_fault"


def test_a_mistake_on_a_refused_line_is_the_tests_not_the_agents() -> None:
    mistake = "Ledger is a class and cannot be gated"
    bare = {"sha256": "0", "bytes": 2, "preview": "{}"}
    klass = line(
        1, "tool:Ledger", "refused", reason="unlisted", args=bare, mistake=mistake, by_config=True
    )
    base_case = case(
        "refused call", "left out", "refused call", fault=False, lines=[klass, line(2)]
    )
    base = suggest.main(*base_case)
    assert isinstance(base, suggest.Suggestion) and base.code == "test_mistake"
    assert mistake in base.text and base.doc["evidence"] == [{"seq": 1, "proc": PROC}]
    blind = line(
        3, "process:python", "refused", reason="blind_child", mistake="no gates", by_config=True
    )
    fault_case = case(
        "refused call", "fail", None, {"refused"}, lines=[line(1), fault_line(2), blind]
    )
    fault = suggest.main(*fault_case)
    assert isinstance(fault, suggest.Suggestion) and fault.code == "test_mistake"
    assert fault.doc["evidence"] == [{"seq": 3, "proc": PROC}]
    # An agent's own call to an unlisted tool also carries a mistake: that is its choice.
    own = line(3, "tool:refund_order", "refused", reason="unlisted", mistake="add it to real")
    chosen = suggest.main(
        *case("refused call", "fail", None, {"refused"}, lines=[line(1), fault_line(2), own])
    )
    assert isinstance(chosen, suggest.Suggestion) and chosen.code == "refused_call"
    chose_then_blind = suggest.main(
        *case("refused call", "fail", None, {"refused"}, lines=[line(1), fault_line(2), own, blind])
    )
    assert isinstance(chose_then_blind, suggest.Suggestion)
    assert chose_then_blind.code == "refused_call"  # the first refusal was the agent's
    own_base = case("refused call", "left out", "refused call", fault=False, lines=[line(1), own])
    chosen_base = suggest.main(*own_base)
    assert isinstance(chosen_base, suggest.Suggestion) and chosen_base.code == "refused_on_baseline"


def test_the_wording_follows_what_the_record_shows() -> None:
    before = suggest.main(
        *case(
            "refused call",
            "fail",
            None,
            {"refused"},
            lines=[line(1, "x", "refused"), fault_line(2)],
        )
    )
    after = suggest.main(*RULE_CASES["refused_call"][0])
    assert isinstance(before, suggest.Suggestion) and isinstance(after, suggest.Suggestion)
    assert (
        "when the fault fires" not in before.text
        and "after the injected failure" not in before.text
    )
    assert "when the fault fires" in after.text and "after the injected failure" in after.text
    base = suggest.main(*case("answered", "fail", None, {"empty"}, fault=False))
    fault = suggest.main(*case("answered", "fail", None, {"empty"}))
    assert isinstance(base, suggest.Suggestion) and isinstance(fault, suggest.Suggestion)
    assert "baseline" in base.text and "fallback" not in base.text and "fallback" in fault.text
    crashed = suggest.main(*case("crashed", "left out", judge.NOT_FIRED, lines=[line(1)]))
    answered = suggest.main(*case("answered", "left out", judge.NOT_FIRED, lines=[line(1)]))
    assert isinstance(crashed, suggest.Suggestion) and isinstance(answered, suggest.Suggestion)
    assert 'ended "crashed"' in crashed.text and "ended" not in answered.text


def test_the_runners_own_rules_decide_what_the_judge_is_given() -> None:
    assert runner.fault_fired(False, 3) is None and runner.fault_fired(True, 0) is False
    assert runner.is_exercised(None, "answered") and not runner.is_exercised(False, "answered")
    assert runner.is_exercised(False, "stopped")
    assert runner.chose_refused(True, "unlisted") and not runner.chose_refused(False, "unlisted")
    assert not runner.chose_refused(True, "other_python")


def test_a_first_call_with_no_arguments_to_an_unlisted_tool_is_the_agents_choice() -> None:
    bare = {"sha256": "0", "bytes": 2, "preview": "{}"}
    first = line(1, "tool:refund_order", "refused", reason="unlisted", args=bare, mistake="add it")
    got = suggest.main(
        *case("refused call", "left out", "refused call", fault=False, lines=[first])
    )
    assert isinstance(got, suggest.Suggestion) and got.code == "refused_on_baseline"
