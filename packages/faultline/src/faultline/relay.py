"""Pass Ctrl-C and SIGTERM on to a child process: the agent, or a wrapped MCP server."""

import asyncio
import os
import signal
import sys

RELAYED = (signal.SIGINT, signal.SIGTERM)


class Relay:
    """Sends Ctrl-C and SIGTERM on to the agent. Faultline waits for the agent to exit.

    It is in place before the agent starts. A signal that comes while the agent is
    starting is held, then sent once the agent runs.
    """

    def __init__(self) -> None:
        self.child: asyncio.subprocess.Process | None = None
        self.held: list[signal.Signals] = []

    def receive(self, number: signal.Signals) -> None:
        if number == signal.SIGINT and terminal_sent_it_to_the_child():
            return  # Ctrl-C reached the whole foreground group, the child included
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
