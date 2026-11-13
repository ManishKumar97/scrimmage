"""The record store: one JSON Lines file per run, readable only by its owner.

A line sent twice, such as a batch the SDK retried, is written once.
The last line of every record is the run's summary.
"""

import contextlib
import json
import os
import stat
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from scrimmage_core.redact import remove_values, scrubbed

RECORD_VERSION = 1


class Refused(Exception):
    """A file that is not read: a link, another user's file, not a plain file, or too big."""


def open_owned(path: Path, *, folder: bool = False) -> int:
    """A read-only file descriptor for a plain file (or a folder) the caller owns.

    It is looked at before it is opened, opened without following a link, and looked at again
    once open, so a file swapped in between is refused too. Raises Refused or OSError.
    """
    before = os.lstat(path)
    if stat.S_ISLNK(before.st_mode):
        raise Refused(f"{path}: is a symbolic link, so it is not read")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    want = stat.S_ISDIR if folder else stat.S_ISREG
    kind = "folder" if folder else "plain file"
    if not want(before.st_mode):
        raise Refused(f"{path}: is not a {kind}, so it is not read")
    fd = os.open(path, flags | (os.O_DIRECTORY if folder else 0))
    try:
        after = os.fstat(fd)
        if not want(after.st_mode):
            raise Refused(f"{path}: is not a {kind}, so it is not read")
        if after.st_uid != os.getuid():
            raise Refused(f"{path}: is owned by another user, so it is not read")
    except BaseException:
        os.close(fd)
        raise
    return fd


class WrongMode(Refused):
    """A file this user owns whose mode is not 0600. The reader words the refusal."""

    def __init__(self, path: Path, mode: int) -> None:
        super().__init__(f"{path}: has mode {mode:o}")
        self.mode = mode


@dataclass(frozen=True)
class SecretFacts:
    """What the open descriptor of a Secret file says: the only facts the rule trusts."""

    mode: int
    uid: int
    gid: int
    readonly: bool


def secret_facts(fd: int) -> SecretFacts:
    """The facts of an open descriptor. A test replaces this to stand in for a kubelet."""
    found = os.fstat(fd)
    return SecretFacts(
        stat.S_IMODE(found.st_mode),
        found.st_uid,
        found.st_gid,
        bool(os.fstatvfs(fd).f_flag & os.ST_RDONLY),
    )


def secret_refusal(path: Path, facts: SecretFacts, uid: int, groups: frozenset[int]) -> str | None:
    """Why a file in the kubelet's Secret layout is not read, or None when it passes.

    The rule: this process is not root, root owns the file, its group is one of this process's,
    no bit for others, none to write, and none to run, and the mount is read-only.
    """
    if uid == 0:
        return (
            f"{path}: is a Kubernetes Secret file and this process runs as root; set the "
            "pod's securityContext.runAsUser and runAsNonRoot: true"
        )
    if facts.uid != 0:
        return f"{path}: is a Kubernetes Secret file owned by uid {facts.uid}, not root"
    if facts.gid not in groups:
        mine = ", ".join(str(g) for g in sorted(groups))
        return (
            f"{path}: is a Kubernetes Secret file of group {facts.gid}, and this process runs "
            f"with groups {mine}; set the pod's securityContext.fsGroup to {os.getgid()}"
        )
    if facts.mode & 0o337:
        return f"{path}: has mode {facts.mode:o}; set the Secret volume's defaultMode to 0400"
    if not facts.readonly:
        return f"{path}: is on a writable mount; a Kubernetes Secret volume is read-only"
    return None


OUTSIDE = "is a symbolic link outside a Kubernetes Secret volume, so it is not read"
FOLDER = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def open_in_layout(path: Path) -> int:
    """The file of the kubelet's layout, opened by walking it with descriptors: `name ->
    ..data/name`, `..data -> ..<stamp>` with no slash, and a plain file in the stamp folder.

    Each step is looked up inside the folder already open, so a swap of any path after the
    walk starts cannot change which file is opened. Raises Refused outside the layout, and
    PermissionError when the file is there but this process may not open it.
    """
    name = path.name
    try:
        folder = os.open(path.parent, FOLDER)
    except OSError:
        raise Refused(f"{path}: {OUTSIDE}") from None
    stamp_fd = -1
    try:
        try:
            if os.readlink(name, dir_fd=folder) != f"..data/{name}":
                raise Refused(f"{path}: {OUTSIDE}")
            stamp = os.readlink("..data", dir_fd=folder)
            if "/" in stamp or not stamp.startswith("..") or stamp in ("..", "..data"):
                raise Refused(f"{path}: {OUTSIDE}")
            stamp_fd = os.open(stamp, FOLDER, dir_fd=folder)
            return os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=stamp_fd
            )
        except PermissionError:
            if stamp_fd < 0:
                raise Refused(f"{path}: {OUTSIDE}") from None
            raise Refused(unreadable_refusal(path, name, stamp_fd)) from None
        except OSError:
            raise Refused(f"{path}: {OUTSIDE}") from None
    finally:
        os.close(folder)
        if stamp_fd >= 0:
            os.close(stamp_fd)


def unreadable_refusal(path: Path, name: str, stamp_fd: int) -> str:
    """Why this process may not open a file of the Secret layout. The file's own owner, group
    and mode only word the refusal; nothing here trusts the file."""
    try:
        found = os.stat(name, dir_fd=stamp_fd, follow_symlinks=False)
        facts = SecretFacts(
            stat.S_IMODE(found.st_mode),
            found.st_uid,
            found.st_gid,
            bool(os.fstatvfs(stamp_fd).f_flag & os.ST_RDONLY),
        )
        groups = frozenset({os.getgid(), *os.getgroups()})
        reason = secret_refusal(path, facts, os.getuid(), groups)
    except OSError:
        reason = None
    if reason is not None and "fsGroup" in reason:
        return reason
    return (
        f"{reason or f'{path}: is a Kubernetes Secret file'}; this process cannot open it, so set "
        f"the pod's securityContext.fsGroup to {os.getgid()} and the volume's defaultMode to 0440"
    )


def open_secret(path: Path) -> int:
    """A read-only descriptor for a token or a key: a plain file this user owns at mode 0600,
    or else a file in a Kubernetes Secret volume (see secret_refusal).

    A plain file keeps the first rule, so another user's file is still refused. A link is
    followed only through the kubelet's own layout, and every check after that is on the
    open descriptor. Raises WrongMode (own file, not 0600), Refused or OSError.
    """
    if not stat.S_ISLNK(os.lstat(path).st_mode):
        fd = open_owned(path)
        mode = stat.S_IMODE(os.fstat(fd).st_mode)
        if mode != 0o600:
            os.close(fd)
            raise WrongMode(path, mode)
        return fd
    fd = open_in_layout(path)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Refused(f"{path}: is not a plain file, so it is not read")
        groups = frozenset({os.getgid(), *os.getgroups()})
        refusal = secret_refusal(path, secret_facts(fd), os.getuid(), groups)
        if refusal is not None:
            raise Refused(refusal)
    except BaseException:
        os.close(fd)
        raise
    return fd


def read_regular(path: Path, limit: int) -> bytes:
    """All of a plain file the caller owns, or Refused when it is over `limit` bytes."""
    with os.fdopen(open_owned(path), "rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise Refused(f"{path}: is over {limit} bytes, so it is not read")
    return data


class RecordStore:
    def __init__(self, folder: Path) -> None:
        # exist_ok=False: two tests started in one second never share a folder.
        folder.mkdir(mode=0o700, parents=True, exist_ok=False)
        os.chmod(folder, 0o700)
        self.folder = folder
        # The values ${NAME} filled in. Every line is written without them, whichever side
        # made it: this is the one place every line passes before it reaches a file.
        self.scrub: tuple[str, ...] = ()
        self._lock = threading.Lock()
        self._seen: dict[str, set[tuple[object, ...]]] = {}
        self._lines: dict[str, list[dict[str, object]]] = {}

    def add_blob(self, sha: str, data: bytes) -> None:
        """Store one side-folder item, named by its hash; the same content is kept once."""
        folder = self.folder / "blobs"
        with self._lock:
            folder.mkdir(mode=0o700, exist_ok=True)
            target = folder / sha
            if target.exists():
                return
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as out:
                out.write(data)

    def scrub_file(self, path: Path) -> None:
        """A file a run's program wrote, such as its .log, without the values ${NAME} filled
        in: its output can print them, as the SDK's own start line names the test."""
        if not self.scrub or not path.exists():
            return
        text = path.read_bytes().decode("utf-8", "surrogateescape")
        cleaned, count = remove_values(text, self.scrub)
        if not count:
            return
        import tempfile  # the runner's only: the SDK, which imports this module, never needs it

        # A new file renamed over it, so a stop mid-write leaves the old one or the new one,
        # never half of each. mkstemp makes it this user's only, as the old one was.
        fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(cleaned.encode("utf-8", "surrogateescape"))
            os.replace(temp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temp)
            raise

    def path(self, run: str) -> Path:
        return self.folder / f"{run}.jsonl"

    def open_run(self, run: str) -> None:
        with self._lock:
            self._seen[run] = set()
            self._lines[run] = []
            fd = os.open(self.path(run), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)

    def add(self, run: str, lines: list[dict[str, object]]) -> int:
        """Append new lines for an open run; returns how many were new."""
        added = 0
        with self._lock:
            seen = self._seen[run]
            with self.path(run).open("a") as out:
                for line in lines:
                    proc = line.get("proc")
                    key = (json.dumps(proc, sort_keys=True), line.get("seq"))
                    if key in seen:
                        continue
                    seen.add(key)
                    line = cast(dict[str, object], scrubbed(line, self.scrub))
                    self._lines[run].append(line)
                    out.write(json.dumps(line, sort_keys=True) + "\n")
                    added += 1
        return added

    def lines(self, run: str) -> list[dict[str, object]]:
        with self._lock:
            return list(self._lines[run])

    def close_run(self, run: str, test: str, row: str, summary: dict[str, object]) -> None:
        line = {"v": RECORD_VERSION, "run": run, "test": test, "row": row, "summary": summary}
        line = cast(dict[str, object], scrubbed(line, self.scrub))
        with self._lock, self.path(run).open("a") as out:
            out.write(json.dumps(line, sort_keys=True) + "\n")
