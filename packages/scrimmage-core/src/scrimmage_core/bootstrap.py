"""Switch the SDK on in a program scrimmage-run started with SCRIMMAGE_TOKEN set.

The program the runner started fetches its plan before the team's code runs. If it cannot,
the program ends here, so the agent never runs without its plan. A child process prints
nothing and fetches its plan at its first gated call, so a short command costs nothing.
A child whose plan cannot be fetched refuses each call, and tries again for the next one.
"""

import atexit
import contextlib
import ipaddress
import os
import signal
import stat
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from types import FrameType
from typing import Literal, NoReturn
from urllib.parse import urlsplit

from scrimmage_core import (
    http_gate,
    labels,
    langchain_gate,
    mcp_gate,
    process_gate,
    records,
    state,
    threadctx,
    threads,
    tool_gate,
)
from scrimmage_core.client import ControlClient, ControlError, Sender, Senders
from scrimmage_core.mocks import allowed
from scrimmage_core.plan import Plan, no_plan, tools_from
from scrimmage_core.redact import remove_values

TERM_WAIT_S = 4.0  # under the runner's five seconds before SIGKILL
# What a process is: the program the runner started, a child of it, or a service process.
Mode = Literal["program", "child", "service"]


def stop(message: str) -> NoReturn:
    print(
        f"scrimmage: {message}; the program stops before the agent runs",
        file=sys.stderr,
    )
    sys.stderr.flush()
    os._exit(1)


def service_stop(message: str) -> NoReturn:
    """A service process that cannot gate its requests: it stops before it serves one."""
    print(f"scrimmage: {message}; the service stops before it serves a request", file=sys.stderr)
    sys.stderr.flush()
    os._exit(1)


def report_tools(run_state: state.RunState) -> None:
    # A child made only some of the agent's tools, so it does not speak for them.
    if run_state.child:
        return
    # A blocked tool is still one the agent has: its label is spelled right.
    tools = sorted(run_state.seen | run_state.blocked.keys())
    run_state.notice("tools_seen", "scrimmage", "the tools the agent has", tools=tools)


def outside(plan: Plan, prefixes: list[str]) -> str | None:
    """The first import path in the plan that SCRIMMAGE_ALLOW_IMPORTS does not cover."""
    raises = [f.raises for f in plan.faults if f.raises and ":" in f.raises]
    paths = [*plan.tools, *(m.type for m in plan.mocks if m.type), *raises]
    return next((path for path in paths if not allowed(path, prefixes)), None)


def script_folder() -> list[str]:
    """Python adds a script's folder to its path after this file has run."""
    script = sys.argv[0]
    return [] if script in ("", "-c", "-m") else [os.path.dirname(os.path.abspath(script))]


def runs_as_main(module: str, target: str | None, folders: list[str]) -> bool:
    """Whether this module is the program itself, which runs as __main__ and is never
    imported, so no import can put a gate on its tools. target is the -m module, if any."""
    if target is not None:
        return module in (target, f"{target}.__main__")
    spec = tool_gate.module_spec(module, folders)
    if spec is None or spec.origin is None or sys.argv[0] in ("", "-c"):
        return False
    return os.path.realpath(spec.origin) == os.path.realpath(sys.argv[0])


def handle_sigterm(last_lines: Callable[[], None], sender: Sender) -> None:
    """On SIGTERM send the last record lines, then end as Python would have with no handler.

    The handler runs on the main thread, which may hold a lock the lines need, so a thread
    does the sending and the wait for it has a limit. A program with a handler of its own
    replaces this one. The sender thread has then sent all but the last 100 ms.

    A signal that arrives while this thread is itself sending the exit batches waits for them
    to end: they hold the send lock, and the thread that would send the rest needs it. That
    wait has no limit of its own, so a drain stuck on a silent runner ends at the runner's
    SIGKILL, exit 137, about a second later than the four seconds above. No line is lost to
    it that the old path kept.
    """
    if signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL:
        return

    def on_term(signum: int, frame: FrameType | None) -> None:
        del frame
        signal.signal(signum, signal.SIG_IGN)  # the runner may send it twice

        def end() -> None:
            try:
                worker = threading.Thread(target=last_lines, daemon=True)
                threads.start(worker)
                worker.join(TERM_WAIT_S)
            except RuntimeError:  # Python is already shutting down, so no new thread
                pass
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)

        if not sender.defer_to_drain(end):
            end()

    signal.signal(signal.SIGTERM, on_term)


TOKEN_MIN = 32  # characters a service's token needs: the MAC of a run header rests on it


def read_token_file(path: str) -> str:
    """The token, from a plain file this user owns, mode 0600 exactly. Else the process stops:
    a token another user could read is not the boundary the design rests on."""
    try:
        fd = records.open_owned(Path(path))
    except (OSError, records.Refused) as exc:
        service_stop(f"SCRIMMAGE_TOKEN_FILE {path} cannot be read: {exc}")
    with os.fdopen(fd, "rb") as handle:
        mode = stat.S_IMODE(os.fstat(handle.fileno()).st_mode)
        data = handle.read(4096)
    if mode != 0o600:
        service_stop(
            f"SCRIMMAGE_TOKEN_FILE {path} has mode {mode:o}; it must be 600, readable by its "
            "owner only"
        )
    token = data.decode("utf-8", "replace").strip()
    if len(token) < TOKEN_MIN:
        service_stop(f"SCRIMMAGE_TOKEN_FILE {path} holds a token under {TOKEN_MIN} characters")
    return token


def loopback(url: str) -> bool:
    """http:// on this machine: TLS to a control server elsewhere comes in a later version."""
    parts = urlsplit(url)
    if parts.scheme != "http" or not parts.hostname:
        return False
    if parts.hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(parts.hostname).is_loopback
    except ValueError:
        return False


def start_service() -> None:
    """A service process: the uvicorn supervisor, each worker, a reloader. None has a run; each
    request brings its own. Nothing here asks the runner for anything, so a service with no
    runner serves every request without the header as it would without Scrimmage."""
    # Only a service needs these, and a function or a script must not pay for their import.
    from scrimmage_core import asgi_gate, executor

    if "SCRIMMAGE_TOKEN" in os.environ:
        service_stop(
            "SCRIMMAGE_TOKEN is set, and a service reads its token from SCRIMMAGE_TOKEN_FILE only, "
            "so no environment holds it; unset SCRIMMAGE_TOKEN and set SCRIMMAGE_TOKEN_FILE"
        )
    path = os.environ.get("SCRIMMAGE_TOKEN_FILE", "")
    token = read_token_file(path)
    if "spawn_main" in " ".join(getattr(sys, "orig_argv", [])):
        # A worker uvicorn spawned: the team's own children never learn the file's path.
        os.environ.pop("SCRIMMAGE_TOKEN_FILE", None)
    url = os.environ.get("SCRIMMAGE_CONTROL_URL", "")
    if not loopback(url):
        service_stop(
            f"SCRIMMAGE_CONTROL_URL {url!r} must be http:// on this machine in this version; "
            "a control server elsewhere needs TLS, which comes later"
        )
    prefixes = [p for p in os.environ.get("SCRIMMAGE_ALLOW_IMPORTS", "").split(",") if p]
    tools, renames = tools_from(os.environ.get("SCRIMMAGE_TOOLS", ""))
    names = tuple(labels.tool_name(path, dict(renames)) for path in tools)
    process = state.ProcessState(names, service=True)
    process.stop = service_stop
    state.install_process(process)
    client = ControlClient(url, token)
    senders = Senders()
    pool = executor.Executor()
    executor.EXECUTOR = pool
    asgi_gate.GATE = asgi_gate.Gate(process, client, token, prefixes, tools, renames, senders, pool)
    folders = [os.getcwd(), *script_folder()]
    for module, tool_names in tool_gate.targets(tools).items():
        if tool_gate.module_spec(module, folders) is None:
            service_stop(f"{module} is listed in SCRIMMAGE_TOOLS but cannot be found")
        del tool_names
    tool_gate.install(tools, dict(renames))
    http_gate.install(process, client, None, service_stop)
    langchain_gate.install(process, service_stop)
    mcp_gate.install(process, None, service_stop)
    process_gate.install(lambda: None)
    process_gate.install_service()
    threadctx.install(process)
    asgi_gate.install(process)
    senders.start()


def start(*, mode: Mode = "program", target: str | None = None) -> None:
    child = mode == "child"
    token = os.environ.get("SCRIMMAGE_TOKEN", "")
    url = os.environ.get("SCRIMMAGE_CONTROL_URL", "")
    run = os.environ.get("SCRIMMAGE_RUN", "")
    if not token or not url or not run:
        # A script's child that lost its run still fails closed. A service has no run at
        # startup, and says so with SCRIMMAGE_SERVICE=1.
        stop(
            "SCRIMMAGE_TOKEN, SCRIMMAGE_CONTROL_URL and SCRIMMAGE_RUN must be set; to start a "
            "web service under scrimmage-run, set SCRIMMAGE_SERVICE=1 and SCRIMMAGE_TOKEN_FILE"
        )
    prefixes = [p for p in os.environ.get("SCRIMMAGE_ALLOW_IMPORTS", "").split(",") if p]
    plan: Plan | None = None
    try:
        client = ControlClient(url, token)
        if not child:
            plan = Plan.from_json(client.fetch_plan(run))
    except (ControlError, ValueError) as exc:
        stop(f"cannot get the plan for {run} from {url}: {exc}")
    if plan is not None and (path := outside(plan, prefixes)) is not None:
        stop(f"{path} is outside SCRIMMAGE_ALLOW_IMPORTS ({','.join(prefixes) or 'empty'})")

    def load() -> Plan:
        """A child's plan, at its first gated call. With no plan, the call is refused."""
        try:
            fetched = Plan.from_json(client.fetch_plan(run))
        except (ControlError, ValueError) as exc:
            return no_plan(run, str(exc))
        path = outside(fetched, prefixes)
        return fetched if path is None else no_plan(run, f"{path} is outside the allowed imports")

    # G2: a child's code imports its tools before any plan could arrive.
    tools, renames = tools_from(os.environ.get("SCRIMMAGE_TOOLS", ""))
    names = tuple(labels.tool_name(path, dict(renames)) for path in tools)
    sender = Sender(client, run, eager=child)
    process = state.ProcessState(names if plan is None else plan.tool_names())
    process.stop = stop
    run_state = state.RunState(
        plan, sender, prefixes, load=load, tools=names, blobs=sender, process=process
    )
    sender.proc = run_state.proc
    sender.on_closed = run_state.run_closed
    if plan is not None:
        tools, renames = plan.tools, plan.renames
        sender.start()
        run_state.announce()
    atexit.register(sender.flush)
    atexit.register(run_state.stop_open)  # a call on a daemon thread, after the replies below
    if not tools and not child:  # registered after the flush, so it runs first
        # With no tools list the runner cannot know the names; a framework's adapter does.
        atexit.register(lambda: report_tools(run_state))
    state.install(run_state)
    folders = [os.getcwd(), *script_folder()]
    for module, tool_names in tool_gate.targets(tools).items():
        message = None
        if runs_as_main(module, target, folders):
            message = (
                f"{module} is the program itself, so {tool_names[0]} cannot be gated. Move the "
                "tool into a module the program imports"
            )
        elif not child and tool_gate.module_spec(module, folders) is None:
            message = f"{module} is listed under [agent] tools but cannot be found"
        if message is not None:
            run_state.cannot_gate(tool_names[0], message)
            sender.flush()
            stop(message)
    tool_gate.install(tools, dict(renames))

    def cannot_gate(message: str) -> None:
        sender.flush()
        stop(message)

    ctx = http_gate.install(process, client, sender, cannot_gate)
    langchain_gate.install(process, cannot_gate)
    mcp_gate.install(process, sender, cannot_gate)

    def forked() -> None:
        run_state.forked()  # which drops the replies the parent is reading
        sender.forked(run_state.proc)

    os.register_at_fork(after_in_child=forked)

    def last_lines() -> None:
        ctx.close_open()
        run_state.stop_open()
        sender.flush()

    def end_program() -> None:
        """scrimmage init --probe has what it came for, so no more of the team's code runs.

        os._exit skips the flush Python does at exit, so what the agent printed is flushed here,
        first: sending the last lines delivers the refusal, and the runner then signals this
        process, which ends it before anything after that point.
        """
        for stream in (sys.stdout, sys.stderr):
            with contextlib.suppress(BaseException):  # a stream the agent closed or replaced, even
                # one whose flush raises SystemExit: os._exit below must still run
                stream.flush()
        last_lines()
        os._exit(0)

    run_state.end_program = end_program

    def before_exec() -> None:
        """exec ends this image with no exit steps, and if it fails the program goes on."""
        last_lines()
        sender.eager = True

    process_gate.install(before_exec)
    handle_sigterm(last_lines, sender)
    if plan is not None:
        # The test's name can hold a value from the environment, and this line is in the log.
        line = f"active for run {run} ({plan.test}, {plan.row}); calls follow this test's plan"
        print(f"scrimmage: {remove_values(line, plan.scrub)[0]}", file=sys.stderr)
