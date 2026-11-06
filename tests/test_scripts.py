"""An agent run as a script: the command, its answer, its children and how it stops."""

import json
import os
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import cast

import jsonschema
import pytest

import derail_sdk
from conftest import ACME, Project, Result
from derail_sdk import bootstrap, runner
from derail_sdk.control import ControlServer
from derail_sdk.runner import stop_group
from fakes.openai_server import FakeOpenAI

Fake = Callable[[list[str]], FakeOpenAI]
Line = dict[str, object]
RECORD = json.loads((Path(derail_sdk.__file__).parent / "schemas" / "record.v1.json").read_text())
TOOLS = """["acme.tools:get_order", "acme.tools:check_inventory", "acme.tools:get_stock",
    "acme.tools:refund_order"]"""
QUESTION = "Where is order 1234; rm -rf ~"


def write_config(project: Project, command: str, extra: str = "", body: str = "") -> None:
    project.toml.write_text(
        f"""
[agent]
command = {json.dumps(command)}
tools = {TOOLS}
real = ["tool:check_inventory"]
runs = 1
parallel = 1
max_cost_usd = "none"
model_hosts = [{{host = "127.0.0.1", provider = "openai"}}]
{extra}

[[test]]
name = "t"
question = {json.dumps(QUESTION)}
expect = ["x"]
{body}
"""
    )


def script(project: Project, code: str, extra: str = "", body: str = "") -> Result:
    """Run code as bot.py, the one program of a one-run test."""
    (project.root / "bot.py").write_text(code)
    write_config(project, f"{sys.executable} bot.py {{question}}", extra, body)
    return project.test()


def lines_of(result: Result) -> list[Line]:
    records = result.records(processes=True)
    assert len(records) == 1, result.out
    return records[0]


def summary_of(result: Result) -> dict[str, object]:
    return cast(dict[str, object], lines_of(result)[-1]["summary"])


def calls(result: Result) -> list[Line]:
    return [line for line in lines_of(result) if "call" in line]


def processes(result: Result) -> list[dict[str, object]]:
    found = [line for line in lines_of(result) if line.get("notice") == "process"]
    return [cast(dict[str, object], line["process"]) for line in found]


def root_and_other(result: Result) -> tuple[Line, Line]:
    """The two process lines. Each process sends on its own, so they can arrive either way."""
    found = [line for line in lines_of(result) if line.get("notice") == "process"]
    assert len(found) == 2, result.out
    first, second = found
    pid = cast(dict[str, object], first["proc"])["pid"]
    return (
        (first, second)
        if cast(dict[str, object], second["process"])["parent"] == pid
        else (second, first)
    )


def alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as stat_file:
            return stat_file.read().rpartition(")")[2].split()[0] != "Z"
    except FileNotFoundError:
        return False


def wait_for(path: Path, seconds: float = 30) -> str:
    deadline = time.monotonic() + seconds
    while not path.exists() or not path.read_text().strip():
        assert time.monotonic() < deadline, f"{path} never appeared"
        time.sleep(0.01)
    return path.read_text()


GATED_CHILD = """
import subprocess, sys
done = subprocess.run([sys.executable, "report.py"])
print("the bot's answer")
"""
REPORT_REFUNDS = "from acme import tools\ntools.refund_order('1234')\n"


# 6.1 The script shape.


@pytest.mark.design("script.one_argument")
def test_a_question_reaches_the_script_as_one_argument_with_no_shell(project: Project) -> None:
    result = script(
        project,
        "import json, sys\nprint(json.dumps(sys.argv[1:]))\n",
    )
    assert result.code == 0, result.out + result.err
    assert summary_of(result)["answer"] == json.dumps([QUESTION])


@pytest.mark.design("script.no_shell")
@pytest.mark.parametrize(
    "question",
    ["$(touch pwned)", "`touch pwned`", "a' 'b", 'a" "b', "{question}", "-x --flag", "one\ntwo"],
)
def test_nothing_in_a_question_is_read_by_a_shell(project: Project, question: str) -> None:
    (project.root / "bot.py").write_text(
        "import json, os, sys\n"
        "with open(os.environ['DERAIL_ANSWER_FILE'], 'w') as out:\n"
        "    json.dump(sys.argv[1:], out)\n"
    )
    write_config(project, f"{sys.executable} bot.py {{question}}")
    project.edit(json.dumps(QUESTION), json.dumps(question))
    result = project.test()
    assert result.code == 0, result.out + result.err
    assert summary_of(result)["answer"] == json.dumps([question])
    assert not (project.root / "pwned").exists()


def test_a_question_can_sit_inside_an_argument_and_a_quoted_one_stays_whole(
    project: Project,
) -> None:
    (project.root / "bot.py").write_text("import json, sys\nprint(json.dumps(sys.argv[1:]))\n")
    write_config(
        project, f"{sys.executable} bot.py --ask={{question}} 'two words' \"{{question}}\""
    )
    result = project.test()
    assert summary_of(result)["answer"] == json.dumps(
        [f"--ask={QUESTION}", "two words", QUESTION]
    ), result.out + result.err


@pytest.mark.design("script.example")
def test_the_bot_answers_and_its_child_is_refused(project: Project, fake_openai: Fake) -> None:
    write_config(
        project,
        f"{sys.executable} bot.py {{question}}",
        body='[[test.mock]]\ncall = "tool:get_order"\nreturns = "shipped"\n',
    )
    result = project.test(fake_openai(ACME[:1] + ACME[2:]))
    assert result.code == 2, result.out + result.err
    summary = summary_of(result)
    assert summary["outcome"] == "refused call"
    tools = {(x["call"], x["action"]) for x in calls(result) if x.get("kind") == "tool"}
    assert tools == {
        ("tool:get_order", "mock"),
        ("tool:check_inventory", "real"),
        ("tool:refund_order", "refused"),
    }
    assert [line["kind"] for line in calls(result)].count("model") == 2


@pytest.mark.design("script.last_printed_line")
def test_the_last_printed_line_is_the_answer(project: Project) -> None:
    result = script(project, "print('thinking'); print('the answer'); print(); print('   ')\n")
    assert summary_of(result)["answer"] == "the answer"
    assert summary_of(result)["outcome"] == "answered"


@pytest.mark.design("script.stderr_is_not_the_answer")
def test_what_goes_to_stderr_is_never_the_answer(project: Project) -> None:
    code = "import sys\nprint('the answer', flush=True)\nprint('a log line', file=sys.stderr)\n"
    result = script(project, code)
    assert summary_of(result)["answer"] == "the answer"
    run = next((project.root / "derail-runs").glob("*/*.jsonl")).stem
    folder = next((project.root / "derail-runs").glob("*/*.jsonl")).parent
    assert "a log line" in (folder / f"{run}.log").read_text()
    assert "a log line" not in (folder / f"{run}.out").read_text()


@pytest.mark.design("script.answer_file")
def test_the_answer_file_wins_over_the_last_printed_line(project: Project) -> None:
    code = (
        "import os\n"
        "with open(os.environ['DERAIL_ANSWER_FILE'], 'w') as out:\n"
        "    out.write('from the file')\n"
        "print('a printed line')\n"
    )
    assert summary_of(script(project, code))["answer"] == "from the file"


def test_a_printed_secret_is_redacted_from_the_answer(project: Project) -> None:
    secret = f"sk-{'a' * 40}"
    result = script(project, f"print('key {secret}')\n")
    assert secret not in json.dumps(lines_of(result))


def test_output_that_is_not_text_does_not_stop_the_runner(project: Project) -> None:
    code = "import sys\nsys.stdout.buffer.write(b'caf\\xe9 ok\\n')\n"
    assert "ok" in str(summary_of(script(project, code))["answer"])


def test_only_the_tail_of_a_very_long_output_is_read(project: Project) -> None:
    code = "print('x' * 3_000_000)\nprint('the answer')\n"
    assert summary_of(script(project, code))["answer"] == "the answer"


@pytest.mark.design("script.crash_has_no_answer")
def test_a_script_that_fails_has_crashed_and_its_last_line_is_no_answer(project: Project) -> None:
    result = script(project, "print('half way')\nraise SystemExit(4)\n")
    assert (summary_of(result)["outcome"], summary_of(result)["answer"]) == ("crashed", None)


def test_a_script_that_ends_well_with_no_output_gave_no_answer(project: Project) -> None:
    result = script(project, "pass\n")
    assert (summary_of(result)["outcome"], summary_of(result)["answer"]) == ("crashed", None)


def test_the_output_file_is_owner_only(project: Project) -> None:
    script(project, "print('the answer')\n")
    out = next((project.root / "derail-runs").glob("*/*.out"))
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


@pytest.mark.design("script.function_shape_unchanged")
def test_a_function_agents_output_stays_in_one_log(project: Project, fake_openai: Fake) -> None:
    project.edit("[agent]\n", "[agent]\nruns = 1\n")
    project.test(fake_openai(ACME))
    assert not list((project.root / "derail-runs").glob("*/*.out"))


@pytest.mark.design("script.runs_under_any_starter")
def test_a_script_started_by_uv_run_is_still_the_program_under_test(project: Project) -> None:
    """uv keeps its own process, so Python's id is not the one derail-run set."""
    (project.root / "bot.py").write_text(
        "import os\n"
        "from acme import tools\n"
        "try:\n"
        "    print(tools.get_order('1'))\n"
        "except TimeoutError:\n"
        "    print(os.environ['DERAIL_ROOT_PID'] != str(os.getpid()))\n"
    )
    uv = f"uv run --no-project --python {sys.executable} python bot.py {{question}}"
    write_config(project, uv, body=ORDER_ROW)
    project.monkeypatch.setenv("UV_NO_SYNC", "1")
    result = project.test()
    records = {str(lines[-1]["row"]): lines for lines in result.records()}
    assert records["fault 1"][-1]["summary"]["fault_fired"] is True, result.out  # type: ignore[index]
    assert records["fault 1"][-1]["summary"]["answer"] == "True"  # type: ignore[index]


def test_derail_run_in_front_of_the_script_changes_nothing(project: Project) -> None:
    (project.root / "bot.py").write_text("print('the answer')\n")
    command = f"{sys.executable} -m derail_sdk.run_command {sys.executable} bot.py {{question}}"
    write_config(project, command)
    assert summary_of(project.test())["answer"] == "the answer"


def test_a_tool_in_the_scripts_own_folder_is_found(project: Project) -> None:
    folder = project.root / "deep"
    folder.mkdir()
    (folder / "own_tools.py").write_text("def lookup(sku):\n    return 'real'\n")
    (folder / "bot.py").write_text(
        "import own_tools\nprint(own_tools.lookup('1'))\n",
    )
    write_config(project, f"{sys.executable} deep/bot.py {{question}}")
    project.edit(TOOLS, '["own_tools:lookup"]')
    project.edit('real = ["tool:check_inventory"]', 'real = ["tool:lookup"]')
    assert summary_of(project.test())["answer"] == "real"


def test_a_listed_module_that_does_not_exist_stops_a_script(project: Project) -> None:
    write_config(project, f"{sys.executable} bot.py {{question}}")
    project.edit(TOOLS, '["acme.nothing:tool"]')
    project.edit('real = ["tool:check_inventory"]', "real = []")
    (project.root / "bot.py").write_text("print('ran')\n")
    result = project.test()
    assert result.code == 2
    assert "acme.nothing is listed under [agent] tools but cannot be found" in result.out


@pytest.mark.parametrize(
    "command",
    ["no-such-program {question}", "./no-such-script.py {question}"],
)
def test_a_program_that_cannot_be_found_is_a_mistake_in_the_test(
    project: Project, command: str
) -> None:
    write_config(project, command)
    result = project.test()
    assert result.code == 2 and "is not a program it can find" in result.err
    assert not (project.root / "derail-runs").exists()


def test_derail_run_says_why_a_program_cannot_start(tmp_path: Path) -> None:
    def derail_run(program: str) -> subprocess.CompletedProcess[str]:
        command = [sys.executable, "-m", "derail_sdk.run_command", program]
        return subprocess.run(command, capture_output=True, text=True, timeout=60)

    missing = derail_run(str(tmp_path / "missing"))
    assert missing.returncode == 127 and "derail-run: cannot run" in missing.stderr
    plain = tmp_path / "plain.txt"
    plain.write_text("not a program\n")
    assert derail_run(str(plain)).returncode == 126


# 6.2 Children.


@pytest.mark.design("child.refused")
def test_a_childs_write_is_refused_and_its_process_is_in_the_record(project: Project) -> None:
    (project.root / "report.py").write_text(
        "from acme import tools\n"
        "print(tools.check_inventory('SKU-42'))\n"
        "tools.refund_order('1234')\n"
        "print('REFUND RAN')\n"
    )
    result = script(project, GATED_CHILD)
    assert result.code == 2 and summary_of(result)["outcome"] == "refused call", result.out
    root, child = root_and_other(result)
    assert root["proc"] != child["proc"]
    refused = next(x for x in calls(result) if x["action"] == "refused")
    assert refused["call"] == "tool:refund_order" and refused["proc"] == child["proc"]
    assert "neither mocked" in str(refused["error"])
    details = cast(dict[str, object], child["process"])
    assert details["python"] == sys.executable
    assert details["command"] == f"{sys.executable} report.py"
    assert details["parent"] == cast(dict[str, object], root["proc"])["pid"]
    validator = jsonschema.Draft202012Validator(RECORD, format_checker=jsonschema.FormatChecker())
    for line in lines_of(result):
        validator.validate(line)


@pytest.mark.design("child.mocks_apply")
def test_a_childs_mock_applies_and_its_line_is_in_the_record(project: Project) -> None:
    (project.root / "report.py").write_text("from acme import tools\nprint(tools.get_order('1'))\n")
    code = (
        "import subprocess, sys\n"
        "done = subprocess.run([sys.executable, 'report.py'], capture_output=True, text=True)\n"
        "print(done.stdout.strip())\n"
    )
    result = script(project, code, body='[[test.mock]]\ncall = "tool:get_order"\nreturns = "m"\n')
    assert result.code == 0, result.out
    assert summary_of(result)["answer"] == "m"
    assert [x["action"] for x in calls(result)] == ["mock"]


@pytest.mark.design("child.same_python_by_any_name")
def test_a_child_started_as_python3_is_the_same_python(project: Project) -> None:
    (project.root / "report.py").write_text(REPORT_REFUNDS)
    code = (
        "import os, subprocess, sys\n"
        "alias = os.path.join(os.path.dirname(sys.executable), 'python3')\n"
        "subprocess.run([alias, 'report.py'])\n"
        "print('the answer')\n"
    )
    result = script(project, code)
    assert summary_of(result)["outcome"] == "refused call", result.out


@pytest.mark.design("child.tools_at_startup")
def test_a_child_that_binds_a_tool_at_import_is_gated_before_any_plan_arrives(
    project: Project,
) -> None:
    (project.root / "report.py").write_text(
        "from acme.tools import refund_order\n"
        "import derail_sdk.state as state\n"
        "assert state.current()._plan is None\n"
        "refund_order('1234')\n"
    )
    result = script(project, GATED_CHILD)
    assert summary_of(result)["outcome"] == "refused call", result.out


@pytest.mark.design("child.tool_renamed")
def test_a_childs_renamed_tool_keeps_the_name_the_model_sees(project: Project) -> None:
    (project.root / "report.py").write_text(REPORT_REFUNDS)
    (project.root / "bot.py").write_text(GATED_CHILD)
    write_config(project, f"{sys.executable} bot.py {{question}}")
    project.edit(
        '"acme.tools:refund_order"]',
        '{path = "acme.tools:refund_order", name = "pay-back"}]',
    )
    result = project.test()
    assert [x["call"] for x in calls(result)] == ["tool:pay-back"], result.out


CHILD_CALLS_ORDER = (
    "import subprocess, sys\nsubprocess.run([sys.executable, 'report.py'])\nprint('the answer')\n"
)
REPORT_ORDER = (
    "from acme import tools\n"
    "try:\n"
    "    print(tools.get_order('1'))\n"
    "except TimeoutError:\n"
    "    print('timed out')\n"
)
ORDER_ROW = (
    '[[test.mock]]\ncall = "tool:get_order"\nreturns = "m"\n'
    '[[test.fault]]\ncall = "tool:get_order"\ndo = "timeout"\n'
)


@pytest.mark.design("child.no_faults")
def test_a_fault_does_not_fire_in_a_child_unless_children_is_set(project: Project) -> None:
    (project.root / "report.py").write_text(REPORT_ORDER)
    result = script(project, CHILD_CALLS_ORDER, body=ORDER_ROW)
    rows = {str(lines[-1]["row"]): lines for lines in result.records()}
    assert [x["action"] for x in rows["fault 1"] if "call" in x] == ["mock"], result.out
    assert "the fault never fired in any run" in result.out and result.code == 2


@pytest.mark.design("child.children_set")
def test_with_children_set_a_fault_fires_in_a_child(project: Project) -> None:
    (project.root / "report.py").write_text(REPORT_ORDER)
    result = script(project, CHILD_CALLS_ORDER, "children = true", ORDER_ROW)
    rows = {str(lines[-1]["row"]): lines for lines in result.records()}
    assert [x["action"] for x in rows["fault 1"] if "call" in x] == ["fault"], result.out
    assert result.code == 0, result.out


@pytest.mark.design("child.lazy_plan")
def test_a_child_that_makes_no_calls_costs_nothing(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    (project.root / "report.py").write_text("print('nothing gated here')\n")
    served: list[str] = []
    real_plan = ControlServer._plan  # type: ignore[reportPrivateUsage]

    def counted(self: ControlServer, run: str) -> tuple[int, dict[str, object]]:
        served.append(run)
        return real_plan(self, run)

    monkeypatch.setattr(ControlServer, "_plan", counted)
    posts: list[bytes] = []
    real_records = ControlServer._records  # type: ignore[reportPrivateUsage]

    def posted(self: ControlServer, body: bytes) -> tuple[int, dict[str, object]]:
        posts.append(body)
        return real_records(self, body)

    monkeypatch.setattr(ControlServer, "_records", posted)
    result = script(project, CHILD_CALLS_ORDER)
    assert result.code == 0, result.out
    assert len(served) == 1  # the program's own
    assert len(processes(result)) == 1
    pids = {json.loads(body)["proc"]["pid"] for body in posts if json.loads(body).get("proc")}
    assert len(pids) == 1


@pytest.mark.design("child.model_call_budgeted")
def test_a_childs_model_call_is_recorded_and_counts_against_the_budget(
    project: Project, fake_openai: Fake
) -> None:
    (project.root / "report.py").write_text(
        "import openai\n"
        "client = openai.OpenAI(max_retries=0)\n"
        "client.chat.completions.create(model='gpt-4o-mini', messages=[])\n"
    )
    (project.root / "bot.py").write_text(CHILD_CALLS_ORDER)
    write_config(project, f"{sys.executable} bot.py {{question}}")
    project.edit('max_cost_usd = "none"', "max_cost_usd = 1.0")
    result = project.test(fake_openai(["acme_answer"]))
    assert summary_of(result)["outcome"] == "answered", result.out
    assert summary_of(result)["model_calls"] == 1
    assert float(str(summary_of(result)["cost_usd"])) > 0


@pytest.mark.design("child.prints_nothing")
def test_a_child_on_the_same_python_prints_nothing_extra(project: Project) -> None:
    code = (
        "import subprocess, sys\n"
        "program = 'from acme import tools; print(tools.check_inventory(\"1\"))'\n"
        "done = subprocess.run([sys.executable, '-c', program], capture_output=True)\n"
        "print(repr((done.returncode, done.stdout, done.stderr)))\n"
    )
    result = script(project, code)
    assert summary_of(result)["answer"] == repr((0, b"1: 12 in stock\n", b"")), result.out


def test_a_child_does_not_stop_in_another_folder_for_a_tool_it_cannot_find(
    project: Project,
) -> None:
    code = (
        "import subprocess, sys\n"
        "done = subprocess.run([sys.executable, '-c', 'print(1)'], cwd='/', capture_output=True)\n"
        "print(repr((done.returncode, done.stdout, done.stderr)))\n"
    )
    assert summary_of(script(project, code))["answer"] == repr((0, b"1\n", b""))


@pytest.mark.design("record.process_redacted")
def test_a_secret_on_a_command_line_is_not_recorded(project: Project) -> None:
    secret = f"sk-{'b' * 40}"
    (project.root / "report.py").write_text(REPORT_REFUNDS)
    code = (
        "import subprocess, sys\n"
        f"subprocess.run([sys.executable, 'report.py', '--key', {secret!r}])\n"
        "print('the answer')\n"
    )
    result = script(project, code)
    assert len(processes(result)) == 2, result.out
    assert secret not in json.dumps(lines_of(result))
    assert any("[secret removed]" in str(found["command"]) for found in processes(result))


def test_a_very_long_command_line_is_cut_on_the_record(project: Project) -> None:
    (project.root / "bot.py").write_text("print('the answer')\n")
    write_config(project, f"{sys.executable} bot.py {{question}}")
    project.edit(json.dumps(QUESTION), json.dumps("q" * 5000))
    result = project.test()
    assert len(str(processes(result)[0]["command"])) == 1000


# Forked children.


def refuse_in_a_fork(ending: str) -> str:
    return (
        "import contextlib, os\n"
        "from acme import tools\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    with contextlib.suppress(Exception):\n"
        "        tools.refund_order('1234')\n"
        f"    {ending}\n"
        "os.waitpid(pid, 0)\n"
        "print('the answer')\n"
    )


@pytest.mark.design("fork.refused_line_arrives")
@pytest.mark.parametrize("ending", ["os._exit(0)", "raise SystemExit(0)"])
def test_a_forked_childs_refusal_is_not_lost(project: Project, ending: str) -> None:
    result = script(project, refuse_in_a_fork(ending))
    assert result.code == 2, result.out
    assert summary_of(result)["outcome"] == "refused call"
    assert [x["call"] for x in calls(result)] == ["tool:refund_order"]
    root, fork = root_and_other(result)
    assert root["proc"] != fork["proc"]
    assert (
        cast(dict[str, object], fork["process"])["parent"]
        == (cast(dict[str, object], root["proc"])["pid"])
    )


@pytest.mark.design("fork.lines_arrive")
def test_a_forked_childs_calls_are_in_the_record_with_no_gap(project: Project) -> None:
    code = (
        "import os\n"
        "from acme import tools\n"
        "tools.check_inventory('parent')\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    tools.check_inventory('child')\n"
        "    os._exit(0)\n"
        "os.waitpid(pid, 0)\n"
        "print('the answer')\n"
    )
    result = script(project, code)
    assert summary_of(result)["outcome"] == "answered", result.out
    assert summary_of(result)["incomplete"] is False
    made = calls(result)
    assert len(made) == 2 and made[0]["proc"] != made[1]["proc"]


@pytest.mark.design("fork.multiprocessing")
@pytest.mark.parametrize("method", ["fork", "spawn", "forkserver"])
def test_a_multiprocessing_workers_refusal_is_not_lost(project: Project, method: str) -> None:
    code = (
        "import contextlib, multiprocessing\n"
        "from acme import tools\n"
        "def work():\n"
        "    with contextlib.suppress(Exception):\n"
        "        tools.refund_order('1234')\n"
        "if __name__ == '__main__':\n"
        f"    context = multiprocessing.get_context({method!r})\n"
        "    worker = context.Process(target=work)\n"
        "    worker.start()\n"
        "    worker.join()\n"
        "    print('the answer')\n"
    )
    result = script(project, code)
    assert summary_of(result)["outcome"] == "refused call", result.out


@pytest.mark.design("fork.no_faults")
def test_a_forked_child_fires_no_fault_unless_children_is_set(project: Project) -> None:
    code = (
        "import os\n"
        "from acme import tools\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    print('m' if tools.get_order('1') == 'm' else 'other', flush=True)\n"
        "    os._exit(0)\n"
        "os.waitpid(pid, 0)\n"
    )
    result = script(project, code, body=ORDER_ROW)
    rows = {str(lines[-1]["row"]): lines for lines in result.records()}
    assert [x["action"] for x in rows["fault 1"] if "call" in x] == ["mock"], result.out
    allowed = script(project, code, "children = true", ORDER_ROW)
    rows = {str(lines[-1]["row"]): lines for lines in allowed.records()}
    assert any(x.get("action") == "fault" for x in rows["fault 1"]), allowed.out


def test_a_fork_while_another_thread_holds_the_locks_does_not_hang(project: Project) -> None:
    code = (
        "import os, threading\n"
        "import derail_sdk.state as state\n"
        "from acme import tools\n"
        "run = state.current()\n"
        "with run._lock, run.sink._counts, run.sink._sending:\n"
        "    pid = os.fork()\n"
        "if pid == 0:\n"
        "    tools.check_inventory('child')\n"
        "    os._exit(0)\n"
        "_, status = os.waitpid(pid, 0)\n"
        "print(status)\n"
    )
    result = script(project, code, "run_timeout = 10")
    assert summary_of(result)["answer"] == "0", result.out
    assert len(calls(result)) == 1


def test_a_fork_does_not_send_the_parents_waiting_lines_or_tools_twice(project: Project) -> None:
    code = (
        "import os\n"
        "import derail_sdk.state as state\n"
        "from acme import tools\n"
        "tools.check_inventory('parent')\n"
        "if os.fork() == 0:\n"
        "    raise SystemExit(0)\n"
        "os.wait()\n"
        "print('the answer')\n"
    )
    result = script(project, code)
    assert len(calls(result)) == 1 and summary_of(result)["outcome"] == "answered", result.out


def test_a_forked_child_does_not_speak_for_the_agents_tools(project: Project) -> None:
    (project.root / "bot.py").write_text(
        "import os\nif os.fork() == 0:\n    raise SystemExit(0)\nos.wait()\nprint('the answer')\n"
    )
    write_config(project, f"{sys.executable} bot.py {{question}}")
    project.edit(TOOLS, "[]")
    result = project.test()
    seen = [line for line in lines_of(result) if line.get("notice") == "tools_seen"]
    assert len(seen) == 1, result.out


def test_a_fork_leaves_the_parents_open_reply_to_the_parent(
    project: Project, fake_openai: Fake
) -> None:
    (project.root / "bot.py").write_text(
        "import os\n"
        "import openai\n"
        "client = openai.OpenAI(max_retries=0)\n"
        "messages = []\n"
        "stream = client.chat.completions.create(\n"
        "    model='gpt-4o-mini', messages=messages, stream=True\n"
        ")\n"
        "if os.fork() == 0:\n"
        "    raise SystemExit(0)\n"
        "os.wait()\n"
        "for _ in stream:\n"
        "    pass\n"
        "print('the answer')\n"
    )
    write_config(project, f"{sys.executable} bot.py {{question}}")
    result = project.test(fake_openai(["chat_stream"]))
    assert [x["kind"] for x in calls(result)] == ["model"], result.out


# Stopping. 6.3


SLOW_TOOL = """
import time
from pathlib import Path


def wait_a_while(sku):
    Path("inside the tool").touch()
    time.sleep(600)
    return sku
"""
UNTIL_THE_TOOL_RUNS = """
while not os.path.exists("inside the tool"):
    time.sleep(0.01)
"""


@pytest.mark.design("stop.timed_out_summary")
def test_a_script_stopped_at_run_timeout_has_a_summary_saying_timed_out(project: Project) -> None:
    code = (
        "import time\n"
        "from acme import tools\n"
        "tools.check_inventory('SKU-1')\n"
        "print('working', flush=True)\n"
        "time.sleep(600)\n"
    )
    result = script(project, code, "run_timeout = 3")
    assert summary_of(result)["outcome"] == "timed out", result.out
    assert summary_of(result)["answer"] is None
    assert [x["call"] for x in calls(result)] == ["tool:check_inventory"]
    assert result.code == 0


@pytest.mark.design("stop.last_line_flushed")
def test_a_call_just_before_sigterm_is_in_the_record(project: Project) -> None:
    """The line waits in the queue when the signal lands, so only the handler can send it."""
    code = (
        "import os, signal\n"
        "from acme import tools\n"
        "tools.check_inventory('SKU-1')\n"
        "os.kill(os.getpid(), signal.SIGTERM)\n"
    )
    result = script(project, code)
    assert [x["call"] for x in calls(result)] == ["tool:check_inventory"], result.out


@pytest.mark.design("stop.call_in_flight")
def test_a_call_going_on_at_the_time_limit_is_in_the_record(project: Project) -> None:
    (project.root / "acme" / "waits.py").write_text(SLOW_TOOL)
    code = "from acme import waits\nwaits.wait_a_while('SKU-1')\n"
    (project.root / "bot.py").write_text(code)
    write_config(project, f"{sys.executable} bot.py {{question}}", "run_timeout = 4")
    project.edit(TOOLS, '["acme.waits:wait_a_while"]')
    project.edit('real = ["tool:check_inventory"]', 'real = ["tool:wait_a_while"]')
    result = project.test()
    assert summary_of(result)["outcome"] == "timed out", result.out
    line = calls(result)[-1]
    assert line["call"] == "tool:wait_a_while" and "DerailStopped" in str(line["error"])


@pytest.mark.design("stop.child_call_in_flight")
def test_a_childs_call_going_on_when_the_group_is_stopped_is_in_the_record(
    project: Project,
) -> None:
    (project.root / "acme" / "waits.py").write_text(SLOW_TOOL)
    (project.root / "report.py").write_text("from acme import waits\nwaits.wait_a_while('1')\n")
    (project.root / "bot.py").write_text(
        "import os, signal, subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, 'report.py'])\n"
        + UNTIL_THE_TOOL_RUNS
        + "os.killpg(0, signal.SIGTERM)\n"
    )
    write_config(project, f"{sys.executable} bot.py {{question}}")
    project.edit(TOOLS, '["acme.waits:wait_a_while"]')
    project.edit('real = ["tool:check_inventory"]', 'real = ["tool:wait_a_while"]')
    result = project.test()
    assert "DerailStopped" in str(calls(result)[-1]["error"]), result.out


@pytest.fixture
def silent_model(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A model host that takes a request and never answers."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    held: list[socket.socket] = []

    def serve() -> None:
        while True:
            try:
                held.append(listener.accept()[0])
            except OSError:
                return

    threading.Thread(target=serve, daemon=True).start()
    monkeypatch.setenv("OPENAI_BASE_URL", f"http://127.0.0.1:{listener.getsockname()[1]}/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    yield
    listener.close()
    for conn in held:
        conn.close()


@pytest.mark.design("stop.model_call_in_flight")
def test_a_model_call_going_on_at_the_time_limit_is_a_time_out_not_a_failure(
    project: Project, silent_model: None
) -> None:
    del silent_model
    code = (
        "import openai\n"
        "client = openai.OpenAI(max_retries=0)\n"
        "client.chat.completions.create(model='gpt-4o-mini', messages=[])\n"
    )
    result = script(project, code, "run_timeout = 10")
    assert summary_of(result)["outcome"] == "timed out", result.out
    assert "DerailStopped" in str(calls(result)[-1]["error"])
    assert result.code == 0


def pids_in(path: Path) -> list[int]:
    return [int(word) for word in path.read_text().split()]


@pytest.mark.design("stop.sigkill")
def test_a_program_that_ignores_sigterm_is_stopped_with_sigkill(project: Project) -> None:
    code = (
        "import os, signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "with open('pid', 'w') as out:\n"
        "    out.write(str(os.getpid()))\n"
        "time.sleep(600)\n"
    )
    project.monkeypatch.setattr(runner, "KILL_GRACE_S", 1.0)
    result = script(project, code, "run_timeout = 4")
    assert summary_of(result)["outcome"] == "timed out", result.out
    assert not alive(pids_in(project.root / "pid")[0])


STAYS_BEHIND = """
import os, subprocess, sys, time
subprocess.Popen([sys.executable, "stays.py"])
while not os.path.exists("pid"):
    time.sleep(0.01)
print("the answer")
"""
CHILD_WRITES_ITS_PID = """
open("pid.tmp", "w").write(str(os.getpid()))
os.rename("pid.tmp", "pid")
time.sleep(600)
"""


@pytest.mark.design("stop.child_outlives_parent")
def test_a_child_that_outlives_the_program_is_stopped_with_the_group(project: Project) -> None:
    (project.root / "stays.py").write_text("import os, time\n" + CHILD_WRITES_ITS_PID)
    result = script(project, STAYS_BEHIND)
    assert summary_of(result)["outcome"] == "answered", result.out
    assert not alive(pids_in(project.root / "pid")[0])


@pytest.mark.design("stop.child_ignores_sigterm")
def test_a_child_that_ignores_sigterm_after_the_program_ended_gets_sigkill(
    project: Project,
) -> None:
    ignores = "import os, signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    (project.root / "stays.py").write_text(ignores + CHILD_WRITES_ITS_PID)
    project.monkeypatch.setattr(runner, "KILL_GRACE_S", 1.0)
    result = script(project, STAYS_BEHIND)
    assert summary_of(result)["outcome"] == "answered", result.out
    assert not alive(pids_in(project.root / "pid")[0])


@pytest.mark.design("stop.child_line_after_the_program_ended")
def test_a_call_made_by_a_child_left_running_reaches_the_record(project: Project) -> None:
    (project.root / "acme" / "waits.py").write_text(SLOW_TOOL)
    (project.root / "report.py").write_text("from acme import waits\nwaits.wait_a_while('1')\n")
    (project.root / "bot.py").write_text(
        "import os, subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, 'report.py'])\n"
        + UNTIL_THE_TOOL_RUNS
        + "print('the answer')\n"
    )
    write_config(project, f"{sys.executable} bot.py {{question}}")
    project.edit(TOOLS, '["acme.waits:wait_a_while"]')
    project.edit('real = ["tool:check_inventory"]', 'real = ["tool:wait_a_while"]')
    result = project.test()
    assert summary_of(result)["outcome"] == "answered", result.out
    assert "DerailStopped" in str(calls(result)[-1]["error"])


DOUBLE_FORK = """
import os, time
{setsid_first}
if os.fork() == 0:
    {setsid_second}
    if os.fork() == 0:
        with open('pid.tmp', 'w') as out:
            out.write(str(os.getpid()))
        os.rename('pid.tmp', 'pid')
        {after}
        time.sleep(600)
    os._exit(0)
os.wait()
while not os.path.exists('pid'):
    time.sleep(0.01)
print('the answer')
"""


@pytest.mark.design("stop.double_fork_in_group")
def test_a_double_fork_with_no_new_session_is_stopped_with_the_group(project: Project) -> None:
    code = DOUBLE_FORK.format(setsid_first="", setsid_second="pass", after="")
    result = script(project, code)
    assert summary_of(result)["outcome"] == "answered", result.out
    assert not alive(pids_in(project.root / "pid")[0])


@pytest.mark.design("stop.daemon_escapes")
def test_a_daemon_with_a_new_session_escapes_but_its_write_stays_refused(project: Project) -> None:
    """The listed limit: only the group is stopped. Its gates stay on, so writes stay refused."""
    after = (
        "\n        import contextlib\n"
        "        from acme import tools\n"
        "        while not os.path.exists('the run ended'):\n"
        "            time.sleep(0.01)\n"
        "        try:\n"
        "            print(tools.refund_order('1'))\n"
        "        except Exception as exc:\n"
        "            open('result.tmp', 'w').write(type(exc).__name__)\n"
        "            os.rename('result.tmp', 'result')\n"
    )
    code = DOUBLE_FORK.format(setsid_first="", setsid_second="os.setsid()", after=after)
    result = script(project, code)
    pid = pids_in(project.root / "pid")[0]
    try:
        assert summary_of(result)["outcome"] == "answered", result.out
        assert alive(pid)
        (project.root / "the run ended").write_text("yes")
        assert wait_for(project.root / "result") == "DerailRefused"
    finally:
        os.kill(pid, signal.SIGKILL)


@pytest.mark.design("stop.exit_status_kept")
def test_sigterm_ends_the_program_as_it_would_without_derail(project: Project) -> None:
    (project.root / "bot.py").write_text(
        "import atexit, os, signal\n"
        "atexit.register(lambda: open('atexit ran', 'w').close())\n"
        "try:\n"
        "    os.kill(os.getpid(), signal.SIGTERM)\n"
        "finally:\n"
        "    open('finally ran', 'w').close()\n"
    )
    write_config(project, f"{sys.executable} bot.py {{question}}")
    result = project.test()
    assert summary_of(result)["outcome"] == "crashed", result.out
    assert not (project.root / "atexit ran").exists()
    assert not (project.root / "finally ran").exists()
    run = next((project.root / "derail-runs").glob("*/*.jsonl"))
    assert "Traceback" not in run.with_suffix(".log").read_text()


@pytest.mark.design("stop.team_handler_wins")
def test_a_programs_own_sigterm_handler_replaces_the_sdks(project: Project) -> None:
    code = (
        "import os, signal, sys\n"
        "from acme import tools\n"
        "def bye(signum, frame):\n"
        "    print('tidied up', flush=True)\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, bye)\n"
        "tools.check_inventory('1')\n"
        "os.kill(os.getpid(), signal.SIGTERM)\n"
    )
    result = script(project, code)
    assert summary_of(result)["answer"] == "tidied up", result.out
    assert summary_of(result)["outcome"] == "answered"
    assert len(calls(result)) == 1


def test_a_sigterm_that_was_ignored_on_entry_stays_ignored(project: Project) -> None:
    code = (
        "import os, signal, subprocess, sys\n"
        "program = 'import signal; print(signal.getsignal(signal.SIGTERM) is signal.SIG_IGN)'\n"
        "done = subprocess.run(\n"
        "    [sys.executable, '-c', program],\n"
        "    preexec_fn=lambda: signal.signal(signal.SIGTERM, signal.SIG_IGN),\n"
        "    capture_output=True, text=True,\n"
        ")\n"
        "print(done.stdout.strip())\n"
    )
    assert summary_of(script(project, code))["answer"] == "True"


def test_a_sigterm_while_python_shuts_down_still_ends_the_program(project: Project) -> None:
    code = (
        "import atexit, os, signal\n"
        "atexit.register(lambda: os.kill(os.getpid(), signal.SIGTERM))\n"
        "print('the answer')\n"
    )
    result = script(project, code)
    assert summary_of(result)["outcome"] in ("crashed", "infrastructure error"), result.out


def test_a_second_sigterm_during_the_first_changes_nothing(project: Project) -> None:
    code = (
        "import os, signal\n"
        "from acme import tools\n"
        "tools.check_inventory('1')\n"
        "group = os.getpgrp()\n"
        "os.killpg(group, signal.SIGTERM)\n"
        "os.killpg(group, signal.SIGTERM)\n"
    )
    result = script(project, code)
    assert len(calls(result)) == 1, result.out


@pytest.mark.design("stop.handler_sends_then_ends")
def test_the_handler_sends_the_last_lines_then_ends_the_program_by_the_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In its own process the handler ends in a signal, which leaves no coverage data."""
    done: list[str] = []
    sent: list[int] = []

    class Sender:
        eager = False

        def flush(self) -> None:
            done.append("flush")

    class Run:
        def stop_open(self) -> None:
            done.append("stop_open")

    class Context:
        def close_open(self) -> None:
            done.append("close_open")

    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append(sig))
    before = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    try:
        bootstrap.handle_sigterm(Run(), Sender(), Context())  # type: ignore[arg-type]
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)
        assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL  # the second one kills
    finally:
        signal.signal(signal.SIGTERM, before)
    assert done == ["close_open", "stop_open", "flush"] and sent == [signal.SIGTERM]


def test_the_handler_ends_the_program_when_python_cannot_start_a_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[int] = []

    def no_thread(self: threading.Thread) -> None:
        raise RuntimeError("can't create new thread at interpreter shutdown")

    monkeypatch.setattr(threading.Thread, "start", no_thread)
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append(sig))
    before = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    try:
        bootstrap.handle_sigterm(None, None, None)  # type: ignore[arg-type]
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)
    finally:
        signal.signal(signal.SIGTERM, before)
    assert sent == [signal.SIGTERM]


def test_the_sdks_wait_is_shorter_than_the_runners() -> None:
    assert bootstrap.TERM_WAIT_S < runner.KILL_GRACE_S
    assert bootstrap.CHILD_TERM_WAIT_S < runner.KILL_GRACE_S


def test_stop_group_returns_when_the_program_left_its_group() -> None:
    code = "import os, time\nos.setpgid(0, os.getppid())\ntime.sleep(600)\n"
    proc = subprocess.Popen(
        [sys.executable, "-c", code], start_new_session=True, stdout=subprocess.DEVNULL
    )
    deadline = time.monotonic() + 10
    while os.getpgid(proc.pid) == proc.pid and time.monotonic() < deadline:
        time.sleep(0.01)
    stop_group(proc)
    assert proc.returncode is not None
