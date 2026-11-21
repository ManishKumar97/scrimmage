"""Log every network address and child process of this process, from before any startup file.

Loaded by 00_egress_audit.pth: site runs a .pth import line before it imports any
sitecustomize. The log path comes from EGRESS_AUDIT_LOG and is made absolute at install.
Each line is one JSON object, written with a single O_APPEND write so processes and
threads do not interleave.

The first line a process writes, "start", has its pid, parent pid, sys.orig_argv and
executable, and says whether
scrimmage and sitecustomize were already imported, which tells an early hook from a late
one. An exec keeps the pid and loads the hook again, so it writes a second start line.
Then one line per event, by kind:
connect, sendto, sendmsg (the address), getaddrinfo (host and port),
popen, exec, spawn (the executable and the child's argv), system (the command), fork
(os.fork and os.forkpty).

Limits: a child started with `python -S` or `-I -S` loads no .pth, so it writes no start
line, and neither does a child that is not Python. The popen and spawn lines hold the argv and
the parent's pid, not the child's, because the audit event comes before the child exists. A
reader pairs each popen or spawn line of pid P with one start line whose ppid is P and whose
argv is that argv; an unpaired line is an unwatched child. An exec line of pid P needs a
second start line of P. os.system and a fork have no child to pair: a shell command and a
forked child load no .pth, so a reader treats any such line as a problem. A child started
below subprocess, as multiprocessing's spawn does, raises none of these events, so a reader
also compares the start lines with the pids alive in the container.
Not seen at all: sockets opened in C code, such as libcurl, grpc, uvloop and ctypes calls,
which never raise socket.connect.

If the hook cannot install, for example the log cannot be written, it writes
to stderr and exits the process with status 70, because site swallows a .pth error and the
process would otherwise run unwatched.
"""

import json
import os
import sys
from typing import cast


def _write(path: str, record: dict[str, object]) -> None:
    line = (json.dumps(record, default=str) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def _record(event: str, args: tuple[object, ...]) -> dict[str, object] | None:
    match event:
        case "socket.connect":
            return {"event": "connect", "address": args[1]}
        case "socket.sendto":
            return {"event": "sendto", "address": args[1]}
        case "socket.sendmsg":
            return {"event": "sendmsg", "address": args[1]}
        case "socket.getaddrinfo":
            return {"event": "getaddrinfo", "host": args[0], "port": args[1]}
        case "subprocess.Popen":
            return {"event": "popen", "executable": args[0], "argv": args[1]}
        case "os.exec" | "os.posix_spawn":
            kind = "exec" if event == "os.exec" else "spawn"
            return {"event": kind, "executable": args[0], "argv": args[1]}
        case "os.system":
            return {"event": "system", "command": os.fsdecode(cast(bytes | str, args[0]))}
        case "os.fork" | "os.forkpty":
            return {"event": "fork"}
    return None


def install() -> None:
    raw = os.environ.get("EGRESS_AUDIT_LOG")
    if not raw:
        return
    path = os.path.abspath(raw)
    _write(
        path,
        {
            "event": "start",
            "pid": os.getpid(),
            "ppid": os.getppid(),
            "argv": list(sys.orig_argv),
            "executable": sys.executable,
            "scrimmage": "scrimmage" in sys.modules,
            "sitecustomize": "sitecustomize" in sys.modules,
        },
    )

    def hook(event: str, args: tuple[object, ...]) -> None:
        record = _record(event, args)
        if record is not None:
            _write(path, {"pid": os.getpid()} | record)

    sys.addaudithook(hook)


try:
    install()
except BaseException as exc:
    print(f"egress_audit: cannot install: {exc!r}", file=sys.stderr, flush=True)
    os._exit(70)
