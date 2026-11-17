"""A test copy of a web service, started by the runner with [agent] start, and stopped by it.

The runner picks a free port, writes its own token to a file only this user can read, and
starts the copy through scrimmage-run, in a session of its own. The copy's environment holds
the settings a service needs and no other SCRIMMAGE_ variable. Stopping its process group
stops the supervisor and every worker; the token's folder goes only after that, so a worker
that uvicorn restarts mid-test can still read it.
"""

import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

from scrimmage_core import plan as planner
from scrimmage_core.config import Config, start_args
from scrimmage_core.records import RecordStore
from scrimmage_core.redact import remove_values
from scrimmage_core.service_client import Setup

LAST_LINES = 20  # how much of service.log a failure shows
TAIL_BYTES = 64 * 1024  # how much of service.log is read to find those lines
GONE_S = 1.0  # how long a failed request waits to see that the copy has exited
STARTED = "the service started with [agent] start"


def free_port() -> int:
    """A port nothing holds on 127.0.0.1. Another process can take it before the copy binds it;
    the copy then exits, and its log says the address is in use."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


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
        )
        return env

    def start(self, control_url: str) -> None:
        """Start the copy. Called from the main thread, which lives until the copy is stopped:
        Linux kills the copy's supervisor when the thread that started it exits."""
        from scrimmage_core.runner import owner_only  # the runner imports this module

        self.token_folder = Path(tempfile.mkdtemp(prefix="scrimmage-token-"))  # mode 0700
        token_file = self.token_folder / "token"
        fd = owner_only(token_file)
        with os.fdopen(fd, "w") as out:
            out.write(self.token)
        words = start_args(self.config.agent, self.port)
        log = owner_only(self.log)
        try:
            self.proc = subprocess.Popen(
                [sys.executable, "-m", "scrimmage_core.run_command", *words],
                cwd=self.config.root,
                env=self.env(control_url, token_file),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,  # its own process group, so every worker stops too
            )
        finally:
            os.close(log)

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def gone(self) -> bool:
        """The copy has exited. A request it failed can come a moment before its exit."""
        if self.proc is None:
            return False
        try:
            self.proc.wait(timeout=GONE_S)
        except subprocess.TimeoutExpired:
            return False
        return True

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

    def failed(self, what: str) -> Setup:
        """Exit 3, with what happened, the log's last lines and, when it is kept, its path."""
        lines = self.last_lines()
        said = f"{STARTED} {what}"
        said += f"; its last lines:\n{lines}" if lines else "; it wrote nothing"
        if self.keep_log:
            said += f"\nits whole output is in {self.log}"
        return Setup(3, said)

    def exited(self, when: str) -> Setup:
        """The copy has exited: how, and when, such as before it was ready."""
        assert self.proc is not None and self.proc.returncode is not None
        code = self.proc.returncode
        how = f"code {code}" if code >= 0 else f"signal {signal.Signals(-code).name}"
        return self.failed(f"exited with {how} {when}")

    def stop(self) -> None:
        """SIGTERM to the copy's group, SIGKILL after the runner's grace, then reap it; then the
        log is scrubbed and the token's folder removed."""
        from scrimmage_core.runner import stop_groups  # the runner imports this module

        try:
            if self.proc is not None:
                stop_groups([self.proc])
        finally:
            self.store.scrub_file(self.log)
            if self.token_folder is not None:
                shutil.rmtree(self.token_folder, ignore_errors=True)
