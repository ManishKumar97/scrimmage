"""Claims on the security page that no other test held: where the control server listens,
that it is the only connection Scrimmage makes, the record folder's mode, each secret shape
redaction removes, a known gap in redaction, and the numbers the page states."""

import re
import stat
from collections.abc import Callable
from pathlib import Path

import pytest

from conftest import ACME, Project
from fakes.openai_server import FakeOpenAI
from scrimmage_core import config, protocol
from scrimmage_core.control import ControlServer
from scrimmage_core.records import RecordStore
from scrimmage_core.redact import REMOVED, redact
from test_stdlib_only import imported_roots

TOKEN = "f00d" * 16
ROOT = Path(__file__).parents[1]
PACKAGES = ROOT / "packages"
SDK = PACKAGES / "scrimmage" / "src" / "scrimmage_core"
# Every standard-library name that opens a connection or listens for one.
NETWORK = re.compile(
    r"\b(HTTPS?Connection|urlopen|urlretrieve|create_connection|open_connection|"
    r"open_unix_connection|create_server|start_server|SMTP(?:_SSL)?|FTP(?:_TLS)?|"
    r"ServerProxy|ThreadingHTTPServer|HTTPServer|TCPServer|UDPServer|wrap_socket|"
    r"socket(?=\())\b"
)


@pytest.mark.design("security.no_telemetry")
def test_the_only_connections_are_to_and_from_the_control_server() -> None:
    """A fast check by name. test_security_audit.py watches a real run do the same."""
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
    }
    uses = {
        name
        for path in PACKAGES.rglob("*.py")
        for name in re.findall(r"\bsocket\.(\w+)", path.read_text())
    }
    # SHUT_RDWR: service_client cuts the runner's own probe to the service at its deadline.
    # socket: managed.py's free port, bound and closed, never connected.
    assert uses == {"gethostname", "SHUT_RDWR", "socket"}


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
