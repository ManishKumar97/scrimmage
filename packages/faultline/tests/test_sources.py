import json
import time
from datetime import timedelta
from pathlib import Path

import pytest
from fake_jaeger import fake_jaeger
from spans import write_trace

from faultline import cli
from faultline.runs import show_run
from faultline.sources import (
    FileSource,
    JaegerSource,
    SourceStats,
    parse_since,
    parse_source,
)

FIXTURES = sorted((Path(__file__).parent / "fixtures").glob("*.jsonl"))
SECOND = 1_000_000_000
HOUR = timedelta(hours=1)


def now() -> int:
    return time.time_ns()


def fixture_spans() -> list[dict]:
    """Every span in the committed fixtures that the exporter wrote, hex ids and all."""
    spans = []
    for path in FIXTURES:
        for raw in path.read_bytes().splitlines():
            try:
                request = json.loads(raw)
                spans += [
                    span
                    for resource in request["resourceSpans"]
                    for scope in resource["scopeSpans"]
                    for span in scope["spans"]
                    if isinstance(span, dict) and span.get("traceId")
                ]
            except (ValueError, TypeError, KeyError):
                continue  # the fixtures of broken lines
    return spans


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


def write_spans(path: Path, spans: list[dict]) -> Path:
    request = {"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]}
    path.write_text(json.dumps(request) + "\n")
    return path


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


def shown(runs) -> list[str]:
    return sorted(show_run(run) for run in runs)


def test_jaeger_gives_the_same_runs_steps_and_tool_calls_as_the_file(tmp_path):
    spans = shifted(fixture_spans(), now() - 600 * SECOND)
    path = write_spans(tmp_path / "spans.jsonl", spans)
    with fake_jaeger() as jaeger:
        jaeger.add(spans)
        source = JaegerSource(jaeger.url, "agent", HOUR)

        from_jaeger = list(source.runs())

    from_file = list(FileSource(path).runs())
    assert len(from_file) > 10
    assert shown(from_jaeger) == shown(from_file)
    # The fixtures hold errors and finish reasons, so the conversion was exercised.
    assert any(run.error_type for run in from_jaeger)
    assert any(step.finish_reason for run in from_jaeger for step in run.steps)
    assert source.stats() == SourceStats(traces=len(jaeger.traces))
    [query] = jaeger.queries
    assert query["query.service_name"] == "agent"


def test_jaeger_runs_come_oldest_first(tmp_path):
    path = tmp_path / "s.jsonl"
    for number, trace in enumerate(["b" * 32, "a" * 32, "c" * 32]):
        write_trace(
            path, {"text": "x"}, trace=trace, base=now() - (9 - number) * SECOND
        )
    with fake_jaeger() as jaeger:
        jaeger.add(list(reversed(hexed(path))))
        found = list(JaegerSource(jaeger.url, "agent", HOUR).runs())

    assert [run.trace_id for run in found] == ["b" * 32, "a" * 32, "c" * 32]


def test_a_search_that_hits_the_limit_is_split_until_none_is_dropped():
    spans = shifted(fixture_spans(), now() - 3000 * SECOND)
    with fake_jaeger() as jaeger:
        # Spread the traces over the last hour, a minute apart.
        for number, trace in enumerate(sorted({s["traceId"] for s in spans})):
            mine = [s for s in spans if s["traceId"] == trace]
            jaeger.add(shifted(mine, now() - (3000 - number * 60) * SECOND))
        source = JaegerSource(jaeger.url, "agent", HOUR, limit=3)

        found = list(source.runs())

        expected = list(JaegerSource(jaeger.url, "agent", HOUR, limit=1000).runs())
    assert len(jaeger.queries) > 3
    assert len(found) == len(expected) > 3
    assert shown(found) == shown(expected)
    assert source.stats().traces == len(jaeger.traces)


def test_traces_before_the_window_are_left_out_of_jaeger_and_files(tmp_path):
    old = now() - 2 * 3600 * SECOND
    path = write_trace(tmp_path / "s.jsonl", {"text": "old"}, trace="0" * 32, base=old)
    write_trace(path, {"text": "new"}, trace="1" * 32, base=now() - 60 * SECOND)
    with fake_jaeger() as jaeger:
        jaeger.add(hexed(path))
        from_jaeger = list(JaegerSource(jaeger.url, "agent", HOUR).runs())

    from_file = list(FileSource(path, since=HOUR).runs())
    assert [run.answer for run in from_jaeger] == ["new"]
    assert [run.answer for run in from_file] == ["new"]
    assert len(list(FileSource(path).runs())) == 2


@pytest.mark.parametrize(
    ("status", "body", "message"),
    [
        (500, b'{"error":{"httpCode":500,"message":"storage down"}}', "storage down"),
        (200, b"<!doctype html><html></html>", "base path"),
        (200, b'{"error":{"httpCode":500,"message":"cut"}}', "failed the search"),
        (
            200,
            b'{"result":{"resourceSpans":[{"scopeSpans":[{"spans":[{"traceId":"!"}]}]}]}}',
            "neither hex",
        ),
    ],
    ids=["http-error", "html", "error-chunk", "bad-id"],
)
def test_a_bad_answer_from_jaeger_is_an_os_error_that_says_why(status, body, message):
    with fake_jaeger() as jaeger:
        jaeger.reply = (status, body)
        source = JaegerSource(jaeger.url, "agent", HOUR)

        with pytest.raises(OSError, match=message):
            list(source.runs())


def test_an_unreachable_jaeger_is_an_os_error():
    with fake_jaeger() as jaeger:
        url = jaeger.url
    with pytest.raises(OSError, match="cannot reach Jaeger"):
        list(JaegerSource(url, "agent", HOUR, timeout=2).runs())


@pytest.mark.parametrize(
    ("text", "seconds"), [("30m", 1800), ("1h", 3600), ("2d", 172800), ("45s", 45)]
)
def test_since_reads_a_number_and_a_unit(text, seconds):
    assert parse_since(text) == timedelta(seconds=seconds)


@pytest.mark.parametrize("text", ["", "1", "h", "0h", "1.5h", "-1h", "1w", "1h30m"])
def test_since_rejects_anything_else(text):
    with pytest.raises(ValueError, match="bad duration"):
        parse_since(text)


def test_a_jaeger_spec_defaults_to_localhost():
    source = parse_source("jaeger", service="agent", since=HOUR)

    assert isinstance(source, JaegerSource)
    assert source.base_url == "http://localhost:16686"
    assert parse_source("jaeger:http://j:1/x/", service="a", since=HOUR).name == (
        "jaeger:http://j:1/x"
    )


@pytest.mark.parametrize(
    ("spec", "service", "since", "message"),
    [
        ("jaeger", None, HOUR, "needs --service"),
        ("jaeger", "agent", None, "needs a window"),
        ("jaeger:file:///etc/passwd", "agent", HOUR, "must start with http"),
    ],
)
def test_a_jaeger_spec_needs_a_service_a_window_and_an_http_url(
    spec, service, since, message
):
    with pytest.raises(ValueError, match=message):
        parse_source(spec, service=service, since=since)


def test_find_reads_jaeger_and_says_how_many_traces_it_fetched(tmp_path, capsys):
    spans = shifted(fixture_spans(), now() - 600 * SECOND)
    path = write_spans(tmp_path / "spans.jsonl", spans)
    with fake_jaeger() as jaeger:
        jaeger.add(spans)
        spec = f"jaeger:{jaeger.url}"
        args = ["find", "--source", spec, "--service", "agent", "--since", "1h"]

        assert cli.main([*args, "--json"]) == 0
        captured = capsys.readouterr()

    assert f"from {len(jaeger.traces)} fetched trace(s)" in captured.err
    assert cli.main(["find", "--json", "--since", "1h", str(path)]) == 0
    from_file = json.loads(capsys.readouterr().out)
    from_jaeger = json.loads(captured.out)
    assert from_jaeger["summary"] == from_file["summary"]
    by_trace = sorted(from_file["runs"], key=lambda run: run["trace_id"])
    assert sorted(from_jaeger["runs"], key=lambda run: run["trace_id"]) == by_trace


def test_find_says_what_a_jaeger_source_is_missing(capsys):
    assert cli.main(["find", "--source", "jaeger", "--since", "1h"]) == 2
    assert "needs --service" in capsys.readouterr().err
    assert cli.main(["find", "--source", "file:x", "--since", "1x"]) == 2
    assert "bad duration" in capsys.readouterr().err


def test_find_with_since_and_no_run_in_the_window_says_so(tmp_path, capsys):
    path = write_trace(tmp_path / "s.jsonl", {"text": "old"})

    assert cli.main(["find", "--since", "1h", str(path)]) == 2

    assert "no runs that started in the last 1h" in capsys.readouterr().err
