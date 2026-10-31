"""A plain standard-library HTTP server that records every byte it receives."""

import http.client
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast
from urllib.parse import urlsplit


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


def connect(url: str) -> http.client.HTTPConnection:
    parts = urlsplit(url)
    return http.client.HTTPConnection(parts.hostname or "", parts.port, timeout=5)
