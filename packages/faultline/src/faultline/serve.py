"""`faultline serve`: the findings report as JSON over HTTP, for Grafana and scripts.

    GET /api/findings?since=1h   the report `faultline find --json` prints for the window
    GET /api/health              {"status": "ok"}

Read-only. Every request reads the sources again, so the answer is always current.
Every error is JSON too: {"error": "...", "status": 404}.
"""

import argparse
import json
import sys
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


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        url = urlsplit(self.path)
        if url.path == "/api/health":
            self.send_json(HTTPStatus.OK, {"status": "ok"})
        elif url.path == "/api/findings":
            self.findings(parse_qs(url.query))
        else:
            self.send_error(HTTPStatus.NOT_FOUND, f"no route {url.path}")

    def findings(self, query: dict[str, list[str]]) -> None:
        try:
            since = parse_since(query["since"][-1]) if "since" in query else None
            report = cast(FindingsServer, self.server).report(since)
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
