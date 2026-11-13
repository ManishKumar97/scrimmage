"""The Secret volume rule: a token or a key in a Kubernetes Secret volume is read, any other
file that is not the plain 0600 file of this user is not.

The kubelet's layout is built on disk (`name -> ..data/name`, `..data -> ..<stamp>`). A test
cannot make a file root's, nor mount a folder read-only, so each test says what the kubelet
would have made through `records.secret_facts`, the one place that reads the open descriptor.
"""

import os
import secrets
import stat
from pathlib import Path
from typing import NoReturn

import pytest

from scrimmage_core import bootstrap, records, service_client
from scrimmage_core import config as settings
from scrimmage_core.records import Refused, SecretFacts, WrongMode
from test_control_tls import pki, runner_file

TOKEN = secrets.token_hex(32)
STAMP = "..2026_11_13_09_00_00.1234567890"
FS_GROUP = 10001
GROUPS = frozenset({FS_GROUP})
KUBELET = SecretFacts(mode=0o440, uid=0, gid=FS_GROUP, readonly=True)


class Stopped(Exception):
    pass


@pytest.fixture
def stops(monkeypatch: pytest.MonkeyPatch) -> None:
    def stop(message: str) -> NoReturn:
        raise Stopped(message)

    monkeypatch.setattr(bootstrap, "service_stop", stop)


def kubelet_layout(folder: Path, name: str = "token", data: bytes = TOKEN.encode()) -> Path:
    """What the kubelet writes: the file in a timestamped folder, and two links to it."""
    (folder / STAMP).mkdir(parents=True)
    (folder / STAMP / name).write_bytes(data)
    (folder / STAMP / name).chmod(0o440)
    (folder / "..data").symlink_to(STAMP)
    (folder / name).symlink_to(f"..data/{name}")
    return folder / name


def as_kubelet(
    monkeypatch: pytest.MonkeyPatch, *, uid: int = 0, readonly: bool = True, mode: int = 0o440
) -> None:
    """Every file now reads as the kubelet made it: root's, in this process's own group (the
    pod's fsGroup), read-only. The arguments change one fact."""
    facts = SecretFacts(mode=mode, uid=uid, gid=os.getgid(), readonly=readonly)
    monkeypatch.setattr(records, "secret_facts", lambda fd: facts)


def refusal(path: Path) -> str:
    with pytest.raises(Refused) as raised:
        os.close(records.open_secret(path))
    return str(raised.value)


def test_a_kubernetes_secret_file_is_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = Path("/run/scrimmage/token/token")
    assert records.secret_refusal(path, KUBELET, 10001, GROUPS) is None
    assert (
        records.secret_refusal(path, SecretFacts(0o400, 0, FS_GROUP, True), 10001, GROUPS) is None
    )
    # The same on disk: the two links are walked and the file is opened.
    link = kubelet_layout(tmp_path)
    as_kubelet(monkeypatch)
    with os.fdopen(records.open_secret(link), "rb") as handle:
        assert handle.read().decode() == TOKEN


def test_a_secret_file_others_can_read_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    said = records.secret_refusal(Path("t"), SecretFacts(0o444, 0, FS_GROUP, True), 10001, GROUPS)
    assert said is not None and "has mode 444" in said and "defaultMode to 0400" in said
    as_kubelet(monkeypatch, mode=0o444)
    assert "has mode 444" in refusal(kubelet_layout(tmp_path))


def test_a_group_writable_secret_file_is_refused():
    said = records.secret_refusal(Path("t"), SecretFacts(0o460, 0, FS_GROUP, True), 10001, GROUPS)
    assert said is not None and "has mode 460" in said


def test_a_secret_file_of_another_group_is_refused_naming_fsgroup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    said = records.secret_refusal(Path("t"), SecretFacts(0o440, 0, 2000, True), 10001, GROUPS)
    assert said is not None
    assert "of group 2000" in said and "runs with groups 10001" in said
    assert "securityContext.fsGroup" in said
    # On disk, a group that is none of this process's.
    groups = {os.getgid(), *os.getgroups()}
    other = SecretFacts(0o440, 0, max(groups) + 1, True)
    monkeypatch.setattr(records, "secret_facts", lambda fd: other)
    assert f"of group {other.gid}" in refusal(kubelet_layout(tmp_path))


def test_a_secret_file_on_a_writable_mount_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    said = records.secret_refusal(Path("t"), SecretFacts(0o440, 0, FS_GROUP, False), 10001, GROUPS)
    assert said is not None and "writable mount" in said
    as_kubelet(monkeypatch, readonly=False)
    assert "writable mount" in refusal(kubelet_layout(tmp_path))


def test_a_file_of_a_third_uid_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    said = records.secret_refusal(
        Path("t"), SecretFacts(0o440, 4242, FS_GROUP, True), 10001, GROUPS
    )
    assert said is not None and "owned by uid 4242, not root" in said
    as_kubelet(monkeypatch, uid=4242)
    assert "owned by uid 4242" in refusal(kubelet_layout(tmp_path))
    # Outside the layout, another user's plain file is refused as it always was.
    plain = tmp_path / "plain"
    plain.write_text(TOKEN)
    plain.chmod(0o600)
    monkeypatch.setattr(os, "getuid", lambda: os.geteuid() + 1)
    assert "owned by another user" in refusal(plain)


def test_a_link_out_of_the_folder_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    as_kubelet(monkeypatch)  # every other fact would pass
    out = tmp_path / "token"
    out.symlink_to("/etc/hostname")
    assert "symbolic link outside a Kubernetes Secret volume" in refusal(out)
    # The right name, but the data link leaves the folder.
    folder = tmp_path / "second"
    folder.mkdir()
    (folder / "..data").symlink_to("/etc")
    (folder / "hostname").symlink_to("..data/hostname")
    assert "symbolic link outside" in refusal(folder / "hostname")
    # The stamp link holds a slash, or is `..` itself.
    for number, stamp in enumerate(("../etc", "..")):
        third = tmp_path / f"third{number}"
        third.mkdir()
        (third / "..data").symlink_to(stamp)
        (third / "token").symlink_to("..data/token")
        assert "symbolic link outside" in refusal(third / "token")
    # A user's own file behind a link of their own is still a link, and is refused.
    own = tmp_path / "own"
    own.write_text(TOKEN)
    own.chmod(0o600)
    (tmp_path / "ln").symlink_to(own)
    assert "symbolic link outside" in refusal(tmp_path / "ln")


def test_a_root_process_is_told_to_set_run_as_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    as_kubelet(monkeypatch)
    link = kubelet_layout(tmp_path)
    monkeypatch.setattr(os, "getuid", lambda: 0)
    said = refusal(link)
    assert "runs as root" in said and "runAsUser and runAsNonRoot: true" in said


def test_the_s7a_rule_is_unchanged(tmp_path: Path, stops: None, monkeypatch: pytest.MonkeyPatch):
    own = tmp_path / "token"
    own.write_text(TOKEN)
    own.chmod(0o600)
    assert bootstrap.read_token_file(str(own)) == TOKEN
    monkeypatch.setenv("SCRIMMAGE_TOKEN_FILE", str(own))
    assert service_client.runner_token() == TOKEN
    own.chmod(0o640)
    with pytest.raises(Stopped) as raised:
        bootstrap.read_token_file(str(own))
    assert "has mode 640; it must be 600, readable by its owner only" in str(raised.value)
    with pytest.raises(service_client.Setup, match="has mode 640; it must be 600"):
        service_client.runner_token()
    with pytest.raises(WrongMode):
        os.close(records.open_secret(own))


def test_all_three_readers_use_the_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stops: None
):
    opened: list[Path] = []
    real = records.open_secret

    def spy(path: Path) -> int:
        opened.append(path)
        return real(path)

    monkeypatch.setattr(records, "open_secret", spy)
    token = kubelet_layout(tmp_path / "service-token")
    runner = kubelet_layout(tmp_path / "runner-token")
    files = pki(tmp_path)
    key = kubelet_layout(tmp_path / "tls", "tls.key", files.key.read_bytes())
    as_kubelet(monkeypatch)
    # Each reader takes the Secret file, which the plain-file rule alone would refuse.
    assert bootstrap.read_token_file(str(token)) == TOKEN
    monkeypatch.setenv("SCRIMMAGE_TOKEN_FILE", str(runner))
    assert service_client.runner_token() == TOKEN
    agent = settings.load(runner_file(tmp_path, files.cert, key)).agent
    assert service_client.server_tls(agent, tmp_path) is not None
    assert opened == [token, runner, key]


def test_server_tls_follows_the_link_when_it_compares_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    files = pki(tmp_path)
    key = kubelet_layout(tmp_path / "tls", "tls.key", files.key.read_bytes())
    assert stat.S_ISLNK(os.lstat(key).st_mode)  # lstat sees the link, stat the file
    as_kubelet(monkeypatch)
    agent = settings.load(runner_file(tmp_path, files.cert, key)).agent
    assert service_client.server_tls(agent, tmp_path) is not None


def test_a_secret_file_this_process_cannot_open_names_fsgroup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stops: None
):
    """The open itself fails with EACCES, as for root:root 0400 under a user, and no fact of
    the file is patched: the refusal must still name the setting."""
    link = kubelet_layout(tmp_path / "volume")
    (tmp_path / "volume" / STAMP / "token").chmod(0)
    with pytest.raises(Stopped) as raised:
        bootstrap.read_token_file(str(link))
    assert "securityContext.fsGroup" in str(raised.value)
    assert "set user:" not in str(raised.value)  # the Docker hint is not for a Secret file
    monkeypatch.setenv("SCRIMMAGE_TOKEN_FILE", str(link))
    with pytest.raises(service_client.Setup) as setup:
        service_client.runner_token()
    assert "securityContext.fsGroup" in str(setup.value)


def test_a_swap_after_the_walk_cannot_change_which_file_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    volume = tmp_path / "volume"
    link = kubelet_layout(volume)
    attacker = tmp_path / "attacker"
    kubelet_layout(attacker, data=b"x" * 64)
    as_kubelet(monkeypatch)
    real = os.readlink
    swapped: list[bool] = []

    def swap_then_read(path: str | Path, *, dir_fd: int | None = None) -> str:
        if not swapped:  # once the folder is open: put the attacker's folder in its place
            swapped.append(True)
            volume.rename(tmp_path / "moved")
            attacker.rename(volume)
        return real(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "readlink", swap_then_read)
    with os.fdopen(records.open_secret(link), "rb") as handle:
        assert handle.read().decode() == TOKEN
    assert swapped


def test_a_secret_file_the_owner_can_write_is_refused():
    said = records.secret_refusal(Path("t"), SecretFacts(0o640, 0, FS_GROUP, True), 10001, GROUPS)
    assert said is not None and "has mode 640" in said
