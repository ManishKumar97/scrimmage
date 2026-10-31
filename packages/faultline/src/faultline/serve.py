"""`faultline serve`: the findings report as JSON over HTTP, for Grafana and scripts.

    GET /api/findings?since=1h   the report `faultline find --json` prints for the window
    GET /api/health              {"status": "ok"}

Read-only. Every request reads the sources again, so the answer is always current.
Every error is JSON too: {"error": "...", "status": 404}.
"""

import argparse
import json
import sys
import traceback
from collections.abc import Sequence
from datetime import timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlsplit

from faultline.config import Config, load_config
from faultline.find import find
from faultline.sources import parse_since, parse_source


class FindingsServer(ThreadingHTTPServer):
    daemon_threads = True  # a slow client never holds up a shutdown

    def __init__(
        self,
        address: tuple[str, int],
        specs: Sequence[str],
        service: str | None,
        config: Config,
    ):
        super().__init__(address, Handler)
        self.specs = list(specs)
        self.service = service
        self.config = config
        # A trace store source cannot read without a window; a file source can.
        self.needs_window = not all(can_read_all(spec, service) for spec in specs)

    def report(self, since: timedelta | None) -> dict:
        """The report for the window, as `find --json` prints it.
        Raises ValueError for a bad request and OSError when a source cannot be read."""
        sources = [
            parse_source(
                spec, self.config.attributes, service=self.service, since=since
            )
            for spec in self.specs
        ]
        runs = [run for source in sources for run in source.runs()]
        return find(runs, self.config).to_json()


def can_read_all(spec: str, service: str | None) -> bool:
    try:
        parse_source(spec, service=service)
    except ValueError:
        return False
    return True


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        try:
            self.route()
        except Exception:  # noqa: BLE001 - last resort: a bug answers JSON, logs detail
            traceback.print_exc(file=sys.stderr)
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal error")

    def do_HEAD(self) -> None:
        self.do_GET()  # the same status and headers, and no body (see send_json)

    def route(self) -> None:
        url = urlsplit(self.path)
        if url.path == "/api/health":
            self.send_json(HTTPStatus.OK, {"status": "ok"})
        elif url.path == "/api/findings":
            # Blank values kept, so ?since= is an error, not the same as no since.
            self.findings(parse_qs(url.query, keep_blank_values=True))
        else:
            self.send_error(HTTPStatus.NOT_FOUND, f"no route {url.path}")

    def findings(self, query: dict[str, list[str]]) -> None:
        server = cast(FindingsServer, self.server)
        try:
            since = window(query, server.needs_window)
            report = server.report(since)
        except ValueError as error:
            self.send_error(HTTPStatus.BAD_REQUEST, str(error))
        except OSError as error:
            # The trace source is upstream of this server, a file or a trace store.
            what = error.filename or "a trace source"
            reason = error.strerror or str(error)
            self.send_error(HTTPStatus.BAD_GATEWAY, f"cannot read {what}: {reason}")
        else:
            self.send_json(HTTPStatus.OK, report)

    def send_error(
        self, code: int, message: str | None = None, explain: str | None = None
    ) -> None:
        """Every error as JSON, including the ones http.server sends itself, such as
        501 for a method other than GET."""
        status = HTTPStatus(code)
        self.send_json(status, {"error": message or status.phrase, "status": code})

    def send_json(self, status: HTTPStatus, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)


def window(query: dict[str, list[str]], needed: bool) -> timedelta | None:
    """The `since` parameter, None when absent. Raises ValueError worded for HTTP."""
    if "since" not in query:
        if needed:
            raise ValueError(
                "this server reads a trace store, which needs a window: "
                "add ?since=, for example /api/findings?since=1h"
            )
        return None
    text = query["since"][-1]
    if not text:
        raise ValueError("since= is empty; give a window, for example ?since=1h")
    try:
        return parse_since(text)
    except ValueError as error:
        raise ValueError(f"query parameter since: {error}") from error


def add_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "serve", help="answer the findings report as JSON over HTTP"
    )
    parser.add_argument(
        "--source",
        action="append",
        required=True,
        metavar="SPEC",
        help="where traces come from, as for find (repeatable)",
    )
    parser.add_argument(
        "--service", help="the traced service to read from a trace store like Jaeger"
    )
    parser.add_argument(
        "--config", type=Path, help="TOML settings, default ./faultline.toml if present"
    )
    parser.add_argument("--host", default="127.0.0.1", help="default 127.0.0.1")
    parser.add_argument("--port", type=int, default=8765, help="default 8765")
    parser.set_defaults(handler=run)


def fail(message: str) -> int:
    print(f"faultline serve: {message}", file=sys.stderr)
    return 2


def run(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
        # Check every spec now, with any window, so a typo fails at start, not later.
        for spec in args.source:
            parse_source(spec, service=args.service, since=timedelta(hours=1))
    except ValueError as error:  # includes ConfigError
        return fail(str(error))
    try:
        server = FindingsServer(
            (args.host, args.port), args.source, args.service, config
        )
    except OSError as error:
        return fail(f"cannot listen on {args.host}:{args.port}: {error.strerror}")
    host, port = server.server_address[:2]
    print(f"faultline serve: listening on http://{host}:{port}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
