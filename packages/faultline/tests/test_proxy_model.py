"""The model proxy against a plain standard-library upstream, so every byte is visible."""

import asyncio
import http.client
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast
from urllib.parse import urlsplit

from faultline.proxy.model import ModelProxy, Steps


class Upstream(ThreadingHTTPServer):
    """Records each request as it arrived and answers with a set reply."""

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), Handler)
        self.seen: list[tuple[str, str, list[tuple[str, str]], bytes]] = []
        self.status = 200
        self.reply_headers = [("Content-Type", "application/json")]
        self.chunks = [b'{"ok": true}']
        self.between = threading.Event()  # set by a test to release the second chunk
        self.between.set()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        server = cast(Upstream, self.server)
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        server.seen.append((self.command, self.path, self.headers.items(), body))
        self.send_response(server.status)
        for name, value in server.reply_headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(sum(map(len, server.chunks))))
        self.end_headers()
        for number, chunk in enumerate(server.chunks):
            if number:
                server.between.wait(5)
            self.wfile.write(chunk)
            self.wfile.flush()

    def log_message(self, format, *args) -> None:
        pass


@contextmanager
def upstream_server() -> Iterator[Upstream]:
    server = Upstream()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.between.set()
        server.shutdown()
        server.server_close()


@contextmanager
def running(proxy: ModelProxy) -> Iterator[str]:
    """The proxy on its own event loop thread, so the test can use blocking clients."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    url = asyncio.run_coroutine_threadsafe(proxy.start(), loop).result(5)
    try:
        yield url
    finally:
        asyncio.run_coroutine_threadsafe(proxy.stop(), loop).result(5)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)
        loop.close()


def connect(url: str) -> http.client.HTTPConnection:
    parts = urlsplit(url)
    return http.client.HTTPConnection(parts.hostname or "", parts.port, timeout=5)


# Not what json.dumps would write: odd spacing, key order, escapes and raw UTF-8.
BODY = '{"model":"m",  "messages":[{"role":"user","content":"caf\\u00e9 été"}] ,"z":1}'


def test_the_upstream_receives_the_bytes_the_client_sent():
    body = BODY.encode()
    headers = {
        "Authorization": "Bearer fake",
        "Content-Type": "application/json",
        "X-Stainless-Retry-Count": "0",
    }
    with (
        upstream_server() as upstream,
        running(ModelProxy(upstream.url + "/v1")) as url,
    ):
        connection = connect(url)
        connection.request("POST", "/chat/completions?x=1&y=%2F", body, headers)
        reply = connection.getresponse().read()

    method, path, seen_headers, seen_body = upstream.seen[0]
    assert seen_body == body
    assert (method, path) == ("POST", "/v1/chat/completions?x=1&y=%2F")
    sent = {name.lower(): value for name, value in headers.items()}
    received = {name.lower(): value for name, value in seen_headers}
    assert {k: received.get(k) for k in sent} == sent
    # Nothing added that the client did not send, besides the upstream's Host.
    assert set(received) == set(sent) | {"host", "content-length", "accept-encoding"}
    assert received["content-length"] == str(len(body))
    assert reply == b'{"ok": true}'


def test_the_reply_comes_back_with_its_status_headers_and_bytes():
    with upstream_server() as upstream, running(ModelProxy(upstream.url)) as url:
        upstream.status = 429
        upstream.reply_headers = [("Retry-After", "1"), ("Content-Encoding", "gzip")]
        upstream.chunks = [b"\x1f\x8b not really gzip"]
        connection = connect(url)
        connection.request("POST", "/x", b"{}")
        response = connection.getresponse()
        body = response.read()

    assert response.status == 429
    assert response.getheader("Retry-After") == "1"
    assert response.getheader("Content-Encoding") == "gzip"  # passed on, not decoded
    assert body == b"\x1f\x8b not really gzip"


def test_the_reply_streams_back_before_the_upstream_has_finished():
    with upstream_server() as upstream, running(ModelProxy(upstream.url)) as url:
        upstream.chunks = [b"data: one\n\n", b"data: two\n\n"]
        upstream.between.clear()  # the second chunk waits until the first arrived
        connection = connect(url)
        connection.request("POST", "/x", b"{}")
        response = connection.getresponse()
        first = response.read(len(b"data: one\n\n"))
        upstream.between.set()
        rest = response.read()

    assert first == b"data: one\n\n"
    assert rest == b"data: two\n\n"


def test_an_unreachable_upstream_is_a_bad_gateway():
    with running(ModelProxy("http://127.0.0.1:9")) as url:
        connection = connect(url)
        connection.request("POST", "/x", b"{}")
        response = connection.getresponse()
        text = response.read().decode()

    assert response.status == 502
    assert "upstream http://127.0.0.1:9 failed" in text


def test_a_request_with_the_same_body_is_the_next_attempt_of_the_same_step():
    steps = Steps()

    counted = [steps.count(body) for body in (b"a", b"a", b"a", b"b", b"a")]

    assert counted == [(1, 1), (1, 2), (1, 3), (2, 1), (3, 1)]
