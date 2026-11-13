"""The runner's side of a service: its token and address, the probe, and each run's POST.

The standard library's http.client, so scrimmage-core keeps no third-party dependency. The
runner reads the same token file and control address the service was started with, so one
environment file serves both.
"""

import contextlib
import http.client
import ipaddress
import json
import os
import secrets
import ssl
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path

from scrimmage_core import protocol, records, tls
from scrimmage_core.config import (
    SERVICE_URL,
    Agent,
    control_address_of,
    request_body,
    request_headers,
)
from scrimmage_core.start import BadAnswerPath, pick

TOKEN_MIN = 32
CONNECT_S = 2.0  # how long the probe's connection may take before it is asked again
STREAMED = ("text/event-stream", "application/x-ndjson", "application/jsonl")
# The gate's own 503, exactly: what says Scrimmage answered and could not claim the plan.
GATE_HEADERS = {"content-type", "content-length", "x-scrimmage-error"}
SERVER_HEADERS = {"date", "server"}


class Setup(Exception):
    """The service or the runner's environment is not ready: the test stops before any run.
    code is the exit code: 2 for a mistake to fix, 3 for an infrastructure error."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class Reply:
    """What the service answered, or why there is no answer."""

    status: int | None = None
    headers: dict[str, str] = field(default_factory=dict[str, str])  # names in lower case
    body: bytes = b""
    failed: str | None = None  # the connection was refused, reset, or broke
    timed_out: bool = False
    connected: bool = True  # False: no connection was made at all

    @property
    def gate_refused(self) -> str | None:
        """The gate's own 503 code, if this is exactly that reply, else None. An app that
        sends the header itself, with any other body or header, is not the gate."""
        code = self.headers.get("x-scrimmage-error")
        # The server adds its own date and name to every reply, the gate's too.
        if self.status != 503 or code is None or set(self.headers) - SERVER_HEADERS != GATE_HEADERS:
            return None
        if self.headers.get("content-type") != "application/json":
            return None
        try:
            return code if json.loads(self.body) == {"error": code} else None
        except ValueError:
            return None


def runner_token() -> str:
    """The token, from SCRIMMAGE_TOKEN_FILE only: a plain file this user owns, mode 0600."""
    path = os.environ.get("SCRIMMAGE_TOKEN_FILE", "")
    if not path:
        raise Setup(2, "a service test reads its token from SCRIMMAGE_TOKEN_FILE, which is not set")
    try:
        fd = records.open_owned(Path(path))
    except (OSError, records.Refused) as exc:
        raise Setup(2, f"SCRIMMAGE_TOKEN_FILE {path} cannot be read: {exc}") from None
    with os.fdopen(fd, "rb") as handle:
        mode = stat.S_IMODE(os.fstat(handle.fileno()).st_mode)
        token = handle.read(4096).decode("utf-8", "replace").strip()
    if mode != 0o600:
        raise Setup(2, f"SCRIMMAGE_TOKEN_FILE {path} has mode {mode:o}; it must be 600")
    if len(token) < TOKEN_MIN:
        raise Setup(2, f"SCRIMMAGE_TOKEN_FILE {path} holds a token under {TOKEN_MIN} characters")
    return token


def control_address(agent: Agent) -> tuple[str, int]:
    """Where the runner listens: the file's control setting, or else SCRIMMAGE_CONTROL_URL, which
    the service was started with: http:// on this machine."""
    if agent.control:
        found_control = control_address_of(agent.control)
        assert found_control is not None  # the file reader checked it
        return found_control
    url = os.environ.get("SCRIMMAGE_CONTROL_URL", "")
    found = SERVICE_URL.fullmatch(url)
    host = found["host"].strip("[]") if found else ""
    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if found is None or found["scheme"] != "http" or not found["port"] or not loopback:
        raise Setup(
            2,
            f"SCRIMMAGE_CONTROL_URL {url!r} must be http://127.0.0.1:port, the address the "
            "service was started with; for a service in a container, set control and "
            "control_tls in the file",
        )
    return host, int(found["port"])


def server_tls(agent: Agent, root: Path) -> ssl.SSLContext | None:
    """The runner's TLS context, or None without control_tls. Exit 2 for a key that others can
    read, a certificate that does not match its key, or one outside its dates."""
    if agent.control_tls is None:
        return None
    cert, key = (root / value for value in agent.control_tls)
    try:
        fd = records.open_owned(key)
    except (OSError, records.Refused) as exc:
        raise Setup(2, f"control_tls key {key} cannot be read: {exc}") from None
    with os.fdopen(fd, "rb") as handle:
        mode = stat.S_IMODE(os.fstat(handle.fileno()).st_mode)
    if mode != 0o600:
        raise Setup(2, f"control_tls key {key} has mode {mode:o}; it must be 600")
    try:
        context = tls.server_context(cert, key)
        tls.check_valid(context, cert)
    except tls.TlsError as exc:
        raise Setup(2, f"control_tls: {exc}") from None
    return context


def target(agent: Agent) -> tuple[str, int, str]:
    found = SERVICE_URL.fullmatch(str(agent.service))
    assert found is not None  # the file reader checked it
    return found["host"].strip("[]"), int(found["port"] or 80), found["path"] or "/"


def post(
    agent: Agent,
    headers: dict[str, str],
    body: bytes,
    timeout: float,
    connect_timeout: float | None = None,
) -> Reply:
    """One POST. connect_timeout: how long the connection may take, if shorter than timeout."""
    host, port, path = target(agent)
    conn = http.client.HTTPConnection(host, port, timeout=connect_timeout or timeout)
    try:
        conn.connect()
    except OSError as exc:  # refused, no such host, unreachable, or the connect timed out
        conn.close()
        return Reply(failed=f"{type(exc).__name__}: {exc}", connected=False)
    if conn.sock is not None:
        conn.sock.settimeout(timeout)
    try:
        conn.request("POST", path, body=body, headers=headers)
        response = conn.getresponse()
        data = response.read()
        named = {k.lower(): v for k, v in response.getheaders()}
        return Reply(response.status, named, data)
    except TimeoutError:
        return Reply(timed_out=True)
    except (OSError, http.client.HTTPException) as exc:
        return Reply(failed=f"{type(exc).__name__}: {exc}")
    finally:
        conn.close()


def probe(agent: Agent, token: str) -> tuple[dict[str, object] | None, Reply]:
    """The signed probe: the service's startup report, signed for this request, or None when
    no Scrimmage with this token answered, and the reply itself. Its body is empty, so an
    app reached by mistake rejects it as a bad request."""
    _, _, path = target(agent)
    signed = protocol.request_headers(token, "POST", path, b"", time.time())
    headers = {
        "x-scrimmage-run": f"probe-{secrets.token_hex(6)}",
        "x-scrimmage-probe": "1",
        **signed,
    }
    reply = post(agent, headers, b"", 30.0, connect_timeout=CONNECT_S)
    if reply.status != 200 or reply.headers.get("x-scrimmage-probe") != "report":
        return None, reply
    expected = protocol.sign_reply(token, signed[protocol.SIGNATURE], 200, reply.body)
    if not protocol.same(reply.headers.get(protocol.SIGNATURE.lower(), ""), expected):
        return None, reply
    with contextlib.suppress(ValueError):
        found = json.loads(reply.body)
        if isinstance(found, dict):
            return dict(found), reply  # type: ignore[arg-type]
    return None, reply


def not_listening(reply: Reply) -> bool:
    """Nothing listens at the address yet, or its connection timed out: a service still
    starting, or not started, so the probe asks again."""
    return (
        reply.failed is not None
        and not reply.connected
        and (reply.failed.startswith(("ConnectionRefusedError", "TimeoutError")))
    )


def run_request(agent: Agent, header: str, question: str, timeout: float) -> Reply:
    """One run: the file's body and headers, with a new session, and the run header."""
    from scrimmage_core.config import new_session

    session = new_session()
    headers = dict(request_headers(agent, session))
    headers.update({"content-type": "application/json", "x-scrimmage-run": header})
    body = json.dumps(request_body(agent, question, session)).encode()
    return post(agent, headers, body, timeout)


def streamed(reply: Reply, agent: Agent) -> bool:
    """A streamed reply: a streaming content type, or a body that is not one JSON document
    while answer names a field in it. Chunked encoding alone is not streaming."""
    kind = reply.headers.get("content-type", "").partition(";")[0].strip().lower()
    if kind in STREAMED:
        return True
    if agent.answer:
        try:
            json.loads(reply.body)
        except ValueError:
            return True
    return False


def answer_of(reply: Reply, agent: Agent) -> tuple[str | None, str | None]:
    """The answer in a 2xx reply, and a mistake if answer does not reach text."""
    kind = reply.headers.get("content-type", "").partition(";")[0].strip().lower()
    text = reply.body.decode("utf-8", "replace")
    if not agent.answer:
        if kind == "text/plain":
            return text, None
        try:
            whole = json.loads(reply.body)
        except ValueError:
            whole = None
        if isinstance(whole, str):
            return whole, None
        return None, (
            "the service's reply is neither text/plain nor a JSON string; name the answer's "
            'field with [agent] answer, such as answer = "reply"'
        )
    try:
        found = pick(json.loads(reply.body), agent.answer)
    except (ValueError, BadAnswerPath) as exc:
        return None, str(exc)
    return (found if isinstance(found, str) else json.dumps(found)), None
