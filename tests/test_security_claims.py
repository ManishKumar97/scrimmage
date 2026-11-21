"""Claims on the security page that no other test held: where the control server listens,
that it is the only connection Scrimmage makes, the record folder's mode, each secret shape
redaction removes, a known gap in redaction, the numbers the page states, and that no fault
but the carried ones sends a request."""

import asyncio
import contextlib
import http.server
import json
import re
import stat
import threading
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import ClassVar

import httpx2
import mcp
import mcp.client.session as mcp_session
import pytest
from mcp.server.mcpserver import MCPServer

from conftest import ACME, Project
from fakes.openai_server import FakeOpenAI
from scrimmage import config, http_gate, mcp_gate, protocol, state, tool_gate
from scrimmage.config import Fault, ModelHost
from scrimmage.control import ControlServer
from scrimmage.errors import ConfigError
from scrimmage.plan import Plan
from scrimmage.records import RecordStore
from scrimmage.redact import REMOVED, redact
from test_connection_faults import never_stopped
from test_http_gate import Budget, Sink
from test_stdlib_only import imported_roots

TOKEN = "f00d" * 16
ROOT = Path(__file__).parents[1]
PACKAGES = ROOT / "packages"
SDK = PACKAGES / "scrimmage" / "src" / "scrimmage"
# Every standard-library name that opens a connection or listens for one.
NETWORK = re.compile(
    r"\b(HTTPS?Connection|urlopen|urlretrieve|create_connection|open_connection|"
    r"open_unix_connection|create_server|start_server|SMTP(?:_SSL)?|FTP(?:_TLS)?|"
    r"ServerProxy|ThreadingHTTPServer|HTTPServer|TCPServer|UDPServer|wrap_socket|"
    r"socket(?=\())\b"
)


@pytest.mark.design("security.no_telemetry")
def test_the_only_connections_are_to_the_control_server_and_the_service() -> None:
    """A fast check by name. test_security_audit.py watches a script run do the same; no test
    watches a service run's connections."""
    found = {
        (path.name, name)
        for path in PACKAGES.rglob("*.py")
        for name in NETWORK.findall(path.read_text())
    }
    assert found == {
        ("client.py", "HTTPConnection"),
        ("client.py", "HTTPSConnection"),  # the same control server, over TLS
        ("control.py", "ThreadingHTTPServer"),
        ("control.py", "wrap_socket"),  # each accepted connection, in TLS
        # The runner's own request to a team's service, at the address its file names.
        ("service_client.py", "HTTPConnection"),
        # It binds 127.0.0.1 port 0 and closes it, to pick the port of a service it starts.
        ("managed.py", "socket"),
        # The one fetch: `scrimmage prices update` reads prices.toml from the repository's main
        # over HTTPS, when a person types it. No test run calls it.
        ("prices_update.py", "urlopen"),
    }
    uses = {
        name
        for path in PACKAGES.rglob("*.py")
        for name in re.findall(r"\bsocket\.(\w+)", path.read_text())
    }
    # SHUT_RDWR: service_client cuts the runner's own probe to the service at its deadline.
    # socket: managed.py's free port, bound and closed, never connected.
    # gaierror, EAI_NONAME: the error http_gate builds for an injected dns fault; no lookup.
    assert uses == {"gethostname", "SHUT_RDWR", "socket", "gaierror", "EAI_NONAME"}


@pytest.mark.design("security.one_fetch")
def test_only_the_prices_update_command_calls_the_one_fetch() -> None:
    """prices_update.py is the one module with a connection out, and the scrimmage command's
    prices update is the only caller: nothing a test run loads reaches it."""
    callers = {
        path.name
        for path in SDK.glob("*.py")
        if path.name != "prices_update.py" and "prices_update" in path.read_text()
    }
    assert callers == {"cli.py"}
    cli_text = (SDK / "cli.py").read_text()
    assert cli_text.count("prices_update.") == 1
    assert "prices_update.update(args.path" in cli_text


# Every standard-library way to start a process, and the modules that hold them.
STARTS = re.compile(
    r"\b(?:subprocess\.\w+\s*\(|Popen\s*\(|os\.(?:exec\w*|spawn\w*|system|popen|posix_spawnp?|"
    r"fork\w*)\s*\(|pty\.spawn\s*\(|ProcessPoolExecutor|multiprocessing\.\w+|"
    r"create_subprocess_\w+|open_process\s*\(|run_process\s*\(|resource_tracker\.ensure_running\s*\()"
)
STARTERS = {"subprocess", "pty", "multiprocessing", "concurrent", "asyncio"}


@pytest.mark.design("security.process_starts_scanned")
def test_the_only_processes_scrimmage_starts_are_its_runs() -> None:
    """Scrimmage's start-up inside the agent runs before an audit hook could watch it, so this
    scan is what checks it: no product module but the runner, the copy of a service that
    [agent] start names, and scrimmage-run starts anything, and the Scrimmage MCP server, a
    command of its own that never loads into the agent, which starts each scrimmage command it
    runs. A service starts Python's resource tracker, once, at its startup, before any
    request."""
    found = {
        (path.name, " ".join(call.split()))
        for path in PACKAGES.rglob("*.py")
        for call in STARTS.findall(path.read_text())
    }
    assert found == {
        ("runner.py", "subprocess.Popen("),
        # The test copy of a service that [agent] start names, through scrimmage-run.
        ("managed.py", "subprocess.Popen("),
        ("run_command.py", "os.execvpe("),
        ("mcp_server.py", "open_process("),
        # It names the module to patch its thread pool when the agent imports it; it never
        # makes a pool or starts a process itself.
        ("threadctx.py", "multiprocessing.pool"),
        # It names these to refuse them under a service run, when the agent imports them.
        ("process_gate.py", "ProcessPoolExecutor"),
        ("process_gate.py", "multiprocessing.connection"),
        ("process_gate.py", "multiprocessing.pool"),
        ("process_gate.py", "multiprocessing.process"),
        ("process_gate.py", "multiprocessing.queues"),
        # It names these to know multiprocessing's own helpers, where Scrimmage stays off.
        ("bootstrap.py", "multiprocessing.resource_tracker"),
        ("bootstrap.py", "multiprocessing.forkserver"),
        # A service's one start of its own: Python's resource tracker, before any request.
        ("process_gate.py", "resource_tracker.ensure_running("),
        # It names this to decide spawnv_passfds's starts; it starts nothing through it.
        ("process_gate.py", "multiprocessing.util"),
    }
    # The same AST scan as the standard-library test, so __import__("subprocess") counts.
    imports = {
        (path.name, name)
        for path in PACKAGES.rglob("*.py")
        for name in imported_roots(path) & STARTERS
    }
    # The runner runs its runs on a pool of threads, and a process pool is caught above.
    # asyncio can start processes too; the modules that use it never call those, which the
    # scan above checks.
    assert imports == {
        ("runner.py", "subprocess"),
        ("runner.py", "concurrent"),
        ("managed.py", "subprocess"),
        ("http_gate.py", "asyncio"),
        ("mcp_gate.py", "asyncio"),
        ("start.py", "asyncio"),
        ("mcp_server.py", "subprocess"),  # for DEVNULL and PIPE; anyio starts the child
        ("mcp_server.py", "asyncio"),  # for the loop its signal handler wakes
        ("threadctx.py", "asyncio"),  # to read a loop's tasks at a run's close
        ("asgi_gate.py", "asyncio"),  # the entry gate runs on the server's loop
        ("executor.py", "concurrent"),  # for its Future: its workers are threads
        ("process_gate.py", "multiprocessing"),  # a service starts the resource tracker
    }


@pytest.mark.design("security.loopback_only")
def test_the_control_server_listens_on_loopback_only(tmp_path: Path) -> None:
    control = ControlServer(TOKEN, RecordStore(tmp_path / "records"))
    try:
        assert control.server.server_address[0] == "127.0.0.1"
        assert control.url.startswith("http://127.0.0.1:")
    finally:
        control.server.server_close()


@pytest.mark.design("security.records_owner_only")
@pytest.mark.slow
def test_the_record_root_is_owner_only_even_if_it_was_open(
    project: Project, fake_openai: Callable[..., FakeOpenAI]
) -> None:
    root = project.root / "scrimmage-runs"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    assert project.test(fake_openai(ACME)).finished
    assert stat.S_IMODE(root.stat().st_mode) == 0o700


# One value of each shape the security page lists, as its prefix and a made-up body. They are
# joined only when the test runs, so the source holds nothing that looks like a real secret.
LETTERS = "abcdefghijklmnopqrstuvwxyz"
SHAPES = {
    "openai key": ("sk-", LETTERS),
    "openai project key": ("sk-proj-", LETTERS),
    "anthropic key": ("sk-ant-", LETTERS),
    "openai service account key": ("sk-svcacct-", LETTERS),
    "aws access key id": ("AK" + "IA", "Q" * 16),
    "aws temporary access key id": ("AS" + "IA", "Q" * 16),
    "google api key": ("AI" + "za", "B" * 35),
    "github token ghp": ("gh" + "p_", "a1B2" * 9),
    "github token gho": ("gh" + "o_", "a1B2" * 9),
    "github token ghu": ("gh" + "u_", "a1B2" * 9),
    "github token ghs": ("gh" + "s_", "a1B2" * 9),
    "github token ghr": ("gh" + "r_", "a1B2" * 9),
    "github fine-grained token": ("github" + "_pat_", LETTERS),
    "slack bot token": ("xo" + "xb-", LETTERS),
    "slack user token": ("xo" + "xp-", LETTERS),
    "slack app token": ("xo" + "xa-", LETTERS),
    "slack refresh token": ("xo" + "xr-", LETTERS),
    "slack legacy token": ("xo" + "xs-", LETTERS),
    "slack config token": ("xo" + "xe-", LETTERS),
    "slack rotating token": ("xo" + "xe.xoxp-", LETTERS),
    "slack app-level token": ("xa" + "pp-", LETTERS),
    "bearer token": ("Bearer ", LETTERS),
    "basic credential": ("Authorization: Basic ", "dXNlcjpwYXNz"),
    "form password": ("password=", "hunter2"),
    "form passwd": ("passwd=", "hunter2"),
    "form token": ("token=", "hunter2"),
    "form access token": ("access_token=", "hunter2"),
    "form secret": ("secret=", "hunter2"),
    "form api key": ("api_key=", "hunter2"),
    "form encoded password": ("password%3D", "hunter2"),
    "form quoted password": ("password=", "'hunter2'"),
    "form double quoted secret": ("secret=", '"hunter 2"'),
    "form bracketed token": ("token=", "<hunter2>"),
    "form client secret": ("client_secret=", "hunter2"),
    "environment password": ("DB_PASSWORD=", "hunter2"),
    "environment aws secret": ("AWS_SECRET_ACCESS_KEY=", "abc123"),
    "environment github token": ("GITHUB_TOKEN=", "x1"),
    "environment api key": ("MY_API_KEY=", "k1"),
    "json web token": ("ey" + "J", f"{LETTERS}.ey{LETTERS}.{LETTERS}"),
}


@pytest.mark.design("security.redaction_shapes")
@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_each_listed_secret_shape_is_removed(shape: str) -> None:
    prefix, body = SHAPES[shape]
    secret = prefix + body
    assert redact(f"value: {secret} end") == f"value: {REMOVED} end"


@pytest.mark.design("security.redaction_gap")
def test_a_secret_with_no_known_shape_passes_through_redaction() -> None:
    """A known limit, stated on the security page: only known shapes are removed."""
    assert redact("pw is hunter2") == "pw is hunter2"
    assert redact("pass=hunter2") == "pass=hunter2"
    assert redact("password: hunter2") == "password: hunter2"


@pytest.mark.design("security.redaction_exempt")
def test_names_that_are_not_credentials_are_kept() -> None:
    """The names the security page lists as exempt, and a name glued to a word."""
    for text in (
        "next_page_token=abc",
        "page_token=abc",
        "is_secret=true",
        "has_token=1",
        "top_secret=false",
        "max_token=100",
        "min_token=1",
        "num_token=3",
        "n_token=3",
        "eos_token=2",
        "pad_token=0",
        "mytoken=5",
    ):
        assert redact(text) == text


@pytest.mark.design("security.page_numbers")
def test_the_numbers_on_the_security_page_are_the_codes() -> None:
    page = (ROOT / "docs" / "security.md").read_text()
    control = (SDK / "control.py").read_text()
    records = (SDK / "records.py").read_text()
    runner = (SDK / "runner.py").read_text()
    assert '("127.0.0.1", 0)' in control and "`127.0.0.1`" in page
    assert "mode=0o700" in records and "0o700" in runner and "`0700`" in page
    assert "0o600" in records and "`0600`" in page
    assert protocol.WINDOW_S == 60 and "more than 60 seconds" in page
    assert "self.token = secrets.token_hex(32)" in runner and "256 bits" in page
    assert config.DEFAULT_BUDGET_USD == 5 and "$5 unless" in page
    assert "hashlib.sha256" in (SDK / "protocol.py").read_text() and "HMAC-SHA256" in page


class Counter(http.server.BaseHTTPRequestHandler):
    """Counts every request it gets, so a send cannot go unseen."""

    seen: ClassVar[list[str]] = []

    def answer(self) -> None:
        self.seen.append(f"{self.command} {self.path}")
        self.rfile.read(int(self.headers.get("content-length") or 0))
        body = json.dumps({"choices": [], "usage": {}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = answer

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def counting_server() -> Iterator[str]:
    Counter.seen.clear()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Counter)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


# One label of each kind a fault can name. The model host is the same server by another name.
LABELS = {"model": "model", "http": "http:127.0.0.1", "tool": "tool:lookup", "mcp": "mcp:docs/x"}
# It offers the one tool the run lists, so a fault that needs a tool offered has one.
LOOKUP = {"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}
CHAT = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}], "tools": [LOOKUP]}
# A model fault names the model, as the faults in a model's reply must.
MODEL = {"model": {"model": "gpt-4o-mini"}}


def spelled(word: str) -> str:
    return "status 503" if word == "status" else word


def loads(word: str, label: str) -> bool:
    table: dict[str, object] = {"call": label, "do": spelled(word), "when": "first 1"}
    table.update(MODEL.get(label, {}))
    try:
        config.read_fault(table, "[[test.fault]] 1")
    except ConfigError:
        return False
    return True


# Read from FAULTS and read_fault, so a word a later version adds is covered here.
NOT_CARRIED = [
    (word, kind)
    for word in config.FAULTS
    if word not in config.CARRIED
    for kind, label in LABELS.items()
    if loads(word, label)
]


@pytest.fixture
def gated_run() -> Iterator[Callable[..., tuple[state.RunState, Sink]]]:
    """Makes a run with these faults and gates this process's httpx2 and MCP on it."""
    mcp_send = mcp_session.ClientSession.send_request

    def make(*faults: Fault, real: tuple[str, ...] = ()) -> tuple[state.RunState, Sink]:
        sink = Sink()
        hosts = (ModelHost("localhost", "openai"),)
        tools = ("security_claims:lookup",)
        plan = Plan("run-1", "t", "row", (), faults, tools, real, 60.0, model_hosts=hosts)
        run = state.RunState(plan, sink, [])
        http_gate.patch(httpx2, http_gate.Context(run, Budget(), sink, never_stopped))
        mcp_gate.patch(mcp_session, mcp_gate.Gate(run, sink, never_stopped))
        return run, sink

    yield make
    mcp_session.ClientSession.send_request = mcp_send  # type: ignore[method-assign]
    state.install(None)


def fetch(url: str) -> str:
    """A request no gate sees, as a tool's own code might send it."""
    with urllib.request.urlopen(url, timeout=5) as reply:
        return str(reply.read())


def call(kind: str, server: str, run: state.RunState) -> None:
    """One call of this kind, whose real form sends a request to the counting server."""
    port = server.rpartition(":")[2]
    if kind == "model":
        httpx2.Client().post(f"http://localhost:{port}/v1/chat/completions", json=CHAT)
    elif kind == "http":
        httpx2.Client().get(f"http://{server}/stock/SKU-42")
    elif kind == "tool":

        def lookup(sku: str) -> str:
            return fetch(f"http://{server}/tool/{sku}")

        tool_gate.gate(lookup, "lookup")("SKU-42")
    else:
        docs = MCPServer("docs")

        @docs.tool()
        def x(q: str) -> str:  # pyright: ignore[reportUnusedFunction]
            return fetch(f"http://{server}/mcp/{q}")

        async def go() -> None:
            async with mcp.Client(docs) as client:
                state.install(run)  # after the handshake, which is not a tool call
                await client.call_tool("x", {"q": "SKU-42"})

        asyncio.run(go())


@pytest.mark.design("security.no_fault_sends")
@pytest.mark.parametrize(("word", "kind"), NOT_CARRIED, ids=[f"{w} {k}" for w, k in NOT_CARRIED])
def test_no_fault_but_the_carried_ones_sends_a_request(
    gated_run: Callable[..., tuple[state.RunState, Sink]],
    counting_server: str,
    word: str,
    kind: str,
) -> None:
    """Every fault word that is not carried, on every kind it loads on: the gate builds the
    failure and nothing reaches the server."""
    named = MODEL.get(LABELS[kind], {}).get("model")
    run, sink = gated_run(Fault(LABELS[kind], spelled(word), "first 1", model=named))
    state.install(run)
    with contextlib.suppress(Exception):  # what the agent gets is each fault's own test
        call(kind, counting_server, run)
    state.install(None)
    assert Counter.seen == [], f"{word} on {kind} sent {Counter.seen}"
    assert [x["action"] for x in sink.lines if x.get("kind") == kind] == ["fault"]


@pytest.mark.design("security.no_fault_sends")
def test_the_words_and_kinds_checked_come_from_what_loads() -> None:
    assert ("refused", "http") in NOT_CARRIED and ("dns", "model") in NOT_CARRIED
    assert ("timeout", "mcp") in NOT_CARRIED and ("error", "tool") in NOT_CARRIED
    assert ("refused", "tool") not in NOT_CARRIED and ("append", "http") not in NOT_CARRIED
    # The two faults that make a model's whole reply send nothing, so they are checked here.
    assert ("refuse", "model") in NOT_CARRIED and ("unknown tool", "model") in NOT_CARRIED


@pytest.mark.design("security.no_fault_sends")
def test_the_counting_server_sees_append_on_a_listed_web_read_once_per_call(
    gated_run: Callable[..., tuple[state.RunState, Sink]], counting_server: str
) -> None:
    """The positive control: under a carried fault the same server sees the call."""
    fault = Fault(LABELS["http"], "append", "always", text=" more")
    run, _ = gated_run(fault, real=(LABELS["http"],))
    state.install(run)
    for _ in range(2):
        call("http", counting_server, run)
    state.install(None)
    assert Counter.seen == ["GET /stock/SKU-42"] * 2
