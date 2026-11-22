"""A test copy of a web service, started by the runner with [agent] start, and stopped by it.

The runner picks a free port, writes its own token to a file only this user can read, and
starts the copy through scrimmage-run, in a session of its own. The copy's environment holds
the settings a service needs and no other SCRIMMAGE_ variable. Stopping its process group
stops the supervisor and every worker; the token's folder goes only after that, so a worker
that uvicorn restarts mid-test can still read it.
"""

import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

from scrimmage import plan as planner
from scrimmage.config import Config, start_args
from scrimmage.records import RecordStore
from scrimmage.redact import remove_values
from scrimmage.service_client import Setup

LAST_LINES = 20  # how much of service.log a failure shows
TAIL_BYTES = 64 * 1024  # how much of service.log is read to find those lines
GONE_S = 1.0  # how long a failed request waits to see that the copy has exited
LOST_PORT_STEP_S = 0.1
LISTEN = "0A"  # a listening socket's state in /proc/net/tcp
# States in /proc/net/tcp of a socket that no longer holds its port against a new bind.
RELEASED = {"06", "07"}  # TIME_WAIT, CLOSE
STOP_SIGNALS = {signal.SIGINT, signal.SIGTERM}
STARTED = "the service started with [agent] start"
# A copy whose port another process took between the pick and its bind is started again on
# a new port, up to this many starts in all.
START_TRIES = 3
# How a server says its bind failed, as uvicorn and Python "address already in use" or
# "Errno 98", and Node "EADDRINUSE". The same line must name the copy's port as an address
# does, ":PORT" or ", PORT)", for that port to be the one taken.
IN_USE = re.compile(r"address already in use|EADDRINUSE|Errno 98", re.IGNORECASE)
# An address with a port: a Python socket tuple, as uvicorn and gunicorn print ("127.0.0.1",
# 8000) or ('::1', 8000, 0, 0), or host:port with an IPv4, bracketed IPv6, localhost or
# dotted host, or ":::PORT". A time such as 18:37:33 is not one.
ADDRESS = re.compile(
    r"\('[^']*', \d+[,)]"
    r"|(?<![\w.:])(?:\d{1,3}(?:\.\d{1,3}){3}|\[[0-9a-fA-F:]+\]|localhost|[A-Za-z][\w-]*(?:\.[\w-]+)+):\d+(?!\d)"
    r"|:::\d+(?!\d)"
)


class CopyFailed(Setup):
    """Exit 3 that already names the copy and its last lines."""


def free_port() -> int:
    """A port nothing holds on 127.0.0.1. Another process can take it before the copy binds it;
    the copy then exits, its log says the address is in use, and it is started again."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def listening(port: int) -> set[str] | None:
    """The sockets listening on this port, as a process's fd links name them, from
    /proc/net/tcp and tcp6; None on a system without them, such as macOS."""
    return sockets_on(port, listen_only=True)


def sockets_on(port: int, *, listen_only: bool = False) -> set[str] | None:
    """The sockets whose own end is this port and that hold it against a new bind, a
    listener or a connection; listen_only: listeners only. None without /proc.

    A row with inode 0 is left out: an orphan the kernel keeps closing, such as the dead
    copy's own accepted connection in FIN_WAIT2, which no process holds and which does not
    stop a bind with SO_REUSEADDR."""
    found: set[str] = set()
    read = False
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            rows = Path(table).read_text().splitlines()[1:]
        except OSError:
            continue
        read = True
        for row in rows:
            fields = row.split()
            state = fields[3]
            held = state == LISTEN if listen_only else state not in RELEASED
            orphan = fields[9] == "0"
            if held and not orphan and int(fields[1].rsplit(":", 1)[1], 16) == port:
                found.add(f"socket:[{fields[9]}]")
    return found if read else None


class Managed:
    """One copy of the service, for one test or one scrimmage check --service."""

    def __init__(
        self, config: Config, port: int, token: str, store: RecordStore, *, keep_log: bool
    ) -> None:
        """keep_log: the log stays after the test, so a failure names its path."""
        self.config = config
        self.port = port
        self.token = token
        self.store = store
        self.log = store.folder / "service.log"
        self.keep_log = keep_log
        self.proc: subprocess.Popen[bytes] | None = None
        self.token_folder: Path | None = None
        self.control_url = ""
        self.ports = [port]  # one for each start: a port taken before the bind gets another
        self.log_from = 0  # where this start's output begins in service.log
        # The runner's stop thread stops the copy on a stop signal, and the test's end stops
        # it too: one at a time, and once.
        self.lock = threading.Lock()
        self.stopped = False

    def env(self, control_url: str, token_file: Path) -> dict[str, str]:
        """The runner's environment with every SCRIMMAGE_ variable dropped, so a value left
        from attach mode never reaches the copy, then the settings it needs."""
        agent = self.config.agent
        env = {k: v for k, v in os.environ.items() if not k.startswith("SCRIMMAGE_")}
        env.update(
            SCRIMMAGE_SERVICE="1",
            SCRIMMAGE_TOKEN_FILE=str(token_file),
            SCRIMMAGE_CONTROL_URL=control_url,
            SCRIMMAGE_TOOLS=planner.tools_text(agent.tools, agent.renames),
            SCRIMMAGE_ALLOW_IMPORTS=",".join(agent.allowed_imports()),
            SCRIMMAGE_RUNNER_PID=str(os.getpid()),  # run_command asks to be stopped with it
            SCRIMMAGE_MANAGED="1",  # each worker asks to be stopped with its supervisor
        )
        return env

    def start(self, control_url: str) -> None:
        """Start the copy. Called from the main thread, which lives until the copy is stopped:
        Linux kills the copy's supervisor when the thread that started it exits."""
        from scrimmage.runner import owner_only  # the runner imports this module

        with self.lock:  # a stop on a signal waits until the copy has started, or comes first
            self.token_folder = Path(tempfile.mkdtemp(prefix="scrimmage-token-"))  # mode 0700
            token_file = self.token_folder / "token"
            fd = owner_only(token_file)
            with os.fdopen(fd, "w") as out:
                out.write(self.token)
            self.control_url = control_url
            self.launch(owner_only(self.log))

    def launch(self, log: int) -> None:
        """The copy's process, on self.port, its output to log, which it closes."""
        assert self.token_folder is not None
        try:
            self.log_from = os.fstat(log).st_size
            self.proc = subprocess.Popen(
                [sys.executable, "-P", "-m", "scrimmage.run_command", *self.words()],
                cwd=self.config.root,
                env=self.env(self.control_url, self.token_folder / "token"),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,  # its own process group, so every worker stops too
            )
        finally:
            os.close(log)

    def words(self) -> list[str]:
        return start_args(self.config.agent, self.port)

    def port_taken(self) -> bool:
        """The copy exited because another process took its port before it bound it. The
        copy's log decides first: a line saying an address is in use names the port, or
        names another address, which a new port would not change. Where no line names an
        address, a socket of no process of the copy's group holds the port, as /proc says;
        without /proc, it is not taken. Never for a copy that still runs, exited with code
        0, as a start that forks a daemon does, or whose command fixes its own port."""
        if self.proc is None or self.proc.poll() is None:
            return False
        if self.proc.returncode == 0:  # a failed bind is never a clean exit; a daemon's is
            return False
        if self.words() == start_args(self.config.agent, self.port + 1):
            return False  # the port is not in the command, so another would not help
        said = self.bind_errors()
        if any(self.names_port(line) for line in said):
            return True
        if any(ADDRESS.search(line) for line in said):
            return False  # its bind failed on another address, whatever else holds this port
        held = sockets_on(self.port)
        if held is not None:
            return bool(held - self.group_sockets())
        return False

    def group_sockets(self) -> set[str]:
        """What the fds of the copy's group's processes link to, its sockets among them."""
        found: set[str] = set()
        if self.proc is None:
            return found
        for entry in Path("/proc").iterdir():
            try:
                if not entry.name.isdigit() or os.getpgid(int(entry.name)) != self.proc.pid:
                    continue
                found.update(os.readlink(fd) for fd in (entry / "fd").iterdir())
            except OSError:  # it ended while it was read, or is another user's
                continue
        return found

    def names_port(self, line: str) -> bool:
        """A line names the copy's port as an address does, ":PORT", ", PORT)" or ", PORT,"."""
        return re.search(rf"(?::{self.port}|, {self.port}[,)])(?!\d)", line) is not None

    def bind_errors(self) -> list[str]:
        """The lines of this start's last output that say an address is in use. When the
        copy bound a port of its own that another process holds, they name that port, and
        a socket on the picked port that /proc shows is not the cause."""
        try:
            with self.log.open("rb") as handle:
                handle.seek(max(self.log_from, self.log.stat().st_size - TAIL_BYTES))
                text = handle.read().decode("utf-8", "replace")
        except OSError:
            return []
        return [line for line in text.splitlines() if IN_USE.search(line)]

    def start_again(self, port: int, config: Config, stopping: Callable[[], bool]) -> bool:
        """After port_taken: the copy's group reaped, then the copy started on port, its
        output added to the same log. False when a stop came first, or during the reap as
        stopping says, so none is started."""
        from scrimmage.runner import stop_groups  # the runner imports this module

        with self.lock:
            if self.stopped or self.proc is None:
                return False
            stop_groups([self.proc])  # a process it left behind would live on unstopped
            if stopping():  # a stop signal came during the reap; its stop waits on the lock
                return False
            self.config, self.port = config, port
            self.ports.append(port)
            self.launch(os.open(self.log, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW))
            return True

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def gone(self, within: float = GONE_S) -> bool:
        """The copy has exited, or does within that many seconds. A request it failed can
        come a moment before its exit."""
        if self.proc is None:
            return False
        try:
            self.proc.wait(timeout=within)
        except subprocess.TimeoutExpired:
            return False
        return True

    def lost_its_port(self, until: float) -> bool:
        """After a probe that something other than a gated copy answered: whether the copy
        exits, which one whose port another process took does once it has loaded the app.

        While the copy runs and none of its processes listens on its port, it is still
        loading the app, so the wait goes on, up to until; once one listens, the copy is what
        answered. Where /proc cannot say, the wait for its exit goes on up to until too: a
        copy that loses its port exits only once it has loaded the app and tried to bind,
        which takes as long as its imports do on a loaded machine, so no shorter wait is
        safe. A copy that answers without the gate is then named at until."""
        while time.monotonic() < until:
            if self.gone(LOST_PORT_STEP_S):
                return True
            held = self.holds_port()
            if held is None:
                return self.gone(max(0.0, until - time.monotonic()))
            if held:
                return False
        return False

    def holds_port(self) -> bool | None:
        """A process of the copy's group listens on its port; None where /proc cannot say."""
        sockets = listening(self.port)
        if sockets is None or self.proc is None:
            return None
        return bool(sockets & self.group_sockets())

    def last_lines(self) -> str:
        """The log's last lines, each value ${NAME} filled in removed."""
        try:
            with self.log.open("rb") as handle:
                handle.seek(max(0, self.log.stat().st_size - TAIL_BYTES))
                tail = handle.read().decode("utf-8", "replace")
        except OSError:
            return ""
        text = remove_values(tail, self.config.scrub)[0]
        lines = [line for line in text.splitlines() if line.strip()][-LAST_LINES:]
        return "\n".join(f"  {line}" for line in lines)

    def failed(self, what: str) -> CopyFailed:
        """Exit 3, with what happened, the log's last lines and, when it is kept, its path."""
        lines = self.last_lines()
        said = f"{STARTED} {what}"
        said += f"; its last lines:\n{lines}" if lines else "; it wrote nothing"
        if self.keep_log:
            said += f"\nits whole output is in {self.log}"
        return CopyFailed(3, said)

    def exited(self, when: str) -> CopyFailed:
        """The copy has exited: how, and when, such as before it was ready."""
        assert self.proc is not None and self.proc.returncode is not None
        code = self.proc.returncode
        how = f"code {code}" if code >= 0 else f"signal {signal.Signals(-code).name}"
        if len(self.ports) > 1:
            when += f", after {len(self.ports)} starts on ports {', '.join(map(str, self.ports))}"
        return self.failed(f"exited with {how} {when}")

    def stop(self) -> None:
        """SIGTERM to the copy's group, SIGKILL after the runner's grace, then reap it; then the
        log is scrubbed and the token's folder removed, only once the group is gone.

        The runner's stop thread calls it on Ctrl-C or SIGTERM, so a run in flight ends at
        once, and the test's end calls it too: the second call waits for the first, and does
        nothing more. A call before the start does nothing, so the start still happens and
        the test's end stops that copy.

        SIGINT and SIGTERM are blocked in this thread while the group stops, so a second
        Ctrl-C cannot cut the grace short and skip the SIGKILL. Python still runs its handler
        on the main thread, so each caller has its own handler in place that only notes them.
        """
        from scrimmage.runner import stop_groups  # the runner imports this module

        with self.lock:
            if self.stopped or self.token_folder is None:  # stopped, or not started yet
                return
            try:
                if self.proc is not None:
                    held = signal.pthread_sigmask(signal.SIG_BLOCK, STOP_SIGNALS)
                    try:
                        stop_groups([self.proc])
                    finally:
                        signal.pthread_sigmask(signal.SIG_SETMASK, held)
            finally:
                self.store.scrub_file(self.log)
            # Not after a failed stop: a worker may still run.
            shutil.rmtree(self.token_folder, ignore_errors=True)
            self.stopped = True
