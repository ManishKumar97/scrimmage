"""Tools that read the test bed through the real Kubernetes API."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated

from kubernetes_asyncio import config
from kubernetes_asyncio.client import ApiClient, ApiException, AppsV1Api, CoreV1Api
from pydantic import Field

from faultline.tools import Registry

NAMESPACE = "astronomy-shop"
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
    async with kube_api() as api, api_errors("pod list"):
        core = CoreV1Api(api)
        selector = ""
        if service:
            with_name = f"deployment named {service!r}"
            async with api_errors(with_name):
                deployment = await AppsV1Api(api).read_namespaced_deployment(
                    service, NAMESPACE
                )
            labels = deployment.spec.selector.match_labels or {}
            selector = ",".join(f"{key}={value}" for key, value in labels.items())
        pods = await core.list_namespaced_pod(NAMESPACE, label_selector=selector)
    now = datetime.now(UTC)
    rows = [pod_row(pod, now) for pod in pods.items]
    header = f"Now: {now.strftime('%Y-%m-%dT%H:%M:%SZ')}."
    if not rows:
        return f"{header} No pods."
    return "\n".join([header, "POD PHASE READY RESTARTS LAST_TERMINATION AGE", *rows])


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
        f"{pod.metadata.name} {pod.status.phase} {ready}/{len(statuses)} "
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
