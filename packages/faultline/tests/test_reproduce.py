"""`faultline test`: the tasks file, the plan of runs and where each run is saved."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from upstream import openai_reply, upstream_server

from faultline.faults import parse
from faultline.reproduce import (
    Task,
    TasksError,
    command_for,
    load_tasks,
    parse_tasks,
    plan,
    safe_names,
)

FAULTLINE = str(Path(sys.executable).parent / "faultline")
# Asks the model its one argument and prints the reply's text.
ASKS = """
import json, os, sys, urllib.request
body = json.dumps({"model": "m", "messages": [{"role": "user", "content": sys.argv[1]}]})
request = urllib.request.Request(
    os.environ["OPENAI_BASE_URL"] + "/chat/completions", body.encode(),
    {"Content-Type": "application/json"})
with urllib.request.urlopen(request) as reply:
    print(json.load(reply)["choices"][0]["message"]["content"])
"""
TWO_TASKS = """
[[task]]
id = "cart"
input = "Why does the cart fail?"
expected = "cart"

[[task]]
id = "ads.v2"
input = "Why are ads slow?"
"""


def test_a_tasks_file_gives_each_task_with_its_optional_expected_answer():
    assert parse_tasks(TWO_TASKS) == [
        Task("cart", "Why does the cart fail?", "cart"),
        Task("ads.v2", "Why are ads slow?", None),
    ]


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("[[task]]\nid = 'a'\n", "task a: no input"),
        (
            "[[task]]\nid = 'a'\ninput = 'q'\n[[task]]\nid = 'a'\ninput = 'r'\n",
            "used twice",
        ),
        ("[[task]]\nid = '../x'\ninput = 'q'\n", "id '../x' must be"),
        ("[[task]]\nid = 'a'\ninput = 'q'\nanswer = 'x'\n", "unknown key answer"),
        ("[[task]]\nid = 'a'\ninput = 3\n", "input must be a string"),
        ("[tasks]\nid = 'a'\n", "unknown table tasks"),
        ("", "no tasks"),
        ("[[task]\n", "not valid TOML"),
    ],
)
def test_a_bad_tasks_file_is_an_error_that_names_the_problem(text, error):
    with pytest.raises(TasksError, match=error.replace(".", r"\.")):
        parse_tasks(text)


def test_expected_answers_from_another_file_win(tmp_path):
    tasks = tmp_path / "tasks.toml"
    tasks.write_text(TWO_TASKS)
    expected = tmp_path / "expected.toml"
    expected.write_text("[[task]]\nid = 'ads.v2'\nexpected = 'ad'\n")

    loaded = load_tasks(tasks, expected)

    assert [task.expected for task in loaded] == ["cart", "ad"]
    expected.write_text("[[task]]\nid = 'nope'\nexpected = 'x'\n")
    with pytest.raises(TasksError, match="no task nope"):
        load_tasks(tasks, expected)


def test_the_input_replaces_its_placeholder_or_is_appended():
    assert command_for(["agent", "--q={input}", "-v"], "why?") == [
        "agent",
        "--q=why?",
        "-v",
    ]
    assert command_for(["agent", "-v"], "why?") == ["agent", "-v", "why?"]


def test_fault_specs_become_distinct_directory_names():
    names = safe_names(
        ["tool:get_logs:empty@2", "tool:a:wrong=x/y", "tool:a:wrong=x_y"]
    )

    assert names["tool:get_logs:empty@2"] == "tool_get_logs_empty_2"
    assert names["tool:a:wrong=x/y"] != names["tool:a:wrong=x_y"]
    assert names["tool:a:wrong=x/y"].startswith("tool_a_wrong=x_y-")


def test_the_plan_runs_a_tasks_clean_runs_before_its_faulted_ones(tmp_path):
    tasks = [Task("a", "q"), Task("b", "r")]
    faults = [parse("model:timeout"), parse("tool:x:empty")]

    planned = plan(tasks, faults, 2, 1, tmp_path)

    assert [str(p.directory.relative_to(tmp_path)) for p in planned] == [
        "a/clean/1",
        "a/clean/2",
        "a/model_timeout/1",
        "a/tool_x_empty/1",
        "b/clean/1",
        "b/clean/2",
        "b/model_timeout/1",
        "b/tool_x_empty/1",
    ]


def faultline_test(tmp_path: Path, upstream: str, *flags: str):
    tasks = tmp_path / "tasks.toml"
    tasks.write_text(TWO_TASKS)
    return subprocess.run(
        [FAULTLINE, "test", "--tasks", str(tasks), "--upstream", upstream, *flags]
        + ["--", sys.executable, "-c", ASKS],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_every_run_is_saved_with_its_output_trace_and_result(tmp_path):
    out = tmp_path / "out"
    with upstream_server() as upstream:
        upstream.chunks = [openai_reply({"content": "ROOT CAUSE: cart"}, "stop")]
        result = faultline_test(
            tmp_path,
            upstream.url,
            "--fault",
            "model:rate_limit",
            "--runs",
            "1",
            "--clean-runs",
            "2",
            "--out",
            str(out),
        )
        asked = [
            json.loads(body)["messages"][0]["content"] for *_, body in upstream.seen
        ]

    assert result.returncode == 0, result.stderr
    # The rate-limited runs never reach the upstream.
    assert asked == ["Why does the cart fail?"] * 2 + ["Why are ads slow?"] * 2
    first = out / "cart" / "clean" / "1"
    assert (first / "stdout.txt").read_text() == "ROOT CAUSE: cart\n"
    assert "faultline: proxy http://127.0.0.1:" in (first / "stderr.txt").read_text()
    assert (first / "trace" / "model.jsonl").is_file()
    saved = json.loads((first / "result.json").read_text())
    assert {k: saved[k] for k in ("task", "fault", "number", "exit_code")} == {
        "task": "cart",
        "fault": None,
        "number": 1,
        "exit_code": 0,
    }
    assert (saved["answer"], saved["verdict"]) == ("ROOT CAUSE: cart", "passed")
    faulted = json.loads((out / "cart/model_rate_limit/1/result.json").read_text())
    assert (faulted["fault"], faulted["exit_code"], faulted["verdict"]) == (
        "model:rate_limit",
        1,
        "failed_loudly",
    )
    assert "Traceback" in (out / "cart/model_rate_limit/1/stderr.txt").read_text()
    assert result.stderr.count("\n[") == 6


def test_runs_are_never_mixed_into_an_earlier_test(tmp_path):
    out = tmp_path / "out"
    (out / "old").mkdir(parents=True)

    result = faultline_test(
        tmp_path, "http://127.0.0.1:9", "--fault", "model:timeout", "--out", str(out)
    )

    assert result.returncode == 2
    assert "is not an empty directory" in result.stderr


def test_an_unknown_fault_or_a_bad_count_is_an_error_before_any_run(tmp_path):
    bad_fault = faultline_test(tmp_path, "http://127.0.0.1:9", "--fault", "model:nope")
    no_runs = faultline_test(
        tmp_path, "http://127.0.0.1:9", "--fault", "model:timeout", "--runs", "0"
    )

    assert bad_fault.returncode == 2
    assert "unknown fault 'model:nope'" in bad_fault.stderr
    assert no_runs.returncode == 2
    assert "must be 1 or more" in no_runs.stderr
