"""scrimmage test --records DIR: the records go where the team says, not beside the file."""

import os
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from conftest import ACME, Project
from fakes.openai_server import FakeOpenAI
from scrimmage.cli import main

pytestmark = [pytest.mark.usefixtures("signals_kept"), pytest.mark.slow]
Fake = Callable[..., FakeOpenAI]


def run(
    project: Project, fake: FakeOpenAI, *args: str, command: str = "test"
) -> tuple[int, str, str]:
    project.monkeypatch.setenv("OPENAI_BASE_URL", fake.base_url)
    project.monkeypatch.setenv("OPENAI_API_KEY", "test")
    project.capsys.readouterr()
    code = main([command, *args])
    captured = project.capsys.readouterr()
    return code, captured.out, captured.err


@contextmanager
def read_only(folder: Path) -> Iterator[None]:
    """A folder nobody can write into, as a ConfigMap mount is; put back so pytest can remove it."""
    before = folder.stat().st_mode
    folder.chmod(before & ~0o222)
    try:
        yield
    finally:
        folder.chmod(before)


def test_records_goes_where_the_flag_says(
    project: Project, fake_openai: Fake, tmp_path: Path
) -> None:
    dest = tmp_path / "volume" / "records"
    dest.parent.mkdir()
    with read_only(project.root):
        # Positive control: the folder is read-only for this process, so the old place fails.
        assert not os.access(project.root, os.W_OK), "run as root: a read-only folder stops nothing"
        code, out, err = run(project, fake_openai(ACME), str(project.toml), "--records", str(dest))
    assert (code, err) == (0, "")
    made = [p for p in dest.iterdir() if p.is_dir()]
    assert len(made) == 1 and (made[0] / "result.json").is_file()
    assert out.startswith(f"scrimmage test: 1 test, 2 rows, 6 runs; records in {made[0]}")
    assert not (project.root / "scrimmage-runs").exists()


def test_records_makes_a_missing_folder_mode_700(
    project: Project, fake_openai: Fake, tmp_path: Path
) -> None:
    dest = tmp_path / "new" / "records"
    code, _, err = run(project, fake_openai(ACME), str(project.toml), "--records", str(dest))
    assert (code, err) == (0, "")
    assert stat.S_IMODE(dest.stat().st_mode) == 0o700
    assert (dest / ".gitignore").read_text() == "*\n"


@pytest.mark.skipif(os.getuid() == 0, reason="root owns the folder it is told to use")
def test_records_on_a_folder_of_another_owner_is_exit_2_naming_a_folder_inside(
    project: Project, fake_openai: Fake
) -> None:
    other = Path("/usr")  # root's, and this process is not
    assert other.stat().st_uid != os.getuid()
    before = stat.S_IMODE(other.stat().st_mode)
    code, out, err = run(project, fake_openai([]), str(project.toml), "--records", str(other))
    assert code == 2 and out == ""
    assert err == (
        f"scrimmage: --records {other} is owned by uid 0; point it at a folder inside it, "
        f"such as {other / 'scrimmage-runs'}\n"
    )
    assert stat.S_IMODE(other.stat().st_mode) == before
    assert not (other / "scrimmage-runs").exists()


def test_show_reads_a_folder_from_records(
    project: Project, fake_openai: Fake, tmp_path: Path
) -> None:
    dest = tmp_path / "records"
    fake = fake_openai(ACME)
    code, _, err = run(project, fake, str(project.toml), "--records", str(dest))
    assert (code, err) == (0, "")
    (folder,) = (p for p in dest.iterdir() if p.is_dir())
    code, out, err = run(project, fake, str(folder), command="show")
    assert (code, err) == (0, "")
    assert out.startswith(f"scrimmage test: 1 test, 2 rows, 6 runs; records in {folder}")


@pytest.mark.skipif(os.getuid() == 0, reason="root can write into a read-only folder")
def test_records_under_a_read_only_parent_is_exit_2_naming_the_folder(
    project: Project, fake_openai: Fake, tmp_path: Path
) -> None:
    parent = tmp_path / "locked"
    parent.mkdir()
    dest = parent / "x" / "y"
    with read_only(parent):
        assert not os.access(parent, os.W_OK)
        code, out, err = run(project, fake_openai([]), str(project.toml), "--records", str(dest))
    assert code == 2 and out == ""
    assert err == f"scrimmage: --records {dest}: Permission denied\n"
    assert not parent.joinpath("x").exists()


def test_an_existing_own_folder_of_mode_700_is_used_as_it_is(
    project: Project, fake_openai: Fake, tmp_path: Path
) -> None:
    dest = tmp_path / "mine"
    dest.mkdir(mode=0o700)
    code, _, err = run(project, fake_openai(ACME), str(project.toml), "--records", str(dest))
    assert (code, err) == (0, "")
    assert stat.S_IMODE(dest.stat().st_mode) == 0o700
    assert sorted(p.name for p in dest.iterdir() if p.is_file()) == []  # no .gitignore


def test_an_existing_folder_open_to_others_is_exit_2_and_left_alone(
    project: Project, fake_openai: Fake, tmp_path: Path
) -> None:
    dest = tmp_path / "shared"
    dest.mkdir()
    dest.chmod(0o755)
    code, out, err = run(project, fake_openai([]), str(project.toml), "--records", str(dest))
    assert code == 2 and out == ""
    assert err == (
        f"scrimmage: --records {dest} has mode 755, which lets others in; run chmod 700 on it, "
        f"or point it at a new folder inside it, such as {dest / 'scrimmage-runs'}\n"
    )
    assert stat.S_IMODE(dest.stat().st_mode) == 0o755
    assert list(dest.iterdir()) == []


def test_the_config_folder_is_never_changed_by_records(project: Project, fake_openai: Fake) -> None:
    project.root.chmod(0o700)
    before = sorted(p.name for p in project.root.iterdir())
    code, _, err = run(
        project, fake_openai(ACME), str(project.toml), "--records", str(project.root)
    )
    assert (code, err) == (0, "")
    assert stat.S_IMODE(project.root.stat().st_mode) == 0o700
    after = sorted(p.name for p in project.root.iterdir())
    assert len(after) == len(before) + 1 and set(before) <= set(after)  # the test's own folder
    assert (project.root / ".gitignore").exists() == (".gitignore" in before)
