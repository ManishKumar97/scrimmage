import dataclasses
import json
from pathlib import Path

from spans import write_trace

from faultline import cli
from faultline.config import Config
from faultline.find import find, show_report
from faultline.runs import read_runs

LOGS = {"name": "get_logs", "arguments": '{"service": "cart"}'}
FAILED = {**LOGS, "error": "ConnectionError"}
EMPTY = {"name": "list_pods", "arguments": "{}", "result": "[]"}


def write_runs(path: Path) -> Path:
    """Three runs: a hidden tool error, a retried empty result, and a clean run that
    calls one tool too often and names a service no tool returned."""
    write_trace(path, {"calls": [FAILED]}, {"text": "cart is the cause"}, trace="a1")
    write_trace(
        path,
        {"calls": [EMPTY]},
        {"calls": [{**EMPTY, "result": '["cart-1"]'}]},
        {"text": "cart-1 may be down"},
        trace="b2",
    )
    write_trace(
        path,
        {"calls": [LOGS] * 4},
        {"text": "the cause is `payment-gateway`"},
        trace="c3",
    )
    return path


def report_lines(tmp_path: Path, capsys) -> list[str]:
    path = write_runs(tmp_path / "spans.jsonl")

    assert cli.main(["find", str(path)]) == 0

    return capsys.readouterr().out.splitlines()


def test_find_prints_each_fault_kind_and_how_the_agent_reacted(tmp_path, capsys):
    lines = report_lines(tmp_path, capsys)

    assert lines[1:] == [
        "Faultline find: 3 runs, 2 with faults, 2 faults, 4 flagged",
        "",
        "tool_error  1 fault in 1 run",
        "  no_retry           flagged 1 of 1          certain",
        "  not_disclosed      flagged 1 of 1 runs     heuristic: likely, review these runs",
        "    a1  no_retry, not_disclosed",
        "",
        "empty_result  1 fault in 1 run",
        "  no_retry           flagged 0 of 1          certain",
        "  not_disclosed      flagged 0 of 1 runs     heuristic",
        "",
        "Across all runs",
        "  unsupported_value  flagged 1 run           heuristic: likely, review these runs",
        "    c3  answer names payment-gateway, found in no tool result",
        "  repeated_call      flagged 1 run           certain",
        '    c3  get_logs({"service": "cart"}) called 4 times, limit 3',
    ]


def test_the_report_never_says_wrong(tmp_path, capsys):
    text = "\n".join(report_lines(tmp_path, capsys)).lower()

    assert "wrong" not in text


def test_every_flagged_finding_is_in_the_report_with_its_run_id(tmp_path, capsys):
    lines = report_lines(tmp_path, capsys)
    report = find(read_runs(tmp_path / "spans.jsonl").runs, Config())

    for result in report.runs:
        for finding in result.findings:
            if finding.flagged:
                assert any(
                    line.startswith(f"    {result.run.trace_id}  ")
                    and (finding.check in line or finding.evidence in line)
                    for line in lines
                )


def test_json_prints_the_report_and_nothing_else_on_stdout(tmp_path, capsys):
    path = write_runs(tmp_path / "spans.jsonl")

    assert cli.main(["find", "--json", "--runs", str(path)]) == 0

    captured = capsys.readouterr()
    printed = json.loads(captured.out)
    report = find(read_runs(path).runs, Config())
    assert printed == report.to_json()
    assert "read 3 run(s) from 1 file(s)" in captured.err


def test_to_json_holds_only_plain_values(tmp_path):
    path = write_runs(tmp_path / "spans.jsonl")

    data = find(read_runs(path).runs, Config()).to_json()

    assert json.loads(json.dumps(data)) == data
    [a1, b2, c3] = data["runs"]
    assert a1["trace_id"] == "a1"
    assert a1["faults"][0]["kind"] == "tool_error"
    assert type(a1["faults"][0]["kind"]) is str  # not the enum
    assert type(a1["faults"][0]["origin"]) is str
    first = a1["findings"][0]
    assert set(first) == {
        "trace_id",
        "check",
        "scope",
        "certainty",
        "flagged",
        "evidence",
        "fault",
    }
    assert (first["trace_id"], first["scope"]) == ("a1", "fault")
    assert type(first["certainty"]) is str
    assert first["fault"] == a1["faults"][0]
    [unsupported, repeated] = c3["findings"]
    assert unsupported["fault"] is None and repeated["check"] == "repeated_call"
    assert (repeated["trace_id"], repeated["scope"]) == ("c3", "run")
    assert [f["check"] for f in b2["findings"]] == [
        "no_retry",
        "unsupported_value",
        "not_disclosed",
    ]


def test_to_json_has_a_version_and_a_summary_with_every_key(tmp_path):
    path = write_runs(tmp_path / "spans.jsonl")

    data = find(read_runs(path).runs, Config()).to_json()

    assert data["version"] == 1
    assert data["summary"] == {
        "runs": 3,
        "runs_with_faults": 2,
        "faults": {
            "tool_error": 1,
            "tool_timeout": 0,
            "empty_result": 1,
            "schema_mismatch": 0,
            "model_error": 0,
            "model_rate_limit": 0,
            "model_token_limit": 0,
        },
        "flagged": {
            "no_retry": 1,
            "unsupported_value": 1,
            "repeated_call": 1,
            "not_disclosed": 1,
        },
    }


def test_a_fault_or_run_a_check_could_not_judge_is_counted(tmp_path):
    # A tool error with no answer: no_retry judges it, not_disclosed cannot.
    path = write_trace(tmp_path / "spans.jsonl", {"calls": [FAILED]}, trace="d4")
    report = find(read_runs(path).runs, Config())
    [result] = report.runs
    # And a fault no_retry cannot place: its call is not in the run.
    lost = dataclasses.replace(result.faults[0], call_id="gone")
    report = dataclasses.replace(
        report, runs=(dataclasses.replace(result, faults=(*result.faults, lost)),)
    )

    lines = show_report(report).splitlines()

    assert "  no_retry           flagged 1 of 1" in lines[3]
    assert lines[3].endswith("certain; not judged: 1")
    assert lines[4].endswith("heuristic; not judged: 1")
