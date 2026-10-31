"""`faultline test`: the tasks file, the plan of runs and where each run is saved."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from upstream import ANSWERS, openai_reply, upstream_server
from upstream import ASKS as ASKS_FOR_LOGS

from faultline import matrix
from faultline.faults import parse
from faultline.model import Verdict
from faultline.reproduce import (
    Task,
    TasksError,
    command_for,
    load_tasks,
    parse_tasks,
    plan,
    safe_names,
)

HERE = Path(__file__).parent
FAULTLINE = str(Path(sys.executable).parent / "faultline")
# Asks the model its one argument and prints the reply's text.
ASKER = """
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


def faultline_test(
    tmp_path: Path,
    upstream: str,
    *flags: str,
    tasks: str = TWO_TASKS,
    agent: list[str] | None = None,
):
    path = tmp_path / "tasks.toml"
    path.write_text(tasks)
    return subprocess.run(
        [
            FAULTLINE,
            "test",
            "--allow-live-tools",
            "--tasks",
            str(path),
            "--upstream",
            upstream,
            *flags,
        ]
        + ["--", *(agent or [sys.executable, "-c", ASKER])],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=tmp_path,  # where a --matcher module is found
    )


ONE_TASK = '[[task]]\nid = "cart"\ninput = "Why does the cart fail?"\n'
WRONG = openai_reply({"role": "assistant", "content": "payment is down"}, "stop")


def tool_agent(tmp_path: Path) -> list[str]:
    """The tiny agent: one get_logs call over MCP, then the model's answer."""
    return [sys.executable, str(HERE / "tiny_agent.py"), str(tmp_path / "server.log")]


def results(out: Path) -> dict[str, dict]:
    return {
        str(path.parent.relative_to(out)): json.loads(path.read_text())
        for path in sorted(out.glob("*/*/*/result.json"))
    }


def test_a_faulted_answer_unlike_the_first_clean_one_failed_silently(tmp_path):
    out = tmp_path / "out"
    with upstream_server() as upstream:
        # Two clean runs, then one under the fault: two requests each.
        upstream.replies = [ASKS_FOR_LOGS, ANSWERS] * 2 + [ASKS_FOR_LOGS, WRONG]
        result = faultline_test(
            tmp_path,
            upstream.url,
            "--fault",
            "tool:get_logs:empty",
            "--runs",
            "1",
            "--clean-runs",
            "2",
            "--out",
            str(out),
            tasks=ONE_TASK,
            agent=tool_agent(tmp_path),
        )

    assert result.returncode == 0, result.stderr
    saved = results(out)
    assert {run: (r["verdict"], r["reference"]) for run, r in saved.items()} == {
        "cart/clean/1": (None, "clean run 1"),  # the reference run itself
        "cart/clean/2": ("passed", "clean run 1"),
        "cart/tool_get_logs_empty/1": ("failed_silently", "clean run 1"),
    }
    assert saved["cart/tool_get_logs_empty/1"]["reason"] == (
        'answer lacks "cart", compared with clean run 1, '
        "and says nothing of its own work falling short (heuristic); injected tool:get_logs:empty"
    )
    # The fault matrix: printed, and saved with each cell's runs.
    assert result.stdout == (
        "fault                passed  failed loudly  failed silently  no verdict  runs\n"
        "clean                     1              0                0           1     2\n"
        "tool:get_logs:empty       0              0                1           0     1\n"
    )
    saved_matrix = json.loads((out / "matrix.json").read_text())
    assert saved_matrix["rows"][1] == {
        "fault": "tool:get_logs:empty",
        "passed": 0,
        "failed_loudly": 0,
        "failed_silently": 1,
        "no_verdict": 0,
        "runs": 1,
        "cells": {
            "passed": [],
            "failed_loudly": [],
            "failed_silently": ["cart/tool_get_logs_empty/1"],
            "no_verdict": [],
        },
    }
    assert saved_matrix["rows"][0]["cells"]["passed"] == ["cart/clean/2"]
    assert saved_matrix["rows"][0]["cells"]["no_verdict"] == ["cart/clean/1"]
    assert saved["cart/clean/1"]["reason"] == "no verdict: reference run"


def test_when_the_clean_runs_disagree_no_faulted_run_is_called_wrong(tmp_path):
    out = tmp_path / "out"
    with upstream_server() as upstream:
        # Clean run 1 says cart, clean run 2 says payment, the faulted run payment.
        upstream.replies = [ASKS_FOR_LOGS, ANSWERS] + [ASKS_FOR_LOGS, WRONG] * 2
        result = faultline_test(
            tmp_path,
            upstream.url,
            "--fault",
            "tool:get_logs:empty",
            "--runs",
            "1",
            "--clean-runs",
            "2",
            "--out",
            str(out),
            tasks=ONE_TASK,
            agent=tool_agent(tmp_path),
        )

    assert result.returncode == 0, result.stderr
    saved = results(out)
    assert saved["cart/clean/1"]["reason"] == "no verdict: reference run"
    assert saved["cart/tool_get_logs_empty/1"]["verdict"] is None
    assert saved["cart/tool_get_logs_empty/1"]["reason"] == (
        'no verdict: answer lacks "cart", compared with clean run 1, '
        "but the clean runs disagree"
    )


def test_a_team_matcher_decides_what_counts_as_the_same(tmp_path):
    (tmp_path / "team_match.py").write_text(
        "def anything_down(answer, reference):\n    return 'down' in answer\n"
    )
    out = tmp_path / "out"
    with upstream_server() as upstream:
        upstream.replies = [ASKS_FOR_LOGS, ANSWERS, ASKS_FOR_LOGS, WRONG]
        result = faultline_test(
            tmp_path,
            upstream.url,
            "--fault",
            "tool:get_logs:empty",
            "--runs",
            "1",
            "--clean-runs",
            "1",
            "--matcher",
            "team_match:anything_down",
            "--out",
            str(out),
            tasks=ONE_TASK,
            agent=tool_agent(tmp_path),
        )
    missing = faultline_test(
        tmp_path, "http://127.0.0.1:9", "--fault", "model:timeout", "--matcher", "x:y"
    )

    assert result.returncode == 0, result.stderr
    assert results(out)["cart/tool_get_logs_empty/1"]["verdict"] == "passed"
    assert missing.returncode == 2
    assert "--matcher x:y: cannot import x" in missing.stderr


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


def test_a_fault_given_twice_is_refused_before_any_run(tmp_path):
    result = faultline_test(
        tmp_path, "http://127.0.0.1:9", "--allow-live-tools",
        "--fault", "model:timeout", "--fault", "model:timeout",
        "--out", str(tmp_path / "out"),
    )  # fmt: skip

    assert result.returncode == 2
    assert "--fault model:timeout is given twice" in result.stderr
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("target", ["real", "missing"], ids=["link", "dangling link"])
def test_a_symlinked_out_directory_is_refused(tmp_path, target):
    (tmp_path / "real").mkdir()
    (tmp_path / "out").symlink_to(tmp_path / target)

    result = faultline_test(
        tmp_path, "http://127.0.0.1:9", "--allow-live-tools",
        "--fault", "model:timeout", "--out", str(tmp_path / "out"),
    )  # fmt: skip

    assert result.returncode == 2
    assert "is a symbolic link" in result.stderr
    assert not (tmp_path / "missing").exists()
    assert list((tmp_path / "real").iterdir()) == []


def test_task_ids_that_differ_only_in_case_are_refused():
    with pytest.raises(TasksError, match="task ids Cart and cart differ only in case"):
        parse_tasks(
            "[[task]]\nid = 'Cart'\ninput = 'q'\n[[task]]\nid = 'cart'\ninput = 'r'\n"
        )


def test_the_help_says_the_cap_can_be_passed_by_one_call():
    result = subprocess.run(
        [FAULTLINE, "test", "--help"], capture_output=True, text=True, check=True
    )

    assert "passed by one model call" in " ".join(result.stdout.split())


# The first run starts a process that sends SIGINT to faultline half a second after
# the agent exits: between runs, or as the next one starts.
INTERRUPTS = """
import os, pathlib, subprocess, sys
fired = pathlib.Path(sys.argv[1])
if not fired.exists():
    fired.write_text("")
    kill = f"import os, signal, time; time.sleep(0.5); os.kill({os.getppid()}, signal.SIGINT)"
    subprocess.Popen([sys.executable, "-c", kill], start_new_session=True)
print("cart")
"""


def test_ctrl_c_between_runs_keeps_the_matrix_and_shows_no_traceback(tmp_path):
    out = tmp_path / "out"
    result = faultline_test(
        tmp_path, "http://127.0.0.1:9", "--allow-live-tools",
        "--fault", "model:timeout", "--runs", "3", "--clean-runs", "3",
        "--out", str(out),
        agent=[sys.executable, "-c", INTERRUPTS, str(tmp_path / "fired"), "{input}"],
    )  # fmt: skip

    assert result.returncode == 130, result.stderr
    assert "Traceback" not in result.stderr
    assert "faultline test: stopped by" in result.stderr
    saved = json.loads((out / "matrix.json").read_text())
    assert 1 <= sum(row["runs"] for row in saved["rows"]) < 12


def test_every_run_lands_in_one_cell_of_its_row():
    marks = [
        matrix.Mark("clean", Verdict.PASSED, "a/clean/1"),
        matrix.Mark("model:timeout", Verdict.FAILED_LOUDLY, "a/model_timeout/1"),
        matrix.Mark("model:timeout", None, "a/model_timeout/2"),
    ]

    table = matrix.rows(marks, ["model:timeout", "tool:x:empty"])

    assert [(row.name, row.runs) for row in table] == [
        ("clean", 1),
        ("model:timeout", 2),
        ("tool:x:empty", 0),
    ]
    assert table[1].cells["no_verdict"] == ["a/model_timeout/2"]
    assert matrix.show(table).splitlines()[2] == (
        "model:timeout       0              1                0           1     2"
    )
