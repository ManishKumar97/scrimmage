"""The wheel and the sdist a team installs: what is in them, that a build repeats, and that a
clean venv runs the quickstart from the wheel alone, away from this repository."""

import hashlib
import importlib.metadata
import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from scrimmage_core.cli import main

ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "packages" / "scrimmage"
# The quickstart's conversation: the bot asks for the order, then the stock, then answers.
QUICKSTART = ["acme_get_order", "acme_get_stock", "acme_answer|acme_admits"]
EPOCH = "1767225600"


def clean_env() -> dict[str, str]:
    """This process's environment as a team's shell has it: no pytest, uv or coverage settings."""
    drop = ("PYTHON", "VIRTUAL_ENV", "UV_", "COVERAGE", "SCRIMMAGE_", "OPENAI_")
    return {k: v for k, v in os.environ.items() if not k.startswith(drop)}


def run(*command: str | Path, cwd: Path, env: dict[str, str] | None = None) -> str:
    done = subprocess.run(
        [str(c) for c in command],
        cwd=cwd,
        env=clean_env() if env is None else env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert done.returncode == 0, (
        f"{command} exited {done.returncode}:\n{done.stdout}\n{done.stderr}"
    )
    return done.stdout


def build(package: Path, out: Path, *kinds: str) -> list[Path]:
    """Build the package's sdist and wheel into out, as the release will, and list the files."""
    env = {**clean_env(), "SOURCE_DATE_EPOCH": EPOCH}
    run("uv", "build", *kinds, "-o", out, package, cwd=package, env=env)
    return sorted(f for f in out.iterdir() if f.suffix in (".gz", ".whl"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def venv(folder: Path, python: str, *, seed: bool = False) -> Path:
    """A venv outside the repository. Returns its python."""
    folder.mkdir(parents=True, exist_ok=True)
    seeding = ["--seed"] if seed else []
    run("uv", "venv", "--no-project", *seeding, "-p", python, folder / "venv", cwd=folder)
    return folder / "venv" / "bin" / "python"


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """The sdist and the wheel of the package in this tree."""
    files = build(PACKAGE, tmp_path_factory.mktemp("dist"))
    return {
        "sdist": next(f for f in files if f.suffix == ".gz"),
        "wheel": next(f for f in files if f.suffix == ".whl"),
    }


def wheel_names(wheel: Path) -> list[str]:
    with zipfile.ZipFile(wheel) as archive:
        return sorted(n for n in archive.namelist() if not n.endswith(".dist-info/RECORD"))


@pytest.mark.slow
def test_the_version_comes_from_the_wheel_not_from_the_code(tmp_path: Path) -> None:
    """The same code built as another version prints that version."""
    copy = tmp_path / "package"
    shutil.copytree(PACKAGE, copy, ignore=shutil.ignore_patterns("__pycache__"))
    text = (copy / "pyproject.toml").read_text()
    renamed = re.sub(r'(?m)^version = ".*"$', 'version = "0.1.0rc9"', text)
    assert renamed != text
    (copy / "pyproject.toml").write_text(renamed)
    (wheel,) = build(copy, tmp_path / "dist", "--wheel")
    python = venv(tmp_path / "env", "3.11")
    run("uv", "pip", "install", "--python", python, "--no-index", wheel, cwd=tmp_path)
    assert run(python.parent / "scrimmage", "--version", cwd=tmp_path) == "scrimmage 0.1.0rc9\n"


def test_version_prints_the_installed_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stop:
        main(["--version"])
    assert stop.value.code == 0
    assert capsys.readouterr().out == f"scrimmage {importlib.metadata.version('scrimmage')}\n"


@pytest.mark.slow
def test_a_build_repeats_byte_for_byte(built: dict[str, Path], tmp_path: Path) -> None:
    again = build(PACKAGE, tmp_path / "again")
    assert {p.name: sha256(p) for p in again} == {p.name: sha256(p) for p in built.values()}
