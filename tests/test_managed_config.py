"""The start key: the command that starts a test copy of a web service.

start is read like command: split once, then ${NAME} filled in each word. It must hold {port},
and service must hold {port} as its port, on a loopback host. control and control_tls belong
to a service someone else started, so they are refused beside start. A file without start
reads as it did before the key existed.
"""

import re
import sys
from pathlib import Path

import pytest

from scrimmage_core import config
from scrimmage_core.cli import main
from scrimmage_core.errors import ConfigError

TESTS = Path(__file__).parent
SERVICE_LINE = re.compile(r"^service\s*=", re.M)
PYTHON = sys.executable
START = f"{PYTHON} -m uvicorn acme.service:app --port {{port}} --loop asyncio"
BODY = 'body = {message = "{question}"}\n'
TEST = '\n[[test]]\nname = "t"\nquestion = "Where is order 1234?"\nexpect = ["shipped"]\n'


def managed(
    start: str = START, service: str = "http://127.0.0.1:{port}/chat", extra: str = ""
) -> str:
    return f"[agent]\nstart = {start!r}\nservice = {service!r}\n{BODY}{extra}" + TEST


def load_text(tmp_path: Path, text: str) -> config.Config:
    path = tmp_path / "scrimmage.toml"
    path.write_text(text)
    return config.load(path)


def test_start_with_port_in_start_and_service_reads(tmp_path: Path) -> None:
    cfg = load_text(tmp_path, managed())
    assert cfg.agent.start == (
        PYTHON,
        "-m",
        "uvicorn",
        "acme.service:app",
        "--port",
        "{port}",
        "--loop",
        "asyncio",
    )
    assert cfg.agent.service == "http://127.0.0.1:{port}/chat"
    assert config.start_args(cfg.agent, 41234)[4:6] == ["--port", "41234"]
    # {port} as part of a word, and the other loopback hosts.
    for service in (
        "http://[::1]:{port}/chat",
        "http://localhost:{port}/",
        "http://LOCALHOST:{port}",
    ):
        cfg = load_text(tmp_path, managed(f"{PYTHON} serve.py --bind=:{{port}}", service))
        assert config.start_args(cfg.agent, 9) == [PYTHON, "serve.py", "--bind=:9"], service
        assert cfg.agent.service == service


def test_start_without_port_is_exit_2(tmp_path: Path) -> None:
    text = managed(f"{PYTHON} -m uvicorn acme.service:app --port 8765")
    with pytest.raises(ConfigError, match=r"\[agent\] start: must hold \{port\}"):
        load_text(tmp_path, text)
    # A value from the environment that holds {port} is not the file's {port}.
    path = tmp_path / "scrimmage.toml"
    path.write_text(managed(f"{PYTHON} -m uvicorn acme.service:app --port ${{THE_PORT}}"))
    with pytest.raises(ConfigError, match=r"must hold \{port\}"):
        config.load(path, {"THE_PORT": "{port}"})
    assert main(["check", str(path)]) == 2


@pytest.mark.parametrize(
    "service",
    [
        "http://127.0.0.1:8765/chat",
        "http://127.0.0.1/chat",
        "http://127.0.0.1:{port}/chat/{port}",
        "http://127.0.0.1:{port}{port}/chat",
        "http://{port}/chat",
        "http://127.0.0.1:8765/{port}",
        "https://127.0.0.1:{port}/chat",
    ],
)
def test_service_without_port_beside_start_is_exit_2(tmp_path: Path, service: str) -> None:
    with pytest.raises(ConfigError, match=r"\[agent\] service: with start it must be of the form"):
        load_text(tmp_path, managed(service=service))


@pytest.mark.parametrize(
    "host", ["0.0.0.0", "[::]", "10.0.0.5", "127.0.0.2", "acme-agent", "localhost.example.com"]
)
def test_start_needs_a_loopback_service_host(tmp_path: Path, host: str) -> None:
    with pytest.raises(ConfigError, match=r"the host must be 127\.0\.0\.1, \[::1\] or localhost"):
        load_text(tmp_path, managed(service=f"http://{host}:{{port}}/chat"))


@pytest.mark.parametrize(
    "extra",
    [
        'control = "127.0.0.1:8766"\n',
        'control = "172.17.0.1:8766"\ncontrol_tls = {cert = "c.pem", key = "k.pem"}\n',
        'control_tls = {cert = "c.pem", key = "k.pem"}\n',
    ],
)
def test_start_refuses_control_and_control_tls_naming_attach_mode(
    tmp_path: Path, extra: str
) -> None:
    with pytest.raises(ConfigError) as caught:
        load_text(tmp_path, managed(extra=extra))
    message = str(caught.value)
    assert "control" in message
    assert "works only for a service someone else started, with no start" in message


@pytest.mark.parametrize(
    "agent",
    [
        f"start = {START!r}\n",
        f'start = {START!r}\nfunction = "acme.agent:run"\n',
        f'start = {START!r}\ncommand = "{PYTHON} bot.py {{question}}"\n',
    ],
)
def test_start_needs_service(tmp_path: Path, agent: str) -> None:
    with pytest.raises(ConfigError, match=r"start: works only with service|exactly one of them"):
        load_text(tmp_path, f"[agent]\n{agent}" + TEST)
    if "function" in agent:
        with pytest.raises(ConfigError, match=r"\[agent\] start: works only with service"):
            load_text(tmp_path, f"[agent]\n{agent}" + TEST)


@pytest.mark.parametrize("program", ["no-such-uvicorn", "./no-such-script.py"])
def test_a_start_program_that_cannot_be_found_is_exit_2(tmp_path: Path, program: str) -> None:
    text = managed(f"{program} acme.service:app --port {{port}}")
    with pytest.raises(ConfigError) as caught:
        load_text(tmp_path, text)
    message = str(caught.value)
    assert f"[agent] start: {program} is not a program it can find" in message
    assert "virtual environment" in message and "python -m uvicorn" in message
    assert main(["check", str(tmp_path / "scrimmage.toml")]) == 2


def test_start_fills_names_and_scrubs_them(tmp_path: Path) -> None:
    secret = "sk-start-0123456789abcdef"
    path = tmp_path / "scrimmage.toml"
    start = f"{PYTHON} -m uvicorn acme.service:app --port {{port}} --header 'x-key: ${{APP_KEY}}'"
    path.write_text(managed(start))
    cfg = config.load(path, {"APP_KEY": secret})
    assert cfg.agent.start[-2:] == ("--header", f"x-key: {secret}")
    assert config.start_args(cfg.agent, 7)[-1] == f"x-key: {secret}"
    assert secret in cfg.scrub
    # A value with a space stays one word, and a value's {port} is sent as written.
    cfg = config.load(path, {"APP_KEY": "a b {port} c-long-enough"})
    assert config.start_args(cfg.agent, 7)[-1] == "x-key: a b {port} c-long-enough"
    # A message that quotes a filled value removes it.
    path.write_text(managed("${APP_KEY} --port {port}"))
    with pytest.raises(ConfigError) as caught:
        config.load(path, {"APP_KEY": secret})
    leaked = secret in str(caught.value)
    assert not leaked, "a start program's filled value is in the error message"


def test_a_service_file_without_start_reads_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every service file under tests/, and the forms the service tests write.
    monkeypatch.setenv("POD_IP", "10.244.0.7")
    files = [p for p in sorted(TESTS.rglob("*.toml")) if SERVICE_LINE.search(p.read_text())]
    assert [p.relative_to(TESTS).as_posix() for p in files] == ["k8s/scrimmage.toml"]
    texts = [p.read_text() for p in files] + [
        f'[agent]\nservice = "http://127.0.0.1:8765/chat"\n{BODY}' + TEST,
        f'[agent]\nservice = "http://localhost/chat"\n{BODY}control = "127.0.0.1:8766"\n' + TEST,
        f'[agent]\nservice = "http://acme-agent:8000/"\n{BODY}' + TEST,
    ]
    for text in texts:
        path = tmp_path / "scrimmage.toml"
        path.write_text(text)
        agent = config.load(path).agent
        assert agent.start == () and agent.start_pieces == ()
        written = text.split("service", 1)[1].split('"')[1]
        assert agent.service == written
    # {port} means nothing without start: such a file is refused as it was before the key.
    for service in ("http://127.0.0.1:{port}/chat", "http://localhost:{port}/"):
        with pytest.raises(ConfigError, match=r"is not of the form http://host:port/path"):
            load_text(tmp_path, f"[agent]\nservice = {service!r}\n{BODY}" + TEST)
