"""Is the test bed clean enough to start an incident?

Clean means: every pod Ready, no alert firing, no flag left on, and no new failed
spans for the last three minutes.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from kubernetes_asyncio.client import ApiClient, CoreV1Api

from faultline.incidents import flagd
from faultline.kube_tools import NAMESPACE
from faultline.metrics_tools import prom_get

QUIET_WINDOW = "3m"
# Alerts Prometheus has in the firing state. The ALERTS series exists only while one fires.
FIRING_ALERTS = 'ALERTS{alertstate="firing"}'
# Failed spans in the quiet window. The load generator is left out: its browser users
# log constant connection errors of their own, which no customer ever sees.
FAILED_SPANS = (
    f"sum(increase(traces_span_metrics_calls_total{{"
    f'status_code="STATUS_CODE_ERROR",service_name!="load-generator"}}[{QUIET_WINDOW}]))'
)
# Failed spans allowed in the quiet window. On the healthy test bed this reads 0, apart
# from bursts of 1 to 4 right after a pod restarts, which clear within the window.
MAX_FAILED_SPANS = 0.0


@dataclass(frozen=True)
class Health:
    not_ready: list[str] = field(default_factory=list)
    firing: list[str] = field(default_factory=list)
    faults: dict[str, str] = field(default_factory=dict)
    failed_spans: float | None = None  # None: Prometheus has no such series yet
    error: str | None = None  # the check itself could not run

    @property
    def ok(self) -> bool:
        return not (
            self.error
            or self.not_ready
            or self.firing
            or self.faults
            or (self.failed_spans or 0.0) > MAX_FAILED_SPANS
        )

    def describe(self) -> str:
        if self.ok:
            return "healthy"
        parts = []
        if self.error:
            parts.append(f"health check failed: {self.error}")
        if self.not_ready:
            parts.append(f"pods not ready: {', '.join(sorted(self.not_ready))}")
        if self.firing:
            parts.append(f"alerts firing: {', '.join(sorted(self.firing))}")
        if self.faults:
            parts.append(f"flags on: {self.faults}")
        if (self.failed_spans or 0.0) > MAX_FAILED_SPANS:
            parts.append(f"{self.failed_spans:g} failed spans in {QUIET_WINDOW}")
        return "; ".join(parts)


def pod_problem(pod) -> str | None:
    """Why a pod is not Ready, or None. A finished Job pod (Succeeded) is fine."""
    if pod.status.phase == "Succeeded":
        return None
    containers = pod.status.container_statuses or []
    if pod.status.phase != "Running" or not containers:
        return f"{pod.metadata.name} ({pod.status.phase})"
    if not all(c.ready for c in containers):
        return f"{pod.metadata.name} (not ready)"
    return None


async def not_ready_pods(api: ApiClient) -> list[str]:
    pods = await CoreV1Api(api).list_namespaced_pod(NAMESPACE)
    return [p for p in map(pod_problem, pods.items) if p]


async def firing_alerts(api: ApiClient) -> list[str]:
    data = await prom_get(api, "query", [("query", FIRING_ALERTS)])
    return [r["metric"].get("alertname", "?") for r in data["result"]]


async def failed_spans(api: ApiClient) -> float | None:
    data = await prom_get(api, "query", [("query", FAILED_SPANS)])
    result = data["result"]
    return float(result[0]["value"][1]) if result else None


async def check_health(api: ApiClient) -> Health:
    return Health(
        not_ready=await not_ready_pods(api),
        firing=await firing_alerts(api),
        faults=await flagd.active_faults(api),
        failed_spans=await failed_spans(api),
    )


async def wait_healthy(
    check: Callable[[], Awaitable[Health]],
    timeout: float,
    interval: float = 15.0,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    now: Callable[[], float] = time.monotonic,
) -> Health:
    """Poll until healthy or the timeout passes. Return the last reading.

    A check that raises counts as unhealthy, since a cluster that is mid-recovery
    often cannot answer. The error is kept in the returned reading.
    """
    deadline = now() + timeout
    last = Health()
    while True:
        try:
            last = await check()
        except Exception as exc:  # noqa: BLE001 - an unreachable API is not healthy
            last = Health(error=f"{type(exc).__name__}: {exc}")
        if last.ok or now() >= deadline:
            return last
        await sleep(min(interval, max(deadline - now(), 0.0)))
