"""faultline test on Acme's bot, a function agent, against the fake OpenAI server."""

import json
import stat
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
