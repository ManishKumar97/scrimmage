"""faultline test on Acme's bot, a function agent, against the fake OpenAI server."""

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

import faultline_sdk
from conftest import ACME, Project
from fakes.openai_server import FakeOpenAI
from faultline_sdk import config
from faultline_sdk.runner import Runner, report

Fake = Callable[[list[str]], FakeOpenAI]
RECORD = json.loads(
    (Path(faultline_sdk.__file__).parent / "schemas" / "record.v1.json").read_text()
)
FAULT_BLOCK = '[[test.fault]]\ncall = "tool:get_order"\ndo   = "timeout"\n'


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
        assert [(x["call"], x["action"]) for x in lines[:-1]] == [
            ("tool:get_order", "fault"),
            ("tool:get_stock", "mock"),
        ]
        assert lines[-1]["summary"]["fault_fired"] is True  # type: ignore[index]
    for lines in by_row["baseline"]:
        assert [x["action"] for x in lines[:-1]] == ["mock", "mock"]
        assert lines[-1]["summary"]["outcome"] == "answered"  # type: ignore[index]
    assert "order-status  fault 1   tool:get_order timeout  3     3" in result.out


@pytest.mark.design("record.owner_only_files")
def test_every_record_file_is_owner_only(project: Project, fake_openai: Fake) -> None:
    project.test(fake_openai(ACME))
    files = list((project.root / "faultline-runs").glob("*/*"))
    assert files and all(stat.S_IMODE(f.stat().st_mode) == 0o600 for f in files)


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
    # parallel = 1, so after the first refusal no more runs of that row start.
    assert len(summaries) == 2 and {s["outcome"] for s in summaries} == {"refused call"}
    refused = [x for lines in result.records() for x in lines if x.get("action") == "refused"]
    assert refused and refused[0]["call"] == "tool:refund_order"
    assert "made a call the test does not allow" in result.out
    assert not fake.errors


@pytest.mark.design("runner.timed_out")
def test_a_hanging_run_ends_as_timed_out(project: Project) -> None:
    project.edit(
        'function = "acme.agent:answer"',
        'function = "acme.slow:hang"\nrun_timeout = 1\nruns = 1',
    )
    project.edit(FAULT_BLOCK, "")
    result = project.test()
    assert result.code == 0, result.out
    assert [s["outcome"] for s in result.summaries()] == ["timed out"]


def test_an_agent_that_raises_has_crashed(project: Project) -> None:
    project.edit('function = "acme.agent:answer"', 'function = "acme.slow:crash"\nruns = 1')
    project.edit(FAULT_BLOCK, "")
    result = project.test()
    assert (result.code, [s["outcome"] for s in result.summaries()]) == (0, ["crashed"])
    log = next((project.root / "faultline-runs").glob("*/*.log")).read_text()
    assert "the bot crashed" in log


@pytest.mark.design("runner.not_exercised")
def test_a_fault_that_never_fires_is_a_test_mistake(project: Project, fake_openai: Fake) -> None:
    project.edit("[agent]\n", "[agent]\nruns = 2\n")
    result = project.test(fake_openai(["acme_answer"]))  # the model never asks for get_order
    assert result.code == 2
    assert "order-status  fault 1   tool:get_order timeout  2     0          0" in result.out
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
    assert not (project.root / "faultline-runs").exists()


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
    assert "infrastructure error: see the runs" in out.getvalue()


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
    project.edit('function = "acme.agent:answer"', f'function = "{function}"\nruns = 1{extra}')
    project.edit(FAULT_BLOCK, "")


@pytest.mark.design("runner.refused_kills_process")
def test_a_refused_run_is_stopped_even_if_the_agent_carries_on(project: Project) -> None:
    only_baseline(project, "acme.slow:refuse_then_hang", "\nrun_timeout = 60")
    started = time.monotonic()
    result = project.test()
    assert time.monotonic() - started < 30  # stopped at the refusal, not at run_timeout
    assert result.code == 2 and [s["outcome"] for s in result.summaries()] == ["refused call"]


@pytest.mark.design("runner.sigkill_after_grace")
def test_an_agent_that_ignores_sigterm_is_killed(project: Project) -> None:
    only_baseline(project, "acme.slow:stubborn", "\nrun_timeout = 1")
    started = time.monotonic()
    result = project.test()
    assert [s["outcome"] for s in result.summaries()] == ["timed out"]
    assert time.monotonic() - started < 20  # 1 s, then SIGTERM, then SIGKILL 5 s later


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
def test_stopping_faultline_test_stops_every_run(project: Project) -> None:
    only_baseline(project, "acme.slow:hang", "\nrun_timeout = 120")
    env = {k: v for k, v in os.environ.items() if not k.startswith("FAULTLINE_")}
    runner = subprocess.Popen(
        [sys.executable, "-m", "faultline_sdk.cli", "test", str(project.toml)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )  # fmt: skip
    pid = None
    for _ in range(100):
        time.sleep(0.1)
        pid = agent_pid(project.root)
        if pid:
            break
    assert pid, "the agent never started"
    runner.send_signal(signal.SIGTERM)
    _, err = runner.communicate(timeout=30)
    assert runner.returncode == 3 and "stopped by SIGTERM" in err
    time.sleep(0.5)
    assert not alive(pid), "the agent outlived faultline test"


def agent_pid(root: Path) -> int | None:
    for proc in Path("/proc").iterdir():
        if proc.name.isdigit():
            try:
                cmd = (proc / "cmdline").read_bytes().split(b"\0")
            except OSError:
                continue
            if b"acme.slow:hang" in cmd and (proc / "cwd").resolve() == root:
                return int(proc.name)
    return None


def alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().split()[2]
    except OSError:
        return False
    return state != "Z"


def test_a_run_that_dropped_lines_is_an_infrastructure_error(
    project: Project, fake_openai: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    from faultline_sdk.control import ControlServer

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
    from faultline_sdk.control import ControlServer

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


def test_a_stale_root_pid_in_the_runners_environment_is_ignored(
    project: Project, fake_openai: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAULTLINE_ROOT_PID", "1")
    project.edit("[agent]\n", "[agent]\nruns = 1\n")
    result = project.test(fake_openai(ACME))
    assert result.code == 0 and "  1     1  " in result.out  # the fault row was exercised
