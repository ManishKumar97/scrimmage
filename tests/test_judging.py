"""Judging: the rules, the rates, the table and the exit codes, on real runs of scrimmage test."""

import json
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import cast

import jsonschema
import pytest

import scrimmage
from conftest import ACME, Project, Result, TimeLimit
from fakes.openai_server import FakeOpenAI
from scrimmage import config, judge, plan
from scrimmage.errors import ConfigError
from scrimmage.judge import (
    Broken,
    Rules,
    RunJudgement,
    broken_rules,
    floor_runs,
    judge_row,
    judge_run,
    normal,
    wilson,
)
from scrimmage.runner import RunResult, report

Fake = Callable[[list[str]], FakeOpenAI]
DATE = ("Oct 30", "October 30")
RULES = Rules((("shipped",), DATE), ("refunded",), 1.0)
SECRET = "sk-" + "a1b2c3d4" * 3
RECORD = json.loads((Path(scrimmage.__file__).parent / "schemas" / "record.v1.json").read_text())
AGENT = '''"""Plays one scripted step per run, in order, so a test says what each run does."""

import json
import pathlib
import time

from acme import tools

HERE = pathlib.Path(__file__).parent


def answer(question):
    turn = HERE / "turn"
    n = int(turn.read_text()) if turn.exists() else 0
    turn.write_text(str(n + 1))
    step = json.loads((HERE / "script.json").read_text())[n]
    for _ in range(step.get("calls", 1 if step.get("fired") else 0)):
        try:
            tools.get_order("1234")
        except TimeoutError:
            pass
    if step.get("refund"):
        tools.refund_order("1234")
    if step.get("post"):
        import httpx

        httpx.post("https://payments.example.invalid/refund")
    if step.get("blind"):
        import subprocess, sys

        subprocess.run([sys.executable, "-c", "print(1)"], env={"PATH": "/usr/bin"}, check=False)
    if step.get("model"):
        import openai

        openai.OpenAI(max_retries=0).chat.completions.create(
            model="gpt-4o-mini", messages=[{"role": "user", "content": question}]
        )
    if step.get("crash"):
        raise RuntimeError("the bot crashed")
    if step.get("hang"):
        pathlib.Path("hanging").touch()  # the test's event: the time limit finds it here
        time.sleep(600)
    return step["answer"]
'''
FAULT = '[[test.fault]]\ncall = "tool:get_order"\ndo = "timeout"\n'
ALWAYS = FAULT + 'forbid = ["never said"]\n'
GOOD = "It shipped, and it arrives on October 30."
BAD = "It shipped."


@pytest.mark.parametrize("blank", ["\u034f", "\ufe0f", "\u3164"])
def test_a_blank_character_hides_no_word(blank: str) -> None:
    assert broken_texts(f"re{blank}funded", Rules((), ("refunded",), 1.0))


def broken_texts(answer: str, rules: Rules) -> list[str]:
    return [broken.text for broken in broken_rules(answer, rules)]


def scripted(
    project: Project,
    steps: Sequence[Mapping[str, object]],
    body: str,
    agent: str = "",
    head: str = 'expect = ["shipped", ["Oct 30", "October 30"]]\n',
) -> Result:
    """scrimmage test on an agent that plays `steps`, one per run, baselines first."""
    (project.root / "canned.py").write_text(AGENT)
    (project.root / "script.json").write_text(json.dumps(steps))
    project.toml.write_text(
        '[agent]\nfunction = "canned:answer"\ntools = ["acme.tools:get_order", '
        '"acme.tools:refund_order"]\nparallel = 1\nmax_cost_usd = "none"\n'
        f'{agent}\n[[test]]\nname = "order"\nquestion = "Where is order 1234?"\n{head}'
        f"{body}"
    )
    return project.test()


def table_row(result: Result, row: str) -> list[str]:
    """One row of the table, as its cells split on runs of spaces."""
    line = next(x for x in result.out.splitlines() if x.startswith(f"order  {row}"))
    return re.split(r" {2,}", line)


@pytest.mark.design("judge.alternatives")
@pytest.mark.parametrize(
    ("answer", "broken"),
    [
        ("It shipped, due October 30.", []),
        ("It shipped, due Oct 30.", []),
        ("It shipped.", ['expect "Oct 30" or "October 30" not found']),
        ("Due October 30.", ['expect "shipped" not found']),
    ],
)
def test_every_item_must_match_and_a_list_item_on_any_alternative(
    answer: str, broken: list[str]
) -> None:
    assert broken_texts(answer, RULES) == broken


@pytest.mark.design("judge.forbid")
def test_an_answer_with_a_forbidden_text_fails() -> None:
    answer = "It shipped on Oct 30 and was REFUNDED."
    assert broken_texts(answer, RULES) == ['forbid "refunded" found']


@pytest.mark.design("judge.every_broken_rule")
def test_every_broken_rule_is_named() -> None:
    assert broken_texts("It was refunded.", RULES) == [
        'expect "shipped" not found',
        'expect "Oct 30" or "October 30" not found',
        'forbid "refunded" found',
    ]


@pytest.mark.design("judge.normal_form")
@pytest.mark.parametrize(
    ("answer", "rule"),
    [
        ("OCT 30", "oct 30"),
        ("Oct\u00a030", "Oct 30"),  # a non-breaking space
        ("Oct \n  30", "Oct 30"),
        ("couldn\u2019t", "couldn't"),
        ("\u201cshipped\u201d", '"shipped"'),
        ("five\u2011day", "five-day"),  # the non-breaking hyphen real models write
        ("Oct 28\u201330", "Oct 28-30"),
        ("\uff33hipped", "shipped"),  # a full-width letter
        ("STRASSE", "Stra\u00dfe"),  # casefold, which lower does not do
        ("ship\u200bped", "shipped"),  # a zero-width space
        ("ship\u00adped", "shipped"),  # a soft hyphen
        ("\u0130stanbul", "istanbul"),  # casefold leaves a combining dot
        ("2\u00b2", "22"),  # NFKC, which the docs warn about
    ],
)
def test_case_spaces_and_unicode_forms_do_not_decide_a_match(answer: str, rule: str) -> None:
    assert broken_texts(answer, Rules(((rule,),), (), 1.0)) == []


def test_normal_touches_nothing_else() -> None:
    assert normal("  Oct **30**,  2026 ") == "oct **30**, 2026"


@pytest.mark.design("judge.substring")
def test_a_rule_is_a_plain_substring() -> None:
    assert broken_texts("It has not shipped.", Rules((("shipped",),), (), 1.0)) == []


@pytest.mark.design("judge.empty_answer")
@pytest.mark.parametrize("answer", ["", "  \n\t"])
def test_an_empty_answer_fails_by_name(answer: str) -> None:
    assert broken_texts(answer, RULES) == [judge.EMPTY]


@pytest.mark.design("judge.forbid_alone")
def test_a_row_judged_by_forbid_alone_still_fails_an_empty_answer_and_a_crash() -> None:
    rules = Rules((), ("refunded",), 1.0)
    assert judge_run("answered", "I could not reach the model.", True, rules).verdict == judge.PASS
    assert judge_run("answered", "Refunded.", True, rules).verdict == judge.FAIL
    for answer in ("", "   ", "\u200b"):
        assert [b.kind for b in judge_run("answered", answer, True, rules).broken] == ["empty"]
    assert judge_run("crashed", None, True, rules).verdict == judge.FAIL
    assert judge_run("timed out", None, True, rules).verdict == judge.FAIL


@pytest.mark.design("judge.what_is_judged")
@pytest.mark.parametrize(
    ("outcome", "broken"),
    [
        ("answered", []),
        ("crashed", ["crashed"]),
        ("timed out", ["timed out"]),
        ("refused call", None),
        ("budget reached", None),
        ("infrastructure error", None),
        ("stopped", None),
    ],
)
def test_a_crash_or_a_time_out_fails_and_the_rest_are_left_out(
    outcome: str, broken: list[str] | None
) -> None:
    judged = judge_run(outcome, "It shipped, Oct 30.", True, RULES)
    if broken is None:
        assert (judged.verdict, judged.left_out) == ("left out", outcome)
    else:
        assert [b.kind for b in judged.broken] == broken and judged.left_out is None


@pytest.mark.design("judge.not_exercised_left_out")
@pytest.mark.parametrize("outcome", ["answered", "crashed", "timed out"])
def test_a_run_where_the_fault_did_not_fire_is_left_out_however_it_ended(outcome: str) -> None:
    assert judge_run(outcome, "It shipped, Oct 30.", False, RULES).left_out == judge.NOT_FIRED


PASSED = RunJudgement()
FAILED_RUN = RunJudgement((Broken("crashed", "crashed"),))
OUT = RunJudgement(left_out="stopped")


@pytest.mark.design("judge.row_verdict")
@pytest.mark.parametrize(
    ("broken", "runs", "rate", "refused", "verdict"),
    [
        ([PASSED] * 5, 5, 1.0, False, "pass"),
        ([PASSED] * 4 + [FAILED_RUN], 5, 1.0, False, "fail"),
        ([PASSED] * 4 + [FAILED_RUN], 5, 0.8, False, "pass"),
        ([PASSED] * 3 + [FAILED_RUN] * 2, 5, 0.8, False, "fail"),
        ([PASSED] * 4 + [OUT], 5, 1.0, False, "pass"),  # one unfired run does not block a pass
        ([PASSED] * 3 + [OUT] * 2, 5, 1.0, False, "pass"),  # at the floor
        ([PASSED] * 2 + [OUT] * 3, 5, 1.0, False, "no result"),  # under the floor
        ([PASSED] + [OUT] * 4, 5, 1.0, False, "no result"),  # never a pass on one run
        ([PASSED] * 2 + [FAILED_RUN] + [OUT] * 2, 5, 0.8, False, "fail"),  # 2/3, over judged runs
        ([FAILED_RUN] + [OUT] * 4, 5, 0.8, False, "no result"),  # the floor comes first
        ([OUT] * 5, 5, 1.0, False, "no result"),
        ([], 5, 1.0, False, "no result"),
        ([PASSED], 1, 1.0, False, "pass"),
        ([PASSED], 2, 1.0, False, "no result"),
        ([PASSED] * 4 + [OUT], 5, 0.8, True, "no result"),  # a refusal stops the row
        ([PASSED] * 2 + [FAILED_RUN], 5, 1.0, True, "fail"),
    ],
)
def test_a_rows_verdict(
    broken: list[RunJudgement], runs: int, rate: float, refused: bool, verdict: str
) -> None:
    assert judge_row(broken, runs, rate, refused).verdict == verdict


@pytest.mark.design("judge.floor")
@pytest.mark.parametrize(("runs", "floor"), [(1, 1), (2, 2), (3, 2), (4, 3), (5, 3), (100, 51)])
def test_a_row_needs_more_than_half_its_runs_judged(runs: int, floor: int) -> None:
    assert floor_runs(runs) == floor


@pytest.mark.design("judge.wilson_range")
@pytest.mark.parametrize(
    ("passed", "judged", "low", "high"),
    [(5, 5, 57, 100), (0, 5, 0, 43), (4, 5, 38, 96), (1, 1, 21, 100), (3, 5, 23, 88)],
)
def test_the_range_is_the_95_percent_wilson_interval(
    passed: int, judged: int, low: int, high: int
) -> None:
    bottom, top = wilson(passed, judged)
    assert (round(bottom * 100), round(top * 100)) == (low, high)


def load(project: Project) -> config.Config:
    return config.load(project.toml)


@pytest.mark.design("config.alternatives")
def test_expect_reads_text_and_lists_of_alternatives(project: Project) -> None:
    assert load(project).tests[0].expect == (("shipped",), ("Oct 30", "October 30"))


@pytest.mark.design("config.rules_on_every_fault_kind")
@pytest.mark.parametrize(
    "fault",
    [
        'call = "tool:get_order"\ndo = "timeout"\n',
        'call = "model"\ndo = "status 429"\n',
        'call = "http:orders.acme.internal"\ndo = "status 503"\n',
        'call = "mcp:docs/search"\ndo = "error"\n',
    ],
)
def test_every_kind_of_fault_can_carry_its_own_rules(project: Project, fault: str) -> None:
    text = project.toml.read_text()
    block = text[text.index("[[test.fault]]") :]
    rules = 'expect = [["sorry"]]\nforbid = ["shipped"]\npass_rate = 0.5\n'
    project.toml.write_text(text.replace(block, f"[[test.fault]]\n{fault}{rules}"))
    parsed = load(project).tests[0].faults[0]
    assert (parsed.expect, parsed.forbid, parsed.pass_rate) == ((("sorry",),), ("shipped",), 0.5)


EXPECT = 'expect   = ["shipped", ["Oct 30", "October 30"]]'


@pytest.mark.design("config.bad_rules")
@pytest.mark.parametrize(
    ("new", "message"),
    [
        ('expect = ["a", []]', "a list of alternatives is empty"),
        ('expect = ["a", [["b"]]]', "expected text"),
        ("expect = [1]", "expected text"),
        ('expect = ["  "]', "expected text"),
        ('expect = [["a", ""]]', "expected text"),
        ('expect = "shipped"', "expected a list"),
        (f'{EXPECT}\nforbid = "a"', "forbid: expected a list"),
        (f"{EXPECT}\nforbid = [1]", "forbid: expected text"),
        (f'{EXPECT}\nforbid = [""]', "forbid: expected text"),
        (f'{EXPECT}\nforbid = ["\u200b"]', "forbid: expected text"),
        ('expect = ["\u00ad "]', "expected text"),
        (f"{EXPECT}\npass_rate = 0", "pass_rate: expected a share"),
        (f"{EXPECT}\npass_rate = 1.5", "pass_rate: expected a share"),
        (f"{EXPECT}\npass_rate = 80", "pass_rate: expected a share"),
        (f"{EXPECT}\npass_rate = -0.5", "pass_rate: expected a share"),
        (f"{EXPECT}\npass_rate = true", "pass_rate: expected a share"),
        (f'{EXPECT}\npass_rate = "0.8"', "pass_rate: expected a share"),
        (f"{EXPECT}\npass_rate = nan", "pass_rate: expected a share"),
        (f"{EXPECT}\npass_rate = inf", "pass_rate: expected a share"),
    ],
)
def test_a_bad_rule_is_refused_when_the_file_is_read(
    project: Project, new: str, message: str
) -> None:
    project.edit(EXPECT, new)
    with pytest.raises(ConfigError, match=message):
        load(project)


def test_pass_rate_of_one_is_a_whole_number_in_toml(project: Project) -> None:
    project.edit(EXPECT, f"{EXPECT}\npass_rate = 1")
    assert load(project).tests[0].pass_rate == 1.0


def test_a_rule_on_a_mock_is_unknown(project: Project) -> None:
    project.edit('returns = "shipped, Oct 30"', 'returns = "x"\nexpect = ["y"]')
    with pytest.raises(ConfigError, match=r"expect: unknown setting; see docs/configuration.md"):
        load(project)


@pytest.mark.design("config.errors_redacted")
@pytest.mark.parametrize(
    ("line", "message"),
    [
        (f'expect = [["x", ["{SECRET}"]]]', "expected text"),
        (f'forbid = [["{SECRET}"]]', "expected text"),
        (f'pass_rate = "{SECRET}"', "pass_rate"),
    ],
)
def test_a_bad_rule_prints_no_known_secret(project: Project, line: str, message: str) -> None:
    extra = f"{EXPECT}\n{line}" if not line.startswith("expect") else line
    project.edit(EXPECT, extra)
    result = project.test()
    assert result.code == 2 and message in result.err
    assert SECRET not in result.err and "[secret removed]" in result.err


@pytest.mark.design("config.impossible_redacted")
def test_a_row_no_answer_can_pass_prints_no_known_secret(project: Project) -> None:
    project.edit(EXPECT, f'expect = ["{SECRET}"]\nforbid = ["{SECRET}"]')
    result = project.test()
    assert result.code == 2 and "no answer can pass" in result.err
    assert SECRET not in result.err


@pytest.mark.design("config.impossible_row")
@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (EXPECT, f'{EXPECT}\nforbid = ["ship"]', "baseline: no answer can pass"),
        ('do   = "timeout"', 'do   = "timeout"\nforbid = ["SHIPPED"]', "fault on tool:get_order"),
    ],
)
def test_a_row_no_answer_can_pass_costs_no_model_call(
    project: Project, old: str, new: str, message: str
) -> None:
    project.edit('expect = [["unable", "sorry", "try again"]]\n', "")
    project.edit('forbid = ["Oct 30", "October 30"]\n', "")
    project.edit('do   = "timeout"\n', 'do   = "timeout"\nwhen = "first 1"\n')
    project.edit(old, new)
    fake = FakeOpenAI(ACME)
    fake.start()
    try:
        result = project.test(fake)
    finally:
        fake.stop()
    assert result.code == 2 and fake.requests == []
    assert message in result.err


def test_two_forbid_texts_can_rule_out_an_item_between_them(project: Project) -> None:
    project.edit(EXPECT, 'expect = [["Oct 30", "shipped"]]\nforbid = ["Oct", "shipped"]')
    with pytest.raises(ConfigError, match="baseline: no answer can pass"):
        load(project)


def test_one_open_alternative_is_enough_for_a_row_to_be_possible(project: Project) -> None:
    project.edit(EXPECT, 'expect = [["shipped", "on its way"]]\nforbid = ["shipped"]')
    assert load(project).tests[0].forbid == ("shipped",)


@pytest.mark.design("judge.agent_never_sees_rules")
def test_no_rule_goes_in_a_plan(project: Project) -> None:
    project.edit(EXPECT, 'expect = ["hidden-expect"]\nforbid = ["hidden-forbid"]\npass_rate = 0.25')
    loaded = load(project)
    for row in plan.rows(loaded):
        text = json.dumps(plan.plan_for(loaded, row, "run-1").to_json())
        assert "hidden" not in text and "pass_rate" not in text and "expect" not in text
        assert "unable" not in text


@pytest.mark.design("judge.raw_answer")
@pytest.mark.slow
def test_the_raw_answer_is_compared_and_never_reaches_a_record(project: Project) -> None:
    # Redacted, this answer has no key in it and the forbid would find nothing.
    result = scripted(
        project,
        [{"answer": f"shipped Oct 30, key {SECRET}"}],
        "",
        "runs = 1",
        f'expect = ["shipped"]\nforbid = ["{SECRET}"]\n',
    )
    assert result.code == 1, result.out
    summary = result.summaries()[0]
    assert summary["judged"] == "fail"
    assert summary["broken_rules"] == [
        {"kind": "forbid", "text": 'forbid "[secret removed]" found'}
    ]
    assert SECRET not in str(summary["answer"]) and "[secret removed]" in str(summary["answer"])
    assert SECRET not in result.out + result.err
    for path in (project.root / "scrimmage-runs").rglob("*.jsonl"):
        assert SECRET not in path.read_text()


@pytest.mark.slow
def test_the_recorded_answer_is_redacted_as_the_python_string_it_was(project: Project) -> None:
    """The answer is a Python string, so its backslash-n is a backslash and a letter."""
    answer = "shipped Oct 30, password=C:\\new2zz"
    result = scripted(project, [{"answer": answer}], "", "runs = 1")
    summary = result.summaries()[0]
    assert summary["answer"] == "shipped Oct 30, [secret removed]", summary
    for path in (project.root / "scrimmage-runs").rglob("*.jsonl"):
        assert "2zz" not in path.read_text()


@pytest.mark.design("judge.raw_answer_expect")
@pytest.mark.slow
def test_an_expected_url_matches_though_the_record_cuts_its_query(project: Project) -> None:
    url = "https://track.acme.example/1234?carrier=ups"
    result = scripted(project, [{"answer": url}], "", "runs = 1", f'expect = ["{url}"]\n')
    assert result.code == 0, result.out
    summary = result.summaries()[0]
    assert summary["judged"] == "pass" and "carrier" not in str(summary["answer"])


@pytest.mark.design("judge.fault_own_expect")
@pytest.mark.slow
def test_a_fault_with_its_own_expect_judges_by_it(project: Project) -> None:
    result = scripted(
        project,
        [
            {"answer": GOOD},
            {"answer": GOOD},
            {"answer": "Sorry, I am unable to look that up.", "fired": True},
            {"answer": GOOD, "fired": True},
        ],
        f'{FAULT}expect = [["sorry", "unable"]]\nforbid = ["October 30"]\n',
        "runs = 2",
    )
    assert result.code == 1, result.out
    assert table_row(result, "baseline")[-1] == "pass"
    cells = table_row(result, "fault 1")
    assert cells[-1] == "fail" and "1/2" in cells
    assert 'expect "sorry" or "unable" not found' in result.out
    assert 'forbid "October 30" found' in result.out


@pytest.mark.design("judge.fault_inherits_expect")
@pytest.mark.slow
def test_a_fault_with_no_expect_of_its_own_uses_the_tests(project: Project) -> None:
    result = scripted(
        project,
        [{"answer": GOOD}, {"answer": GOOD, "fired": True}],
        f'{FAULT}when = "first 1"\n',
        "runs = 1",
    )
    assert result.code == 0, result.out
    assert "general knowledge" not in result.out


@pytest.mark.design("config.always_fault_needs_own_rules")
@pytest.mark.parametrize("rules", ["", "forbid = []\n", "pass_rate = 0.5\n", 'when = "always"\n'])
def test_a_fault_on_every_call_needs_its_own_rules(project: Project, rules: str) -> None:
    result = scripted(project, [{"answer": GOOD}], f"{FAULT}{rules}", "runs = 1")
    assert result.code == 2 and not result.summaries()
    assert "general knowledge" in result.err and "its own expect or forbid" in result.err


@pytest.mark.design("config.own_rules_on_an_always_fault")
@pytest.mark.parametrize("rules", ['expect = ["sorry"]\n', 'forbid = ["shipped"]\n'])
@pytest.mark.slow
def test_own_expect_or_forbid_is_enough(project: Project, rules: str) -> None:
    head = 'expect = [["delivered", "sorry"]]\n'
    scripted(project, [{"answer": "sorry"}] * 2, f"{FAULT}{rules}", "runs = 1", head)
    assert load(project).tests[0].faults[0].when == "always"


@pytest.mark.design("judge.fault_forbid_replaces")
@pytest.mark.slow
def test_a_faults_forbid_replaces_the_tests_and_an_empty_one_drops_it(project: Project) -> None:
    head = 'expect = ["shipped"]\nforbid = ["delayed"]\n'
    result = scripted(
        project,
        [
            {"answer": "shipped"},
            {"answer": "shipped, delayed", "fired": True},
            {"answer": "shipped, delayed", "fired": True},
        ],
        f'{FAULT}when = "first 1"\nforbid = []\n{FAULT}forbid = ["other"]\n',
        "runs = 1",
        head,
    )
    assert result.code == 0, result.out


@pytest.mark.design("judge.pass_rate_threshold")
@pytest.mark.parametrize(("good", "code", "verdict"), [(4, 0, "pass"), (3, 1, "fail")])
@pytest.mark.slow
def test_pass_rate_passes_four_of_five_and_fails_three_of_five(
    project: Project, good: int, code: int, verdict: str
) -> None:
    steps = [{"answer": GOOD}] * good + [{"answer": BAD}] * (5 - good)
    result = scripted(
        project,
        steps,
        "",
        "runs = 5",
        'expect = ["shipped", ["Oct 30", "October 30"]]\npass_rate = 0.8\n',
    )
    assert result.code == code, result.out
    assert table_row(result, "baseline")[-1] == verdict
    assert f"{good}/5" in table_row(result, "baseline")
    word = "note" if code == 0 else "fail"
    assert f"{word}: order baseline: {good}/5 passed, pass_rate is 0.8" in result.out


@pytest.mark.design("report.names_the_runs")
@pytest.mark.slow
def test_a_failed_rule_names_its_runs_and_stops_at_three(project: Project) -> None:
    result = scripted(project, [{"answer": BAD}] * 4, "", "runs = 4")
    assert result.code == 1, result.out
    names = {path.stem for path in (project.root / "scrimmage-runs").glob("*/*.jsonl")}
    line = next(x for x in result.out.splitlines() if x.startswith("  4 runs: expect"))
    shown = re.findall(r"run-[0-9a-f]+", line)
    assert len(shown) == 3 and set(shown) <= names and line.endswith(" and 1 more")


@pytest.mark.design("report.log_of_a_crash")
@pytest.mark.slow
def test_a_crashed_or_timed_out_run_gives_the_path_of_its_log(project: Project) -> None:
    result = scripted(project, [{"answer": GOOD, "crash": True}], "", "runs = 1")
    line = next(x for x in result.out.splitlines() if x.startswith("  1 run: crashed"))
    log = Path(line.split(": ")[-1])
    assert log.suffix == ".log" and "the bot crashed" in log.read_text()


@pytest.mark.design("judge.default_all_runs")
@pytest.mark.slow
def test_by_default_one_failed_run_fails_the_row(project: Project) -> None:
    result = scripted(project, [{"answer": GOOD}] * 4 + [{"answer": BAD}], "", "runs = 5")
    assert result.code == 1 and table_row(result, "baseline")[-1] == "fail"


@pytest.mark.design("judge.fault_pass_rate_wins")
@pytest.mark.slow
def test_a_faults_pass_rate_wins_over_the_tests_for_its_row(project: Project) -> None:
    result = scripted(
        project,
        [
            {"answer": GOOD},
            {"answer": GOOD},
            *[{"answer": GOOD, "fired": True}, {"answer": BAD, "fired": True}] * 2,
        ],
        f"{ALWAYS}pass_rate = 0.5\n{ALWAYS}",
        "runs = 2",
        'expect = ["shipped", ["Oct 30", "October 30"]]\npass_rate = 1\n',
    )
    assert table_row(result, "fault 1")[-1] == "pass"
    assert table_row(result, "fault 2")[-1] == "fail"
    assert result.code == 1


@pytest.mark.design("judge.fault_rate_inherits_tests")
@pytest.mark.slow
def test_a_tests_pass_rate_applies_to_a_fault_that_gives_none(project: Project) -> None:
    result = scripted(
        project,
        [
            {"answer": GOOD},
            {"answer": GOOD},
            {"answer": GOOD, "fired": True},
            {"answer": BAD, "fired": True},
        ],
        ALWAYS,
        "runs = 2",
        'expect = ["shipped", ["Oct 30", "October 30"]]\npass_rate = 0.5\n',
    )
    assert table_row(result, "fault 1")[-1] == "pass" and result.code == 0


@pytest.mark.design("judge.not_fired_in_no_rate")
@pytest.mark.slow
def test_a_run_where_the_fault_did_not_fire_counts_in_no_rate(project: Project) -> None:
    result = scripted(
        project,
        [{"answer": GOOD}] * 5 + [{"answer": GOOD, "fired": True}] * 4 + [{"answer": BAD}],
        f"{ALWAYS}pass_rate = 0.8\n",
        "runs = 5",
    )
    assert result.code == 0, result.out
    cells = table_row(result, "fault 1")
    assert "4/4" in cells and "100%" in cells and cells[-1] == "pass"
    assert "not exercised, the fault did not fire: 1 answered" in result.out
    judged = [s["judged"] for s in result.summaries()]
    assert judged.count("left out") == 1 and "fail" not in judged
    assert [s["left_out"] for s in result.summaries() if s["left_out"]] == [judge.NOT_FIRED]


@pytest.mark.design("judge.too_few_runs")
@pytest.mark.slow
def test_a_row_under_the_floor_has_no_result(project: Project) -> None:
    result = scripted(
        project,
        [{"answer": GOOD}] * 3 + [{"answer": GOOD, "fired": True}] + [{"answer": GOOD}] * 2,
        ALWAYS,
        "runs = 3",
    )
    assert result.code == 2, result.out
    cells = table_row(result, "fault 1")
    assert "1/1" in cells and cells[-1] == "no result"
    assert "1 of 3 runs were judged, and a result needs 2. Make the fault fire" in result.out


@pytest.mark.design("judge.unfired_run_does_not_block")
@pytest.mark.slow
def test_one_unfired_run_does_not_block_a_pass_and_gets_a_note(project: Project) -> None:
    result = scripted(
        project,
        [{"answer": GOOD}] * 5 + [{"answer": GOOD, "fired": True}] * 4 + [{"answer": GOOD}],
        ALWAYS,
        "runs = 5",
    )
    assert result.code == 0, result.out
    assert table_row(result, "fault 1")[-1] == "pass"
    assert "note: order fault 1: 4 of 5 runs judged" in result.out


@pytest.mark.design("judge.no_judged_runs")
@pytest.mark.slow
def test_a_row_with_no_judged_runs_is_never_a_pass(project: Project) -> None:
    result = scripted(project, [{"answer": GOOD}] * 4, ALWAYS, "runs = 2")
    assert result.code == 2, result.out
    cells = table_row(result, "fault 1")
    assert "0/0" in cells and cells[-1] == "no result"
    assert cells[7:9] == ["-", "-"]  # the rate and the range
    assert "the fault never fired in any run" in result.out
    assert "order fault 1: no result: the fault never fired" in result.out
    assert "Make the fault fire" not in result.out


@pytest.mark.design("report.no_advice_when_nothing_was_unfired")
@pytest.mark.slow
def test_a_row_cut_short_gets_no_advice_to_fire_its_fault(project: Project) -> None:
    result = scripted(project, [{"answer": GOOD, "crash": True}], ALWAYS, "runs = 1")
    assert "order fault 1: no result: 0 of 1 runs were judged" in result.out
    assert "Make the fault fire" not in result.out


@pytest.mark.design("judge.crash_is_a_result")
@pytest.mark.slow
def test_a_crash_on_a_fault_row_fails_it(project: Project) -> None:
    result = scripted(
        project,
        [{"answer": GOOD}, {"answer": GOOD, "fired": True, "crash": True}],
        ALWAYS,
        "runs = 1",
    )
    assert result.code == 1, result.out
    assert table_row(result, "fault 1")[-1] == "fail"
    assert "1 run: crashed" in result.out
    assert "order fault 1: 1 crashed" in result.out
    assert sorted(str(s["judged"]) for s in result.summaries()) == ["fail", "pass"]


@pytest.mark.design("judge.timeout_is_a_result")
@pytest.mark.slow
def test_a_run_past_its_time_limit_fails_its_row(project: Project, time_limit: TimeLimit) -> None:
    time_limit.at((project.root / "hanging").exists)
    result = scripted(
        project,
        [{"answer": GOOD}, {"answer": GOOD, "fired": True, "hang": True}],
        ALWAYS,
        "runs = 1",
    )
    assert result.code == 1, result.out
    assert "1 run: timed out" in result.out


@pytest.mark.design("judge.refused_stops_row")
@pytest.mark.slow
def test_a_refused_run_leaves_its_row_with_no_pass(project: Project) -> None:
    result = scripted(
        project,
        [{"answer": GOOD}, {"answer": GOOD}, {"answer": GOOD, "refund": True}, {"answer": GOOD}],
        "",
        "runs = 4",
        'expect = ["shipped", ["Oct 30", "October 30"]]\npass_rate = 0.5\n',
    )
    assert result.code == 2, result.out
    cells = table_row(result, "baseline")
    assert "2/2" in cells and cells[-1] == "no result"
    assert "made a call the test does not allow" in result.out
    assert [s["judged"] for s in result.summaries()].count("left out") == 1
    assert "no result: 2 of" not in result.out  # the refusal says it already


@pytest.mark.design("judge.refused_on_a_fault_row_fails")
@pytest.mark.slow
def test_a_refusal_on_a_fault_row_fails_at_any_rate_and_keeps_exit_2(project: Project) -> None:
    result = scripted(
        project,
        [
            {"answer": GOOD},
            {"answer": GOOD},
            {"answer": GOOD, "fired": True},
            {"answer": GOOD, "fired": True, "refund": True},
        ],
        f"{ALWAYS}pass_rate = 0.5\n",
        "runs = 2",
    )
    assert result.code == 2, result.out
    cells = table_row(result, "fault 1")
    assert cells[-1] == "fail" and "1/2" in cells
    assert "under this fault the agent called tool:refund_order" in result.out
    assert "Mocking it would hide this" in result.out
    assert "1 run: refused tool:refund_order" in result.out
    assert "Mock it in the test" not in result.out
    assert "made a call the test does not allow" in result.out
    kinds = [b["kind"] for s in result.summaries() for b in s["broken_rules"]]  # type: ignore[attr-defined]
    assert kinds == ["refused"]


@pytest.mark.slow
def test_a_refusal_the_agent_chose_on_a_fault_row_leaves_the_rows_other_runs_running(
    project: Project,
) -> None:
    """An attack the agent obeys in 3 of 4 runs reads as 1/4, not as the first refusal alone.
    Each refused call is still refused, the row still fails and the exit stays 2."""
    fault_runs = [
        {"answer": GOOD, "fired": True, "refund": True},
        {"answer": GOOD, "fired": True},
        {"answer": GOOD, "fired": True, "refund": True},
        {"answer": GOOD, "fired": True, "refund": True},
    ]
    result = scripted(project, [{"answer": GOOD}] * 4 + fault_runs, ALWAYS, "runs = 4")
    assert result.code == 2, result.out
    cells = table_row(result, "fault 1")
    assert cells[-1] == "fail" and cells[3] == "4" and "1/4" in cells
    assert "3 runs: refused tool:refund_order" in result.out
    fault = [run[-1]["summary"] for run in result.records() if run[-1]["row"] == "fault 1"]
    assert sorted(str(s["outcome"]) for s in fault) == ["answered"] + ["refused call"] * 3  # type: ignore[index]
    refunds = [x for run in result.records() for x in run if x.get("call") == "tool:refund_order"]
    assert [x["action"] for x in refunds] == ["refused"] * 3  # none ran


@pytest.mark.slow
def test_a_refusal_on_the_baseline_still_ends_its_row(project: Project) -> None:
    steps = [{"answer": GOOD, "refund": True}, {"answer": GOOD, "refund": True}]
    result = scripted(project, steps, ALWAYS, "runs = 2")
    assert result.code == 2, result.out
    assert [run[-1]["row"] for run in result.records()] == ["baseline"]  # its row stopped


@pytest.mark.slow
def test_a_setup_refusal_on_a_fault_row_still_ends_its_row(project: Project) -> None:
    """A child started with no gate is refused as blind_child, a reason the agent can choose,
    yet the test's setup caused it and every run would meet it again, so its row stops."""
    steps = [{"answer": GOOD}] * 3 + [{"answer": GOOD, "fired": True, "blind": True}] * 3
    result = scripted(project, steps, ALWAYS, "runs = 3")
    assert result.code == 2, result.out
    rows = [run[-1]["row"] for run in result.records()]
    assert rows.count("baseline") == 3 and rows.count("fault 1") == 1, result.out  # one started
    blind = [x for run in result.records() for x in run if x.get("reason") == "blind_child"]
    assert [x["by_config"] for x in blind] == [True]


@pytest.mark.parametrize(
    ("step", "shown"),
    [("post", "http:payments.example.invalid"), ("blind", "process:python")],
    ids=["a web write", "a child with no gate"],
)
@pytest.mark.slow
def test_every_call_the_agent_chose_fails_a_fault_row(
    project: Project, step: str, shown: str
) -> None:
    result = scripted(
        project,
        [{"answer": GOOD}, {"answer": GOOD, "fired": True, step: True}],
        ALWAYS,
        "runs = 1",
    )
    assert result.code == 2, result.out
    assert table_row(result, "fault 1")[-1] == "fail"
    assert f"1 run: refused {shown}" in result.out
    assert ("Mocking it would hide this" in result.out) == (step == "post")
    assert ("{**os.environ" in result.out) == (step == "blind")


@pytest.mark.design("judge.refused_unfired_left_out")
@pytest.mark.slow
def test_a_refusal_before_the_fault_fired_stays_left_out(project: Project) -> None:
    result = scripted(
        project,
        [{"answer": GOOD}, {"answer": GOOD, "refund": True}],
        ALWAYS,
        "runs = 1",
    )
    assert result.code == 2, result.out
    assert table_row(result, "fault 1")[-1] == "no result"
    assert "Mocking it would hide this" not in result.out


@pytest.mark.design("judge.exit_mistake_over_failure")
@pytest.mark.slow
def test_a_test_mistake_keeps_exit_2_and_the_table_still_shows_the_failure(
    project: Project,
) -> None:
    result = scripted(project, [{"answer": BAD}, {"answer": BAD}], ALWAYS, "runs = 1")
    assert result.code == 2, result.out
    assert table_row(result, "baseline")[-1] == "fail"
    assert "the fault never fired in any run" in result.out
    assert "fail: order baseline: 0/1 passed" in result.out


@pytest.mark.design("judge.exit_infra_over_failure")
@pytest.mark.slow
def test_an_infrastructure_error_keeps_exit_3_and_is_not_held_against_the_agent(
    project: Project, fake_openai: Fake
) -> None:
    project.monkeypatch.setenv("OPENAI_BASE_URL", fake_openai(["500:error_500"]).base_url)
    project.monkeypatch.setenv("OPENAI_API_KEY", "test")
    result = scripted(
        project,
        [{"answer": BAD}, {"answer": BAD}, {"answer": GOOD, "model": True}],
        "",
        'runs = 3\nmodel_hosts = [{host = "127.0.0.1", provider = "openai"}]',
    )
    assert result.code == 3, result.out
    cells = table_row(result, "baseline")
    assert cells[-1] == "fail" and "0/2" in cells
    summaries = result.summaries()
    assert sorted(str(s["judged"]) for s in summaries) == ["fail", "fail", "left out"]


@pytest.mark.design("judge.budget_left_out")
@pytest.mark.slow
def test_a_budget_stopped_run_is_left_out(project: Project) -> None:
    project.edit("[agent]\n", "[agent]\nmax_cost_usd = 0.0000001\n")
    fake = FakeOpenAI(ACME)  # the budget stops the run before any request
    fake.start()
    try:
        result = project.test(fake)
    finally:
        fake.stop()
    assert result.code == 2, result.out
    assert result.summaries() and {s["judged"] for s in result.summaries()} == {"left out"}
    assert table_row_any(result, "order-status  baseline").endswith("no result")
    assert "order-status  fault 1" in result.out  # a row that never ran is still shown


def table_row_any(result: Result, start: str) -> str:
    return next(x for x in result.out.splitlines() if x.startswith(start))


@pytest.mark.design("judge.baseline_with_bad_answers_still_runs_faults")
@pytest.mark.slow
def test_a_baseline_that_answered_badly_still_runs_its_fault_rows(project: Project) -> None:
    result = scripted(
        project, [{"answer": BAD}, {"answer": "unable", "fired": True}], ALWAYS, "runs = 1"
    )
    assert len(result.summaries()) == 2
    assert result.code == 1, result.out


@pytest.mark.design("judge.failed_baseline_skips")
@pytest.mark.slow
def test_a_crashed_baseline_reads_fail_and_its_fault_rows_read_baseline_failed(
    project: Project,
) -> None:
    result = scripted(project, [{"answer": GOOD, "crash": True}], ALWAYS, "runs = 1")
    assert result.code == 2, result.out
    assert table_row(result, "baseline")[-1] == "fail"
    assert table_row(result, "fault 1")[-1] == "baseline failed"
    assert "baseline failed" in result.out


@pytest.mark.design("judge.empty_answer_end_to_end")
@pytest.mark.slow
def test_an_agent_that_returns_nothing_fails_by_name(project: Project) -> None:
    result = scripted(project, [{"answer": ""}], "", "runs = 1")
    assert result.code == 1, result.out
    assert "1 run: empty answer" in result.out


def script_project(project: Project, source: str, body: str) -> Result:
    (project.root / "say.py").write_text(source)
    project.toml.write_text(
        '[agent]\ncommand = "python say.py {question}"\nruns = 1\nmax_cost_usd = "none"\n'
        f'\n[[test]]\nname = "order"\nquestion = "Where?"\n{body}'
    )
    return project.test()


@pytest.mark.design("judge.script_last_line")
@pytest.mark.slow
def test_only_a_scripts_last_line_is_judged(project: Project) -> None:
    result = script_project(project, 'print("shipped")\nprint("delayed")\n', 'expect = ["shipped"]')
    assert result.code == 1, result.out
    assert 'expect "shipped" not found' in result.out


@pytest.mark.design("judge.script_answer_file")
@pytest.mark.slow
def test_a_scripts_answer_file_is_judged_whole(project: Project) -> None:
    source = (
        "import os\n"
        'open(os.environ["SCRIMMAGE_ANSWER_FILE"], "w").write("line one shipped\\nline two")\n'
    )
    result = script_project(project, source, 'expect = ["shipped", "line two"]')
    assert result.code == 0, result.out


@pytest.mark.design("judge.script_no_answer")
@pytest.mark.slow
def test_a_script_that_prints_nothing_fails_as_crashed(project: Project) -> None:
    result = script_project(project, "pass\n", 'expect = ["shipped"]')
    assert result.code == 1, result.out
    assert "1 run: crashed" in result.out


def returns(project: Project, value: str, extra: str = "", body: str = "") -> Result:
    """scrimmage test on an agent that returns a value, with one row and one run."""
    (project.root / "shaped.py").write_text(f"def answer(question):\n    return {value}\n")
    project.toml.write_text(
        '[agent]\nfunction = "shaped:answer"\nruns = 1\nmax_cost_usd = "none"\n'
        f'{extra}\n[[test]]\nname = "order"\nquestion = "Where?"\n{body}'
    )
    return project.test()


STATE = {
    "messages": [
        {"content": "shipped, Oct 30"},
        {"content": "Sorry, I am unable to look up order 1234 right now."},
    ]
}


@pytest.mark.design("judge.not_text_is_a_mistake")
@pytest.mark.parametrize("value", [repr(STATE), "['shipped, Oct 30']", "12", "None", "True"])
@pytest.mark.slow
def test_a_value_that_is_not_text_needs_an_answer_path(project: Project, value: str) -> None:
    result = returns(project, value, body='expect = ["shipped", ["Oct 30", "October 30"]]\n')
    assert result.code == 2, result.out
    assert "not text, so the whole value would be judged" in result.out
    assert "Set answer to the path of the final reply" in result.out
    cells = table_row(result, "baseline")
    assert cells[-1] == "no result" and "0/0" in cells
    summary = result.summaries()[0]
    assert (summary["judged"], summary["left_out"]) == ("left out", judge.TEST_MISTAKE)


@pytest.mark.design("judge.answer_path_is_what_is_judged")
@pytest.mark.slow
def test_with_an_answer_path_the_final_reply_is_what_is_judged(project: Project) -> None:
    result = returns(
        project,
        repr(STATE),
        'answer = "messages[-1].content"',
        'expect = ["shipped", ["Oct 30", "October 30"]]\n',
    )
    assert result.code == 1, result.out
    assert 'expect "shipped" not found' in result.out


@pytest.mark.design("judge.answer_path_is_what_is_judged")
@pytest.mark.parametrize(
    ("value", "path"),
    [
        ('{"messages": [{"content": "tool text: shipped, Oct 30"}]}', "messages"),
        ('{"state": {"reply": "shipped, Oct 30"}}', "state"),
    ],
    ids=["a list", "a table"],
)
@pytest.mark.slow
def test_an_answer_path_must_lead_to_the_answer_not_to_all_of_it(
    project: Project, value: str, path: str
) -> None:
    result = returns(
        project, value, f'answer = "{path}"', 'expect = ["shipped", ["Oct 30", "October 30"]]\n'
    )
    assert result.code == 2, result.out + result.err
    assert "not the answer" in result.out + result.err


@pytest.mark.slow
def test_an_answer_path_may_lead_to_a_number(project: Project) -> None:
    result = returns(project, '{"count": 12}', 'answer = "count"', 'expect = ["12"]\n')
    assert result.code == 0, result.out + result.err


@pytest.mark.design("record.judged_fields")
@pytest.mark.slow
def test_every_summary_says_how_the_run_was_judged(project: Project, fake_openai: Fake) -> None:
    result = project.test(fake_openai(ACME))
    validator = jsonschema.Draft202012Validator(RECORD, format_checker=jsonschema.FormatChecker())
    for lines in result.records():
        validator.validate(lines[-1])
    summaries = result.summaries()
    assert {s["judged"] for s in summaries} == {"pass"}
    assert all(s["broken_rules"] == [] and s["left_out"] is None for s in summaries)


@pytest.mark.design("record.judged_required")
@pytest.mark.slow
def test_a_summary_without_a_judgement_does_not_fit_the_schema(
    project: Project, fake_openai: Fake
) -> None:
    line = project.test(fake_openai(ACME)).records()[0][-1]
    summary = cast(dict[str, object], line["summary"])
    del summary["judged"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(RECORD).validate({**line, "summary": summary})


@pytest.mark.design("table.example")
@pytest.mark.slow
def test_the_table_shows_rate_range_tools_cost_and_result(
    project: Project, fake_openai: Fake
) -> None:
    project.edit("[agent]\n", "[agent]\nruns = 2\n")
    result = project.test(fake_openai(ACME))
    assert result.code == 0, result.out
    lines = result.out.splitlines()
    head = next(i for i, line in enumerate(lines) if line.startswith("test "))
    assert lines[head].split() == [
        "test",
        "row",
        "fault",
        "runs",
        "fired",
        "done",
        "passed",
        "rate",
        "range",
        "tools",
        "cost",
        "result",
    ]
    base = lines[head + 1].split()
    assert base[:3] == ["order-status", "baseline", "-"]
    assert base[3:10] == ["2", "-", "2", "2/2", "100%", "34-100%", "2.0"]
    assert base[-1] == "pass"
    assert "range: a 95% Wilson interval, from these runs alone" in lines
    costs = [float(line.split()[-2]) for line in lines[head + 1 : head + 3]]
    spent = sum(float(str(s["cost_usd"])) for s in result.summaries())
    assert costs[0] > 0 and sum(costs) == pytest.approx(spent, abs=0.00002)  # the whole row's


def fabricated(cfg: config.Config, row: plan.Row, judged: RunJudgement) -> RunResult:
    fired = None if row.fault is None else judged.left_out is None
    outcome = "answered" if judged.left_out is None else "refused call"
    mocked = set(range(len(cfg.tests[0].mocks)))
    result = RunResult(
        "run-1", row, outcome, "x", fired, {}, model_calls=1, mocked=mocked, tool_calls=2
    )
    result.judgement = judged
    return result


@pytest.mark.design("table.fits_120")
def test_the_table_stays_within_120_columns_and_notes_keep_full_names(
    project: Project, tmp_path: Path
) -> None:
    name = "a-very-long-test-name-" * 3
    project.edit('name     = "order-status"', f'name = "{name}"')
    project.edit('call = "tool:get_order"', 'call = "http:inventory.warehouse.acme.internal"')
    project.edit('do   = "timeout"', 'do = "status 503"')
    cfg = load(project)
    results = [fabricated(cfg, row, FAILED_RUN) for row in plan.rows(cfg) for _ in range(3)]
    out = Capture()
    code = report(cfg, results, tmp_path, out)  # type: ignore[arg-type]
    assert code == 1
    table = [line for line in out.text.splitlines() if " baseline " in line or " fault 1 " in line]
    assert table and all(len(line) <= 120 for line in table)
    assert any("..." in line for line in table)
    assert f"fail: {name} baseline: 0/3 passed" in out.text
    assert f"full name: {name} fault 1: http:inventory.warehouse.acme.internal" in out.text


@pytest.mark.design("table.shows_when")
def test_the_fault_cell_shows_a_schedule(project: Project, tmp_path: Path) -> None:
    project.edit('do   = "timeout"', 'do   = "timeout"\nwhen = "first 1"')
    out = Capture()
    report(load(project), [], tmp_path, out)  # type: ignore[arg-type]
    row = next(line for line in out.text.splitlines() if line.startswith("order-status  fault 1"))
    assert "..." in row and "first 1" in row
    assert "full name: order-status fault 1: tool:get_order timeout first 1" in out.text
    project.edit('name     = "order-status"', 'name = "o"')
    out = Capture()
    report(load(project), [], tmp_path, out)  # type: ignore[arg-type]
    assert "tool:get_order timeout first 1" in out.text and "full name" not in out.text


def test_a_short_test_name_leaves_its_room_to_the_fault(project: Project, tmp_path: Path) -> None:
    project.edit('call = "tool:get_order"', 'call = "http:stock.acme.internal"')
    project.edit('do   = "timeout"', 'do = "status 503"')
    project.edit('name     = "order-status"', 'name = "a"')
    cfg = load(project)
    out = Capture()
    report(cfg, [], tmp_path, out)  # type: ignore[arg-type]
    assert "http:stock.acme.internal status 503" in out.text


class Capture:
    """A text stream, kept as one string."""

    def __init__(self) -> None:
        self.text = ""

    def write(self, text: str) -> int:
        self.text += text
        return len(text)

    def flush(self) -> None:
        pass


@pytest.mark.design("exit.zero_only_when_every_row_passes")
@pytest.mark.parametrize("first", [PASSED, FAILED_RUN, OUT])
@pytest.mark.parametrize("second", [PASSED, FAILED_RUN, OUT])
def test_exit_zero_only_when_every_row_passes(
    project: Project, tmp_path: Path, first: RunJudgement, second: RunJudgement
) -> None:
    cfg = load(project)
    base, fault = plan.rows(cfg)
    results = [fabricated(cfg, base, first) for _ in range(3)]
    results += [fabricated(cfg, fault, second) for _ in range(3)]
    code = report(cfg, results, tmp_path, Capture())  # type: ignore[arg-type]
    if first == second == PASSED:
        assert code == 0
    elif OUT in (first, second):
        assert code == 2
    else:
        assert code == 1


@pytest.mark.parametrize("baseline", [FAILED_RUN, OUT])
def test_a_fault_row_beside_a_failing_baseline_shows_baseline_failed(
    project: Project, tmp_path: Path, baseline: RunJudgement
) -> None:
    """A guard that blocks every question fails the baseline and passes every attack row: a
    rate there means nothing, so none is shown. The exit code stays the baseline's."""
    cfg = load(project)
    base, fault = plan.rows(cfg)
    results = [fabricated(cfg, base, baseline) for _ in range(3)]
    results += [fabricated(cfg, fault, PASSED) for _ in range(3)]
    out = Capture()
    code = report(cfg, results, tmp_path, out)  # type: ignore[arg-type]
    assert code == (1 if baseline == FAILED_RUN else 2)
    row = next(x for x in out.text.splitlines() if x.startswith("order-status  fault 1"))
    cells = re.split(r" {2,}", row)
    assert cells[-1] == "baseline failed"
    assert cells[6:9] == ["3/3", "-", "-"]  # the counts, then no rate and no range
    # A passing baseline leaves the fault row's rate as it was.
    results = [fabricated(cfg, base, PASSED) for _ in range(3)] + results[3:]
    out = Capture()
    assert report(cfg, results, tmp_path, out) == 0  # type: ignore[arg-type]
    assert "baseline failed" not in out.text
