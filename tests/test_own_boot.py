"""is_own_boot: a startup file is Scrimmage's own only when it is this package's _boot one."""

import os
from pathlib import Path

import pytest

from scrimmage import asgi_gate

OWN = Path(asgi_gate.__file__).parent / "_boot" / "sitecustomize.py"


def make(path: Path) -> Path:
    path.parent.mkdir(parents=True)
    path.write_text("")
    return path


def test_this_packages_own_boot_file_is_its_own() -> None:
    assert OWN.is_file()
    assert asgi_gate.is_own_boot(str(OWN))


def test_the_same_file_through_a_symlink_or_a_relative_path_is_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    link = tmp_path / "link.py"
    link.symlink_to(OWN)
    assert asgi_gate.is_own_boot(str(link))
    folder_link = tmp_path / "boot-link"
    folder_link.symlink_to(OWN.parent, target_is_directory=True)
    assert asgi_gate.is_own_boot(str(folder_link / "sitecustomize.py"))
    monkeypatch.chdir(tmp_path)
    assert asgi_gate.is_own_boot(os.path.relpath(OWN, tmp_path))


def test_a_team_file_under_a_folder_named_scrimmage_is_not_its_own(tmp_path: Path) -> None:
    team = make(tmp_path / "scrimmage-demo" / "scrimmage" / "sitecustomize.py")
    assert not asgi_gate.is_own_boot(str(team))
    deeper = make(tmp_path / "scrimmage-demo" / "scrimmage" / "_boot-copy" / "sitecustomize.py")
    assert not asgi_gate.is_own_boot(str(deeper))


def test_another_installs_boot_file_is_not_its_own(tmp_path: Path) -> None:
    other = make(tmp_path / "other-venv" / "scrimmage" / "_boot" / "sitecustomize.py")
    assert not asgi_gate.is_own_boot(str(other))
