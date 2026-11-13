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
import stat
import tempfile
from pathlib import Path

LOW, HIGH = 20000, 32000  # HIGH is lowered to the kernel's ephemeral range start when lower
RANGE_FILE = Path("/proc/sys/net/ipv4/ip_local_port_range")
held: list[int] = []  # lock files kept open, so the kernel frees the claims at exit


def port_range() -> range:
    """The ports to draw from: LOW up to HIGH, and never into the kernel's ephemeral range."""
    high = HIGH
    try:
        ephemeral_low = int(RANGE_FILE.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        ephemeral_low = None  # no such file (not Linux): keep the default range
    if ephemeral_low is not None:
        high = min(high, ephemeral_low)
    if high <= LOW:
        raise RuntimeError(
            f"no test port is left below the kernel's ephemeral range (starts at {ephemeral_low}, "
            f"tests need ports from {LOW})"
        )
    return range(LOW, high)


def claims_dir() -> Path:
    """A directory only this user can use, for the claim files. Refuses one that is not."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and Path(runtime).is_absolute():
        path = Path(runtime) / "scrimmage-test-ports"
    else:
        path = Path(tempfile.gettempdir()) / f"scrimmage-test-ports-{os.getuid()}"
    path.mkdir(mode=0o700, exist_ok=True)
    info = os.lstat(path)  # lstat: a symlink in a shared /tmp is not a directory of ours
    if not stat.S_ISDIR(info.st_mode):
        raise RuntimeError(f"{path} is not a directory")
    if info.st_uid != os.getuid():
        raise RuntimeError(f"{path} is owned by uid {info.st_uid}, not by this user")
    if info.st_mode & 0o077:
        raise RuntimeError(f"{path} has mode {stat.S_IMODE(info.st_mode):04o}; expected 0700")
    return path


def free_port(also: str | None = None) -> int:
    """also: a second address that must be free at the port too, such as a bridge address."""
    claims = claims_dir()
    ports = list(port_range())
    random.shuffle(ports)  # each port is tried once, so the walk ends only when none is free
    for port in ports:
        fd = os.open(claims / str(port), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
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
