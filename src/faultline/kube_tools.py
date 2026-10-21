"""Tools that read the test bed through the real Kubernetes API."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
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
# Jaeger keeps a fixed number of traces (--memory.max-traces=24000), not a time.
# Measured on the test bed with 3 load generator users: about 1500 new traces a
# minute, so the store reaches back about 15 minutes (oldest trace found 15.5 minutes
# old, none at 16). More load means less.
MAX_TRACE_MINUTES = 15
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


# Not in this registry: the IDA reaches get_logs through the MCP server in mcp_logs.
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
    try:
        return json.loads(await response.text())
    except json.JSONDecodeError:
        raise RuntimeError("Jaeger answered with something that is not JSON") from None


def tag_value(span: dict, key: str) -> str:
    return next((str(t["value"]) for t in span["tags"] if t["key"] == key), "")


def is_error(span: dict) -> bool:
    return (
        tag_value(span, "error").lower() == "true"
        or tag_value(span, "otel.status_code") == "ERROR"
    )


def is_stream(span: dict) -> bool:
    """A streaming call, such as flagd's EventStream, lives for the whole process.

    It fails whenever the server restarts, which says nothing about a request.
    """
    return tag_value(span, "rpc.method").endswith("Stream")


def peer(span: dict) -> str:
    """Who a span talks to. For a client span this is the callee."""
    for key in ("rpc.service", "peer.service", "server.address", "net.peer.name"):
        if value := tag_value(span, key):
            return value
    return ""


def error_message(span: dict) -> str:
    """The status text of a failed span, else the exception message of its events."""
    text = tag_value(span, "otel.status_description")
    for log in span.get("logs", []):
        for field in log["fields"]:
            if not text and field["key"] == "exception.message":
                text = str(field["value"])
    text = " ".join(text.split())
    return text[:MAX_MESSAGE_CHARS] or "no message"


@dataclass(frozen=True)
class Failure:
    """One kind of failed span. `last` is the deepest one of its chain."""

    service: str
    operation: str
    kind: str
    peer: str
    message: str
    last: bool


def summarize_trace(trace: dict) -> list[tuple[Failure, int, int]]:
    """The failed spans of one trace, each with its start and duration in microseconds.

    A failed span is `last` when no span below it failed. Errors travel up a call
    chain, so the last failed span is where the failed call was made. That is
    not always the cause: if it is a client span, its callee may be down.
    """
    spans = trace["spans"]
    failed = [s for s in spans if is_error(s) and not is_stream(s)]
    parent_of = {
        s["spanID"]: ref["spanID"]
        for s in spans
        for ref in s.get("references", [])
        if ref["refType"] == "CHILD_OF"
    }
    has_failed_descendant = set()
    for s in failed:
        # Walk up through every ancestor, failed or not.
        ancestor = parent_of.get(s["spanID"])
        while ancestor is not None and ancestor not in has_failed_descendant:
            has_failed_descendant.add(ancestor)
            ancestor = parent_of.get(ancestor)
    return [
        (
            Failure(
                trace["processes"][s["processID"]]["serviceName"],
                s["operationName"],
                tag_value(s, "span.kind"),
                peer(s),
                error_message(s),
                s["spanID"] not in has_failed_descendant,
            ),
            s["startTime"],
            s["duration"],
        )
        for s in failed
    ]


def format_duration(micros: int) -> str:
    seconds = micros / 1_000_000
    if seconds < 1:
        return f"{micros / 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{int(seconds // 60)}m{int(seconds % 60):02d}s"


def describe_failure(f: Failure) -> str:
    kind = f" {f.kind}" if f.kind else ""
    callee = f" calls {f.peer}" if f.kind == "client" and f.peer else ""
    last = " [last failed span]" if f.last else ""
    return f"{f.service} {f.operation}{kind}{callee}{last}: {f.message}"


@registry.tool
async def get_error_traces(
    service: Annotated[
        str, Field(description="Service name, for example checkout or frontend.")
    ],
    minutes: Annotated[
        int,
        Field(
            ge=1,
            le=MAX_TRACE_MINUTES,
            description=(
                f"How far back to look, at most {MAX_TRACE_MINUTES}. Jaeger keeps "
                "a fixed number of traces, so older failures may be gone."
            ),
        ),
    ] = MAX_TRACE_MINUTES,
) -> str:
    """Find recent traces in which a span of this service failed.

    A service that hides a downstream failure has no failed span, so it shows none
    here: query its callees too. Each line is one kind of failure, with the number
    of traces and when the latest one ended, with how long that call took: a call
    that fails after minutes ends long after it started. Jaeger keeps only a
    fixed number of recent traces, so "no failed spans" says nothing about failures
    it has already dropped. A span marked last has no failed span
    below it, so the failed call was made there. If it is a client span, the
    service it calls may be the one that is down or unreachable.
    Long-lived stream spans are ignored.
    """
    now = utc_now()
    end = int(now.timestamp() * 1_000_000)
    start = end - minutes * 60 * 1_000_000
    async with kube_api() as api, api_errors("trace query"):
        found = await jaeger_get(
            api,
            "traces",
            [
                ("service", service),
                ("start", str(start)),
                ("end", str(end)),
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
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    # failure -> traces, (end, duration) of the span that ended last
    groups: dict[Failure, tuple[int, tuple[int, int]]] = {}
    for trace in traces:
        # Count a failure once per trace, however many spans repeat it.
        newest: dict[Failure, tuple[int, int]] = {}
        for failure, started, duration in summarize_trace(trace):
            newest[failure] = max(
                newest.get(failure, (0, 0)), (started + duration, duration)
            )
        for failure, ended in newest.items():
            count, latest = groups.get(failure, (0, (0, 0)))
            groups[failure] = (count + 1, max(latest, ended))
    if not groups:
        return (
            f"Now: {stamp}. No failed spans of {service} in the last {minutes} "
            f"minutes. Jaeger keeps a fixed number of traces, about {MAX_TRACE_MINUTES} "
            "minutes at normal test-bed load, so older failures may be gone. If it "
            "hides a downstream failure, check the services it calls."
        )
    ranked = sorted(groups.items(), key=lambda item: (not item[0].last, -item[1][0]))
    header = (
        f"Now: {stamp}. {len(traces)} traces with a failed {service} span in the last "
        f"{minutes} minutes (up to {MAX_TRACES} newest). Failures, last failed spans "
        "first, with the number of traces and when the latest one ended:"
    )
    lines = [header]
    for failure, (count, (ended, duration)) in ranked[:MAX_ERROR_GROUPS]:
        when = datetime.fromtimestamp(ended / 1_000_000, UTC).strftime("%H:%M:%SZ")
        lines.append(
            f"{count} traces, latest ended {when} after {format_duration(duration)}: "
            + describe_failure(failure)
        )
    if len(ranked) > MAX_ERROR_GROUPS:
        lines.append(
            f"{len(ranked) - MAX_ERROR_GROUPS} more kinds of failure not shown."
        )
    return "\n".join(lines)
