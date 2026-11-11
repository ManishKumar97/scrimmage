"""The result file: what a finished test leaves in its records folder, and who may read it."""

import json
import os
import signal
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from conftest import Project
from report_cases import AGENT, FAULT, TEST, load
from scrimmage_core import cli, config, entry, results, runner
from scrimmage_core.records import RecordStore, Refused, read_regular
from scrimmage_core.runner import Runner, row_json, row_reports

pytestmark = pytest.mark.usefixtures("signals_kept")


@pytest.fixture(autouse=True)
def nothing_pending() -> None:
    entry.pending.clear()


def no_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Runs start no program: the report is of a test with no result, which is still a report."""
    monkeypatch.setattr(Runner, "one", lambda self, row: None)


def folders(project: Project) -> list[Path]:
    return sorted(p for p in (project.root / "scrimmage-runs").iterdir() if p.is_dir())


def records(project: Project) -> Path:
    (folder,) = folders(project)
    return folder


def document(project: Project) -> dict[str, object]:
    return cast(dict[str, object], json.loads(project.capsys.readouterr().out))


def test_a_finished_test_writes_its_json_document_owner_only(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    no_runs(monkeypatch)
    before = os.umask(0)
    try:
        assert cli.main(["test", str(project.toml)]) == 2  # text form: it writes the file too
    finally:
        os.umask(before)
    file = records(project) / results.RESULT_FILE
    assert stat.S_IMODE(file.stat().st_mode) == 0o600
    doc = json.loads(file.read_text())
    assert doc["command"] == "test" and doc["v"] == 1 and doc["exit"] == 2
    assert not [p for p in file.parent.iterdir() if p.name.endswith(".tmp")]


def test_the_file_equals_the_json_document_byte_for_byte(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    no_runs(monkeypatch)
    cli.main(["test", "--json", str(project.toml)])
    printed = document(project)
    file = records(project) / results.RESULT_FILE
    assert printed.pop("result_file") == str(file)
    assert file.read_text() == json.dumps(printed, indent=2) + "\n"


def test_a_stopped_test_writes_no_result_file(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[bool] = []

    def one(self: Runner, row: object) -> None:
        if not sent:
            sent.append(True)
            os.kill(os.getpid(), signal.SIGTERM)

    monkeypatch.setattr(Runner, "one", one)
    assert cli.main(["test", "--json", str(project.toml)]) == 3  # a signal
    document(project)
    # The runner's own failure.
    monkeypatch.setattr(Runner, "run_all", failing)
    assert cli.main(["test", "--json", str(project.toml)]) == 3
    document(project)
    # The budget stopped the test: its rows have fewer runs than planned.
    monkeypatch.undo()
    monkeypatch.setattr(Runner, "one", stopped_by_budget)
    assert cli.main(["test", "--json", str(project.toml)]) == 2
    assert document(project)["result_file"] is None
    made = folders(project)
    assert len(made) == 3 and not [f for f in made if (f / results.RESULT_FILE).exists()]


def failing(self: Runner) -> list[runner.RunResult]:
    raise RuntimeError("the runner broke")


def stopped_by_budget(self: Runner, row: object) -> None:
    self.stopped_by = "budget"


def test_a_sigterm_during_the_report_writes_no_file(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    no_runs(monkeypatch)
    real = runner.build_report

    def building(*args: object, **kwargs: object) -> runner.Report:
        report = real(*args, **kwargs)  # type: ignore[arg-type]
        os.kill(os.getpid(), signal.SIGTERM)
        return report

    monkeypatch.setattr(runner, "build_report", building)
    assert cli.main(["test", "--json", str(project.toml)]) == 3
    assert document(project)["infra"] == [{"message": "stopped by SIGTERM"}]
    assert sorted(p.name for p in records(project).iterdir()) == []  # no file, no temp file


def test_a_failed_rename_leaves_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(OSError, match="No space"):
        results.write(tmp_path, {"v": 1})
    assert list(tmp_path.iterdir()) == []


def test_a_failed_write_is_a_warning_and_keeps_the_exit(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    no_runs(monkeypatch)
    expected = cli.main(["test", "--json", str(project.toml)])
    project.capsys.readouterr()

    def full(folder: Path, doc: dict[str, object]) -> Path:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(results, "write", full)
    assert cli.main(["test", "--json", str(project.toml)]) == expected
    doc = document(project)
    warnings = [w["message"] for w in cast(list[dict[str, str]], doc["warnings"])]
    assert doc["exit"] == expected and doc["result_file"] is None
    assert warnings == ["result.json was not written: No space left on device"]
    assert cli.main(["test", str(project.toml)]) == expected  # the text form says it on stderr
    assert "result.json was not written" in project.capsys.readouterr().err


def test_the_reader_refuses_a_symlink_another_owner_and_over_16_mb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    good = tmp_path / "good"
    good.mkdir()
    results.write(good, {"v": 1, "command": "test"})
    doc, problems = results.read(good)
    assert doc == {"v": 1, "command": "test"} and problems == []
    assert results.read(good / results.RESULT_FILE)[1] == []  # the file itself
    link = tmp_path / "link"
    link.mkdir()
    (link / results.RESULT_FILE).symlink_to(good / results.RESULT_FILE)
    assert "symbolic link" in results.read(link)[1][0]
    folder_link = tmp_path / "folder-link"
    folder_link.symlink_to(good, target_is_directory=True)
    assert "symbolic link" in results.read(folder_link)[1][0]
    big = tmp_path / "big"
    big.mkdir()
    with open(big / results.RESULT_FILE, "wb") as out:
        out.truncate(results.MAX_BYTES + 1)
    assert "is over" in results.read(big)[1][0]
    with open(big / results.RESULT_FILE, "wb") as out:
        out.truncate(results.MAX_BYTES)  # at the cap it is read, and then it is not JSON
    assert "cannot read" in results.read(big)[1][0]
    monkeypatch.setattr(os, "getuid", lambda: good.stat().st_uid + 1)
    assert "owned by another user" in results.read(good)[1][0]
    monkeypatch.undo()
    (good / results.RESULT_FILE).write_text('{"v": 2}')
    assert "version" in results.read(good)[1][0]
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(Refused):
        read_regular(fifo, 10)


def test_two_runners_in_one_second_get_two_folders(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Noon(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:  # type: ignore[override]
            return datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC)

    suffixes = iter(["aaaaaa", "aaaaaa", "bbbbbb"])
    monkeypatch.setattr(runner, "datetime", Noon)
    monkeypatch.setattr(runner, "folder_suffix", lambda: next(suffixes))
    cfg = config.load(project.toml)
    first, second = Runner(cfg), Runner(cfg)
    try:
        assert first.folder != second.folder
        assert first.folder.name == "20261111T120000Z-aaaaaa"
        assert second.folder.name == "20261111T120000Z-bbbbbb"
    finally:
        for made in (first, second):
            made.server.server.server_close()
    monkeypatch.setattr(runner, "folder_suffix", lambda: "aaaaaa")  # always taken
    with pytest.raises(RuntimeError, match="5 tries"):
        Runner(cfg)
    with pytest.raises(FileExistsError):
        RecordStore(first.folder)


def test_a_faults_own_rules_are_in_its_row() -> None:
    cfg = load(AGENT + TEST + FAULT + 'forbid = ["Oct 30"]\npass_rate = 0.5\n')
    agent = cfg.agent
    base, fault = row_reports(cfg, [])
    assert row_json(base, agent)["fault"] is None
    assert cast(dict[str, object], row_json(fault, agent)["fault"]) == {
        "call": "tool:get_order",
        "do": "timeout",
        "when": "always",
        "model": None,
        "method": None,
        "path": None,
        "raises": None,
        "expect": [["unable", "sorry"]],
        "forbid": ["Oct 30"],
        "pass_rate": 0.5,
    }
    plain = load(
        AGENT
        + TEST
        + '\n[[test.fault]]\ncall = "tool:get_order"\ndo = "timeout"\nwhen = "first 1"\n'
    )
    _, plain_fault = row_reports(plain, [])
    own = cast(dict[str, object], row_json(plain_fault, plain.agent)["fault"])
    assert (own["expect"], own["forbid"], own["pass_rate"]) == (None, None, None)


def hashes(text: str) -> tuple[str, str]:
    cfg = load(text)
    base, fault = row_reports(cfg, [])
    return (
        str(row_json(base, cfg.agent)["test_sha256"]),
        str(row_json(fault, cfg.agent)["test_sha256"]),
    )


SOURCE = AGENT + TEST + FAULT


@pytest.mark.parametrize(
    "old, new, changes",
    [
        ("Where is order 1234?", "Where is order 9999?", True),  # the question
        ('expect   = ["shipped", ', 'expect   = ["delivered", ', True),  # the test's rules
        ('forbid   = ["refunded"]', 'forbid   = ["refunded", "lost"]', True),
        ('expect = [["unable", "sorry"]]', 'expect = [["unable", "cannot"]]', True),  # a fault's
        ('returns = "12"', 'returns = "13"', True),  # a mock
        ("runs     = 3\n", "runs     = 3\nrun_timeout = 31\n", True),  # the time limit
        (
            'tools    = ["acme.tools:get_order", "acme.tools:get_stock"]',
            'tools    = ["acme.tools:get_stock", "acme.tools:get_order"]',  # the tools' order
            True,
        ),
        (
            'tools    = ["acme.tools:get_order", "acme.tools:get_stock"]',
            'tools    = ["acme.tools:get_order", "acme.tools:get_stock"]\n'
            'real     = ["tool:get_order"]',  # what may run for real
            True,
        ),
        # The agent's own code is what a comparison measures, so it is not in the hash.
        ('function = "acme.agent:answer"', 'function = "acme.better:answer"', False),
        ("runs     = 3\n", "runs     = 8\n", False),
    ],
)
def test_the_test_hash_changes_with_each_input_and_not_with_the_agents_code(
    old: str, new: str, changes: bool
) -> None:
    before = hashes(SOURCE)
    assert old in SOURCE
    after = hashes(SOURCE.replace(old, new))
    assert (after != before) is changes
    assert hashes(SOURCE) == before  # the same file always gives the same hash


def test_a_faults_own_rule_changes_its_row_hash_and_not_the_baselines() -> None:
    before = hashes(SOURCE)
    after = hashes(SOURCE.replace('expect = [["unable", "sorry"]]', 'expect = [["unable"]]'))
    assert after[0] == before[0] and after[1] != before[1]
    assert results.HASH_VERSION == 1


def test_the_open_flag_alone_refuses_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the look-before-open check blinded, O_NOFOLLOW still refuses a link."""
    target = tmp_path / "real.json"
    target.write_text("{}")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with monkeypatch.context() as patch:
        patch.setattr(os, "lstat", os.stat)  # a link now looks like the plain file it points at
        with pytest.raises((Refused, OSError)):
            read_regular(link, 100)
    assert read_regular(target, 100) == b"{}"
