"""Tools that read the test bed through the real Kubernetes API."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated

from kubernetes_asyncio import config
from kubernetes_asyncio.client import ApiClient, ApiException, AppsV1Api, CoreV1Api
from pydantic import Field

from faultline.tools import Registry

NAMESPACE = "astronomy-shop"
# Jaeger's query API, reached through the Kubernetes API server's service proxy.
# The chart serves Jaeger under /jaeger/ui, so its API lives below that path.
JAEGER_API = (
    f"/api/v1/namespaces/{NAMESPACE}/services/jaeger-query:16686/proxy/jaeger/ui/api"
)
MAX_TRACES = 20  # traces fetched per call
MAX_ERROR_GROUPS = 8  # distinct errors shown per call
MAX_MESSAGE_CHARS = 200  # one error message, cut
API_TIMEOUT = 30.0  # seconds, for all the API calls of one tool call

registry = Registry()


@asynccontextmanager
async def kube_api() -> AsyncIterator[ApiClient]:
    """An API client for the test bed, authenticated from ~/.kube/config like kubectl."""
    await config.load_kube_config()
    # One client per tool call. Production keeps one shared client to reuse connections.
    async with ApiClient() as api:
        yield api


@asynccontextmanager
async def api_errors(what: str) -> AsyncIterator[None]:
    """Turn API failures into errors the model can read."""
    try:
        async with asyncio.timeout(API_TIMEOUT):
            yield
    except TimeoutError:
        raise RuntimeError(f"{what} timed out after {API_TIMEOUT:g}s") from None
    except ApiException as exc:
        if exc.status == 404:
            raise RuntimeError(f"no {what} in {NAMESPACE}") from None
        raise RuntimeError(f"{what} failed: {exc.status} {exc.reason}") from None


@registry.tool
async def get_logs(
    service: Annotated[
        str, Field(description="Service name, for example cart, checkout or frontend.")
    ],
) -> str:
    """Get the last 100 log lines from the past 10 minutes of each pod of one service."""
    async with kube_api() as api, api_errors(f"deployment named {service!r}"):
        deployment = await AppsV1Api(api).read_namespaced_deployment(service, NAMESPACE)
        labels = deployment.spec.selector.match_labels or {}
        selector = ",".join(f"{key}={value}" for key, value in labels.items())
        core = CoreV1Api(api)
        pods = await core.list_namespaced_pod(NAMESPACE, label_selector=selector)
        sections = []
        # Every pod, not one: a single healthy replica can hide the failing ones.
        for pod in pods.items:
            logs = await core.read_namespaced_pod_log(
                pod.metadata.name,
                NAMESPACE,
                container=service,
                since_seconds=600,
                tail_lines=100,
                timestamps=True,
            )
            body = logs.rstrip("\n") or "No log lines in the last 10 minutes."
            sections.append(f"--- pod {pod.metadata.name} ---\n{body}")
    # Every line starts with its time. "Now" lets the model judge how old a line is.
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not sections:
        return f"Now: {now}. The deployment has no pods."
    header = f"Now: {now}. Last 100 lines of the past 10 minutes per pod, oldest first."
    return "\n".join([header, *sections])


@registry.tool
async def list_pods(
    service: Annotated[
        str | None,
        Field(description="Service name. Leave out to list the pods of every service."),
    ] = None,
) -> str:
    """List pods with phase, ready containers, restarts, last termination reason and age."""
    async with kube_api() as api:
        selector = ""
        if service:
            async with api_errors(f"deployment named {service!r}"):
                deployment = await AppsV1Api(api).read_namespaced_deployment(
                    service, NAMESPACE
                )
            labels = deployment.spec.selector.match_labels or {}
            selector = ",".join(f"{key}={value}" for key, value in labels.items())
        async with api_errors("pod list"):
            pods = await CoreV1Api(api).list_namespaced_pod(
                NAMESPACE, label_selector=selector
            )
    now = utc_now()
    rows = [pod_row(pod, now) for pod in pods.items]
    header = f"Now: {now.strftime('%Y-%m-%dT%H:%M:%SZ')}."
    if not rows:
        return f"{header} No pods."
    columns = "POD STATE PHASE READY RESTARTS LAST_TERMINATION AGE"
    return "\n".join([header, columns, *rows])


def utc_now() -> datetime:
    return datetime.now(UTC)


def pod_state(pod) -> str:
    """What kubectl shows as STATUS: why a container waits, why it ended, or Running."""
    if pod.metadata.deletion_timestamp:
        return "Terminating"
    statuses = pod.status.container_statuses or []
    for c in statuses:
        if c.state and c.state.waiting and c.state.waiting.reason:
            return c.state.waiting.reason
    for c in statuses:
        if c.state and c.state.terminated and c.state.terminated.reason:
            return c.state.terminated.reason
    return "Running" if statuses else pod.status.phase


def pod_row(pod, now: datetime) -> str:
    statuses = pod.status.container_statuses or []
    ready = sum(1 for c in statuses if c.ready)
    restarts = sum(c.restart_count for c in statuses)
    reasons = [
        c.last_state.terminated.reason
        for c in statuses
        if c.last_state and c.last_state.terminated
    ]
    started = pod.metadata.creation_timestamp
    age = f"{int((now - started).total_seconds())}s" if started else "unknown"
    return (
        f"{pod.metadata.name} {pod_state(pod)} {pod.status.phase} {ready}/{len(statuses)} "
        f"{restarts} {','.join(reasons) or 'none'} {age}"
    )


@registry.tool
async def list_services() -> str:
    """List the services running in the Astronomy Shop, with ready replicas."""
    async with kube_api() as api, api_errors("deployment list"):
        deployments = await AppsV1Api(api).list_namespaced_deployment(NAMESPACE)
    rows = [
        f"{d.metadata.name} {d.status.ready_replicas or 0}" for d in deployments.items
    ]
    return "\n".join(["SERVICE READY", *rows])


async def jaeger_get(api: ApiClient, path: str, params: list[tuple[str, str]]) -> dict:
    """GET one Jaeger query API path through the Kubernetes service proxy."""
    response = await api.call_api(
        f"{JAEGER_API}/{path}",
        "GET",
        query_params=params,
        header_params={"Accept": "application/json"},
        auth_settings=["BearerToken"],
        _preload_content=False,
    )
    if response.status >= 400:
        raise ApiException(status=response.status, reason=response.reason)
    return json.loads(await response.text())


def tag_value(span: dict, key: str) -> str:
    return next((str(t["value"]) for t in span["tags"] if t["key"] == key), "")


def is_error(span: dict) -> bool:
    return (
        tag_value(span, "error") == "True"
        or tag_value(span, "otel.status_code") == "ERROR"
    )


def error_message(span: dict) -> str:
    """The status text of a failed span, else the exception message of its events."""
    text = tag_value(span, "otel.status_description")
    for log in span.get("logs", []):
        for field in log["fields"]:
            if not text and field["key"] == "exception.message":
                text = str(field["value"])
    text = " ".join(text.split())
    return text[:MAX_MESSAGE_CHARS] or "no message"


def summarize_trace(trace: dict) -> list[tuple[str, str, str, bool]]:
    """The failed spans of one trace as (service, operation, message, deepest).

    A span is deepest when none of its children failed. Errors travel up a call
    chain, so the deepest failed span is where the failure started.
    """
    failed = [s for s in trace["spans"] if is_error(s)]
    parents = {
        ref["spanID"]
        for s in failed
        for ref in s.get("references", [])
        if ref["refType"] == "CHILD_OF"
    }
    return [
        (
            trace["processes"][s["processID"]]["serviceName"],
            s["operationName"],
            error_message(s),
            s["spanID"] not in parents,
        )
        for s in failed
    ]


@registry.tool
async def get_error_traces(
    service: Annotated[
        str, Field(description="Service name, for example checkout or frontend.")
    ],
    minutes: Annotated[
        int, Field(ge=1, le=60, description="How far back to look.")
    ] = 15,
) -> str:
    """Find recent traces that touch a service and contain failed spans.

    Shows each distinct failure with the service and operation that failed.
    A failed span marked deepest has no failed child, so the failure started there.
    This is how to see which downstream service a failing request called.
    """
    async with kube_api() as api, api_errors("trace query"):
        found = await jaeger_get(
            api,
            "traces",
            [
                ("service", service),
                ("lookback", f"{minutes}m"),
                ("limit", str(MAX_TRACES)),
                ("tags", json.dumps({"error": "true"})),
            ],
        )
        traces = found["data"]
        if not traces:
            known = (await jaeger_get(api, "services", []))["data"]
            if service not in known:
                raise RuntimeError(
                    f"no service {service!r} in the traces. Services with traces: "
                    + ", ".join(sorted(known))
                )
            return f"No failed traces for {service} in the last {minutes} minutes."
    groups: dict[tuple[str, str, str, bool], int] = {}
    for trace in traces:
        # Count a failure once per trace, however many spans repeat it.
        for key in set(summarize_trace(trace)):
            groups[key] = groups.get(key, 0) + 1
    ranked = sorted(groups.items(), key=lambda item: (not item[0][3], -item[1]))
    header = (
        f"{len(traces)} traces with failed spans for {service} in the last {minutes} "
        f"minutes (up to {MAX_TRACES} newest). Failed spans, deepest first, "
        "with the number of traces each appears in:"
    )
    lines = [header]
    for (svc, operation, message, deepest), count in ranked[:MAX_ERROR_GROUPS]:
        mark = " [deepest]" if deepest else ""
        lines.append(f"{count} traces: {svc} {operation}{mark}: {message}")
    if len(ranked) > MAX_ERROR_GROUPS:
        lines.append(
            f"{len(ranked) - MAX_ERROR_GROUPS} more kinds of failure not shown."
        )
    return "\n".join(lines)
