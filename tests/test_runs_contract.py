"""The contract between the lab and the product: what the lab's tracer writes,
`faultline.runs` reads as the committed fixtures the product's tests use."""

import dataclasses
import json
from pathlib import Path

import pytest
from runs_fixtures import BUILDERS, FIXTURES, SECOND, Writer

from faultline.runs import Reading, read_runs


def without_trace_ids(reading: Reading) -> Reading:
    """Trace ids are random per write, so they are replaced by the run's position."""
    runs = [
        dataclasses.replace(run, trace_id=str(number))
        for number, run in enumerate(reading.runs)
    ]
    return dataclasses.replace(reading, runs=runs)


@pytest.mark.parametrize("name", sorted(BUILDERS))
def test_the_tracer_writes_what_the_committed_fixture_holds(name, tmp_path):
    fresh = tmp_path / f"{name}.jsonl"
    BUILDERS[name](fresh)

    committed = read_runs(FIXTURES / f"{name}.jsonl")

    assert without_trace_ids(read_runs(fresh)) == without_trace_ids(committed)


def test_every_committed_fixture_has_a_builder():
    assert sorted(p.stem for p in FIXTURES.glob("*.jsonl")) == sorted(BUILDERS)


def test_written_lines_are_the_otlp_shape_the_reader_expects(tmp_path):
    # Guards the fixtures: if the exporter changes shape, this fails before the others mislead.
    path: Path = tmp_path / "spans.jsonl"
    writer = Writer(path)
    root = writer.root(0, SECOND)
    writer.chat(root, 1, [])

    request = json.loads(path.read_text().splitlines()[0])
    span = request["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert len(span["traceId"]) == 32
    assert isinstance(span["startTimeUnixNano"], str)
