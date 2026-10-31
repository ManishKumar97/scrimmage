"""A stand-in for Jaeger's query API v3 search, for the source's tests.

It answers `GET /api/v3/traces` the way Jaeger 1.x does, checked against the test bed's
Jaeger: whole traces as {"result": {"resourceSpans": [...]}} chunks, one after another,
with base64 ids, status codes as enum names, and array attributes as their JSON text.
It honours the window and `query.num_traces`, and records every query it answered.
"""

import base64
import copy
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

STATUS_NAMES = {0: "STATUS_CODE_UNSET", 1: "STATUS_CODE_OK", 2: "STATUS_CODE_ERROR"}


def jaeger_span(span: dict) -> dict:
    """A span in Jaeger 1.x's JSON, from a span as a file holds it."""
    out = copy.deepcopy(span)
    for key in ("traceId", "spanId", "parentSpanId"):
        if out.get(key):
            out[key] = base64.b64encode(bytes.fromhex(out[key])).decode()
    if "status" in out:
        out["status"]["code"] = STATUS_NAMES[out["status"].get("code", 0)]
    for attribute in out.get("attributes", []):
        array = attribute["value"].get("arrayValue")
        if array is not None:
            items = [item["stringValue"] for item in array.get("values", [])]
            attribute["value"] = {"stringValue": json.dumps(items)}
    return out


def nanos(text: str) -> int:
    return round(datetime.fromisoformat(text).timestamp() * 1_000_000) * 1000


@dataclass
class FakeJaeger:
    traces: dict[str, list[dict]] = field(default_factory=dict)  # by hex trace id
    queries: list[dict[str, str]] = field(default_factory=list)
    reply: tuple[int, bytes] | None = None  # a fixed answer instead of a search
    url: str = ""

    def add(self, spans: list[dict]) -> None:
        for span in spans:
            self.traces.setdefault(span["traceId"], []).append(span)

    def search(self, query: dict[str, str]) -> bytes:
        low = nanos(query["query.start_time_min"])
        high = nanos(query["query.start_time_max"])
        limit = int(query.get("query.num_traces", 20))
        found = [
            spans
            for spans in self.traces.values()
            if any(low <= int(s["startTimeUnixNano"]) <= high for s in spans)
        ][:limit]
        chunks = [
            {"result": {"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]}}
            for spans in ([jaeger_span(s) for s in trace] for trace in found)
        ]
        if not chunks:
            return b'{"result":{}}'
        return "".join(json.dumps(chunk) for chunk in chunks).encode()


@contextmanager
def fake_jaeger() -> Iterator[FakeJaeger]:
    fake = FakeJaeger()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            url = urlparse(self.path)
            query = {key: values[0] for key, values in parse_qs(url.query).items()}
            if fake.reply is not None:
                status, body = fake.reply
            elif url.path == "/api/v3/traces":
                fake.queries.append(query)
                status, body = 200, fake.search(query)
            else:
                status, body = 404, b"404 page not found"
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    fake.url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield fake
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def shifted(spans: list[dict], start: int) -> list[dict]:
    """The spans moved in time so the earliest starts at `start`."""
    earliest = min(int(s.get("startTimeUnixNano", 0)) for s in spans)
    moved = []
    for span in spans:
        span = json.loads(json.dumps(span))
        for key in ("startTimeUnixNano", "endTimeUnixNano"):
            span[key] = str(int(span.get(key, 0)) - earliest + start)
        moved.append(span)
    return moved


def hexed(path: Path) -> list[dict]:
    """The spans of a file written by spans.py, with every id in hex as Jaeger needs.
    The file keeps its own ids; the runs differ only in their trace ids."""
    ids: dict[str, str] = {}

    def to_hex(value: str, width: int) -> str:
        if len(value) == width and all(c in "0123456789abcdef" for c in value):
            return value
        return ids.setdefault(value, f"{len(ids) + 1:0{width}x}")

    spans = []
    for line in path.read_text().splitlines():
        for span in json.loads(line)["resourceSpans"][0]["scopeSpans"][0]["spans"]:
            span["traceId"] = to_hex(span["traceId"], 32)
            # spans.py reuses span names across traces, so key them by trace too.
            for key in ("spanId", "parentSpanId"):
                if key in span:
                    span[key] = to_hex(f"{span['traceId']}/{span[key]}", 16)
            spans.append(span)
    return spans
