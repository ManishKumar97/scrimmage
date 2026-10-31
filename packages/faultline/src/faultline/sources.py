"""Where runs come from: span files, or a trace store's query API (Jaeger first)."""

import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from string import hexdigits
from typing import Protocol

from faultline.config import Attributes
from faultline.model import Run
from faultline.runs import read_requests, read_runs_many
from faultline.semconv import GEN_AI_RESPONSE_FINISH_REASONS


@dataclass(frozen=True, slots=True)
class SourceStats:
    """What a source read, for a report. Filled once `runs()` has been consumed."""

    files: int = 0
    unreadable: int = 0  # lines that were not a span file shape
    no_spans: int = 0  # lines that were JSON but held no spans
    traces: int = 0  # traces fetched from a trace store's API


class TraceSource(Protocol):
    name: str

    def runs(self) -> Iterator[Run]:
        """Every run. Raises OSError when the input cannot be read."""
        ...

    def stats(self) -> SourceStats: ...


UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
# ASCII digits only: \d also matches digits of other scripts, which int() accepts.
DURATION = re.compile(r"([0-9]+)([smhd])")
LONGEST = timedelta(days=3650)  # far past any trace store's retention


def parse_since(text: str) -> timedelta:
    """A window length like `30m`, `1h` or `2d`, at most ten years."""
    match = DURATION.fullmatch(text.strip())
    if not match or int(match[1]) == 0:
        raise ValueError(
            f"bad duration {text!r}; expected a whole number and a unit "
            "(s, m, h or d), for example 30m, 1h or 2d"
        )
    seconds = int(match[1]) * UNITS[match[2]]
    # Checked before timedelta, which overflows on a long enough number.
    if seconds > LONGEST.total_seconds():
        raise ValueError(f"duration {text!r} is longer than {LONGEST.days}d")
    return timedelta(seconds=seconds)


def started(run: Run) -> int:
    """When the run started, in unix nanoseconds: its earliest step or tool call."""
    return min(item.started for item in (*run.steps, *run.unplaced))


def started_since(runs: list[Run], since: timedelta, now: datetime) -> list[Run]:
    """The runs that started in the window. One rule for every source, so a run that
    straddles the window's start is left out of a file and of a trace store alike."""
    cutoff = nanos(now - since)
    return [run for run in runs if started(run) >= cutoff]


class FileSource:
    """One `.jsonl` span file, or every `*.jsonl` in a directory, sorted.
    With `since`, only the runs that started in that window before now."""

    def __init__(
        self,
        path: Path | str,
        attributes: Attributes = Attributes(),
        since: timedelta | None = None,
    ):
        self.path = Path(path)
        self.attributes = attributes
        self.since = since
        self.name = f"file:{path}"
        self._stats = SourceStats()

    def files(self) -> list[Path]:
        if self.path.is_dir():
            return sorted(self.path.glob("*.jsonl"))
        return [self.path]

    def runs(self) -> Iterator[Run]:
        files = self.files()
        # All files at once, so a trace split across files is still one run.
        reading = read_runs_many(files, self.attributes)
        self._stats = SourceStats(len(files), reading.unreadable, reading.no_spans)
        if self.since is None:
            yield from reading.runs
            return
        yield from started_since(reading.runs, self.since, datetime.now(UTC))

    def stats(self) -> SourceStats:
        return self._stats


def nanos(time: datetime) -> int:
    return round(time.timestamp() * 1_000_000) * 1000


class JaegerSource:
    """The traces of one service in a window ending now, from Jaeger's query API v3.

    Jaeger answers a search with whole traces, as OTLP in the protobuf JSON mapping.
    That mapping differs from the files: ids are base64, enums are names, and Jaeger 1.x
    stores an array attribute as its JSON text. `otlp_json` converts each back.
    """

    def __init__(
        self,
        base_url: str,
        service: str,
        since: timedelta,
        attributes: Attributes = Attributes(),
        limit: int = 1000,
        timeout: float = 30.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.service = service
        self.since = since
        self.attributes = attributes
        self.limit = limit  # traces per search; a search that hits it is split
        self.timeout = timeout
        self.name = f"jaeger:{self.base_url}"
        self._stats = SourceStats()

    def runs(self) -> Iterator[Run]:
        end = datetime.now(UTC)
        requests: list[dict] = []
        known: set[str] = set()
        self.search(end - self.since, end, requests, known)
        reading = read_requests(requests, self.attributes)
        self._stats = SourceStats(
            unreadable=reading.unreadable, no_spans=reading.no_spans, traces=len(known)
        )
        # Jaeger finds a trace by any span in the window, so drop the runs that
        # started before it. Jaeger returns no set order: oldest first, as in a file.
        runs = started_since(reading.runs, self.since, end)
        yield from sorted(runs, key=lambda run: (started(run), run.trace_id))

    def stats(self) -> SourceStats:
        return self._stats

    def search(
        self, start: datetime, end: datetime, requests: list[dict], known: set[str]
    ) -> None:
        """Add the traces in [start, end] to `requests`, each trace once."""
        results = [otlp_json(result) for result in self.fetch(start, end)]
        found = {span["traceId"] for result in results for span in spans_of(result)}
        if len(found) >= self.limit:
            # Jaeger stops at the limit and drops the rest without saying so.
            if end - start <= timedelta(seconds=1):
                raise OSError(
                    0,
                    f"more than {self.limit} traces in one second; cannot split further",
                    self.name,
                )
            middle = start + (end - start) / 2
            self.search(start, middle, requests, known)
            self.search(middle, end, requests, known)
            return
        new = found - known  # a trace on both sides of a split comes back twice
        requests += [keep_traces(result, new) for result in results]
        known |= new

    def fetch(self, start: datetime, end: datetime) -> list[dict]:
        """The `result` of every chunk Jaeger streams back for one search."""
        query = urllib.parse.urlencode(
            {
                "query.service_name": self.service,
                "query.start_time_min": rfc3339(start),
                "query.start_time_max": rfc3339(end),
                "query.num_traces": self.limit,
            }
        )
        url = f"{self.base_url}/api/v3/traces?{query}"
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as error:
            raise OSError(
                0, f"Jaeger answered {error.code}: {jaeger_error(error.read())}", url
            ) from error
        except (urllib.error.URLError, TimeoutError) as error:
            reason = getattr(error, "reason", error)
            raise OSError(0, f"cannot reach Jaeger: {reason}", url) from error
        return results_in(body, url)


def rfc3339(time: datetime) -> str:
    return (
        time.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def jaeger_error(body: bytes) -> str:
    """The message in a Jaeger error body, else the start of the body."""
    try:
        return json.loads(body)["error"]["message"]
    except (ValueError, TypeError, KeyError):
        return body[:200].decode("utf-8", "replace") or "no message"


def results_in(body: bytes, url: str) -> list[dict]:
    """Jaeger streams a search as JSON objects one after another, each one chunk:
    {"result": {"resourceSpans": [...]}}, or {"error": {...}} if it failed midway."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise OSError(0, "Jaeger's answer is not UTF-8", url) from error
    decoder = json.JSONDecoder()
    results = []
    index = 0
    while index < len(text):
        if text[index].isspace():
            index += 1
            continue
        try:
            chunk, index = decoder.raw_decode(text, index)
        except ValueError as error:
            hint = "not JSON; does the base URL include Jaeger's base path?"
            raise OSError(0, f"Jaeger's answer is {hint}", url) from error
        if not isinstance(chunk, dict):
            raise OSError(0, "Jaeger's answer is not a JSON object", url)
        if "error" in chunk:
            error = chunk["error"]
            message = error.get("message") if isinstance(error, dict) else error
            raise OSError(
                0, f"Jaeger failed the search: {message or 'no message'}", url
            )
        result = chunk.get("result") or {}
        problem = shape_problem(result)
        if problem:
            raise OSError(0, f"Jaeger's answer has an unknown shape: {problem}", url)
        results.append(result)
    return results


def shape_problem(result: object) -> str | None:
    """What in one result is not the OTLP shape the reader walks, else None.
    Checked once here, so the code after it can index without guarding."""
    if not isinstance(result, dict):
        return f"result is {type(result).__name__}, not an object"
    resources = result.get("resourceSpans", [])
    if not is_list_of_objects(resources):
        return "resourceSpans is not a list of objects"
    for resource in resources:
        scopes = resource.get("scopeSpans", [])
        if not is_list_of_objects(scopes):
            return "scopeSpans is not a list of objects"
        for scope in scopes:
            spans = scope.get("spans", [])
            if not is_list_of_objects(spans):
                return "spans is not a list of objects"
            for span in spans:
                if not isinstance(span.get("traceId"), str) or not span["traceId"]:
                    return "a span has no traceId"
                for key in ("spanId", "parentSpanId"):
                    if not isinstance(span.get(key, ""), str):
                        return f"a span's {key} is not a string"
                if not isinstance(span.get("status", {}), dict):
                    return "a span's status is not an object"
                if not is_list_of_objects(span.get("attributes", [])):
                    return "a span's attributes are not a list of objects"
    return None


def is_list_of_objects(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, dict) for item in value)


def spans_of(result: dict) -> list[dict]:
    return [
        span
        for resource in result.get("resourceSpans", [])
        for scope in resource.get("scopeSpans", [])
        for span in scope.get("spans", [])
    ]


def keep_traces(result: dict, trace_ids: set[str]) -> dict:
    """The result with only the spans of the given traces."""
    resources = []
    for resource in result.get("resourceSpans", []):
        scopes = []
        for scope in resource.get("scopeSpans", []):
            spans = [s for s in scope.get("spans", []) if s["traceId"] in trace_ids]
            if spans:
                scopes.append({**scope, "spans": spans})
        if scopes:
            resources.append({**resource, "scopeSpans": scopes})
    return {"resourceSpans": resources}


STATUS_CODES = {"STATUS_CODE_UNSET": 0, "STATUS_CODE_OK": 1, "STATUS_CODE_ERROR": 2}
ID_KEYS = ("traceId", "spanId", "parentSpanId")
# Attributes the reader reads as arrays, which Jaeger 1.x returns as JSON text.
ARRAY_ATTRIBUTES = {GEN_AI_RESPONSE_FINISH_REASONS}


def otlp_json(result: dict) -> dict:
    """Rewrite one result in place the way a span file holds it: hex ids, integer
    status codes and array attributes as arrays. Raises OSError on a bad id."""
    for span in spans_of(result):
        for key in ID_KEYS:
            if span.get(key):
                span[key] = hex_id(span[key])
        status = span.get("status")
        if isinstance(status, dict) and status.get("code") in STATUS_CODES:
            status["code"] = STATUS_CODES[status["code"]]
        for attribute in span.get("attributes", []):
            if attribute.get("key") in ARRAY_ATTRIBUTES:
                attribute["value"] = as_array(attribute.get("value"))
    return result


def hex_id(value: str) -> str:
    """A span id (8 bytes) or trace id (16 bytes) in hex. Jaeger 1.x sends base64;
    hex passes through."""
    if len(value) in (16, 32) and all(c in hexdigits for c in value):
        return value.lower()
    message = f"Jaeger sent an id that is not 8 or 16 bytes in hex or base64: {value!r}"
    try:
        raw = base64.b64decode(value, validate=True)
    except ValueError as error:  # binascii.Error is one
        raise OSError(0, message) from error
    if len(raw) not in (8, 16):
        raise OSError(0, message)
    return raw.hex()


def as_array(value):
    """An array AnyValue from a string holding a JSON list of strings, else unchanged."""
    if not isinstance(value, dict) or "stringValue" not in value:
        return value
    try:
        items = json.loads(value["stringValue"])
    except ValueError:
        return value
    if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
        return value
    return {"arrayValue": {"values": [{"stringValue": item} for item in items]}}


DEFAULT_JAEGER = "http://localhost:16686"


def parse_source(
    spec: str,
    attributes: Attributes = Attributes(),
    *,
    service: str | None = None,
    since: timedelta | None = None,
) -> TraceSource:
    """A source from `scheme:rest`, for example `file:traces/` or
    `jaeger:http://localhost:16686`. `service` names the traced service in a trace
    store; `since` keeps only the runs in that window before now."""
    scheme, _, rest = spec.partition(":")
    if scheme == "file" and rest:
        return FileSource(rest, attributes, since)
    if scheme == "jaeger":
        base_url = rest or DEFAULT_JAEGER
        if not base_url.startswith(("http://", "https://")):
            raise ValueError(
                f"a jaeger URL must start with http:// or https://: {spec!r}"
            )
        if not service:
            raise ValueError("a jaeger source needs --service NAME")
        if since is None:
            raise ValueError("a jaeger source needs a window, for example --since 1h")
        return JaegerSource(base_url, service, since, attributes)
    raise ValueError(
        f"unknown trace source {spec!r}; expected SCHEME:LOCATION, "
        "known schemes: file, jaeger"
    )
