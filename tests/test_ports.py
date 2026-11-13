"""The test port helper: its range, its claims directory and its walk."""

import multiprocessing
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

import ports


def test_range_narrows_to_a_lower_ephemeral_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    fake = tmp_path / "range"
    fake.write_text("24000\t60999\n")
    monkeypatch.setattr(ports, "RANGE_FILE", fake)
    assert ports.port_range() == range(ports.LOW, 24000)


def test_range_keeps_the_default_when_the_kernel_starts_higher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    fake = tmp_path / "range"
    fake.write_text("49152 65535\n")
    monkeypatch.setattr(ports, "RANGE_FILE", fake)
    assert ports.port_range() == range(ports.LOW, ports.HIGH)


def test_range_keeps_the_default_without_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ports, "RANGE_FILE", tmp_path / "missing")
    assert ports.port_range() == range(ports.LOW, ports.HIGH)


def test_range_fails_clearly_when_nothing_is_left(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    fake = tmp_path / "range"
    fake.write_text("10000 60999\n")
    monkeypatch.setattr(ports, "RANGE_FILE", fake)
    with pytest.raises(RuntimeError, match="no test port is left"):
        ports.port_range()
    with pytest.raises(RuntimeError, match="no test port is left"):
        ports.free_port()


def test_claims_dir_is_created_private(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    path = ports.claims_dir()
    assert path.parent == tmp_path
    assert path.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("mode", [0o750, 0o705, 0o777])
def test_claims_dir_refuses_group_or_other_bits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: int
):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    (tmp_path / "scrimmage-test-ports").mkdir()
    (tmp_path / "scrimmage-test-ports").chmod(mode)
    with pytest.raises(RuntimeError, match="expected 0700"):
        ports.claims_dir()


def test_claims_dir_refuses_another_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    (tmp_path / "scrimmage-test-ports").mkdir(mode=0o700)
    monkeypatch.setattr(os, "getuid", lambda: os.stat(tmp_path).st_uid + 1)
    with pytest.raises(RuntimeError, match="owned by uid"):
        ports.claims_dir()


def test_claims_dir_refuses_a_symlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o700)
    (tmp_path / "scrimmage-test-ports").symlink_to(target)
    with pytest.raises(RuntimeError, match="not a directory"):
        ports.claims_dir()


@pytest.fixture
def own_held(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[int]]:
    """A separate `held` list for one test; the fds the test opened are closed at its end."""
    mine: list[int] = []
    monkeypatch.setattr(ports, "held", mine)
    yield mine
    for fd in mine:
        os.close(fd)


def test_walk_tries_each_port_once_and_leaves_one_claim_each(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, own_held: list[int]
):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(ports, "HIGH", ports.LOW + 5)
    monkeypatch.setattr(ports, "RANGE_FILE", tmp_path / "missing")
    # An address this host does not own makes every probe fail, so the walk covers the range.
    with pytest.raises(RuntimeError, match="no free port"):
        ports.free_port(also="203.0.113.1")
    assert sorted(p.name for p in ports.claims_dir().iterdir()) == [
        str(p) for p in range(ports.LOW, ports.LOW + 5)
    ]


def test_walk_finds_the_last_free_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, own_held: list[int]
):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(ports, "HIGH", ports.LOW + 3)
    monkeypatch.setattr(ports, "RANGE_FILE", tmp_path / "missing")
    first = ports.free_port()
    second = ports.free_port()
    third = ports.free_port()
    assert {first, second, third} == set(range(ports.LOW, ports.LOW + 3))


def test_walk_skips_a_port_whose_claim_file_is_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, own_held: list[int]
):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(ports, "HIGH", ports.LOW + 1)
    monkeypatch.setattr(ports, "RANGE_FILE", tmp_path / "missing")
    target = tmp_path / "elsewhere"
    target.write_text("")
    (ports.claims_dir() / str(ports.LOW)).symlink_to(target)
    with pytest.raises(RuntimeError, match="no free port"):
        ports.free_port()
    assert own_held == []


def _claim(count: int) -> list[int]:
    return [ports.free_port() for _ in range(count)]


@pytest.mark.slow
def test_many_processes_never_share_a_port():
    with multiprocessing.get_context("spawn").Pool(8) as pool:
        results = pool.map(_claim, [25] * 8)
    flat = [p for r in results for p in r]
    assert len(flat) == len(set(flat)) == 200
