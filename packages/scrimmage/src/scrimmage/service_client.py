"""The runner's side of a service: its token and address, the probe, and each run's POST.

The standard library's http.client, so scrimmage keeps no third-party dependency. The
runner reads the same token file and control address the service was started with, so one
environment file serves both.
"""

import contextlib
import http.client
import ipaddress
import json
import os
import secrets
import socket
import ssl
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from scrimmage import protocol, records, tls, versions
from scrimmage.config import (
    SERVICE_URL,
    Agent,
    control_address_of,
    request_body,
    request_headers,
)
from scrimmage.start import BadAnswerPath, pick

TOKEN_MIN = 32
CONNECT_S = 2.0  # how long the probe's connection may take before it is asked again
PROBE_S = 30.0  # the whole probe, from its connection to the last byte of its reply
ALIVE_STEP_S = 0.1  # how often a probe asks whether the service the runner started runs
PROBE_MAX = 1024 * 1024  # a startup report is a few KB; a longer reply is not Scrimmage's
STREAMED = ("text/event-stream", "application/x-ndjson", "application/jsonl")
# The gate's own 503, exactly: what says Scrimmage answered and could not claim the plan.
GATE_HEADERS = {"content-type", "content-length", "x-scrimmage-error"}
SERVER_HEADERS = {"date", "server"}
CLOCK_OFF = (
    "the service's clock and this machine's differ by more than 60 s, so every signed request "
    "is refused; set both from the same NTP source"
)
# The ping's detail when the control server refused it for its time: the service's clock and
# this machine's differ.
PING_CLOCK = "the control server answered 401 clock"


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
    unanswered: bool = False  # taken, then reset or closed before any HTTP reply began
    too_long: bool = False  # the reply passed the most bytes the request may read

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
        fd = records.open_secret(Path(path))
    except records.WrongMode as exc:
        raise Setup(
            2, f"SCRIMMAGE_TOKEN_FILE {path} has mode {exc.mode:o}; it must be 600"
        ) from None
    except (OSError, records.Refused) as exc:
        raise Setup(2, f"SCRIMMAGE_TOKEN_FILE {path} cannot be read: {exc}") from None
    with os.fdopen(fd, "rb") as handle:
        token = handle.read(4096).decode("utf-8", "replace").strip()
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
        fd = records.open_secret(key)
    except records.WrongMode as exc:
        raise Setup(2, f"control_tls key {key} has mode {exc.mode:o}; it must be 600") from None
    except (OSError, records.Refused) as exc:
        raise Setup(2, f"control_tls key {key} cannot be read: {exc}") from None
    with os.fdopen(fd, "rb") as handle:
        checked = os.fstat(handle.fileno())
    try:
        context = tls.server_context(cert, key)  # opens the key again, by its path
        tls.check_valid(context, cert)
        loaded = os.stat(key)  # follows a Secret volume's link, as the open did
    except tls.TlsError as exc:
        raise Setup(2, f"control_tls: {exc}") from None
    except OSError as exc:
        raise Setup(2, f"control_tls key {key} cannot be read: {exc.strerror or exc}") from None
    # The file that was checked must be the file that was loaded, not one swapped in between.
    if (loaded.st_dev, loaded.st_ino, loaded.st_mode) != (
        checked.st_dev,
        checked.st_ino,
        checked.st_mode,
    ):
        raise Setup(2, f"control_tls key {key} changed while it was read; start the test again")
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
    return send("POST", agent, target(agent)[2], headers, body, timeout, connect_timeout)


def send(
    method: str,
    agent: Agent,
    path: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout: float,
    connect_timeout: float | None = None,
    *,
    limit: int | None = None,
    deadline_s: float | None = None,
    alive: Callable[[], bool] | None = None,
) -> Reply:
    """One request to the service's host and port. limit: the most bytes of reply to read.
    deadline_s: the most seconds the request may take once connected, however slowly the
    service sends; timeout bounds each read only. alive: asked while the request waits for
    its deadline; once it says no, the request is cut, as at the deadline."""
    host, port, _ = target(agent)
    conn = http.client.HTTPConnection(host, port, timeout=connect_timeout or timeout)
    try:
        conn.connect()
    except OSError as exc:  # refused, no such host, unreachable, or the connect timed out
        conn.close()
        # A reset while connecting is a port that took the connection and cut it, which a
        # fast loopback shows at connect; it is not a missing host.
        cut_short = isinstance(exc, ConnectionResetError)
        return Reply(failed=f"{type(exc).__name__}: {exc}", connected=False, unanswered=cut_short)
    if conn.sock is not None:
        conn.sock.settimeout(timeout)
    expired, done = threading.Event(), threading.Event()
    cutter: threading.Thread | None = None
    if deadline_s is not None and conn.sock is not None:
        # A read timeout is per read, so a reply sent a byte at a time never meets it; the
        # socket is shut at the deadline, which ends any read, the headers' included.
        watched = (conn.sock, expired, done, deadline_s, alive)
        cutter = threading.Thread(target=watch, args=watched, daemon=True)
        cutter.start()
    replied = False
    try:
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        replied = True
        if limit is None:
            data = response.read()
        else:
            data = b""
            while chunk := response.read1(65536):
                data += chunk
                if len(data) > limit:
                    return Reply(too_long=True, failed=f"the reply is longer than {limit} bytes")
        if expired.is_set():
            return Reply(timed_out=True)  # the reply ended because it was cut
        named = {k.lower(): v for k, v in response.getheaders()}
        return Reply(response.status, named, data)
    except TimeoutError:
        return Reply(timed_out=True)
    except (OSError, http.client.HTTPException, ValueError) as exc:
        if expired.is_set():
            return Reply(timed_out=True)  # the deadline shut the socket under the read
        cut_short = isinstance(exc, ConnectionError | http.client.RemoteDisconnected)
        return Reply(failed=f"{type(exc).__name__}: {exc}", unanswered=cut_short and not replied)
    finally:
        if cutter is not None:
            done.set()
            cutter.join()  # a cut already running ends before the socket is closed
        conn.close()


def watch(
    sock: Any,
    expired: threading.Event,
    done: threading.Event,
    deadline_s: float,
    alive: Callable[[], bool] | None,
) -> None:
    """Cut the request at its deadline, or as soon as alive says no, unless it is done."""
    end = time.monotonic() + deadline_s
    step = deadline_s if alive is None else ALIVE_STEP_S
    while not done.wait(max(0.0, min(step, end - time.monotonic()))):
        if time.monotonic() >= end or (alive is not None and not alive()):
            cut(sock, expired)
            return


def cut(sock: Any, expired: threading.Event) -> None:
    """End the connection under a read that is still waiting."""
    expired.set()
    with contextlib.suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)


@dataclass
class Probed:
    """The probe's answer."""

    report: dict[str, object] | None
    reply: Reply
    clock_refused: bool = False  # signed: this machine's clock is outside the service's window


def probe(agent: Agent, token: str, alive: Callable[[], bool] | None = None) -> Probed:
    """The signed probe: the service's startup report, signed for this request, or no report
    when no Scrimmage with this token answered, and the reply itself. Its body is empty, so an
    app reached by mistake rejects it as a bad request. A signed clock reply says the two
    clocks differ by more than the window. alive: whether the service the runner started is
    still running; a probe that waits on a process that took its port ends when it exits."""
    _, _, path = target(agent)
    signed = protocol.request_headers(token, "POST", path, b"", time.time())
    headers = {
        "x-scrimmage-run": f"probe-{secrets.token_hex(6)}",
        "x-scrimmage-probe": "1",
        **signed,
    }
    reply = send(
        "POST",
        agent,
        path,
        headers,
        b"",
        30.0,
        CONNECT_S,
        limit=PROBE_MAX,
        deadline_s=PROBE_S,
        alive=alive,
    )
    found = Probed(None, reply)
    kind = reply.headers.get("x-scrimmage-probe")
    if (reply.status, kind) not in ((200, "report"), (409, "clock")):
        return found
    expected = protocol.sign_reply(token, signed[protocol.SIGNATURE], reply.status or 0, reply.body)
    if not protocol.same(reply.headers.get(protocol.SIGNATURE.lower(), ""), expected):
        return found
    with contextlib.suppress(ValueError):
        document = json.loads(reply.body)
        if isinstance(document, dict):
            fields = cast(dict[str, object], document)
            if kind == "clock":
                found.clock_refused = fields == {"error": "clock"}
            else:
                found.report = dict(fields)
    return found


def other_version(theirs: object, where: str = "in the service") -> str:
    """What to say when a process of the service runs another Scrimmage than this runner.
    where: the part of the service to install this runner's version in. A runner with no
    package metadata has no version to name, so the fix is in the runner instead."""
    ours = versions.installed()
    named = versions.shown(theirs) if isinstance(theirs, str) and theirs else "no version"
    if ours == versions.UNKNOWN:
        return (
            f"the service runs scrimmage {named} and this runner has no package metadata; "
            "install scrimmage in the runner"
        )
    return (
        f"the service runs scrimmage {named} and this runner is {ours}; "
        f"install scrimmage {ours} {where}"
    )


def check_version(report: dict[str, object]) -> None:
    """The report's Scrimmage against this runner's: the same known version passes with
    nothing said, and anything else is exit 2 naming both."""
    theirs = report.get("sdk")
    if not versions.same(theirs):
        raise Setup(2, other_version(theirs))


ROUTING_ERRORS = ("TimeoutError", "ConnectionRefusedError", "gaierror")  # asking again may help
HINTS = {
    "TimeoutError": (
        "nothing answered, as when a NetworkPolicy or a firewall drops the traffic, or when the "
        "control Service has no ready endpoints and an egress policy allows only named pods; "
        "allow the service's pods to reach this runner on port {port}, and check that the "
        "Service's selector matches this runner's pod"
    ),
    "ConnectionRefusedError": (
        "nothing listens there, as when a Kubernetes Service has no ready endpoints; check "
        "that the Service's selector matches this runner's pod"
    ),
    "gaierror": (
        "the name {host} does not resolve where the service runs; check the host in "
        "SCRIMMAGE_CONTROL_URL and that the Service is in the service's namespace"
    ),
}


NOT_YET = {
    "TimeoutError": "nothing answers yet",
    "ConnectionRefusedError": "nothing listens yet",
    "gaierror": "the name does not resolve yet",
}


def still_routing(report: dict[str, object] | None) -> bool:
    """The ping failed in a way a cluster still setting up its routes explains: a timeout, a
    refusal or a name that does not resolve yet. A certificate error, another runner's reply
    and a control server's own error are final, and so is a ping that was never made."""
    control = cast(dict[str, object], (report or {}).get("control") or {})
    return control.get("reached") is not True and control.get("error") in ROUTING_ERRORS


def check_control(
    report: dict[str, object], runner_id: str, *, asked_s: float, silent: bool = False
) -> None:
    """The service's ping: exit 3 when it could not reach this runner's control server, when
    the control server refused it for its time, or when it reached another runner that holds
    the same token. asked_s: how long the runner asked
    again, said in the message for the errors it asks again about."""
    control = cast(dict[str, object], report.get("control") or {})
    url = str(control.get("url") or "the control address")
    if control.get("reached") is not True:
        error = control.get("error")
        detail = control.get("detail")
        if detail == PING_CLOCK:
            raise Setup(3, CLOCK_OFF)
        if error in ROUTING_ERRORS:
            parts = urlsplit(url)
            hint = HINTS[str(error)].format(host=parts.hostname or url, port=parts.port or "")
            spent = f"{asked_s:.0f}" if asked_s >= 10 else f"{asked_s:.1f}"
            asked = f" after {spent} s of asking"
            if silent:
                asked += " (then the service stopped answering the probe)"
        else:
            asked = ""
            hint = (
                "check SCRIMMAGE_CONTROL_CA"
                if str(error).startswith("SSL")
                else "check that SCRIMMAGE_CONTROL_URL is an address the service can reach"
            )
        raise Setup(
            3,
            f"the service cannot reach the control server at {url}{asked}: {detail}; {hint}",
        )
    if control.get("runner_id") != runner_id:
        raise Setup(
            3,
            f"the control server the service reached at {url} answered as runner "
            f"{control.get('runner_id')}, not as this runner ({runner_id}): another runner "
            "holds the same token, or SCRIMMAGE_CONTROL_URL points at it",
        )


def not_listening(reply: Reply) -> bool:
    """Nothing listens at the address yet, or its connection timed out: a service still
    starting, or not started, so the probe asks again."""
    return (
        reply.failed is not None
        and not reply.connected
        and (reply.failed.startswith(("ConnectionRefusedError", "TimeoutError")))
    )


def accepted_then_cut(reply: Reply) -> bool:
    """The connection was taken and then reset or closed with no HTTP reply: what a
    container's published port does before the app inside listens."""
    return reply.failed is not None and reply.unanswered


def run_request(agent: Agent, header: str, question: str, timeout: float) -> Reply:
    """One run: the file's body and headers, with a new session, and the run header."""
    from scrimmage.config import new_session

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
