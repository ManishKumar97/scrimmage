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
from pathlib import Path

from scrimmage_core import plan as planner
from scrimmage_core.config import Config, start_args
from scrimmage_core.records import RecordStore
from scrimmage_core.redact import remove_values
from scrimmage_core.service_client import Setup

LAST_LINES = 20  # how much of service.log a failure shows
TAIL_BYTES = 64 * 1024  # how much of service.log is read to find those lines
GONE_S = 1.0  # how long a failed request waits to see that the copy has exited
# Where /proc cannot say whether the copy listens on its port, how long a failed probe waits
# to see that the copy has exited: one that lost its port to another process exits once
# uvicorn has loaded the app and tried to bind, about 1 s for the example app.
FAILED_PROBE_S = 3.0
LOST_PORT_STEP_S = 0.1
LISTEN = "0A"  # a listening socket's state in /proc/net/tcp
STOP_SIGNALS = {signal.SIGINT, signal.SIGTERM}
STARTED = "the service started with [agent] start"
# A copy whose port another process took between the pick and its bind is started again on
# a new port, up to this many starts in all.
START_TRIES = 3
# How a server says its bind failed: uvicorn and Python "address already in use", Node
# "EADDRINUSE", Gunicorn "Connection in use". The line must also name the copy's port.
IN_USE = re.compile(r"\bin use\b|EADDRINUSE", re.IGNORECASE)


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
            if fields[3] == LISTEN and int(fields[1].rsplit(":", 1)[1], 16) == port:
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
        from scrimmage_core.runner import owner_only  # the runner imports this module

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
                [sys.executable, "-m", "scrimmage_core.run_command", *self.words()],
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
        """The copy exited because another process took its port before it bound it: a
        line of this start's output says an address is in use and names the copy's port.
        Never for a copy that still runs, failed another way, or could not bind a port the
        command fixes itself, which a new port would not change."""
        if self.proc is None or self.proc.poll() is None:
            return False
        if self.words() == start_args(self.config.agent, self.port + 1):
            return False  # the port is not in the command, so another would not help
        try:
            with self.log.open("rb") as handle:
                handle.seek(self.log_from)
                text = handle.read(TAIL_BYTES).decode("utf-8", "replace")
        except OSError:
            return False
        port = re.compile(rf"(?<!\d){self.port}(?!\d)")
        return any(IN_USE.search(line) and port.search(line) for line in text.splitlines())

    def start_again(self, port: int, config: Config) -> bool:
        """After port_taken: the copy's group reaped, then the copy started on port, its
        output added to the same log. False when a stop came first, so none is started."""
        from scrimmage_core.runner import stop_groups  # the runner imports this module

        with self.lock:
            if self.stopped or self.proc is None:
                return False
            stop_groups([self.proc])  # a worker it left behind would hold the old port
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
        answered. Where /proc cannot say, the wait is FAILED_PROBE_S."""
        while time.monotonic() < until:
            if self.gone(LOST_PORT_STEP_S):
                return True
            held = self.holds_port()
            if held is None:
                return self.gone(FAILED_PROBE_S)
            if held:
                return False
        return False

    def holds_port(self) -> bool | None:
        """A process of the copy's group listens on its port; None where /proc cannot say."""
        sockets = listening(self.port)
        if sockets is None or self.proc is None:
            return None
        for entry in Path("/proc").iterdir():
            try:
                if not entry.name.isdigit() or os.getpgid(int(entry.name)) != self.proc.pid:
                    continue
                if any(os.readlink(fd) in sockets for fd in (entry / "fd").iterdir()):
                    return True
            except OSError:  # it ended while it was read, or is another user's
                continue
        return False

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
        from scrimmage_core.runner import stop_groups  # the runner imports this module

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
