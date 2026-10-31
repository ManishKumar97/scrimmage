"""Where runs come from. Files first; a stream source can join later."""

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from faultline.config import Attributes
from faultline.model import Run
from faultline.runs import read_runs_many


@dataclass(frozen=True, slots=True)
class SourceStats:
    """What a source read, for a report. Filled once `runs()` has been consumed."""

    files: int = 0
    unreadable: int = 0  # lines that were not a span file shape
    no_spans: int = 0  # lines that were JSON but held no spans


class TraceSource(Protocol):
    name: str

    def runs(self) -> Iterator[Run]:
        """Every run. Raises OSError when the input cannot be read."""
        ...

    def stats(self) -> SourceStats: ...


class FileSource:
    """One `.jsonl` span file, or every `*.jsonl` in a directory, sorted."""

    def __init__(self, path: Path | str, attributes: Attributes = Attributes()):
        self.path = Path(path)
        self.attributes = attributes
        self.name = f"file:{path}"
        self._stats = SourceStats()

    def files(self) -> list[Path]:
        if self.path.is_dir():
            return sorted(self.path.glob("*.jsonl"))
        return [self.path]

    def runs(self) -> Iterator[Run]:
        files = self.files()
        # All files at once, so a trace split across files is still one run.
        reading = read_runs_many(files, self.attributes)
        self._stats = SourceStats(len(files), reading.unreadable, reading.no_spans)
        yield from reading.runs

    def stats(self) -> SourceStats:
        return self._stats


def parse_source(spec: str, attributes: Attributes = Attributes()) -> TraceSource:
    """A source from `scheme:rest`, for example `file:traces/`."""
    scheme, _, rest = spec.partition(":")
    if scheme == "file" and rest:
        return FileSource(rest, attributes)
    raise ValueError(
        f"unknown trace source {spec!r}; expected SCHEME:LOCATION, known schemes: file"
    )
