"""How a process started under `faultline run` finds the run it belongs to.

`faultline run` passes the run to its agent in environment variables. An MCP client
often starts its servers with only a few variables (PATH, HOME and the like), so the
MCP wrapper may not see them. So `faultline run` also writes the run to a context file
named by its own process id, in a private directory found without any variable.
A wrapper with no variables walks up its parent processes until one of them has a
context file. That parent is the `faultline run` it runs under.

The file records the process start time as well, so a file left by a dead run whose
process id was reused is not taken for the live one.
"""

import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

TRACE_DIR = "FAULTLINE_TRACE_DIR"
TRACE_ID = "FAULTLINE_TRACE_ID"
ROOT_SPAN_ID = "FAULTLINE_ROOT_SPAN_ID"
FAULTS = "FAULTLINE_FAULTS"  # a JSON list of fault specs
MAX_CONTENT = "FAULTLINE_MAX_CONTENT"
MAX_ANCESTORS = 32  # how far up the process tree to look


@dataclass(frozen=True)
class RunContext:
    trace_dir: str
    trace_id: str
    root_span_id: str
    faults: tuple[str, ...] = ()
    max_content: int | None = None

    def env(self) -> dict[str, str]:
        """The run as environment variables, for the agent."""
        env = {
            TRACE_DIR: self.trace_dir,
            TRACE_ID: self.trace_id,
            ROOT_SPAN_ID: self.root_span_id,
            FAULTS: json.dumps(list(self.faults)),
        }
        if self.max_content is not None:
            env[MAX_CONTENT] = str(self.max_content)
        return env


def from_env(env: Mapping[str, str]) -> RunContext | None:
    if not all(env.get(name) for name in (TRACE_DIR, TRACE_ID, ROOT_SPAN_ID)):
        return None
    limit = env.get(MAX_CONTENT, "")
    return RunContext(
        env[TRACE_DIR],
        env[TRACE_ID],
        env[ROOT_SPAN_ID],
        tuple(json.loads(env.get(FAULTS) or "[]")),
        int(limit) if limit.isdigit() else None,
    )


def runtime_dir() -> Path:
    """A directory only this user can use, found from the user id alone."""
    uid = os.getuid()
    run = Path(f"/run/user/{uid}")
    base = (
        run if run.is_dir() and os.access(run, os.W_OK) else Path(tempfile.gettempdir())
    )
    return base / f"faultline-{uid}"


def private_dir() -> Path:
    """The runtime directory, made if needed, refused if another user owns it."""
    path = runtime_dir()
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if info.st_uid != os.getuid() or path.is_symlink():
        raise PermissionError(f"{path} is not owned by this user")
    return path


def context_path(pid: int) -> Path:
    return runtime_dir() / f"run-{pid}.json"


def publish(context: RunContext) -> Path:
    """Write this process's run context. Remove it with withdraw() when the run ends."""
    path = private_dir() / f"run-{os.getpid()}.json"
    record = {**asdict(context), "pid": os.getpid(), "started": start_time(os.getpid())}
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as file:
        json.dump(record, file)
    return path


def withdraw(path: Path) -> None:
    path.unlink(missing_ok=True)


def find(env: Mapping[str, str] = os.environ) -> RunContext | None:
    """The run this process belongs to: from its variables, else from a parent's file."""
    context = from_env(env)
    if context:
        return context
    for pid in ancestors(os.getpid()):
        path = context_path(pid)
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if record.get("pid") != pid or record.get("started") != start_time(pid):
            continue  # a file left by a dead run whose process id was reused
        return RunContext(
            record["trace_dir"],
            record["trace_id"],
            record["root_span_id"],
            tuple(record.get("faults") or ()),
            record.get("max_content"),
        )
    return None


def ancestors(pid: int) -> Iterator[int]:
    """The parent, grandparent and so on of a process, up to init."""
    for _ in range(MAX_ANCESTORS):
        parent = parent_of(pid)
        if parent is None or parent <= 1:
            return
        yield parent
        pid = parent


def stat_fields(pid: int) -> list[str] | None:
    """Fields of /proc/PID/stat after the command name, which may hold spaces."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return text.rpartition(")")[2].split()


def parent_of(pid: int) -> int | None:
    fields = stat_fields(pid)
    if fields is not None:
        return int(fields[1])  # field 4 of stat: the parent process id
    if sys.platform == "linux":
        return None
    try:  # no /proc, such as macOS
        out = subprocess.run(
            ["ps", "-o", "ppid=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return int(out) if out.isdigit() else None


def start_time(pid: int) -> str | None:
    """When the process started, in clock ticks since boot. None without /proc."""
    fields = stat_fields(pid)
    return fields[19] if fields is not None else None  # field 22 of stat
