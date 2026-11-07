"""Claims on the security page that no other test held: where the control server listens,
that it is the only connection Scrimmage makes, what never reaches a record, and a known gap
in redaction."""

import re
import secrets
from collections.abc import Callable
from pathlib import Path

import pytest

from conftest import ACME, Project
from scrimmage_core.control import ControlServer
from scrimmage_core.records import RecordStore
from scrimmage_core.redact import redact
from fakes.openai_server import FakeOpenAI

TOKEN = "f00d" * 16
PACKAGES = Path(__file__).parents[1] / "packages"
# Every standard-library name that opens a connection or listens for one.
NETWORK = re.compile(
    r"\b(HTTPS?Connection|urlopen|urlretrieve|create_connection|open_connection|"
    r"open_unix_connection|create_server|start_server|SMTP(?:_SSL)?|FTP(?:_TLS)?|"
    r"ServerProxy|ThreadingHTTPServer|HTTPServer|TCPServer|UDPServer|wrap_socket|"
    r"socket(?=\())\b"
)


@pytest.mark.design("security.no_telemetry")
def test_the_only_connections_are_to_and_from_the_control_server() -> None:
    """The SDK's client opens one kind of connection, to the control server's URL, and the
    runner's control server is the only thing that listens. Nothing else reaches the network:
    `socket` is used only to read the host name."""
    found = {
        (path.name, name)
        for path in PACKAGES.rglob("*.py")
        for name in NETWORK.findall(path.read_text())
    }
    assert found == {
        ("client.py", "HTTPConnection"),
        ("control.py", "ThreadingHTTPServer"),
    }
    uses = {
        name
        for path in PACKAGES.rglob("*.py")
        for name in re.findall(r"\bsocket\.(\w+)", path.read_text())
    }
    assert uses == {"gethostname"}


@pytest.mark.design("security.loopback_only")
def test_the_control_server_listens_on_loopback_only(tmp_path: Path) -> None:
    control = ControlServer(TOKEN, RecordStore(tmp_path / "records"))
    try:
        assert control.server.server_address[0] == "127.0.0.1"
        assert control.url.startswith("http://127.0.0.1:")
    finally:
        control.server.server_close()


@pytest.mark.design("security.token_and_environment_unrecorded")
def test_no_record_or_log_holds_the_run_token_or_the_environment(
    project: Project,
    fake_openai: Callable[..., FakeOpenAI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "a1b2c3d4" * 8
    real = secrets.token_hex
    # The token is the one 32-byte value; run ids and folder names stay random.
    monkeypatch.setattr(secrets, "token_hex", lambda n: secret if n == 32 else real(n))
    monkeypatch.setenv("ACME_DATABASE_PASSWORD", "env-only-value-7f3a")
    result = project.test(fake_openai(ACME))
    assert result.finished
    files = [f for f in (project.root / "scrimmage-runs").rglob("*") if f.is_file()]
    assert any(f.suffix == ".jsonl" for f in files) and any(f.suffix == ".log" for f in files)
    for path in files:
        data = path.read_bytes()
        assert secret.encode() not in data, path
        assert b"env-only-value-7f3a" not in data, path
        assert b"ACME_DATABASE_PASSWORD" not in data, path


@pytest.mark.design("security.redaction_gap")
def test_a_secret_with_no_known_shape_passes_through_redaction() -> None:
    """A known limit, stated on the security page: only known shapes are removed."""
    assert redact("password=hunter2") == "password=hunter2"
    assert redact("key sk-abcdefghijklmnop1234") == "key [secret removed]"
