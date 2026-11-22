"""scrimmage prices update: fetch over HTTPS, check the schema, print the diff, then write."""

import contextlib
import http.server
import socket
import ssl
import stat
import threading
import time
from collections.abc import Callable, Iterator
from importlib import resources
from pathlib import Path
from typing import Any

import pytest

from scrimmage import cli, prices, prices_update
from scrimmage.cert import make_certificates

NEWER = (
    resources.files("scrimmage")
    .joinpath("prices.toml")
    .read_text(encoding="utf-8")
    .replace('as_of = "2026-11-22"', 'as_of = "2026-12-01"')
    .replace("input = 2.00\n", "input = 2.25\n", 1)  # gpt-4.1
    .replace(
        '[models."gpt-5-nano"]',
        '[models."gpt-9"]\ninput = 1.0\noutput = 2.0\n\n[models."gpt-5-nano"]',
    )
)
BAD = NEWER.replace("input = 2.25", 'input = "two"')


class Server:
    def __init__(self, certs: Path) -> None:
        self.body = NEWER.encode()
        self.status = 200
        self.asked = 0
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                outer.asked += 1
                self.send_response(outer.status)
                self.end_headers()
                self.wfile.write(outer.body)

            def log_message(self, format: str, *args: object) -> None:
                pass

        self.http = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certs / "server.pem", certs / "server.key")
        self.http.socket = context.wrap_socket(self.http.socket, server_side=True)
        self.url = f"https://127.0.0.1:{self.http.server_address[1]}/prices.toml"
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(5)


@pytest.fixture
def served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Server]:
    files = make_certificates(tmp_path / "certs", ips=("127.0.0.1",))
    monkeypatch.setattr(prices_update, "CAFILE", str(files.ca))
    server = Server(tmp_path / "certs")
    monkeypatch.setattr(prices_update, "URL", server.url)
    yield server
    server.stop()


def project(tmp_path: Path) -> Path:
    folder = tmp_path / "proj"
    folder.mkdir()
    (folder / "scrimmage.toml").write_text("")
    return folder


@pytest.mark.design("prices.update")
def test_update_prints_the_diff_and_writes_the_team_file(
    served: Server, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = project(tmp_path)
    assert cli.main(["prices", "update", str(folder / "scrimmage.toml")]) == 0
    out = capsys.readouterr().out
    assert "added gpt-9: input 1, output 2" in out
    assert "changed gpt-4.1: input 2 to 2.25" in out
    assert "removed" not in out
    assert "the new file is as of 2026-12-01" in out
    runs = folder / "scrimmage-runs"
    assert (runs / "prices.toml").read_text() == NEWER
    assert (runs / ".gitignore").read_text() == "*\n"
    assert stat.S_IMODE(runs.stat().st_mode) == 0o700
    assert prices.team(folder) is not None
    assert [p.name for p in runs.iterdir() if p.name.endswith(".tmp")] == []


@pytest.mark.design("prices.update")
def test_a_second_update_compares_with_the_team_file(
    served: Server, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = project(tmp_path)
    cli.main(["prices", "update", str(folder / "scrimmage.toml")])
    capsys.readouterr()
    served.body = NEWER.replace("input = 2.25", "input = 2.50").encode()
    assert cli.main(["prices", "update", str(folder / "scrimmage.toml")]) == 0
    out = capsys.readouterr().out
    assert "compared with scrimmage-runs/prices.toml" in out
    assert "changed gpt-4.1: input 2.25 to 2.5" in out
    assert "gpt-9" not in out


@pytest.mark.design("prices.update")
def test_a_file_that_fails_its_schema_writes_nothing_and_exits_2(
    served: Server, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = project(tmp_path)
    served.body = BAD.encode()
    assert cli.main(["prices", "update", str(folder / "scrimmage.toml")]) == 2
    err = capsys.readouterr().err
    assert "models gpt-4.1 input: expected dollars per million tokens" in err
    assert "nothing was written" in err
    assert not (folder / "scrimmage-runs").exists()
    assert served.asked == 1


@pytest.mark.design("prices.update")
def test_a_bad_file_does_not_replace_a_good_team_file(
    served: Server, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = project(tmp_path)
    cli.main(["prices", "update", str(folder / "scrimmage.toml")])
    served.body = b"as_of = 5"
    assert cli.main(["prices", "update", str(folder / "scrimmage.toml")]) == 2
    assert (folder / "scrimmage-runs" / "prices.toml").read_text() == NEWER


@pytest.mark.design("prices.update")
def test_an_unreachable_server_writes_nothing_and_exits_3(
    served: Server,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = project(tmp_path)
    served.stop()
    assert cli.main(["prices", "update", str(folder / "scrimmage.toml")]) == 3
    assert "nothing was written" in capsys.readouterr().err
    assert not (folder / "scrimmage-runs").exists()


@pytest.mark.design("prices.update")
def test_a_server_error_exits_3_and_a_certificate_we_do_not_trust_exits_3(
    served: Server,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = project(tmp_path)
    served.status = 500
    assert cli.main(["prices", "update", str(folder / "scrimmage.toml")]) == 3
    served.status = 200
    monkeypatch.setattr(
        prices_update, "CAFILE", None
    )  # the system's authorities do not know the test's
    assert cli.main(["prices", "update", str(folder / "scrimmage.toml")]) == 3
    assert not (folder / "scrimmage-runs").exists()


@pytest.mark.design("prices.update")
def test_only_an_https_address_is_fetched(tmp_path: Path) -> None:
    with pytest.raises(prices_update.FetchError, match="not an https address"):
        prices_update.fetch("http://127.0.0.1:1/prices.toml")
    with pytest.raises(prices_update.FetchError, match="not an https address"):
        prices_update.fetch("file:///etc/passwd")


@pytest.mark.design("prices.file_never_free")
@pytest.mark.parametrize(
    "free",
    ['[models."gpt-4.1"]\ninput = 0\noutput = 0\n', '[models."gpt-4.1"]\ninput = 1\noutput = 0\n'],
)
def test_a_fetched_file_that_makes_a_model_free_writes_nothing_and_exits_2(
    served: Server, tmp_path: Path, capsys: pytest.CaptureFixture[str], free: str
) -> None:
    folder = project(tmp_path)
    served.body = ('as_of = "2026-12-01"\nsource = "x"\n' + free).encode()
    assert cli.main(["prices", "update", str(folder / "scrimmage.toml")]) == 2
    assert "is below 0.001" in capsys.readouterr().err
    assert not (folder / "scrimmage-runs").exists()


class Raw:
    """A TLS server that answers a request with whatever its function writes, byte by byte."""

    def __init__(self, certs: Path, answer: Callable[[ssl.SSLSocket], None]) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certs / "server.pem", certs / "server.key")
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.url = f"https://127.0.0.1:{self.listener.getsockname()[1]}/prices.toml"
        self.answer, self.context, self.asked = answer, context, 0
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self) -> None:
        while True:
            try:
                raw, _ = self.listener.accept()
            except OSError:
                return
            try:
                with self.context.wrap_socket(raw, server_side=True) as tls:
                    if tls.recv(65536):  # a connection that sends no request is not asked
                        self.asked += 1
                        self.answer(tls)
            except OSError:
                pass

    def stop(self) -> None:
        with contextlib.suppress(OSError):
            self.listener.shutdown(socket.SHUT_RDWR)  # wakes the accept that is waiting
        self.listener.close()
        self.thread.join(5)


@pytest.fixture
def raw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., Raw]]:
    files = make_certificates(tmp_path / "certs", ips=("127.0.0.1",))
    monkeypatch.setattr(prices_update, "CAFILE", str(files.ca))
    started: list[Raw] = []

    def start(answer: Callable[[ssl.SSLSocket], None]) -> Raw:
        server = Raw(tmp_path / "certs", answer)
        started.append(server)
        monkeypatch.setattr(prices_update, "URL", server.url)
        return server

    yield start
    for server in started:
        server.stop()


def says(data: bytes) -> Callable[[ssl.SSLSocket], None]:
    return lambda tls: tls.sendall(data)


def nothing_written(folder: Path) -> bool:
    return not (folder / "scrimmage-runs").exists()


@pytest.mark.design("prices.update_hardened")
@pytest.mark.parametrize(
    ("reply", "words"),
    [
        (b"NOT HTTP AT ALL\r\n\r\n", "cannot fetch"),
        (b"HTTP/1.1 200 OK\r\nX: " + b"a" * 70_000 + b"\r\n\r\n", "cannot fetch"),
        (b"HTTP/1.1 200 OK\r\nContent-Length: 5000\r\n\r\nabcde", "5000 it promised"),
        (b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\n\xff\xfe\xfd", "not UTF-8"),
    ],
    ids=["bad status line", "oversized header", "short body", "not text"],
)
def test_a_broken_reply_exits_3_with_a_message_and_writes_nothing(
    raw: Callable[..., Raw],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    reply: bytes,
    words: str,
) -> None:
    folder = project(tmp_path)
    raw(says(reply))
    assert cli.main(["prices", "update", str(folder / "scrimmage.toml")]) == 3
    err = capsys.readouterr().err
    assert words in err and "nothing was written" in err and "Traceback" not in err
    assert nothing_written(folder)


@pytest.mark.design("prices.update_hardened")
def test_a_file_nested_too_deeply_exits_2_with_a_message(
    raw: Callable[..., Raw], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = project(tmp_path)
    body = b"x = " + b"[" * 5000
    raw(says(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body) + body))
    assert cli.main(["prices", "update", str(folder / "scrimmage.toml")]) == 2
    assert "nested too deeply" in capsys.readouterr().err
    assert nothing_written(folder)


@pytest.mark.design("prices.update_hardened")
def test_a_redirect_is_refused_before_a_request_goes_to_the_other_place(
    raw: Callable[..., Raw], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = project(tmp_path)
    plain = socket.create_server(("127.0.0.1", 0))  # a plain-text host the redirect names
    plain.settimeout(0.5)
    other = raw(says(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"))
    for target in (f"http://127.0.0.1:{plain.getsockname()[1]}/x", other.url):
        first = raw(
            says(
                b"HTTP/1.1 302 Found\r\nLocation: %s\r\nContent-Length: 0\r\n\r\n" % target.encode()
            )
        )
        assert cli.main(["prices", "update", str(folder / "scrimmage.toml")]) == 3
        assert "so it was refused" in capsys.readouterr().err
        assert first.asked == 1
    with pytest.raises(TimeoutError):
        plain.accept()  # nothing ever connected to the plain host
    plain.close()
    assert other.asked == 0
    assert nothing_written(folder)


@pytest.mark.design("prices.update_hardened")
def test_the_deadline_covers_the_whole_fetch_not_each_read(
    raw: Callable[..., Raw],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = project(tmp_path)
    monkeypatch.setattr(prices_update, "TIMEOUT_S", 1.0)

    def drip(tls: ssl.SSLSocket) -> None:
        tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n")
        for _ in range(60):  # a byte every 0.2 s: each read is quick, the whole is slow
            tls.sendall(b"#")
            time.sleep(0.2)

    raw(drip)
    began = time.monotonic()
    assert cli.main(["prices", "update", str(folder / "scrimmage.toml")]) == 3
    assert time.monotonic() - began < 4
    assert "took longer than 1 seconds" in capsys.readouterr().err
    assert nothing_written(folder)


@pytest.mark.design("prices.update_hardened")
def test_the_deadline_covers_the_headers_too(
    raw: Callable[..., Raw],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = project(tmp_path)
    monkeypatch.setattr(prices_update, "TIMEOUT_S", 1.0)

    def drip(tls: ssl.SSLSocket) -> None:
        tls.sendall(b"HTTP/1.1 200 OK\r\n")
        for number in range(40):  # a header line every 0.3 s, so no read waits long
            tls.sendall(b"X-%d: y\r\n" % number)
            time.sleep(0.3)

    raw(drip)
    began = time.monotonic()
    assert cli.main(["prices", "update", str(folder / "scrimmage.toml")]) == 3
    assert time.monotonic() - began < 4
    assert "took longer than 1 seconds" in capsys.readouterr().err
    assert nothing_written(folder)


@pytest.mark.design("prices.update_hardened")
def test_the_deadline_covers_the_name_lookup_before_a_connection_exists(
    raw: Callable[..., Raw],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = project(tmp_path)
    monkeypatch.setattr(prices_update, "TIMEOUT_S", 0.5)
    lookup = socket.getaddrinfo

    def slow(*args: Any, **kwargs: Any) -> Any:
        time.sleep(1.2)  # a resolver that takes longer than the whole deadline
        return lookup(*args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", slow)
    server = raw(says(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"))
    began = time.monotonic()
    assert cli.main(["prices", "update", str(folder / "scrimmage.toml")]) == 3
    assert time.monotonic() - began < 4
    assert "took longer than 0.5 seconds" in capsys.readouterr().err
    assert server.asked == 0  # no request was sent on a connection that came too late
    assert nothing_written(folder)
