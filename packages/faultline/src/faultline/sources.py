"""Where runs come from. Files first; a stream source can join later."""

from collections.abc import Iterator
from pathlib import Path
from typing import Protocol

from faultline.model import Run
from faultline.runs import Reading, read_runs


class TraceSource(Protocol):
    name: str

    def runs(self) -> Iterator[Run]: ...

    def readings(self) -> Iterator[tuple[str, Reading]]:
        """The runs by origin, with the lines that gave nothing, for a report."""
        ...


class FileSource:
    """One `.jsonl` span file, or every `*.jsonl` in a directory, sorted."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.name = f"file:{path}"

    def files(self) -> list[Path]:
        if self.path.is_dir():
            return sorted(self.path.glob("*.jsonl"))
        return [self.path]

    def readings(self) -> Iterator[tuple[str, Reading]]:
        for path in self.files():
            yield str(path), read_runs(path)

    def runs(self) -> Iterator[Run]:
        for _, reading in self.readings():
            yield from reading.runs


def parse_source(spec: str) -> TraceSource:
    """A source from `scheme:rest`, for example `file:traces/`."""
    scheme, _, rest = spec.partition(":")
    if scheme == "file" and rest:
        return FileSource(rest)
    raise ValueError(
        f"unknown trace source {spec!r}; expected SCHEME:LOCATION, known schemes: file"
    )
