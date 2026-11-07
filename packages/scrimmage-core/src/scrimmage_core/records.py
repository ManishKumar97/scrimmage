"""The record store: one JSON Lines file per run, readable only by its owner.

A line sent twice, such as a batch the SDK retried, is written once.
The last line of every record is the run's summary.
"""

import json
import os
import threading
from pathlib import Path

RECORD_VERSION = 1


class RecordStore:
    def __init__(self, folder: Path) -> None:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(folder, 0o700)
        self.folder = folder
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
                    self._lines[run].append(line)
                    out.write(json.dumps(line, sort_keys=True) + "\n")
                    added += 1
        return added

    def lines(self, run: str) -> list[dict[str, object]]:
        with self._lock:
            return list(self._lines[run])

    def close_run(self, run: str, test: str, row: str, summary: dict[str, object]) -> None:
        line = {"v": RECORD_VERSION, "run": run, "test": test, "row": row, "summary": summary}
        with self._lock, self.path(run).open("a") as out:
            out.write(json.dumps(line, sort_keys=True) + "\n")
