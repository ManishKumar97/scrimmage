"""Log every socket.connect address in this process, from before any startup file runs.

Loaded by zz_audit_first.pth: site runs a .pth import line before it imports any
sitecustomize. The log path comes from EGRESS_AUDIT_LOG. Each line is one JSON object,
written with a single O_APPEND write so processes and threads do not interleave.
The first line a process writes says whether scrimmage_core and sitecustomize were
already imported, which tells an early hook from a late one.
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


def install() -> None:
    path = os.environ.get("EGRESS_AUDIT_LOG")
    if not path:
        return
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
        if event == "socket.connect":
            _write(path, {"event": "connect", "pid": os.getpid(), "address": args[1]})

    sys.addaudithook(hook)


install()
