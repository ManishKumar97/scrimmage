"""A fault that names a model, through the real openai library, against the fake server."""

from collections.abc import Callable

import pytest

from conftest import Project, Result
from fakes.openai_server import FakeOpenAI

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
    assert f"no call used the model {name!r}; these calls used {MAIN}, {ROUTER}" in result.out
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
    assert "has no model name Derail can read" in result.out
    summary = summary_of(result)
    assert (summary["outcome"], summary["judged"]) == ("refused call", "left out")
    row = next(line for line in result.out.splitlines() if " fault 1 " in line)
    assert row.endswith("no result")
