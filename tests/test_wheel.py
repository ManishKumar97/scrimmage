"""The wheel and the sdist a team installs: what is in them, that a build repeats, and that a
clean venv runs the quickstart from the wheel alone, away from this repository."""

import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import tarfile
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest

from conftest import EXAMPLE
from fakes.openai_server import FakeOpenAI
from scrimmage.cli import main

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


def sdist_top(sdist: Path) -> set[str]:
    """The first path part of every entry in the sdist, below its one root folder."""
    with tarfile.open(sdist) as archive:
        return {n.split("/")[1] for n in archive.getnames() if n.count("/") >= 1} - {""}


TOP = {"src", "pyproject.toml", "README.md", "LICENSE", "PKG-INFO"}


@pytest.mark.slow
def test_the_sdist_holds_only_the_package(built: dict[str, Path]) -> None:
    # hatchling adds the repository's own public ignore file when it builds inside the clone
    assert sdist_top(built["sdist"]) - {".gitignore"} == TOP
    with tarfile.open(built["sdist"]) as sdist:
        names = [n.split("/", 1)[1] for n in sdist.getnames() if "/" in n]
    assert "src/scrimmage/__init__.py" in names


@pytest.mark.slow
def test_a_file_beside_the_package_does_not_reach_the_sdist(tmp_path: Path) -> None:
    """A working tree may hold files that no .gitignore names, such as ones a developer hides
    in .git/info/exclude, which the build never reads. Only the allowlist keeps them out."""
    copy = tmp_path / "package"
    shutil.copytree(PACKAGE, copy, ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("CLAUDE.md", "NOTES.md", ".claude/settings.json", "runs/a.json", "testbed/b"):
        (copy / name).parent.mkdir(parents=True, exist_ok=True)
        (copy / name).write_text("decoy")
    files = build(copy, tmp_path / "dist", "--sdist")
    assert sdist_top(files[0]) == TOP


@pytest.mark.slow
def test_pip_builds_the_same_wheel_from_the_sdist(built: dict[str, Path], tmp_path: Path) -> None:
    python = venv(tmp_path, "3.11", seed=True)
    out = tmp_path / "wheels"
    run(python, "-m", "pip", "wheel", "--no-deps", "-w", out, built["sdist"], cwd=tmp_path)
    (made,) = out.iterdir()
    assert wheel_names(made) == wheel_names(built["wheel"])


@pytest.mark.slow
@pytest.mark.parametrize("version", ["3.11", "3.14"])
def test_a_clean_venv_runs_the_quickstart_from_the_wheel(
    version: str,
    built: dict[str, Path],
    tmp_path: Path,
    fake_openai: Callable[..., FakeOpenAI],
) -> None:
    python = venv(tmp_path / "env", version)
    run("uv", "pip", "install", "--python", python, "--no-index", built["wheel"], cwd=tmp_path)
    run("uv", "pip", "install", "--python", python, "openai", cwd=tmp_path)

    # Nothing of this repository is on the venv's path, and Scrimmage is the wheel's copy.
    probe = "import json, sys, scrimmage; print(json.dumps([sys.path, scrimmage.__file__]))"
    paths, module = json.loads(run(python, "-c", probe, cwd=tmp_path))
    inside = [p for p in [*paths, module] if p and Path(p).resolve().is_relative_to(ROOT)]
    assert not inside, f"the venv sees this repository: {inside}"
    assert "site-packages" in module

    project = tmp_path / "example"
    shutil.copytree(
        EXAMPLE, project, ignore=shutil.ignore_patterns("__pycache__", "scrimmage-runs")
    )
    fake = fake_openai(QUICKSTART)
    env = {**clean_env(), "OPENAI_BASE_URL": fake.base_url, "OPENAI_API_KEY": "test"}
    scrimmage = python.parent / "scrimmage"
    shown = run(scrimmage, "--version", cwd=project, env=env)
    assert re.fullmatch(r"scrimmage 0\.1\.0\S*\n", shown), shown
    run(scrimmage, "check", cwd=project, env=env)
    table = run(scrimmage, "test", cwd=project, env=env)
    passed = [line for line in table.splitlines() if line.rstrip().endswith("pass")]
    assert len(passed) == 2, table


def quiet(*command: str | Path, cwd: Path) -> subprocess.CompletedProcess[str]:
    """A command that may fail: its exit code and streams, as a team's shell sees them."""
    return subprocess.run(
        [str(c) for c in command],
        cwd=cwd,
        env=clean_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.slow
def test_the_wheel_holds_scrimmage_and_no_scrimmage_core(built: dict[str, Path]) -> None:
    tops = {n.split("/")[0] for n in wheel_names(built["wheel"])}
    version = importlib.metadata.version("scrimmage")
    assert tops == {"scrimmage", f"scrimmage-{version}.dist-info"}, tops


@pytest.mark.slow
def test_a_clean_venv_imports_scrimmage_and_loads_nothing_else(
    built: dict[str, Path], tmp_path: Path
) -> None:
    """A team's production code may `import scrimmage`, so that import must start nothing."""
    python = venv(tmp_path / "env", "3.11")
    run("uv", "pip", "install", "--python", python, "--no-index", built["wheel"], cwd=tmp_path)
    probe = (
        "import sys; before = set(sys.modules); import scrimmage; "
        "print(sorted(set(sys.modules) - before))"
    )
    assert run(python, "-I", "-c", probe, cwd=tmp_path) == "['scrimmage']\n"
    old = quiet(python, "-I", "-c", "import scrimmage_core", cwd=tmp_path)
    assert old.returncode != 0 and "ModuleNotFoundError" in old.stderr, old.stderr


@pytest.mark.slow
def test_the_three_commands_of_a_clean_venv_find_their_code(
    built: dict[str, Path], tmp_path: Path
) -> None:
    python = venv(tmp_path / "env", "3.11")
    run("uv", "pip", "install", "--python", python, "--no-index", built["wheel"], cwd=tmp_path)
    scripts = python.parent
    assert run(scripts / "scrimmage", "--version", cwd=tmp_path) == (
        f"scrimmage {importlib.metadata.version('scrimmage')}\n"
    )
    # scrimmage-run takes a command to run, so --version is a command that does not exist, and
    # the message is its own. scrimmage-mcp without its extra says what to install. Neither
    # says "No module named", so each entry point found its module.
    for name, said in (
        ("scrimmage-run", "scrimmage-run: cannot run --version"),
        ("scrimmage-mcp", 'pip install "scrimmage[mcp]"'),
    ):
        done = quiet(scripts / name, "--version", cwd=tmp_path)
        assert said in done.stderr + done.stdout, (name, done.stdout, done.stderr)
        assert "No module named" not in done.stderr, (name, done.stderr)
