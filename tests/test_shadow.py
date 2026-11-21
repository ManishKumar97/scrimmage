"""A team's own module named scrimmage must not stand in for Scrimmage, or run ungated."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

import scrimmage as installed
from scrimmage import cli, shadow

SIDE_EFFECT = 'open("SIDE_EFFECT", "w").write("ran")\n'


def team(tmp_path: Path) -> Path:
    """A function agent that needs no model, and a scrimmage.py in its folder."""
    root = tmp_path / "team"
    root.mkdir()
    (root / "plain.py").write_text('def run(q):\n    return "hello " + q\n')
    (root / "scrimmage.py").write_text(SIDE_EFFECT)
    (root / "scrimmage.toml").write_text(
        '[agent]\nfunction = "plain:run"\nmax_cost_usd = "none"\n\n'
        '[[test]]\nname = "t"\nquestion = "hi"\nexpect = ["hello"]\n'
    )
    return root


def scrimmage(
    root: Path, command: str, cwd: Path | None = None, **env: str
) -> subprocess.CompletedProcess[str]:
    """scrimmage COMMAND for root's file, with -P so that the test's own folder never shadows it."""
    clean = {
        k: v for k, v in os.environ.items() if not k.startswith("SCRIMMAGE_") and k != "PYTHONPATH"
    }
    return subprocess.run(
        [sys.executable, "-P", "-m", "scrimmage.cli", command, str(root / "scrimmage.toml")],
        cwd=cwd or root, env={**clean, **env}, capture_output=True, text=True, timeout=120,
    )  # fmt: skip


@pytest.mark.slow
def test_a_scrimmage_py_in_the_agents_folder_is_not_run(tmp_path: Path) -> None:
    root = team(tmp_path)
    done = scrimmage(root, "test")
    assert done.returncode == 0, done.stdout + done.stderr
    assert not (root / "SIDE_EFFECT").exists(), "the team's scrimmage.py ran"


@pytest.mark.parametrize("command", ["check", "test"])
@pytest.mark.usefixtures("signals_kept")
def test_a_scrimmage_py_on_pythonpath_is_refused_before_any_run(
    tmp_path: Path,
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = team(tmp_path)
    (root / "scrimmage.py").unlink()
    libs = tmp_path / "libs"
    libs.mkdir()
    (libs / "scrimmage.py").write_text(SIDE_EFFECT)
    for folder in (str(libs), "../libs"):  # a relative folder means from the agent's own
        monkeypatch.setenv("PYTHONPATH", folder)
        code = cli.main([command, str(root / "scrimmage.toml")])
        err = capsys.readouterr().err
        assert code == 2, err
        assert "libs/scrimmage.py would be imported as scrimmage" in err
        assert "Rename it, or remove its folder from PYTHONPATH" in err
        assert not (root / "SIDE_EFFECT").exists() and not (libs / "SIDE_EFFECT").exists()
        assert not (root / "scrimmage-runs").exists(), "a run started"


@pytest.mark.slow
@pytest.mark.parametrize("command", ["check", "test"])
def test_a_relative_pythonpath_folder_means_from_the_agents_folder(
    tmp_path: Path, command: str
) -> None:
    """scrimmage runs from another folder, where libs is no folder, so only the agent sees it."""
    root = team(tmp_path)
    (root / "scrimmage.py").unlink()
    (root / "libs").mkdir()
    (root / "libs" / "scrimmage.py").write_text(SIDE_EFFECT)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    done = scrimmage(root, command, cwd=elsewhere, PYTHONPATH="libs")
    assert done.returncode == 2, done.stdout + done.stderr
    assert "libs/scrimmage.py would be imported as scrimmage" in done.stderr
    assert not list(tmp_path.rglob("SIDE_EFFECT")), "the team's scrimmage.py ran"


@pytest.mark.parametrize("folder", ["namespace", "installed", "none"])
def test_what_is_not_a_shadow_is_not_refused(
    tmp_path: Path, folder: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = team(tmp_path)
    (tmp_path / "libs" / "scrimmage").mkdir(parents=True)  # no __init__.py: a namespace
    package = Path(installed.__file__ or "").parent
    given = {"namespace": tmp_path / "libs", "installed": package.parent, "none": ""}[folder]
    monkeypatch.setenv("PYTHONPATH", str(given))
    assert shadow.problem(root) is None


@pytest.mark.slow
@pytest.mark.parametrize("shape", [":{x}", "{x}:", "{x}::{x}"])
@pytest.mark.parametrize("command", ["check", "test"])
def test_an_empty_pythonpath_entry_means_the_agents_folder(
    tmp_path: Path, command: str, shape: str
) -> None:
    """An empty entry puts the folder the agent starts in on its path, even under -P."""
    root = team(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    done = scrimmage(root, command, cwd=elsewhere, PYTHONPATH=shape.format(x=elsewhere))
    assert done.returncode == 2, done.stdout + done.stderr
    assert "team/scrimmage.py would be imported as scrimmage" in done.stderr
    assert not list(tmp_path.rglob("SIDE_EFFECT")), "the team's scrimmage.py ran"


@pytest.mark.usefixtures("signals_kept")
def test_init_with_a_probe_refuses_a_shadow_before_it_runs_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = team(tmp_path)
    (root / "scrimmage.py").unlink()
    (root / "libs").mkdir()
    (root / "libs" / "scrimmage.py").write_text(SIDE_EFFECT)
    monkeypatch.setenv("PYTHONPATH", str(root / "libs"))
    out = root / "draft.toml"
    code = cli.main(["init", str(root), "--probe", "plain:run", "--out", str(out)])
    err = capsys.readouterr().err
    assert code == 2, err
    assert "libs/scrimmage.py would be imported as scrimmage" in err
    assert not out.exists() and not list(tmp_path.rglob("SIDE_EFFECT"))
