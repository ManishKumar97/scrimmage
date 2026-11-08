"""scrimmage test on Acme's bot, a function agent, against the fake OpenAI server."""

import json
import os
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import jsonschema
import pytest

import scrimmage_core
from conftest import ACME, EXAMPLE_FAULT, Project, TimeLimit
from fakes.openai_server import FakeOpenAI
from scrimmage_core import config
from scrimmage_core import runner as runner_module
from scrimmage_core.runner import INFRA_LINE, Runner, report

Fake = Callable[[list[str]], FakeOpenAI]
RECORD = json.loads(
    (Path(scrimmage_core.__file__).parent / "schemas" / "record.v1.json").read_text()
)
FAULT_BLOCK = EXAMPLE_FAULT


@pytest.mark.design("runner.example_end_to_end")
def test_the_example_runs_six_times_and_the_fault_fires(
    project: Project, fake_openai: Fake
) -> None:
    result = project.test(fake_openai(ACME))
    assert result.code == 0, result.out + result.err
    records = result.records()
    assert len(records) == 6
    validator = jsonschema.Draft202012Validator(RECORD, format_checker=jsonschema.FormatChecker())
    for lines in records:
        for line in lines:
            validator.validate(line)
    by_row: dict[object, list[list[dict[str, object]]]] = {}
    for lines in records:
        by_row.setdefault(lines[-1]["row"], []).append(lines)
    for lines in by_row["fault 1"]:
        assert [(x["call"], x["action"]) for x in lines[:-1] if x.get("kind") == "tool"] == [
            ("tool:get_order", "fault"),
            ("tool:get_stock", "mock"),
        ]
        assert lines[-1]["summary"]["fault_fired"] is True  # type: ignore[index]
    for lines in by_row["baseline"]:
        assert [x["action"] for x in lines[:-1] if x.get("kind") == "tool"] == ["mock", "mock"]
        assert lines[-1]["summary"]["outcome"] == "answered"  # type: ignore[index]
    assert "order-status  fault 1   tool:get_order timeout  3     3" in result.out


@pytest.mark.design("record.owner_only_files")
def test_every_record_file_is_owner_only(project: Project, fake_openai: Fake) -> None:
    project.test(fake_openai(ACME))
    found = list((project.root / "scrimmage-runs").rglob("*"))
    files = [f for f in found if f.is_file() and f.name != ".gitignore"]  # holds no data
    folders = [f for f in found if f.is_dir()]
    assert files and all(stat.S_IMODE(f.stat().st_mode) == 0o600 for f in files)
    assert all(stat.S_IMODE(f.stat().st_mode) == 0o700 for f in folders)
    assert any(f.parent.name == "blobs" for f in files)  # model requests and replies


@pytest.mark.design("runner.refused_call")
def test_an_unlisted_write_tool_ends_its_run_as_refused(project: Project) -> None:
    project.edit("[agent]\n", "[agent]\nparallel = 1\n")
    fake = FakeOpenAI(["acme_refund_order", "acme_answer"])  # the answer may never be asked for
    fake.start()
    try:
        result = project.test(fake)
    finally:
        fake.stop()
    assert result.code == 2, result.out
    summaries = result.summaries()
    # parallel = 1, so after the first refusal no more runs of that row start, and a
    # baseline that only refused leaves nothing to compare a fault row with.
    assert len(summaries) == 1 and {s["outcome"] for s in summaries} == {"refused call"}
    assert "baseline failed" in result.out
    refused = [x for lines in result.records() for x in lines if x.get("action") == "refused"]
    assert refused and refused[0]["call"] == "tool:refund_order"
    assert "made a call the test does not allow" in result.out
    assert not fake.errors


@pytest.mark.design("runner.timed_out")
def test_a_hanging_run_ends_as_timed_out(project: Project, time_limit: TimeLimit) -> None:
    project.edit(
        'function = "acme.agent:answer"',
        'function = "acme.slow:hang"\nruns = 1\nmax_cost_usd = "none"',
    )
    project.edit(FAULT_BLOCK, "")
    time_limit.at((project.root / "hanging").exists)
    result = project.test()
    assert result.code == 1, result.out
    assert [s["outcome"] for s in result.summaries()] == ["timed out"]


def test_the_time_limit_is_a_wait_on_the_program() -> None:
    """The real wait, which the tests of a run's outcome replace with an event."""
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    try:
        assert runner_module.wait_run(sleeper, 0.1)  # a 600 s sleep is going after 0.1 s
    finally:
        sleeper.kill()
        sleeper.wait()
    done = subprocess.Popen([sys.executable, "-c", "pass"])
    assert not runner_module.wait_run(done, 120)


def test_an_agent_that_raises_has_crashed(project: Project) -> None:
    project.edit(
        'function = "acme.agent:answer"',
        'function = "acme.slow:crash"\nruns = 1\nmax_cost_usd = "none"',
    )
    project.edit(FAULT_BLOCK, "")
    result = project.test()
    assert (result.code, [s["outcome"] for s in result.summaries()]) == (1, ["crashed"])
    log = next((project.root / "scrimmage-runs").glob("*/*.log")).read_text()
    assert "the bot crashed" in log


@pytest.mark.design("runner.not_exercised")
def test_a_fault_that_never_fires_is_a_test_mistake(project: Project, fake_openai: Fake) -> None:
    project.edit("[agent]\n", "[agent]\nruns = 2\n")
    result = project.test(fake_openai(["acme_answer"]))  # the model never asks for get_order
    assert result.code == 2
    assert "order-status  fault 1   tool:get_order timeout  2     0      2     0/0" in result.out
    assert "the fault never fired in any run" in result.out
    assert {s["fault_fired"] for s in result.summaries()} == {None, False}


@pytest.mark.design("runner.label_check_first")
def test_a_misspelled_label_stops_the_test_before_any_model_call(
    project: Project, fake_openai: Fake
) -> None:
    project.edit('call    = "tool:get_order"', 'call    = "tool:get_ordr"')
    fake = FakeOpenAI(ACME)
    fake.start()
    try:
        result = project.test(fake)
    finally:
        fake.stop()
    assert result.code == 2 and fake.requests == []
    assert "tool:get_ordr names no tool in [agent] tools" in result.err
    assert "get_order, check_inventory, refund_order, get_stock" in result.err
    assert not (project.root / "scrimmage-runs").exists()


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (
            'real     = ["tool:check_inventory"]',
            'real = ["tool:check_inventry"]',
            "real: tool:check_inventry names no tool",
        ),
        (
            'call    = "tool:get_stock"',
            'call    = "tool:get_stock"\ntype    = "other.models:Stock"',
            "outside the agent's packages",
        ),
        ("[agent]\n", "[agent]\nruns = 0\n", "runs: expected a whole number"),
    ],
)
def test_other_mistakes_found_before_the_first_run(
    project: Project, old: str, new: str, message: str
) -> None:
    project.edit(old, new)
    result = project.test()
    assert result.code == 2 and message in result.err


def test_a_missing_file_is_a_mistake(project: Project) -> None:
    project.toml.unlink()
    assert project.test().code == 2


@pytest.mark.design("runner.unreachable_control")
def test_an_unreachable_control_server_is_an_infrastructure_error(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    project.edit("[agent]\n", "[agent]\nruns = 1\n")
    cfg = config.load(project.toml)
    runner = Runner(cfg, control_url=lambda url: "http://127.0.0.1:9")
    import io

    out = io.StringIO()
    code = report(cfg, runner.run_all(), runner.folder, out)
    assert code == 3, out.getvalue()
    assert f"infrastructure error: {INFRA_LINE}" in out.getvalue()


@pytest.mark.design("runner.real_all_header")
def test_the_table_says_so_when_every_tool_runs_for_real(
    project: Project, fake_openai: Fake
) -> None:
    project.edit('real     = ["tool:check_inventory"]', 'real = ["tool:*"]\nruns = 1')
    result = project.test(fake_openai(ACME))
    assert 'every tool that is not mocked runs for real (real = ["tool:*"])' in result.out


@pytest.mark.design("runner.mock_misfit_reported")
def test_a_mock_that_does_not_fit_its_type_is_a_test_mistake(
    project: Project, fake_openai: Fake
) -> None:
    project.edit('returns = {sku = "SKU-42", count = 12}', 'returns = {sku = "SKU-42"}')
    project.edit("[agent]\n", "[agent]\nruns = 1\n")
    result = project.test(fake_openai(ACME))
    assert result.code == 2
    assert "the mock does not fit acme.models:Stock" in result.out


def only_baseline(project: Project, function: str, extra: str = "") -> None:
    """An agent that makes no model call, so no budget either."""
    project.edit(
        'function = "acme.agent:answer"',
        f'function = "{function}"\nruns = 1\nmax_cost_usd = "none"{extra}',
    )
    project.edit(FAULT_BLOCK, "")


@pytest.mark.design("runner.refused_kills_process")
def test_a_refused_run_is_stopped_even_if_the_agent_carries_on(
    project: Project, time_limit: TimeLimit
) -> None:
    only_baseline(project, "acme.slow:refuse_then_hang")
    result = project.test()
    assert time_limit.reached == [False]  # stopped at the refusal, not at run_timeout
    assert result.code == 2 and [s["outcome"] for s in result.summaries()] == ["refused call"]


@pytest.mark.design("runner.sigkill_after_grace")
def test_an_agent_that_ignores_sigterm_is_killed(
    project: Project, time_limit: TimeLimit, monkeypatch: pytest.MonkeyPatch
) -> None:
    only_baseline(project, "acme.slow:stubborn")
    monkeypatch.setattr(runner_module, "KILL_GRACE_S", 1.0)
    time_limit.at((project.root / "ignoring").exists)  # SIGTERM is ignored from here on
    result = project.test()
    assert [s["outcome"] for s in result.summaries()] == ["timed out"]  # so SIGKILL ended it
    assert not alive(int((project.root / "ignoring").read_text()))


@pytest.mark.design("record.gap_is_infrastructure")
def test_a_run_whose_last_batch_never_came_is_an_infrastructure_error(project: Project) -> None:
    only_baseline(project, "acme.slow:vanish")
    result = project.test()
    assert result.code == 3
    (summary,) = result.summaries()
    assert summary["outcome"] == "infrastructure error" and summary["incomplete"] is True


@pytest.mark.design("runner.no_shell")
def test_the_question_reaches_the_agent_as_one_piece_of_text(project: Project) -> None:
    only_baseline(project, "acme.slow:echo")
    project.edit(
        'question = "Where is order 1234?"', 'question = "Where is order 1234; rm -rf ~ $(id)"'
    )
    result = project.test()
    assert [s["answer"] for s in result.summaries()] == ["Where is order 1234; rm -rf ~ $(id)"]


@pytest.mark.design("tools.listed_missing_stops_run")
@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ('"acme.tools:refund_ordr"', "acme.tools has no function refund_ordr"),
        (
            '"acme.toolz:refund_order"',
            "acme.toolz is listed under [agent] tools but cannot be found",
        ),
    ],
)
def test_a_listed_tool_that_does_not_exist_ends_the_run_as_a_mistake(
    project: Project, fake_openai: Fake, entry: str, message: str
) -> None:
    project.edit('"acme.tools:refund_order"', entry)
    project.edit("[agent]\n", "[agent]\nruns = 1\nparallel = 1\n")
    fake = FakeOpenAI(ACME)
    fake.start()
    try:
        result = project.test(fake)
    finally:
        fake.stop()
    assert result.code == 2, result.out
    assert message in result.out
    assert {s["outcome"] for s in result.summaries()} == {"refused call"}


def test_a_failure_inside_the_runner_is_an_infrastructure_error(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(self: Runner) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(Runner, "run_all", broken)
    result = project.test()
    assert result.code == 3 and "infrastructure error in the runner" in result.err


@pytest.mark.design("runner.stops_runs_when_stopped")
def test_stopping_scrimmage_test_stops_every_run(project: Project) -> None:
    only_baseline(project, "acme.slow:hang", "\nrun_timeout = 120")
    env = {k: v for k, v in os.environ.items() if not k.startswith("SCRIMMAGE_")}
    runner = subprocess.Popen(
        [sys.executable, "-m", "scrimmage_core.cli", "test", str(project.toml)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )  # fmt: skip
    pid = wait_for_pid(project.root / "hanging")
    runner.send_signal(signal.SIGTERM)
    _, err = runner.communicate(timeout=60)
    assert runner.returncode == 3 and "stopped by SIGTERM" in err
    assert not alive(pid), "the agent outlived scrimmage test"


def wait_for_pid(path: Path) -> int:
    """The pid an agent wrote once it was going; a ceiling, not a deadline, ends a hang."""
    ceiling = time.monotonic() + 60
    while not path.exists():
        assert time.monotonic() < ceiling, "the agent never started"
        time.sleep(0.01)
    return int(path.read_text())


def alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().split()[2]
    except OSError:
        return False
    return state != "Z"


def test_a_run_that_dropped_lines_is_an_infrastructure_error(
    project: Project, fake_openai: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scrimmage_core.control import ControlServer

    original = ControlServer.close_run

    def dropped_one(self: ControlServer, run: str):  # type: ignore[no-untyped-def]
        state = original(self, run)
        state.dropped = 1  # as if the program's queue had been full once
        return state

    monkeypatch.setattr(ControlServer, "close_run", dropped_one)
    project.edit("[agent]\n", "[agent]\nruns = 1\n")
    result = project.test(fake_openai(ACME))
    assert result.code == 3
    assert {s["outcome"] for s in result.summaries()} == {"infrastructure error"}


@pytest.mark.design("record.gap_per_process")
def test_a_process_that_made_more_lines_than_arrived_is_an_infrastructure_error(
    project: Project, fake_openai: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scrimmage_core.control import ControlServer

    original = ControlServer.close_run

    def one_line_missing(self: ControlServer, run: str):  # type: ignore[no-untyped-def]
        state = original(self, run)
        for proc in state.made:
            state.made[proc] += 1  # as if one line from this process never arrived
        return state

    monkeypatch.setattr(ControlServer, "close_run", one_line_missing)
    project.edit("[agent]\n", "[agent]\nruns = 1\n")
    result = project.test(fake_openai(ACME))
    assert result.code == 3
    assert {s["incomplete"] for s in result.summaries()} == {True}


def test_stale_run_variables_in_the_runners_environment_are_ignored(
    project: Project, fake_openai: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCRIMMAGE_ROOT_PID", "1")
    monkeypatch.setenv("SCRIMMAGE_PYTHON", "a Python from another run")
    monkeypatch.setenv("SCRIMMAGE_CHILDREN", "1")
    project.edit("[agent]\n", "[agent]\nruns = 1\n")
    result = project.test(fake_openai(ACME))
    assert result.code == 0 and "  1     1  " in result.out  # the fault row was exercised


@pytest.mark.design("config.unknown_rejected_exit_2")
@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("[agent]\n", "[agent]\nrnus = 50\n"),
        ("[agent]\n", '[agent]\nservice = "x"\n'),
        ('do   = "timeout"', 'do   = "status 500"'),
        ('do   = "timeout"', 'do   = "timeout"\nwhen = "once"'),
        ('call = "tool:get_order"\ndo', 'call = "model:x"\ndo'),
        ('returns = "shipped, Oct 30"', 'returns = "${ORDER_STATUS}"'),
    ],
)
def test_scrimmage_test_exits_2_on_anything_unsupported(
    project: Project, old: str, new: str
) -> None:
    project.edit(old, new)
    result = project.test()
    assert result.code == 2 and "scrimmage:" in result.err


@pytest.mark.design("config.unreadable_file")
@pytest.mark.parametrize("kind", ["folder", "not utf-8", "no access", "missing"])
def test_a_config_file_that_cannot_be_read_is_a_mistake(project: Project, kind: str) -> None:
    project.toml.unlink()
    if kind == "folder":
        project.toml.mkdir()
    elif kind == "not utf-8":
        project.toml.write_bytes(b"\xff\xfe[agent]")
    elif kind == "no access":
        project.toml.write_text("[agent]")
        project.toml.chmod(0o000)
    result = project.test()
    if kind != "missing":
        project.toml.chmod(0o700)
    assert result.code == 2 and "scrimmage: scrimmage.toml:" in result.err


@pytest.mark.design("record.ignored_by_git")
def test_records_stay_out_of_the_teams_git_repo(project: Project, fake_openai: Fake) -> None:
    project.edit("[agent]\n", "[agent]\nruns = 1\n")
    project.test(fake_openai(ACME))
    subprocess.run(["git", "init", "-q", str(project.root)], check=True)
    status = subprocess.run(
        ["git", "-C", str(project.root), "status", "--short"],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    assert "scrimmage-runs" not in status.stdout


@pytest.mark.design("report.not_exercised_says_why")
def test_the_table_says_how_runs_ended_before_the_fault_fired(
    project: Project, fake_openai: Fake
) -> None:
    project.edit('call = "tool:get_order"', 'call = "tool:refund_order"')
    project.edit("[agent]\n", "[agent]\nruns = 2\n")
    result = project.test(fake_openai(ACME * 4))
    assert (
        "note: order-status fault 1: not exercised, the fault did not fire: 2 answered"
        in result.out
    )


@pytest.mark.design("tools.model_name_differs")
def test_a_tool_can_carry_the_name_the_model_sees(project: Project, fake_openai: Fake) -> None:
    project.edit('"acme.tools:get_order"', '{path = "acme.tools:get_order", name = "lookup-order"}')
    project.edit('call    = "tool:get_order"', 'call    = "tool:lookup-order"')
    project.edit('call = "tool:get_order"', 'call = "tool:lookup-order"')
    project.edit('function = "acme.agent:answer"', 'function = "acme.rag:answer_renamed"\nruns = 1')
    result = project.test(fake_openai(["chat_answer", "chat_answer"]))
    assert result.finished, result.out + result.err
    fault = next(lines for lines in result.records() if lines[-1]["row"] == "fault 1")
    tool = next(x for x in fault if x.get("call") == "tool:lookup-order")
    assert tool["action"] == "fault"
    schema = json.loads(
        (Path(scrimmage_core.__file__).parent / "schemas" / "record.v1.json").read_text()
    )
    jsonschema.Draft202012Validator(schema).validate(tool)


@pytest.mark.design("config.references_checked_on_load")
@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ('call    = "tool:get_order"', 'call    = "tool:get_ordr"', "tool:get_ordr names no tool"),
        ('call = "tool:get_order"', 'call = "tool:get_ordr"', "tool:get_ordr names no tool"),
        (
            'real     = ["tool:check_inventory"]',
            'real = ["tool:check_inventry"]',
            "real: tool:check_inventry names no tool",
        ),
        (
            'call    = "tool:get_stock"',
            'call    = "tool:get_stock"\ntype    = "other.models:Stock"',
            "outside the agent's packages",
        ),
    ],
)
def test_a_bad_reference_stops_the_test_before_any_run(
    project: Project, old: str, new: str, message: str
) -> None:
    project.edit(old, new)
    result = project.test()
    assert result.code == 2 and message in result.err
    assert "the test stopped before the first run" in result.err
    assert not (project.root / "scrimmage-runs").exists()


def test_every_bad_reference_shows_on_its_own_line_in_one_run(project: Project) -> None:
    project.edit('call    = "tool:get_order"', 'call    = "tool:get_ordr"')
    project.edit('real     = ["tool:check_inventory"]', 'real = ["tool:check_inventry"]')
    lines = project.test().err.splitlines()
    assert any("tool:get_ordr names no tool" in line for line in lines)
    assert any("tool:check_inventry names no tool" in line for line in lines)
    assert all(line.startswith("scrimmage: ") for line in lines)


@pytest.mark.design("boot.fork_no_faults")
def test_the_program_drops_faults_in_a_forked_child(project: Project) -> None:
    project.edit(
        'function = "acme.agent:answer"',
        'function = "acme.rag:answer_forked"\nruns = 1\nparallel = 1',
    )
    result = project.test()
    assert any(lines[-1]["row"] == "fault 1" for lines in result.records()), result.out
    seen = (project.root / "forked-child").read_text().splitlines()
    assert seen == ["shipped, Oct 30", "shipped, Oct 30"]  # the mock, in both rows
