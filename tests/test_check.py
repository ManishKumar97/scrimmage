"""scrimmage check: the checks scrimmage test makes before its first run, and nothing else."""

import shutil
import subprocess
from pathlib import Path

import pytest

from config_cases import BAD, GOOD
from conftest import EXAMPLE, Project
from scrimmage import cli, config, control, outcomes, prices, runner

TESTS = Path(__file__).parent
pytestmark = pytest.mark.usefixtures("signals_kept")


class Reached(Exception):
    """scrimmage test got past the file and was about to start its runner."""


def fixtures() -> dict[str, str]:
    """Every scrimmage.toml under tests/, and every good and bad case the schema tests use."""
    files = {
        str(path.relative_to(TESTS)): path.read_text()
        for path in sorted(TESTS.rglob("*.toml"))
        if "fixtures" not in path.parts
    }
    assert "example/scrimmage.toml" in files
    return {**files, **GOOD, **{bad.name: bad.text for bad in BAD}}


def problems(err: str) -> list[str]:
    stopped = "scrimmage: the test stopped before the first run"
    return [line for line in err.splitlines() if line != stopped]


@pytest.mark.design("check.same_as_test")
def test_check_passes_exactly_when_test_gets_past_the_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def reached(self: runner.Runner, cfg: config.Config, **kwargs: object) -> None:
        raise Reached

    monkeypatch.setattr(runner.Runner, "__init__", reached)
    root = tmp_path / "acme-bot"
    shutil.copytree(EXAMPLE, root, ignore=shutil.ignore_patterns("__pycache__"))
    (root / "bot.py").chmod(0o755)
    cases = fixtures()
    assert len(cases) > 90
    for name, text in cases.items():
        path = root / "scrimmage.toml"
        path.write_text(text)
        capsys.readouterr()
        checked = cli.main(["check", str(path)])
        check = capsys.readouterr()
        tested = cli.main(["test", str(path)])
        test = capsys.readouterr()
        got_past = tested == outcomes.INFRA and "Reached" in test.err
        assert (checked == outcomes.OK) == got_past, (name, check, test)
        assert checked in (outcomes.OK, outcomes.MISTAKE), name
        if checked == outcomes.OK:
            assert check.err == "" and check.out.startswith("scrimmage check: "), name
        else:
            assert problems(check.err) == problems(test.err), name
            count = len(problems(check.err))
            assert count >= 1, name
            word = "problem" if count == 1 else "problems"
            assert check.out == f"scrimmage check: {count} {word}; nothing was run\n", name


@pytest.mark.design("check.runs_nothing")
def test_check_starts_no_process_server_or_records_folder(
    project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("scrimmage check started something")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(control.ControlServer, "start", refuse)
    monkeypatch.setattr(control.ControlServer, "__init__", refuse)
    assert cli.main(["check", str(project.toml)]) == outcomes.OK
    assert cli.main(["check", "--json", str(project.toml)]) == outcomes.OK
    assert not (project.root / "scrimmage-runs").exists()
    project.edit('function = "acme.agent:answer"', 'command = "python3 bot.py {question}"')
    assert cli.main(["check", str(project.toml)]) == outcomes.OK, capsys.readouterr()
    assert not (project.root / "scrimmage-runs").exists()


@pytest.mark.design("check.bad_toml")
@pytest.mark.parametrize(
    ("content", "says"),
    [
        (b"[agent\nfunction = 1\n", "scrimmage.toml: Expected ']'"),
        (b'[agent]\nfunction = "\xff"\n', "scrimmage.toml: the file is not UTF-8 text"),
        (None, "scrimmage.toml: cannot read it: No such file or directory"),
    ],
    ids=["not_toml", "not_utf8", "missing"],
)
def test_a_file_that_cannot_be_read_is_one_problem(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], content: bytes | None, says: str
) -> None:
    path = tmp_path / "scrimmage.toml"
    if content is not None:
        path.write_bytes(content)
    assert cli.main(["check", str(path)]) == outcomes.MISTAKE
    captured = capsys.readouterr()
    lines = captured.err.splitlines()
    assert len(lines) == 1 and lines[0].startswith(f"scrimmage: {says}"), lines
    assert captured.out == "scrimmage check: 1 problem; nothing was run\n"


def test_a_valid_file_gets_one_line_with_what_it_holds(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["check", str(project.toml)]) == outcomes.OK
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == (
        f"scrimmage check: {project.toml} is valid: 1 test, 2 rows, 6 runs, 4 tools, "
        f"a budget of $5, OpenAI prices as of {dict(prices.shipped().sections)['openai'].as_of}\n"
    )
    project.edit("[agent]\n", '[agent]\nmax_cost_usd = "none"\nruns = 1\n')
    project.edit(
        'tools    = ["acme.tools:get_order", "acme.tools:check_inventory", '
        '"acme.tools:refund_order", "acme.tools:get_stock"]\n',
        "",
    )
    assert cli.main(["check", str(project.toml)]) == outcomes.OK
    assert capsys.readouterr().out.endswith("1 test, 2 rows, 2 runs, no tools list, no budget\n")


def test_check_reads_scrimmage_toml_in_the_current_folder_by_default(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    project.monkeypatch.chdir(project.root)
    assert cli.main(["check"]) == outcomes.OK
    assert capsys.readouterr().out.startswith("scrimmage check: scrimmage.toml is valid: ")


def test_a_failure_of_scrimmage_itself_is_exit_3_in_text_too(
    project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(path: Path) -> config.Config:
        raise RuntimeError("the reader broke")

    monkeypatch.setattr(config, "load", boom)
    assert cli.main(["check", str(project.toml)]) == outcomes.INFRA
    captured = capsys.readouterr()
    broke = "RuntimeError('the reader broke')"
    assert f"scrimmage: infrastructure error in scrimmage check: {broke}" in captured.err
    assert captured.out == "scrimmage check: the file was not checked; nothing was run\n"
