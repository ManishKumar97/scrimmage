"""Pass signals on to a child process: the agent, or a wrapped MCP server.

Faultline stays up until the child exits, so the trace is always finished. Ctrl-C,
Ctrl-\\, SIGTERM and SIGHUP go to the child, and faultline waits for it.

If faultline itself dies without a chance to pass anything on (SIGKILL, a crash),
the child must not run on alone. On Linux the child starts through a small shim
that asks the kernel to send it SIGTERM when its parent dies (PR_SET_PDEATHSIG),
then becomes the child's own program. The setting survives the exec.
"""

import asyncio
import os
import signal
import sys
from collections.abc import Sequence

RELAYED = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)
# The terminal sends these to the whole foreground process group, the child included.
KEYBOARD = (signal.SIGINT, signal.SIGQUIT)

# Runs in the child before its program. Exits 127 or 126 as a shell does when the
# program cannot start.
SHIM = """
import ctypes, os, signal, sys
label, parent, command = sys.argv[1], int(sys.argv[2]), sys.argv[3:]
ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM)  # 1 is PR_SET_PDEATHSIG
if os.getppid() != parent:
    sys.exit(1)  # the parent died before the setting took
try:
    os.execvp(command[0], command)
except OSError as error:
    print(f"{label}: cannot start {command[0]}: {error}", file=sys.stderr)
    sys.exit(127 if isinstance(error, FileNotFoundError) else 126)
"""


def tied_to_parent(command: Sequence[str], label: str) -> list[str]:
    """The command, started so it dies with this process where the system allows."""
    if sys.platform != "linux":
        return list(command)
    return [sys.executable, "-I", "-c", SHIM, label, str(os.getpid()), *command]


class Relay:
    """Sends signals on to the child. Faultline waits for the child to exit.

    It is in place before the child starts. A signal that comes while the child is
    starting is held, then sent once the child runs.
    """

    def __init__(self) -> None:
        self.child: asyncio.subprocess.Process | None = None
        self.held: list[signal.Signals] = []

    def receive(self, number: signal.Signals) -> None:
        if number in KEYBOARD and terminal_sent_it_to_the_child():
            return  # the key press reached the whole foreground group, the child included
        if self.child is None:
            self.held.append(number)
        elif self.child.returncode is None:
            self.child.send_signal(number)

    def attach(self, child: asyncio.subprocess.Process) -> None:
        self.child = child
        for number in self.held:
            child.send_signal(number)
        self.held.clear()


def terminal_sent_it_to_the_child() -> bool:
    """True when we, and so the child, are the terminal's foreground process group."""
    try:
        return os.tcgetpgrp(sys.stdin.fileno()) == os.getpgrp()
    except (OSError, ValueError):
        return False  # no terminal: the signal was sent to faultline alone
