"""The start key: the command that starts a test copy of a web service.

start is read like command: split once, then ${NAME} filled in each word. It must hold {port},
and service must hold {port} as its port, on a loopback host. control and control_tls belong
to a service someone else started, so they are refused beside start. A file without start
reads as it did before the key existed.
"""

import dataclasses
import re
import sys
from pathlib import Path

import pytest

from scrimmage import config
from scrimmage.cli import main
from scrimmage.errors import ConfigError

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
    assert config.service_url(cfg.agent, 41234) == "http://127.0.0.1:41234/chat"
    # {{ is no escape for {port}: {{port}} is a brace, the port and a brace.
    cfg = load_text(tmp_path, managed(f"{PYTHON} serve.py --port {{{{port}}}}"))
    assert config.start_args(cfg.agent, 9)[-1] == "{9}"
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
        # A value's {port} is not the file's: it is sent as written, so it is no port.
        "http://127.0.0.1:${P}/chat",
        "http://127.0.0.1:8765/${P}",
        # {{ is no escape for {port}.
        "http://127.0.0.1:{{port}}/chat",
    ],
)
def test_service_without_port_beside_start_is_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, service: str
) -> None:
    monkeypatch.setenv("P", "{port}")
    with pytest.raises(ConfigError, match=r"\[agent\] service: with start it must be of the form"):
        load_text(tmp_path, managed(service=service))


def test_a_port_in_a_value_of_service_is_sent_as_written(tmp_path: Path) -> None:
    path = tmp_path / "scrimmage.toml"
    path.write_text(managed(service="http://127.0.0.1:{port}/${X}"))
    cfg = config.load(path, {"X": "chat/{port}/x"})
    assert cfg.agent.service == "http://127.0.0.1:{port}/chat/{port}/x"
    assert config.service_url(cfg.agent, 41234) == "http://127.0.0.1:41234/chat/{port}/x"


@pytest.mark.parametrize("value", ["127.0.0.1", "localhost", "0.0.0.0"])
def test_start_needs_a_loopback_host_written_in_the_file(tmp_path: Path, value: str) -> None:
    """The host is the file's own text, as the schema and the docs say: a ${NAME} host is
    refused, even one whose value is loopback, and the message shows no value."""
    path = tmp_path / "scrimmage.toml"
    for service in ("http://${H}:{port}/chat", "http://127.0.0.${H}:{port}/chat"):
        path.write_text(managed(service=service))
        with pytest.raises(ConfigError, match=r"the host is written in the file") as caught:
            config.load(path, {"H": value})
        shown = str(caught.value).replace(service, "")
        assert value not in shown, shown


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


@pytest.mark.parametrize("flags", ["-I", "-S", "-E", "-W ignore -S"])
def test_a_start_flag_that_skips_scrimmage_is_exit_2(tmp_path: Path, flags: str) -> None:
    text = managed(f"{PYTHON} {flags} -m uvicorn acme.service:app --port {{port}}")
    with pytest.raises(ConfigError, match=r"\[agent\] start: -\w+ keeps Python from running"):
        load_text(tmp_path, text)
    assert main(["check", str(tmp_path / "scrimmage.toml")]) == 2


def test_a_command_whose_question_comes_only_from_a_value_is_exit_2(tmp_path: Path) -> None:
    path = tmp_path / "scrimmage.toml"
    path.write_text('[agent]\ncommand = "python3 bot.py ${Q}"\n' + TEST)
    # The message's {question} is scrubbed, as the value is {question}: a known, logged gap.
    with pytest.raises(ConfigError, match=r"\[agent\] command: must hold .*no question"):
        config.load(path, {"Q": "{question}"})


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


# dataclasses.asdict of each file's Agent as main read it before start existed, with the
# fields start added, empty.
BEFORE: dict[str, object] = {
    "function": "",
    "runs": 3,
    "parallel": 1,
    "run_timeout": 120.0,
    "tools": (),
    "real": (),
    "renames": (),
    "model_hosts": (),
    "max_cost_usd": 5.0,
    "reserve_output_tokens": 4096,
    "prices": (),
    "service_tier": None,
    "answer": None,
    "command": (),
    "children": False,
    "command_pieces": (),
    "command_written": "",
    "service": "",
    "body": {"message": {"pieces": ({"kind": "question", "text": ""},)}},
    "headers": (),
    "record_grace_s": 5.0,
    "record_wait_s": 10.0,
    "background_real": (),
    "control": "",
    "control_tls": None,
    "start": (),
    "start_written": "",
    "start_pieces": (),
    "service_pieces": (),
}
K8S = {
    **BEFORE,
    "runs": 30,
    "run_timeout": 30.0,
    "tools": (
        "acme.tools:get_order",
        "acme.tools:check_inventory",
        "acme.tools:refund_order",
        "acme.tools:get_stock",
    ),
    "real": ("tool:check_inventory", "process:uname"),
    "model_hosts": ({"host": "fake-model", "provider": "openai"},),
    "answer": "reply",
    "service": "http://acme-agent:8000/chat",
    "body": {
        "message": {"pieces": ({"kind": "question", "text": ""},)},
        "session": {"pieces": ({"kind": "session", "text": ""},)},
    },
    "record_grace_s": 0.2,
    "control": "10.244.0.7:7700",
    "control_tls": ("/run/scrimmage/tls/tls.crt", "/run/scrimmage/tls/tls.key"),
}


def test_a_service_file_without_start_reads_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every service file under tests/, and the forms the service tests write.
    monkeypatch.setenv("POD_IP", "10.244.0.7")
    files = [p for p in sorted(TESTS.rglob("*.toml")) if SERVICE_LINE.search(p.read_text())]
    assert [p.relative_to(TESTS).as_posix() for p in files] == ["k8s/scrimmage.toml"]
    cases = [
        (files[0].read_text(), K8S),
        (
            f'[agent]\nservice = "http://127.0.0.1:8765/chat"\n{BODY}' + TEST,
            {**BEFORE, "service": "http://127.0.0.1:8765/chat"},
        ),
        (
            f'[agent]\nservice = "http://localhost/chat"\n{BODY}control = "127.0.0.1:8766"\n'
            + TEST,
            {**BEFORE, "service": "http://localhost/chat", "control": "127.0.0.1:8766"},
        ),
        (
            f'[agent]\nservice = "http://acme-agent:8000/"\n{BODY}' + TEST,
            {**BEFORE, "service": "http://acme-agent:8000/"},
        ),
    ]
    for text, expected in cases:
        path = tmp_path / "scrimmage.toml"
        path.write_text(text)
        assert dataclasses.asdict(config.load(path).agent) == expected, text
    # A value in an attach-mode service is filled in, and the address keeps its written form.
    path = tmp_path / "scrimmage.toml"
    path.write_text(f'[agent]\nservice = "http://127.0.0.1:${{PORT}}/chat"\n{BODY}' + TEST)
    service = config.load(path, {"PORT": "8765"}).agent.service
    assert service == "http://127.0.0.1:8765/chat"
    assert config.as_written(service) == "http://127.0.0.1:${PORT}/chat"
    # {port} means nothing without start: such a file is refused as it was before the key.
    for service in ("http://127.0.0.1:{port}/chat", "http://localhost:{port}/"):
        with pytest.raises(ConfigError, match=r"is not of the form http://host:port/path"):
            load_text(tmp_path, f"[agent]\nservice = {service!r}\n{BODY}" + TEST)
