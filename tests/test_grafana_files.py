"""The Grafana files in deploy/grafana/ ask faultline serve for what it answers.

The dashboard names JSON fields of the report; renaming one in the report would empty a
column without an error. These tests tie the two together.
"""

import json
from pathlib import Path

import yaml

from faultline.config import Config
from faultline.find import find
from faultline.runs import read_runs
from faultline.sources import parse_since

GRAFANA = Path(__file__).parents[1] / "deploy/grafana"
FIXTURE = (
    Path(__file__).parents[1] / "packages/faultline/tests/fixtures/tool_error.jsonl"
)


def targets() -> list[dict]:
    dashboard = json.loads((GRAFANA / "faultline-dashboard.json").read_text())
    return [target for panel in dashboard["panels"] for target in panel["targets"]]


def test_every_panel_queries_the_provisioned_datasource_at_the_findings_route():
    provisioned = yaml.safe_load((GRAFANA / "datasource.yaml").read_text())
    [datasource] = provisioned["datasources"]

    assert datasource["type"] == "yesoreyeram-infinity-datasource"
    for target in targets():
        assert target["datasource"] == {"type": datasource["type"], "uid": "faultline"}
        assert target["url"] == "/api/findings"
        assert target["parser"] == "backend"  # so /api/ds/query parses it too
    assert datasource["uid"] == "faultline"


def test_every_window_grafana_can_send_is_one_serve_reads():
    dashboard = json.loads((GRAFANA / "faultline-dashboard.json").read_text())
    [variable] = dashboard["templating"]["list"]
    choices = variable["query"].split(",")

    for target in targets():
        # Grafana's frontend puts the chosen window in place of the variable.
        assert target["url_options"]["params"] == [{"key": "since", "value": "$since"}]
    assert variable["name"] == "since"
    assert [option["value"] for option in variable["options"]] == choices
    assert variable["current"]["value"] in choices
    for choice in choices:
        parse_since(choice)


def test_the_table_columns_are_fields_of_a_finding_in_the_report():
    report = find(read_runs(FIXTURE).runs, Config()).to_json()
    [table] = [t for t in targets() if t["root_selector"] == "runs.findings[]"]
    finding = next(f for run in report["runs"] for f in run["findings"] if f["fault"])

    for column in table["columns"]:
        value = finding
        for key in column["selector"].split("."):
            value = value[key]
        assert value is not None, column["selector"]


def test_the_count_panel_reads_every_fault_kind_in_the_summary():
    report = find(read_runs(FIXTURE).runs, Config()).to_json()
    [counts] = [t for t in targets() if "summary.faults" in t["root_selector"]]

    assert report["summary"]["faults"]["tool_error"] == 1
    assert [c["selector"] for c in counts["columns"]] == ["kind", "faults"]
