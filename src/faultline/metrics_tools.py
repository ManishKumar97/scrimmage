"""Tools that read the test bed's span metrics from the real Prometheus HTTP API.

The OpenTelemetry Collector's spanmetrics connector turns every span into two
metrics, and Prometheus stores them (chart: opentelemetry-collector.config
connectors.spanmetrics, pipeline metrics, exporter otlphttp/prometheus):
  traces_span_metrics_calls_total                  counter of ended spans
  traces_span_metrics_duration_milliseconds_bucket histogram of span durations (ms)
Labels: service_name, span_name, span_kind (SPAN_KIND_SERVER for a request a
service handled) and status_code (STATUS_CODE_ERROR for a failed span).
"""

import asyncio
import json
import math
import re
from datetime import datetime
from typing import Annotated

from kubernetes_asyncio.client import ApiClient, ApiException
from pydantic import Field

from faultline import kube_tools
from faultline.kube_tools import NAMESPACE, api_errors, format_duration, kube_api

# Prometheus's HTTP API, reached through the Kubernetes API server's service proxy.
PROMETHEUS_API = f"/api/v1/namespaces/{NAMESPACE}/services/prometheus:9090/proxy/api/v1"
CALLS = "traces_span_metrics_calls_total"
DURATION = "traces_span_metrics_duration_milliseconds_bucket"
SERVER = 'span_kind="SPAN_KIND_SERVER"'
ERROR = 'status_code="STATUS_CODE_ERROR"'
MIN_WINDOW = 3  # minutes. The collector flushes span metrics about once a minute.
MAX_WINDOW = 30
BASELINE_MINUTES = 30  # the window before the current one
TREND_RATE_WINDOW = "3m"
MAX_TREND_POINTS = 15
MAX_TOP_OPERATIONS = 3
SERVICE_NAME = re.compile(r"[A-Za-z0-9_.-]{1,63}")


def check_service(service: str) -> str:
    """The name goes into a PromQL label match, so only plain names pass."""
    if not SERVICE_NAME.fullmatch(service):
        raise RuntimeError(
            f"bad service name {service!r}: use letters, digits, '.', '_' or '-'"
        )
    return service


async def prom_get(api: ApiClient, path: str, params: list[tuple[str, str]]) -> dict:
    """GET one Prometheus API path. Return its `data`, or raise a readable error."""
    response = await api.call_api(
        f"{PROMETHEUS_API}/{path}",
        "GET",
        query_params=params,
        header_params={"Accept": "application/json"},
        auth_settings=["BearerToken"],
        _preload_content=False,
    )
    text = await response.text()
    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        body = None
    if response.status >= 400:
        # Prometheus explains a bad query in the body, for example a parse error.
        if isinstance(body, dict) and body.get("error"):
            raise RuntimeError(
                f"Prometheus rejected the query: {str(body['error'])[:200]}"
            )
        raise ApiException(status=response.status, reason=response.reason)
    if not isinstance(body, dict) or body.get("status") != "success":
        raise RuntimeError("Prometheus answered with something unexpected")
    return body["data"]


def number(raw: str) -> float | None:
    """A sample value. Prometheus writes NaN for 'no answer', such as a quantile of nothing."""
    value = float(raw)
    return None if math.isnan(value) else value


async def scalar(api: ApiClient, query: str, at: float) -> float | None:
    """Value of a query that returns one number, or None when it has no series."""
    data = await prom_get(api, "query", [("query", query), ("time", f"{at:.0f}")])
    result = data["result"]
    return number(result[0]["value"][1]) if result else None


def selector(service: str, *extra: str) -> str:
    return ",".join([f'service_name="{service}"', SERVER, *extra])


def rate(metric: str, labels: str, window: str) -> str:
    return f"rate({metric}{{{labels}}}[{window}])"


def per_second(value: float | None) -> str:
    return "no data" if value is None else f"{value:.2f}"


def percent(errors: float | None, total: float | None) -> str:
    if total is None:
        return "no data"
    if not total:
        return "n/a"
    return f"{100 * (errors or 0) / total:.1f}%"


def latency(value_ms: float | None) -> str:
    return "no data" if value_ms is None else format_duration(int(value_ms * 1000))


async def window_stats(
    api: ApiClient, service: str, window: str, at: float
) -> tuple[float | None, float | None, float | None, float | None]:
    """Calls per second, errors per second, p50 and p95 in ms. None means no series."""
    sel, err = selector(service), selector(service, ERROR)
    buckets = f"sum by (le) ({rate(DURATION, sel, window)})"
    total, errors, p50, p95 = await asyncio.gather(
        scalar(api, f"sum({rate(CALLS, sel, window)})", at),
        scalar(api, f"sum({rate(CALLS, err, window)})", at),
        scalar(api, f"histogram_quantile(0.5, {buckets})", at),
        scalar(api, f"histogram_quantile(0.95, {buckets})", at),
    )
    # A counter only gets an error series after the first error. With calls present,
    # a missing error series means no errors were counted in this window.
    if total is not None and errors is None:
        errors = 0.0
    return total, errors, p50, p95


def row(stats: tuple[float | None, float | None, float | None, float | None]) -> str:
    total, errors, p50, p95 = stats
    if total is None:
        return "no data"
    return (
        f"{per_second(total)} {per_second(errors)} {percent(errors, total)} "
        f"{latency(p50)} {latency(p95)}"
    )


async def top_errors(api: ApiClient, service: str, window: str, at: float) -> list[str]:
    query = (
        f"topk({MAX_TOP_OPERATIONS}, sum by (span_name) "
        f"({rate(CALLS, selector(service, ERROR), window)}))"
    )
    data = await prom_get(api, "query", [("query", query), ("time", f"{at:.0f}")])
    rows = [
        (r["metric"].get("span_name", "?"), number(r["value"][1]))
        for r in data["result"]
    ]
    ranked = sorted((r for r in rows if r[1]), key=lambda r: -(r[1] or 0))
    return [f"{name}: {value:.2f}/s" for name, value in ranked]


async def no_data(api: ApiClient, service: str, minutes: int) -> str:
    """Why a service has no metrics: say which services do, so a typo shows."""
    known = sorted(await prom_get(api, "label/service_name/values", []))
    if service not in known:
        return (
            f"no data: Prometheus has no span metrics for service {service!r}. "
            "Services with span metrics: " + ", ".join(known)
        )
    return (
        f"no data: {service} has span metrics, but it handled no requests in the "
        f"last {minutes} minutes. It may be idle or down. This is not proof of no errors."
    )


async def get_service_metrics(
    service: Annotated[
        str, Field(description="Service name, for example cart, checkout or frontend.")
    ],
    minutes: Annotated[
        int,
        Field(
            ge=MIN_WINDOW,
            le=MAX_WINDOW,
            description="Length of the current window. The baseline is the 30 minutes before it.",
        ),
    ] = 5,
) -> str:
    """Request rate, error rate and latency of one service, now against its baseline.

    Counts the requests the service handled (server spans), from span metrics in
    Prometheus. Two rows: the last `minutes`, and the 30 minutes before that.
    Compare them: an error rate far above its baseline is a change, one equal to
    its baseline is how the service always is. Spans count when they end, so a
    call that fails after minutes shows up late. "no data" means Prometheus has
    no series, not that there were no errors. p50 and p95 come from histogram
    buckets, so they are estimates.
    """
    check_service(service)
    now = kube_tools.utc_now()
    at = now.timestamp()
    window = f"{minutes}m"
    async with kube_api() as api, api_errors("Prometheus"):
        current, baseline, top = await asyncio.gather(
            window_stats(api, service, window, at),
            window_stats(api, service, f"{BASELINE_MINUTES}m", at - minutes * 60),
            top_errors(api, service, window, at),
        )
        if current[0] is None:
            return f"Now: {stamp(now)}. " + await no_data(api, service, minutes)
    lines = [
        f"Now: {stamp(now)}. {service}, requests handled, from span metrics.",
        "WINDOW CALLS/S ERRORS/S ERROR% P50 P95",
        f"last {minutes}m {row(current)}",
        f"baseline, the {BASELINE_MINUTES}m before that {row(baseline)}",
    ]
    if top:
        lines.append(f"Failing operations in the last {minutes}m: " + "; ".join(top))
    return "\n".join(lines)


async def range_values(
    api: ApiClient, query: str, start: float, end: float, step: int
) -> dict[int, float]:
    """Samples of a one-series range query, by timestamp. Missing steps are absent."""
    data = await prom_get(
        api,
        "query_range",
        [
            ("query", query),
            ("start", f"{start:.0f}"),
            ("end", f"{end:.0f}"),
            ("step", str(step)),
        ],
    )
    series = data["result"]
    if not series:
        return {}
    points = ((int(t), number(v)) for t, v in series[0]["values"])
    return {t: v for t, v in points if v is not None}


async def get_error_rate_trend(
    service: Annotated[
        str, Field(description="Service name, for example cart, checkout or frontend.")
    ],
    minutes: Annotated[
        int,
        Field(ge=10, le=60, description="How far back to look."),
    ] = 30,
) -> str:
    """Error rate of one service over time, to see when it changed.

    One value per step (up to 15), oldest first, for the requests the service
    handled. "n/a" means Prometheus has no requests for that step. This does not
    mean there were no errors.
    """
    check_service(service)
    now = kube_tools.utc_now()
    end = int(now.timestamp())
    step = 60 * math.ceil(minutes / MAX_TREND_POINTS)
    start = end - minutes * 60
    async with kube_api() as api, api_errors("Prometheus"):
        total, errors = await asyncio.gather(
            range_values(
                api,
                f"sum({rate(CALLS, selector(service), TREND_RATE_WINDOW)})",
                start,
                end,
                step,
            ),
            range_values(
                api,
                f"sum({rate(CALLS, selector(service, ERROR), TREND_RATE_WINDOW)})",
                start,
                end,
                step,
            ),
        )
        if not total:
            return f"Now: {stamp(now)}. " + await no_data(api, service, minutes)
    cells = []
    for t in range(start, end + 1, step):
        clock = datetime.fromtimestamp(t, now.tzinfo).strftime("%H:%M")
        # Requests present and no error series: zero errors. No requests: no answer.
        cells.append(
            f"{clock} "
            + ("n/a" if t not in total else percent(errors.get(t, 0.0), total[t]))
        )
    return (
        f"Now: {stamp(now)}. {service} error rate of requests handled, one value per "
        f"{step // 60} min (rate over {TREND_RATE_WINDOW}), oldest first:\n"
        + ", ".join(cells)
    )


def stamp(now: datetime) -> str:
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")
