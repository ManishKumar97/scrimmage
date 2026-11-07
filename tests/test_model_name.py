"""A fault that names a model, through the real openai library, against the fake server."""

import json
from collections.abc import Callable

import pytest

from conftest import Project, Result
from fakes.openai_server import FakeOpenAI
from scrimmage_core import config, labels
from scrimmage_core.plan import Row
from scrimmage_core.runner import RunResult, unused_model

Fake = Callable[..., FakeOpenAI]
ROUTER, MAIN = "gpt-4o-mini", "gpt-4.1"
ROUTER_EMBEDDING, MAIN_EMBEDDING = "text-embedding-3-small", "text-embedding-3-large"
CHAT, STREAM = {"replies": ["chat_answer"]}, {"replies": ["chat_stream"]}
RESPONSES = {"replies": [], "response": "response_text"}
RESPONSES_STREAM = {"replies": [], "response": "response_stream"}
EMBEDDINGS = {"replies": [], "embedding": "embedding"}
SHAPES = [
    ("chat", CHAT, "Hello"),
    ("chat async", CHAT, "Hello"),
    ("chat stream", STREAM, "Hello"),
    ("chat async stream", STREAM, "Hello"),
    ("responses", RESPONSES, "grove"),
    ("responses async", RESPONSES, "grove"),
    ("responses stream", RESPONSES_STREAM, "Hi"),
    ("responses async stream", RESPONSES_STREAM, "Hi"),
    ("embeddings", EMBEDDINGS, "numbers"),
    ("embeddings async", EMBEDDINGS, "numbers"),
]


def bot(project: Project, shape: str, word: str = "Hello", *, script: bool = False) -> None:
    """One run per row of the routed bot, called in this shape, with no faults yet."""
    start = 'command = "python route.py {question}"' if script else None
    if script:
        (project.root / "route.py").write_text(
            "import sys\nfrom acme.routed import answer\nprint(answer(sys.argv[1]))\n"
        )
    project.toml.write_text(
        f"""[agent]
{start or 'function = "acme.routed:answer"'}
runs = 1
model_hosts = [{{host = "127.0.0.1", provider = "openai"}}]

[[test]]
name = "routed"
question = "{shape}: Say hello"
expect = ["{word}"]
"""
    )


def add_fault(project: Project, model: str, when: str, *, call: str = "model") -> None:
    with project.toml.open("a") as toml:
        toml.write(
            f'[[test.fault]]\ncall = "{call}"\nmodel = "{model}"\ndo = "status 429"\n'
            f'when = "{when}"\nexpect = ["e"]\nforbid = ["never said"]\n'
        )


def model_lines(result: Result, row: str) -> list[dict[str, object]]:
    lines = next(lines for lines in result.records() if lines[-1]["row"] == row)
    return [line for line in lines if line.get("kind") == "model"]


def seen(result: Result, row: str = "fault 1") -> list[tuple[object, object, object]]:
    return [(x["model"], x["action"], x["retry"]) for x in model_lines(result, row)]


def summary_of(result: Result, row: str = "fault 1") -> dict[str, object]:
    lines = next(lines for lines in result.records() if lines[-1]["row"] == row)
    summary = lines[-1]["summary"]
    assert isinstance(summary, dict)
    return summary


def answer(result: Result, row: str = "fault 1") -> object:
    return summary_of(result, row)["answer"]


@pytest.mark.design("model_name.only_that_model")
@pytest.mark.parametrize("when", ["first 3", "requests 1"])
@pytest.mark.parametrize(("shape", "fixtures", "word"), SHAPES, ids=[s[0] for s in SHAPES])
def test_a_fault_fires_on_the_named_model_alone_and_the_library_raises_its_own_error(
    project: Project,
    fake_openai: Fake,
    shape: str,
    fixtures: dict[str, object],
    word: str,
    when: str,
) -> None:
    embeddings = shape.startswith("embeddings")
    router, main = (ROUTER_EMBEDDING, MAIN_EMBEDDING) if embeddings else (ROUTER, MAIN)
    bot(project, shape, word)
    add_fault(project, main, when, call="model:*/embeddings" if embeddings else "model")
    fake = fake_openai(**fixtures)
    result = project.test(fake)
    assert result.finished, result.out + result.err
    assert seen(result) == [
        (router, "real", 0),  # the router runs for real
        (main, "fault", 0),
        (main, "fault", 1),  # the library's own retries meet the gate and still count right
        (main, "fault", 2),
    ]
    assert answer(result) == "error: RateLimitError"
    # The baseline's two calls and the fault row's router; a fault sends nothing.
    assert [r["model"] for r in fake.requests] == [router, main, router]


@pytest.mark.design("model_name.counts_only_matching")
@pytest.mark.parametrize("when", ["first 2", "requests 2"])
def test_a_router_between_the_main_calls_takes_none_of_the_count(
    project: Project, fake_openai: Fake, when: str
) -> None:
    bot(project, "loop")
    add_fault(project, MAIN, when)
    result = project.test(fake_openai(["chat_answer"]))
    assert result.finished, result.out + result.err
    assert seen(result) == [
        (ROUTER, "real", 0),
        (MAIN, "fault", 0),
        (ROUTER, "real", 0),
        (MAIN, "fault", 0),
        (ROUTER, "real", 0),
        (MAIN, "real", 0),
    ]
    numbers = [x["fault"] for x in model_lines(result, "fault 1") if x["action"] == "fault"]
    assert [n["n"] for n in numbers] == [1, 2]  # type: ignore[index]
    assert str(answer(result)).endswith("Hello! How can I assist you today?")


@pytest.mark.design("model_name.first_n_recovers")
def test_a_fault_on_the_main_model_ends_and_the_router_spent_none_of_it(
    project: Project, fake_openai: Fake
) -> None:
    bot(project, "chat")
    add_fault(project, MAIN, "first 1")
    result = project.test(fake_openai(["chat_answer"]))
    assert seen(result) == [(ROUTER, "real", 0), (MAIN, "fault", 0), (MAIN, "real", 1)]


@pytest.mark.design("model_name.parallel")
def test_the_router_running_at_once_keeps_the_mains_retries_counted_right(
    project: Project, fake_openai: Fake
) -> None:
    bot(project, "parallel")
    add_fault(project, MAIN, "requests 1")
    result = project.test(fake_openai(["chat_answer"]))
    assert result.finished, result.out + result.err
    calls = seen(result)
    assert [x for x in calls if x[0] == ROUTER] == [(ROUTER, "real", 0)]
    assert [x for x in calls if x[0] == MAIN] == [(MAIN, "fault", n) for n in range(3)]
    assert answer(result) == "Hello! How can I assist you today? | error: RateLimitError"


@pytest.mark.design("model_name.plain_fault_unchanged")
def test_a_fault_with_no_model_still_hits_every_model_call(
    project: Project, fake_openai: Fake
) -> None:
    bot(project, "loop")
    with project.toml.open("a") as toml:
        toml.write('[[test.fault]]\ncall = "model"\ndo = "status 429"\nwhen = "first 1"\n')
    result = project.test(fake_openai(["chat_answer"]))
    assert seen(result)[0] == (ROUTER, "fault", 0)


@pytest.mark.design("model_name.table_tells_rows_apart")
def test_two_rows_that_differ_by_model_read_apart_in_the_table(
    project: Project, fake_openai: Fake
) -> None:
    bot(project, "chat")
    add_fault(project, MAIN, "first 1")
    add_fault(project, ROUTER, "first 1")
    result = project.test(fake_openai(["chat_answer"]))
    assert f"model {MAIN} status 429 first 1" in result.out
    assert f"model {ROUTER} status 429 first 1" in result.out
    assert seen(result, "fault 1")[1] == (MAIN, "fault", 0)
    assert seen(result, "fault 2")[0] == (ROUTER, "fault", 0)
    # The label stays, and the line's own model field says which model it was.
    assert {x["call"] for x in model_lines(result, "fault 1")} == {"model:openai"}


@pytest.mark.design("model_name.exact")
@pytest.mark.parametrize(
    "name", ["gpt-4", "gpt-4.2", "GPT-4.1", "gpt-4o", "gpt-4.1-2025-04-14"], ids=str
)
def test_a_name_that_is_not_exact_never_fires_and_is_no_pass(
    project: Project, fake_openai: Fake, name: str
) -> None:
    bot(project, "chat")
    add_fault(project, name, "first 3")
    result = project.test(fake_openai(["chat_answer"]))
    assert seen(result) == [(ROUTER, "real", 0), (MAIN, "real", 0)]
    assert result.code == 2
    assert "routed fault 1: no result: the fault never fired" in result.out
    assert f"no call used the model {name!r}; these calls used {MAIN!r}, {ROUTER!r}" in result.out
    summary = summary_of(result)
    assert summary["judged"] == "left out"
    assert summary["left_out"] == "the fault did not fire"


@pytest.mark.design("model_name.lists_only_its_label")
def test_the_names_listed_are_those_of_calls_the_label_names(
    project: Project, fake_openai: Fake
) -> None:
    bot(project, "embeddings", "numbers")
    add_fault(project, MAIN, "first 3")
    result = project.test(fake_openai([], embedding="embedding"))
    assert f"no call used the model {MAIN!r}; no such call was made" in result.out
    assert result.code == 2


@pytest.mark.design("model_name.script")
def test_a_script_gets_the_name_through_its_plan(project: Project, fake_openai: Fake) -> None:
    bot(project, "chat", script=True)
    add_fault(project, MAIN, "first 3")
    result = project.test(fake_openai(["chat_answer"]))
    assert result.finished, result.out + result.err
    assert [x[:2] for x in seen(result)] == [(ROUTER, "real")] + [(MAIN, "fault")] * 3


@pytest.mark.design("model_name.no_name_refused_end_to_end")
def test_a_request_with_no_model_name_is_refused_and_not_sent_and_is_no_pass(
    project: Project, fake_openai: Fake
) -> None:
    bot(project, "stored", "grove")
    project.edit("runs = 1", 'runs = 1\nmax_cost_usd = "none"')  # no name, so no price either
    add_fault(project, MAIN, "always", call="model:openai")
    fake = fake_openai(["chat_answer"], response="response_text")
    result = project.test(fake)
    assert result.code == 2
    assert [(x["action"], x.get("reason")) for x in model_lines(result, "fault 1")] == [
        ("real", None),
        ("refused", "unreadable_model"),
    ]
    assert fake.paths.count("/v1/responses") == 1  # the baseline's, none from the fault row
    assert "has no model name Scrimmage can read" in result.out
    summary = summary_of(result)
    assert (summary["outcome"], summary["judged"]) == ("refused call", "left out")
    row = next(line for line in result.out.splitlines() if " fault 1 " in line)
    assert row.endswith("no result")
    # The refused call's name is unknown, so nothing may say this model was not used.
    assert "no call used the model" not in result.out
    assert "yet the fault did not fire" not in result.out
    assert ": None" not in result.out


@pytest.mark.design("model_name.stored_prompt_with_a_name")
def test_a_stored_prompt_that_sends_its_model_can_be_faulted(
    project: Project, fake_openai: Fake
) -> None:
    bot(project, "stored named", "grove")
    add_fault(project, MAIN, "always", call="model:openai")
    result = project.test(fake_openai(["chat_answer"], response="response_text"))
    assert seen(result) == [(ROUTER, "real", 0), (MAIN, "fault", 0)]
    assert answer(result) == "error: RateLimitError"


@pytest.mark.design("model_name.used_in_a_child")
def test_a_call_in_a_child_is_not_reported_as_unused(project: Project, fake_openai: Fake) -> None:
    bot(project, "child")
    add_fault(project, MAIN, "first 3")
    result = project.test(fake_openai(["chat_answer"]))
    assert result.code == 2
    assert "no call used the model" not in result.out
    assert f"calls used the model {MAIN!r}, yet the fault did not fire" in result.out
    assert "unless [agent] children is true" in result.out


@pytest.mark.design("model_name.refused_run_says_nothing_false")
def test_a_run_refused_on_its_way_to_the_model_is_not_reported_as_unused(
    project: Project, fake_openai: Fake
) -> None:
    bot(project, "poison")
    add_fault(project, MAIN, "first 3")
    result = project.test(fake_openai(["chat_answer"]))
    assert result.code == 2
    # The run is stopped at its refusal, so the later call may or may not have its line.
    assert "no call used the model" not in result.out


FAULT = config.Fault("model", "status 429", "first 3", model=MAIN)
CHAT_LABEL = "model:openai"


def ran(outcome: str, *models: tuple[str, str]) -> RunResult:
    row = Row(config.Test("routed", "q", (("a",),), (), (FAULT,)), "fault 1", FAULT)
    return RunResult("run-1", row, outcome, None, False, {}, models=set(models))


@pytest.mark.design("model_name.poisoned_line_counts_as_used")
def test_a_refused_call_with_a_recorded_name_says_it_was_used() -> None:
    why = unused_model(FAULT, [ran("refused call", (CHAT_LABEL, MAIN))])
    assert why is not None and why.startswith(f"calls used the model {MAIN!r}, yet")


@pytest.mark.design("model_name.silent_when_the_records_cannot_say")
@pytest.mark.parametrize(
    "run",
    [ran("refused call", (CHAT_LABEL, ROUTER)), ran("answered", (CHAT_LABEL, ""))],
    ids=["refused run", "a call with no name"],
)
def test_nothing_is_said_when_the_records_cannot_say(run: RunResult) -> None:
    assert unused_model(FAULT, [run]) is None


@pytest.mark.design("model_name.silent_over_every_run_of_the_row")
def test_one_refused_run_of_three_is_enough_to_say_nothing() -> None:
    runs = [ran("answered", (CHAT_LABEL, ROUTER)), ran("refused call")]
    assert unused_model(FAULT, runs) is None


def send_raw(
    project: Project, fake_openai: Fake, names: list[str], *, word: str = "sent"
) -> Result:
    """Each name goes out as raw JSON, spelled as given, under a fault on another model."""
    bot(project, "raw", word)
    project.edit("runs = 1", 'runs = 1\nmax_cost_usd = "none"')
    (project.root / "names.json").write_text(json.dumps(names))
    add_fault(project, MAIN, "first 3")
    return project.test(fake_openai(["chat_answer"]))


@pytest.mark.design("model_name.message_cannot_forge_a_line")
def test_a_name_with_control_characters_cannot_forge_a_line(
    project: Project, fake_openai: Fake
) -> None:
    result = send_raw(project, fake_openai, ["gpt-4.1\\u001b[31mRED\\nFAKE: exit 0 PASS"])
    assert result.code == 2
    assert "\x1b" not in result.out
    assert not any(line.startswith("FAKE") for line in result.out.splitlines())
    assert "these calls used 'gpt-4.1\\x1b[31mRED\\nFAKE: exit 0 PASS'" in result.out


@pytest.mark.design("model_name.message_survives_a_surrogate")
def test_a_lone_surrogate_prints_and_the_other_mistakes_still_show(
    project: Project, fake_openai: Fake
) -> None:
    result = send_raw(project, fake_openai, ["bad\\ud800name"])
    assert result.code == 2  # a mistake in the test, not a row failure from a traceback
    assert "these calls used 'bad\\ud800name'" in result.out
    assert "the fault never fired in any run" in result.out


@pytest.mark.design("model_name.message_lists_a_few")
def test_a_long_list_of_names_is_cut_with_a_count(project: Project, fake_openai: Fake) -> None:
    result = send_raw(project, fake_openai, [f"model-{i:02}" for i in range(12)])
    line = next(x for x in result.out.splitlines() if "no call used the model" in x)
    assert line.endswith("'model-04' and 7 more")
    assert "model-05" not in line


@pytest.mark.design("model_name.message_cuts_a_long_name")
def test_a_very_long_name_is_cut(project: Project, fake_openai: Fake) -> None:
    result = send_raw(project, fake_openai, ["x" * 200_000])
    line = next(x for x in result.out.splitlines() if "no call used the model" in x)
    assert len(line) < 300 and "..." in line and line.endswith("xxx'")


@pytest.mark.design("model_name.full_name_covers_a_cut_cell")
def test_a_name_cut_from_the_table_is_on_its_full_name_line(
    project: Project, fake_openai: Fake
) -> None:
    bot(project, "chat")
    project.edit('name = "routed"', f'name = "{"routed-" * 16}"')
    alias = "main-model-alias-from-the-team-gateway-used-for-answers"
    add_fault(project, alias, "first 3", call="model:openai")
    result = project.test(fake_openai(["chat_answer"]))
    lines = result.out.splitlines()
    table = [line for line in lines if " baseline " in line or " fault 1 " in line]
    assert table and all(len(line) <= 120 for line in table)
    assert not any(alias in line for line in table)  # the middle cut takes the name
    full = next(line for line in lines if line.startswith("full name:") and "fault 1" in line)
    assert f"model:openai {alias} status 429 first 3" in full


ARN = (
    "arn:aws:bedrock:us-east-1:123456789012:inference-profile/"
    "us.acme.main-answer-model-2026-11-prod1"
)
ARN_V1, ARN_V2 = f"{ARN}-v1:0", f"{ARN}-v2:0"


@pytest.mark.design("model_name.ends_of_a_name_kept")
def test_two_long_names_that_differ_at_the_end_read_apart(
    project: Project, fake_openai: Fake
) -> None:
    assert len(ARN_V2) == 101
    bot(project, "raw", "sent")
    project.edit("runs = 1", 'runs = 1\nmax_cost_usd = "none"')
    (project.root / "names.json").write_text(json.dumps([ARN_V1]))
    add_fault(project, ARN_V2, "first 3")
    result = project.test(fake_openai(["chat_answer"]))
    assert f"no call used the model {ARN_V2!r}; these calls used {ARN_V1!r}" in result.out


@pytest.mark.design("model_name.cut_from_the_middle")
def test_a_name_past_the_cap_keeps_both_ends() -> None:
    one, two = "a" * 300 + "-v1:0", "a" * 300 + "-v2:0"
    shown = labels.shown_name(one)
    assert len(shown) == labels.NAME_CAP and "..." in shown
    assert shown.startswith("'aaa") and shown.endswith("-v1:0'")
    assert shown != labels.shown_name(two)


@pytest.mark.design("model_name.faults_own_name_whole")
def test_the_faults_own_name_is_never_cut(project: Project, fake_openai: Fake) -> None:
    long = "team-gateway/" + "main-answer-model-" * 14 + "v2"
    assert len(repr(long)) > labels.NAME_CAP
    bot(project, "chat")
    add_fault(project, long, "first 3")
    result = project.test(fake_openai(["chat_answer"]))
    assert f"no call used the model {long!r};" in result.out


@pytest.mark.design("model_name.empty_price_key_end_to_end")
def test_a_stored_prompt_runs_on_a_budget_when_the_empty_key_prices_it(
    project: Project, fake_openai: Fake
) -> None:
    bot(project, "stored", "grove")
    with project.toml.open("a") as toml:
        toml.write('\n[agent.prices]\n"" = {input = 2.0, output = 8.0}\n')
    result = project.test(fake_openai(["chat_answer"], response="response_text"))
    assert result.code == 0, result.out
    stored = model_lines(result, "baseline")[-1]
    assert (stored["model"], stored["action"]) == ("", "real")
