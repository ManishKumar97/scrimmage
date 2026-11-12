"""The record store: one JSON Lines file per run, readable only by its owner.

A line sent twice, such as a batch the SDK retried, is written once.
The last line of every record is the run's summary.
"""

import json
import os
import stat
import threading
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
        if count:
            path.write_bytes(cleaned.encode("utf-8", "surrogateescape"))

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
