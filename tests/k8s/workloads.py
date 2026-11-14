"""What the cluster tests share: the committed manifests read as data, and one test run as the
Job, with its records read back through the reader pod.

The result is read from result.json and the records on the volume, through the reader pod,
never from `kubectl logs`: there any stderr line, such as the runner's waiting message, is
mixed into its stdout.
"""

import base64
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from k8s import cluster as kubernetes
from k8s.cluster import Cluster, ClusterError, Tags

MANIFEST_FILES = sorted(kubernetes.MANIFESTS.glob("*.yaml"))
WORKLOAD_KINDS = {"Deployment", "Job", "Pod"}
JOB = "scrimmage-test"
JOB_PODS = f"batch.kubernetes.io/job-name={JOB}"
AGENT_PODS = "app.kubernetes.io/name=acme-agent"
RUNS = "/records/scrimmage-runs"
# What the example's two rows give when every mock and fault is applied: per row, its result,
# its done and passed counts, its runs' outcomes and whether each run's fault fired.
EXAMPLE_OUTCOMES = {
    "baseline": ("pass", 30, 30, {"answered"}, {None}),
    "fault 1": ("pass", 30, 30, {"answered"}, {True}),
}


def documents(path: Path) -> list[dict[str, Any]]:
    return [d for d in yaml.safe_load_all(path.read_text()) if d]


def pod_spec(document: dict[str, Any]) -> dict[str, Any]:
    if document["kind"] == "Pod":
        return document["spec"]
    return document["spec"]["template"]["spec"]


def wait_default_account(cluster: Cluster, namespace: str) -> None:
    """The namespace's default account is made a moment after it, and a pod made before it
    is refused for that, which would be a different failure from the one a test provokes."""
    cluster.wait_until(
        "the default service account",
        lambda: (
            cluster.kubectl("get", "sa", "default", "-n", namespace, check=False).returncode == 0
        ),
        timeout=30,
    )


def pod_names(cluster: Cluster, namespace: str, selector: str) -> list[str]:
    found = cluster.json("get", "pods", "-n", namespace, "-l", selector)
    return sorted(p["metadata"]["name"] for p in found["items"])


def agents_ready(cluster: Cluster, namespace: str) -> bool:
    """Both replicas Ready. A container that restarted stops the wait at once with its log: a
    service that refuses its settings exits, and would otherwise only show as a timeout."""
    found = cluster.json("get", "pods", "-n", namespace, "-l", AGENT_PODS)
    for item in found["items"]:
        for status in item.get("status", {}).get("containerStatuses", []):
            if status.get("restartCount", 0):
                name = item["metadata"]["name"]
                said = cluster.kubectl(
                    "logs", name, "-n", namespace, "--previous", "--tail=20", check=False
                )
                raise ClusterError(f"the agent's container in {name} restarted:\n{said.stdout}")
    return cluster.deployment_ready(namespace, "acme-agent")


def start_job(cluster: Cluster, namespace: str, tags: Tags) -> str:
    """Apply the Job and return its pod's name once the pod exists."""
    cluster.apply(namespace, ("runner-job.yaml",), tags)
    cluster.wait_until(
        "the Job's pod",
        lambda: bool(pod_names(cluster, namespace, JOB_PODS)),
        timeout=60,
    )
    return pod_names(cluster, namespace, JOB_PODS)[0]


def wait_ended(cluster: Cluster, namespace: str, pod: str, timeout: float) -> int:
    """The pod's exit code, from its status, once its container has ended."""
    cluster.wait_until(
        f"{pod} to end",
        lambda: cluster.exit_code(namespace, pod) is not None,
        timeout=timeout,
        namespace=namespace,
        watch=(("pod", pod),),
    )
    code = cluster.exit_code(namespace, pod)
    assert code is not None
    return code


@dataclass(frozen=True)
class Records:
    """What the records volume holds: the test's result.json, each run's summary line and
    every record line."""

    folder: str
    result: dict[str, Any]
    summaries: dict[str, dict[str, Any]]
    lines: tuple[dict[str, Any], ...] = field(repr=False)


def read_records(cluster: Cluster, namespace: str) -> Records:
    """result.json and the record lines, read through the reader pod from the volume."""
    folders = cluster.reader_exec(namespace, "ls", RUNS).stdout.split()
    assert len(folders) == 1, f"one test, one records folder: {folders}"
    folder = f"{RUNS}/{folders[0]}"
    result = json.loads(cluster.reader_exec(namespace, "cat", f"{folder}/result.json").stdout)
    text = cluster.reader_exec(namespace, "sh", "-c", f"cat {folder}/*.jsonl").stdout
    lines = tuple(map(json.loads, text.splitlines()))
    summaries = {line["run"]: line["summary"] for line in lines if "summary" in line}
    return Records(folder, result, summaries, lines)


def run_details(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {d["run"]: d for row in result["rows"] for d in row["run_details"]}


def row_outcomes(result: dict[str, Any]) -> dict[str, tuple[Any, ...]]:
    """Each row of a result in the form of EXAMPLE_OUTCOMES."""
    return {
        row["row"]: (
            row["result"],
            row["done"],
            row["passed"],
            {d["outcome"] for d in row["run_details"]},
            {d["fault_fired"] for d in row["run_details"]},
        )
        for row in result["rows"]
    }


@dataclass(frozen=True)
class JobRun:
    """One test the Job ran, in a namespace that stays up while the tests read it."""

    namespace: str
    token: str = field(repr=False)  # never in a failure's output
    exit_code: int
    runner_pod: str
    agents: tuple[str, ...]
    log: Path  # kubectl logs, saved for a person to read; no test reads it


def run_job(
    cluster: Cluster, namespace: str, tags: Tags, logs: Path, agent: str = "agent.yaml"
) -> JobRun:
    """Deploy everything in `namespace`, with the agent from `agent`, run the Job to its end,
    save its log in `logs`, and start the reader pod."""
    wait_default_account(cluster, namespace)
    cluster.deploy(namespace, tags, agent)
    cluster.wait_ready(namespace, "fake-model")
    cluster.wait_until(
        "both agent replicas",
        lambda: agents_ready(cluster, namespace),
        timeout=180,
        namespace=namespace,
        watch=(("deployment", "acme-agent"),),
    )
    agents = tuple(pod_names(cluster, namespace, AGENT_PODS))
    stored = cluster.kubectl(
        "get", "secret", kubernetes.TOKEN_SECRET, "-n", namespace,
        "-o", "jsonpath={.data.token}",
    ).stdout  # fmt: skip
    runner_pod = start_job(cluster, namespace, tags)
    code = wait_ended(cluster, namespace, runner_pod, timeout=900)
    log = logs / f"{runner_pod}.log"
    log.write_text(cluster.kubectl("logs", runner_pod, "-n", namespace, check=False).stdout)
    cluster.start_reader(namespace, tags)
    return JobRun(
        namespace=namespace,
        token=base64.b64decode(stored).decode(),
        exit_code=code,
        runner_pod=runner_pod,
        agents=agents,
        log=log,
    )
