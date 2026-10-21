"""Test bed housekeeping between trials."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from kubernetes_asyncio.client import ApiClient, AppsV1Api

from faultline.kube_tools import NAMESPACE

JAEGER = "jaeger"
POLL_SECONDS = 3.0


async def reset_traces(
    api: ApiClient,
    timeout: float = 180.0,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    now: Callable[[], float] = time.monotonic,
) -> None:
    """Empty Jaeger's in-memory store by restarting it, then wait until it is ready.

    Without this, the failed traces of one trial sit in the store for the next 15
    minutes and the next trial's agent finds them. `kubectl rollout restart` does the
    same: it stamps the pod template, which makes the deployment roll.
    Raises RuntimeError when the new pod is not ready within the timeout.
    """
    apps = AppsV1Api(api)
    stamp = datetime.now(UTC).isoformat()
    patch = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {"kubectl.kubernetes.io/restartedAt": stamp}
                }
            }
        }
    }
    deployment = await apps.patch_namespaced_deployment(JAEGER, NAMESPACE, patch)
    generation = deployment.metadata.generation
    deadline = now() + timeout
    while True:
        status = (await apps.read_namespaced_deployment(JAEGER, NAMESPACE)).status
        want = status.replicas or 0
        if (
            status.observed_generation is not None
            and status.observed_generation >= generation
            and (status.updated_replicas or 0) == want
            and (status.ready_replicas or 0) == want
            and want == 1
        ):
            return
        if now() >= deadline:
            raise RuntimeError(f"jaeger was not ready {timeout:g}s after the restart")
        await sleep(POLL_SECONDS)
