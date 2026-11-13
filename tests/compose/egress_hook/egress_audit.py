"""Log every network address and child process of this process, from before any startup file.

Loaded by 00_egress_audit.pth: site runs a .pth import line before it imports any
sitecustomize. The log path comes from EGRESS_AUDIT_LOG and is made absolute at install.
Each line is one JSON object, written with a single O_APPEND write so processes and
threads do not interleave.

The first line a process writes says whether scrimmage_core and sitecustomize were
already imported, which tells an early hook from a late one.
Then one line per event, by kind:
connect, sendto, sendmsg (the address), getaddrinfo (host and port),
popen, exec, spawn (the child's argv).

Limits: a child started with `python -S` or `-I -S` loads no .pth, so it writes no start
line. The parent's popen, exec or spawn line is how a reader finds it: a child pid with no
start line. If the hook cannot install, for example the log cannot be written, it writes
to stderr and exits the process with status 70, because site swallows a .pth error and the
process would otherwise run unwatched.
"""

import json
import os
import sys


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
            return {"event": "popen", "argv": args[1]}
        case "os.exec" | "os.posix_spawn":
            return {"event": "exec" if event == "os.exec" else "spawn", "argv": args[1]}
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
            "scrimmage_core": "scrimmage_core" in sys.modules,
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
