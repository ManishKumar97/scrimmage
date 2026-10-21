import asyncio
import json
from typing import cast

import pytest
from fake_model import FakeClient, text_reply
from fake_tools import NOW, FakeCluster, fake_cluster
from mcp import Client
from openai import AsyncOpenAI

from faultline import ida, kube_tools, mcp_client, mcp_metrics, metrics_tools
from faultline.models import OpenAIModel

CALLS = "traces_span_metrics_calls_total"
BUCKET = "traces_span_metrics_duration_milliseconds_bucket"
SERVER = "SPAN_KIND_SERVER"
ERROR = "STATUS_CODE_ERROR"


def labels(service: str, kind: str = SERVER, status: str = "STATUS_CODE_UNSET", **more):
    return {
        "service_name": service,
        "span_kind": kind,
        "status_code": status,
        "span_name": "op",
        **more,
    }


def merged(a: list[tuple[int, float]], b: list[tuple[int, float]]):
    """The sum of two piecewise rates, each (seconds ago it starts, per second)."""
    starts = sorted({start for start, _ in a + b}, reverse=True)

    def at(rates, start):
        return next((r for s, r in reversed(rates) if s >= start), 0.0)

    return [(start, at(a, start) + at(b, start)) for start in starts]


def add_service(
    cluster: FakeCluster,
    service: str,
    ok: float | list[tuple[int, float]],
    errors: list[tuple[int, float]] | None = None,
    fast: list[tuple[int, float]] | None = None,
    span_name: str = "op",
    ended: int = 0,
) -> None:
    """Server-span metrics, as the spanmetrics connector writes them.

    Failed and ok spans are separate series by status_code. Every span, ok or not,
    lands in the duration buckets. `fast` is the rate of spans up to 100 ms
    (default: half of them).
    """
    prom = cluster.prom
    ok_rates: list[tuple[int, float]] = ok if isinstance(ok, list) else [(3600, ok)]
    prom.counter(CALLS, labels(service, span_name=span_name), ok_rates, ended=ended)
    if errors is not None:
        prom.counter(
            CALLS,
            labels(service, status=ERROR, span_name=span_name),
            errors,
            ended=ended,
        )
    total = merged(ok_rates, errors or [])
    half = [(start, rate / 2) for start, rate in total]
    for le, rates in (("100", fast or half), ("1000", total), ("+Inf", total)):
        prom.counter(BUCKET, labels(service, le=le), rates, ended=ended)


def cart_incident(cluster: FakeCluster) -> None:
    """Cart fails 0.8 of its 2 calls per second for the last 5 minutes only."""
    add_service(
        cluster, "cart", [(3600, 2.0), (300, 1.2)], errors=[(3600, 0.0), (300, 0.8)]
    )
    # Other services and a client span that must not leak into cart's numbers.
    add_service(cluster, "checkout", 4.0, errors=[(3600, 3.0)])
    cluster.prom.counter(CALLS, labels("cart", kind="SPAN_KIND_CLIENT"), [(3600, 50.0)])


def text_of(result) -> str:
    return "\n".join(b.text for b in result.content if b.type == "text")


def run(call):
    return asyncio.run(call)


def test_metrics_show_the_current_window_against_its_baseline(monkeypatch):
    cluster = fake_cluster(monkeypatch)
    cart_incident(cluster)

    output = run(metrics_tools.get_service_metrics("cart", 5))

    assert output.splitlines()[1:] == [
        "WINDOW CALLS/S ERRORS/S ERROR% P50 P95",
        "last 5m 2.00 0.80 40.0% 100ms 910ms",
        "baseline, the 30m before that 2.00 0.00 0.0% 100ms 910ms",
        "Failing operations in the last 5m: op: 0.80/s",
    ]
    assert output.startswith("Now: 2026-10-21T12:00:00Z. cart")


def test_the_service_only_sees_its_own_server_spans(monkeypatch):
    cluster = fake_cluster(monkeypatch)
    cart_incident(cluster)

    checkout = run(metrics_tools.get_service_metrics("checkout", 5))

    # checkout fails 3 of 7 at all times: not a change from its baseline
    assert "last 5m 7.00 3.00 42.9%" in checkout
    assert "baseline, the 30m before that 7.00 3.00 42.9%" in checkout


def test_the_queries_use_the_right_window_and_moment(monkeypatch):
    cluster = fake_cluster(monkeypatch)
    cart_incident(cluster)

    run(metrics_tools.get_service_metrics("cart", 10))

    asked = [q for path, q in cluster.prom.requests if path == "query"]
    now = int(NOW.timestamp())
    assert {q["time"] for q in asked} == {str(now), str(now - 600)}
    current = [q["query"] for q in asked if q["time"] == str(now)]
    baseline = [q["query"] for q in asked if q["time"] == str(now - 600)]
    assert all("[10m]" in q and 'service_name="cart"' in q for q in current)
    assert all("[30m]" in q and 'service_name="cart"' in q for q in baseline)
    assert all('span_kind="SPAN_KIND_SERVER"' in q for q in current + baseline)


def test_a_service_with_calls_but_no_error_series_has_zero_errors(monkeypatch):
    cluster = fake_cluster(monkeypatch)
    add_service(cluster, "payment", 1.0)  # no error series at all

    output = run(metrics_tools.get_service_metrics("payment", 5))

    assert "last 5m 1.00 0.00 0.0% 100ms 910ms" in output
    assert "Failing operations" not in output


def test_a_slower_window_shows_in_the_latency(monkeypatch):
    cluster = fake_cluster(monkeypatch)
    add_service(cluster, "frontend", 2.0, fast=[(3600, 1.9), (300, 0.2)])

    output = run(metrics_tools.get_service_metrics("frontend", 5))

    assert "last 5m 2.00 0.00 0.0% 500ms 950ms" in output
    assert "baseline, the 30m before that 2.00 0.00 0.0% 53ms 100ms" in output


def test_a_baseline_without_data_is_not_called_clean(monkeypatch):
    cluster = fake_cluster(monkeypatch)
    cluster.prom.counter(CALLS, labels("cart"), [(600, 1.0)], points=20)  # 5 minutes

    output = run(metrics_tools.get_service_metrics("cart", 5))

    assert "last 5m 1.00 0.00 0.0% no data no data" in output
    assert "baseline, the 30m before that no data" in output


def test_a_typo_in_the_service_name_lists_the_services_with_metrics(monkeypatch):
    cluster = fake_cluster(monkeypatch)
    cart_incident(cluster)

    output = run(metrics_tools.get_service_metrics("crat", 5))

    assert output.endswith(
        "no data: Prometheus has no span metrics for service 'crat'. "
        "Services with span metrics: cart, checkout"
    )


def test_an_idle_service_is_no_data_not_no_errors(monkeypatch):
    cluster = fake_cluster(monkeypatch)
    add_service(cluster, "cart", 2.0, errors=[(3600, 1.0)], ended=1800)

    output = run(metrics_tools.get_service_metrics("cart", 5))

    assert "handled no requests in the last 5 minutes" in output
    assert "not proof of no errors" in output
    assert "ERROR%" not in output


def test_the_output_stays_small_with_many_failing_operations(monkeypatch):
    cluster = fake_cluster(monkeypatch)
    add_service(cluster, "cart", 19.9, errors=[(3600, 0.1)], span_name="op0")
    for i in range(1, 12):
        cluster.prom.counter(
            CALLS,
            labels("cart", status=ERROR, span_name=f"op{i}"),
            [(3600, i / 10)],
        )

    output = run(metrics_tools.get_service_metrics("cart", 5))

    failing = output.splitlines()[-1]
    assert failing == (
        "Failing operations in the last 5m: op11: 1.10/s; op10: 1.00/s; op9: 0.90/s"
    )
    assert len(output) < 600


def test_a_bad_service_name_never_reaches_prometheus(monkeypatch):
    cluster = fake_cluster(monkeypatch)

    for name in ['cart"} or vector(1) #', "", "a b"]:
        with pytest.raises(RuntimeError, match="bad service name"):
            run(metrics_tools.get_service_metrics(name, 5))
        with pytest.raises(RuntimeError, match="bad service name"):
            run(metrics_tools.get_error_rate_trend(name, 30))

    assert cluster.prom.requests == []


def test_prometheus_explaining_a_bad_query_reaches_the_model(monkeypatch):
    cluster = fake_cluster(monkeypatch)
    cluster.prom.status = 400
    cluster.prom.body = json.dumps(
        {"status": "error", "errorType": "bad_data", "error": "1:5: parse error"}
    )

    with pytest.raises(RuntimeError) as error:
        run(metrics_tools.get_service_metrics("cart", 5))

    assert str(error.value) == "Prometheus rejected the query: 1:5: parse error"


def test_a_prometheus_that_is_down_is_a_clear_tool_error(monkeypatch):
    cluster = fake_cluster(monkeypatch)
    cluster.prom.status = 503

    output = run(
        metrics_tools_registry().call("get_service_metrics", '{"service": "cart"}')
    )

    message = (
        "Error: get_service_metrics raised RuntimeError: "
        "Prometheus failed: 503 Service Unavailable"
    )
    assert output == (message, False)


def test_an_answer_that_is_not_a_prometheus_answer_is_a_clear_tool_error(monkeypatch):
    cluster = fake_cluster(monkeypatch)
    cluster.prom.body = "<html>login</html>"

    with pytest.raises(RuntimeError, match="something unexpected"):
        run(metrics_tools.get_service_metrics("cart", 5))


def test_a_hanging_prometheus_is_a_tool_error(monkeypatch):
    cluster = fake_cluster(monkeypatch)
    cluster.hang = True
    monkeypatch.setattr(kube_tools, "API_TIMEOUT", 0.1)

    with pytest.raises(RuntimeError, match="Prometheus timed out after 0.1s"):
        run(metrics_tools.get_service_metrics("cart", 5))


@pytest.mark.parametrize("minutes", [0, 2, 31])
def test_minutes_outside_the_range_is_a_tool_error(monkeypatch, minutes):
    cluster = fake_cluster(monkeypatch)

    output, ok = run(
        metrics_tools_registry().call(
            "get_service_metrics", json.dumps({"service": "cart", "minutes": minutes})
        )
    )

    assert not ok
    assert "minutes" in output
    assert cluster.prom.requests == []


def metrics_tools_registry():
    """The two functions in a registry, as the model calls them."""
    from faultline.tools import Registry

    registry = Registry()
    registry.tool(metrics_tools.get_service_metrics)
    registry.tool(metrics_tools.get_error_rate_trend)
    return registry


def test_the_trend_shows_when_the_error_rate_rose(monkeypatch):
    cluster = fake_cluster(monkeypatch)
    cart_incident(cluster)

    output = run(metrics_tools.get_error_rate_trend("cart", 30))

    assert output.splitlines()[1] == (
        "11:30 0.0%, 11:32 0.0%, 11:34 0.0%, 11:36 0.0%, 11:38 0.0%, 11:40 0.0%, "
        "11:42 0.0%, 11:44 0.0%, 11:46 0.0%, 11:48 0.0%, 11:50 0.0%, 11:52 0.0%, "
        "11:54 0.0%, 11:56 13.3%, 11:58 40.0%, 12:00 40.0%"
    )
    assert "one value per 2 min" in output


def test_the_trend_says_n_a_before_the_data_starts(monkeypatch):
    cluster = fake_cluster(monkeypatch)
    cluster.prom.counter(CALLS, labels("cart"), [(600, 1.0)], points=40)  # 10 minutes

    cells = run(metrics_tools.get_error_rate_trend("cart", 30)).splitlines()[1]

    assert cells.startswith("11:30 n/a, 11:32 n/a")
    assert cells.endswith("11:58 0.0%, 12:00 0.0%")


def test_a_trend_with_no_series_is_no_data(monkeypatch):
    fake_cluster(monkeypatch)

    output = run(metrics_tools.get_error_rate_trend("cart", 30))

    assert "no data: Prometheus has no span metrics for service 'cart'" in output


def test_the_trend_asks_for_the_range_it_shows(monkeypatch):
    cluster = fake_cluster(monkeypatch)
    cart_incident(cluster)

    run(metrics_tools.get_error_rate_trend("cart", 30))

    ranges = [q for path, q in cluster.prom.requests if path == "query_range"]
    now = int(NOW.timestamp())
    assert len(ranges) == 2
    for q in ranges:
        assert (q["start"], q["end"], q["step"]) == (
            str(now - 1800),
            str(now),
            "120",
        )
        assert 'service_name="cart"' in q["query"]


def test_the_mcp_server_serves_both_tools_and_passes_errors_on(monkeypatch):
    cluster = fake_cluster(monkeypatch)
    cart_incident(cluster)

    async def scenario():
        async with Client(mcp_metrics.server) as client:
            names = [t.name for t in (await client.list_tools()).tools]
            ok = await client.call_tool("get_service_metrics", {"service": "cart"})
            bad = await client.call_tool("get_service_metrics", {"service": "a b"})
            trend = await client.call_tool(
                "get_error_rate_trend", {"service": "cart", "minutes": 10}
            )
            return names, ok, bad, trend

    names, ok, bad, trend = run(scenario())

    assert names == ["get_service_metrics", "get_error_rate_trend"]
    assert not ok.is_error and "40.0%" in text_of(ok)
    assert bad.is_error
    assert "bad service name 'a b'" in text_of(bad)
    assert not trend.is_error


def test_the_real_stdio_server_joins_the_registry_beside_the_logs_server():
    async def scenario():
        async with (
            mcp_client.mcp_tools(
                mcp_client.LOGS_SERVER, kube_tools.registry
            ) as with_logs,
            mcp_client.mcp_tools(mcp_client.METRICS_SERVER, with_logs) as tools,
        ):
            bad = await tools.call("get_service_metrics", '{"service": "a b"}')
            return list(tools.tools), bad

    names, bad = run(scenario())

    assert names == [
        "list_pods",
        "list_services",
        "get_error_traces",
        "get_logs",
        "get_service_metrics",
        "get_error_rate_trend",
    ]
    assert bad[1] is False
    assert "bad service name 'a b'" in bad[0]


def test_a_diagnosis_run_offers_the_model_the_metrics_tools(monkeypatch):
    client = FakeClient(text_reply("No fault found."))
    model = OpenAIModel(cast(AsyncOpenAI, client), "fake")
    monkeypatch.setattr(ida, "make_model", lambda _settings: model)
    monkeypatch.setattr(ida, "get_settings", lambda: None)

    result = run(ida.diagnose_async("why?"))

    offered = [t["function"]["name"] for t in client.requests[0]["tools"]]
    assert result.stop_reason == "done"
    assert "get_service_metrics" in offered and "get_error_rate_trend" in offered
    assert "get_logs" in offered
