"""The tools each MCP server listed, as its MCP wrapper saw them.

Under `faultline run`, each wrapper writes `tools-<pid>.json` in the run's trace
directory as soon as it starts, with no tools yet, and again each time its server
answers `tools/list`. So `faultline run` knows which servers started and which have
listed their tools. Each file is replaced whole, never written in place, so a reader
never sees half of one.
"""

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

PREFIX = "tools-"


@dataclass
class ServerTools:
    command: list[str]  # the server's command, as the wrapper runs it
    server: str | None = None  # the name the server gave itself in initialize
    tools: list[str] | None = None  # None until the server answered tools/list
    path: Path | None = field(default=None, compare=False)

    def add(self, names: list[str]) -> None:
        """Tools from one tools/list answer. A paged list comes in several."""
        self.tools = list(dict.fromkeys([*(self.tools or []), *names]))
        self.write()

    def named(self, server: str) -> None:
        self.server = server
        self.write()

    def write(self) -> None:
        if self.path is None:
            return
        record = {"command": self.command, "server": self.server, "tools": self.tools}
        part = self.path.with_name(f".{self.path.name}.part")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            part.write_text(json.dumps(record), encoding="utf-8")
            os.replace(part, self.path)
        except OSError as exc:  # the relay goes on: the list is a record, not traffic
            print(
                f"faultline mcp-wrap: cannot write {self.path}: {exc}", file=sys.stderr
            )


def path_for(trace_dir: str | Path, pid: int) -> Path:
    return Path(trace_dir) / f"{PREFIX}{pid}.json"


def read_all(trace_dir: Path) -> list[ServerTools]:
    """Every server's listing in a trace directory, in the order the files sort."""
    found = []
    for path in sorted(trace_dir.glob(f"{PREFIX}*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        tools = record.get("tools")
        found.append(
            ServerTools(
                [str(part) for part in record.get("command") or []],
                record.get("server") if isinstance(record.get("server"), str) else None,
                [str(t) for t in tools] if isinstance(tools, list) else None,
            )
        )
    return found
