"""Free ports for tests that start a process on a port they choose first.

Binding port 0 and closing the socket hands out a number nothing holds: another test
process, running at the same time under xdist, can be given the same number before the
service binds it. Here each port is claimed with a file lock held until this process exits,
so two test processes never get the same one, and it lies below the range the kernel gives
to outgoing connections, so no connection takes it either.
"""

import fcntl
import os
import random
import socket
import tempfile
from pathlib import Path

LOW, HIGH = 20000, 32000  # below the kernel's ephemeral range (32768 and up)
CLAIMS = Path(tempfile.gettempdir()) / f"scrimmage-test-ports-{os.getuid()}"
held: list[int] = []  # lock files kept open, so the kernel frees the claims at exit


def free_port(also: str | None = None) -> int:
    """also: a second address that must be free at the port too, such as a bridge address."""
    CLAIMS.mkdir(mode=0o700, exist_ok=True)
    for _ in range(HIGH - LOW):
        port = random.randrange(LOW, HIGH)
        fd = os.open(CLAIMS / str(port), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with socket.socket() as probe:  # something outside the tests may hold it
                probe.bind(("127.0.0.1", port))
            if also is not None:
                with socket.socket() as probe:
                    probe.bind((also, port))
        except OSError:
            os.close(fd)
            continue
        held.append(fd)
        return port
    raise RuntimeError("no free port in the test range")
