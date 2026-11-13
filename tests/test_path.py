"""scrimmage path: the folder scrimmage-run puts first on PYTHONPATH, for a service that cannot
change its start command."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from scrimmage_core import cli, run_command
from test_service import BIN, Bench, bench

__all__ = ["bench"]


def path_output(capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = cli.main(["path"])
    out = capsys.readouterr()
    return code, out.out, out.err


def test_path_prints_the_boot_folder_alone(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = path_output(capsys)
    assert Path(out.strip()).name == "_boot", out
    assert code == 0 and err == ""
    lines = out.splitlines()
    assert len(lines) == 1 and out.endswith("\n"), out
    folder = Path(lines[0])
    assert folder.is_absolute() and folder.name == "_boot", folder
    assert (folder / "sitecustomize.py").is_file()
    assert [p.name for p in folder.iterdir() if p.name != "__pycache__"] == ["sitecustomize.py"]


@pytest.mark.slow
def test_path_is_what_scrimmage_run_puts_first(capsys: pytest.CaptureFixture[str]) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("SCRIMMAGE_")}
    env.pop("PYTHONPATH", None)  # no token, so nothing switches on
    done = subprocess.run(
        [
            str(BIN / "scrimmage-run"),
            sys.executable,
            "-c",
            "import os;print(os.environ['PYTHONPATH'])",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    _, out, _ = path_output(capsys)
    assert done.stdout.strip() == out.strip() != ""


def test_path_needs_no_token_and_reads_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in list(os.environ):
        if name.startswith("SCRIMMAGE_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)  # no scrimmage.toml here, and none is looked for
    opened: list[str] = []
    real_open = open

    def watch(file: object, *args: object, **kwargs: object) -> object:
        opened.append(str(file))
        return real_open(file, *args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr("builtins.open", watch)
    code, out, _ = path_output(capsys)
    assert code == 0 and out.strip()
    assert opened == [] and list(tmp_path.iterdir()) == []


@pytest.mark.slow
def test_a_service_started_with_pythonpath_by_hand_runs_a_test(
    bench: Bench, capsys: pytest.CaptureFixture[str]
) -> None:
    _, boot, _ = path_output(capsys)
    service = bench.start(bare=True, PYTHONPATH=boot.strip())
    unmocked = (
        '\n[[test]]\nname = "no-stock-mock"\nquestion = "Where is order 1234?"\n'
        'expect = ["shipped"]\n'
        '\n[[test.mock]]\ncall = "tool:get_order"\nreturns = "shipped, Oct 30"\n'
    )
    code, out = bench.test(service.port, runs=2, tests=unmocked)
    assert code == 2, out  # a refused call, so the gates are on in the hand-started service
    outcomes = [s.get("outcome") for n, s in bench.summaries().items() if n.startswith("run-")]
    assert outcomes.count("refused call") == 1, outcomes
    assert "answered" in outcomes, outcomes
    assert len([n for n in bench.summaries() if n.startswith("setup-")]) == 1


@pytest.mark.slow
def test_a_wrong_pythonpath_stops_the_probe_naming_scrimmage_path(
    bench: Bench, tmp_path: Path
) -> None:
    wrong = tmp_path / "not-the-boot-folder"
    wrong.mkdir()
    service = bench.start(wait=False, bare=True, PYTHONPATH=str(wrong))
    assert service.ready.wait(60), service.text
    code, out = bench.test(service.port)
    assert code == 3, out
    assert "x-scrimmage-run does not reach Scrimmage" in out
    assert "PYTHONPATH beginning with the folder scrimmage path prints" in out


def test_path_on_a_folder_that_is_not_on_disk_is_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gone = tmp_path / "wheel.zip" / "scrimmage_core" / "_boot"
    monkeypatch.setattr(run_command, "BOOT", gone)
    code, out, err = path_output(capsys)
    assert code == 2 and out == ""
    assert f"scrimmage path: {gone} is not a folder on disk" in err
    assert "install scrimmage-core from its wheel" in err
