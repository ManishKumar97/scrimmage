"""The runner as a Job in the cluster, against the agent's two replicas: every run is served
under its own plan by one of them, the records outlive the Job's pod on their volume, the pods
read their secrets from Secret volumes, and a Job that fails never starts a second test.

The result is read from result.json and the records on the volume, through the reader pod,
never from `kubectl logs`: there any stderr line, such as the runner's waiting message,
is mixed into its stdout.

Like the other cluster tests, these run only with SCRIMMAGE_KIND=1, one file at a time:

    SCRIMMAGE_KIND=1 SCRIMMAGE_LANE=2 test-slot uv run pytest -n 0 -m kind \
        tests/test_service_k8s.py
"""

import base64
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from k8s import cluster as kubernetes
from k8s import images
from k8s.cluster import Cluster, ClusterError, Tags
from test_docs import kubernetes_docs_problems
from test_k8s_manifests import wait_default_account

pytestmark = [pytest.mark.slow, pytest.mark.kind]

JOB = "scrimmage-test"
JOB_PODS = f"batch.kubernetes.io/job-name={JOB}"
AGENT_PODS = "app.kubernetes.io/name=acme-agent"
RUNS = "/records/scrimmage-runs"
TOKEN_FILE = "/run/scrimmage/token/token"
# What the agent's own process sees of its token file: the link, the file it leads to, and
# whether its mount is read-only. Printed as JSON, so nothing is parsed from text.
SECRET_FILE_FACTS = f"""
import json, os, stat
path = {TOKEN_FILE!r}
seen = os.stat(path)
print(json.dumps({{
    "link": os.readlink(path) if stat.S_ISLNK(os.lstat(path).st_mode) else None,
    "data": os.readlink(os.path.join(os.path.dirname(path), "..data")),
    "uid": seen.st_uid,
    "gid": seen.st_gid,
    "mode": oct(stat.S_IMODE(seen.st_mode)),
    "read_only": bool(os.statvfs(path).f_flag & os.ST_RDONLY),
    "groups": os.getgroups(),
}}))
"""


@dataclass(frozen=True)
class JobRun:
    """One test the Job ran in a namespace that stays up for this file's tests."""

    namespace: str
    token: str = field(repr=False)  # never in a failure's output
    exit_code: int
    runner_pod: str
    agents: tuple[str, ...]
    log: Path  # kubectl logs, saved for a person to read; no test reads it


@dataclass(frozen=True)
class Records:
    """What the records volume holds: the test's result.json and each run's summary line."""

    folder: str
    result: dict[str, Any]
    summaries: dict[str, dict[str, Any]]


@pytest.fixture(scope="module")
def tags() -> Tags:
    return Tags(
        runner=images.ensure_runner_image(),
        agent=images.ensure_agent_image(),
        fake_model=images.ensure_fake_model_image(),
    )


@pytest.fixture(scope="module")
def cluster(tags: Tags) -> Iterator[Cluster]:
    with kubernetes.session((tags.runner, tags.agent, tags.fake_model)) as made:
        yield made


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


def read_records(cluster: Cluster, namespace: str) -> Records:
    """result.json and the summary lines, read through the reader pod from the volume."""
    folders = cluster.reader_exec(namespace, "ls", RUNS).stdout.split()
    assert len(folders) == 1, f"one test, one records folder: {folders}"
    folder = f"{RUNS}/{folders[0]}"
    result = json.loads(cluster.reader_exec(namespace, "cat", f"{folder}/result.json").stdout)
    lines = cluster.reader_exec(namespace, "sh", "-c", f"cat {folder}/*.jsonl").stdout
    summaries = {
        line["run"]: line["summary"]
        for line in map(json.loads, lines.splitlines())
        if "summary" in line
    }
    return Records(folder, result, summaries)


def run_details(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {d["run"]: d for row in result["rows"] for d in row["run_details"]}


@pytest.fixture(scope="module")
def tested(
    cluster: Cluster, tags: Tags, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[JobRun]:
    with cluster.namespace(restricted=True) as namespace:
        wait_default_account(cluster, namespace)
        cluster.deploy(namespace, tags)
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
        log = tmp_path_factory.mktemp("k8s-job") / f"{runner_pod}.log"
        log.write_text(cluster.kubectl("logs", runner_pod, "-n", namespace, check=False).stdout)
        cluster.start_reader(namespace, tags)
        yield JobRun(
            namespace=namespace,
            token=base64.b64decode(stored).decode(),
            exit_code=code,
            runner_pod=runner_pod,
            agents=agents,
            log=log,
        )


def test_the_job_runs_a_test_against_two_replicas(cluster: Cluster, tested: JobRun) -> None:
    records = read_records(cluster, tested.namespace)
    # The file's own document: --json on stdout adds result_file, and result.json leaves it out.
    assert "result_file" not in records.result, "the result was not read from result.json"
    details = run_details(records.result)
    # 2 rows of 30 runs: the chance that kube-proxy sends all 60 to one replica is 2 in 2**60.
    assert len(details) == 60, f"{len(details)} runs; the runner's log is {tested.log}"
    # Every run has its summary line. The setup request has one too, with no plan to compare.
    setup = set(records.summaries) - set(details)
    assert set(details) <= set(records.summaries), "every run has its summary line"
    assert len(setup) == 1 and next(iter(setup)).startswith("setup-"), setup
    summaries = {run: records.summaries[run] for run in details}
    # The digests first: they name the cause when the outcomes below are infrastructure errors.
    for run, summary in summaries.items():
        assert re.fullmatch(r"[0-9a-f]{64}", str(summary["plan_digest"])), (run, summary)
        assert summary["claimed_plan_digest"] == summary["plan_digest"], (
            f"{run} was served under another plan than the runner made: {summary}"
        )
    served = {s["claimed_by"]["host"] for s in summaries.values()}
    assert len(served) == 2 and served == set(tested.agents), (
        f"runs were served by {sorted(served)}, the replicas are {list(tested.agents)}"
    )
    # The pod's exit code, from its status, agrees with the test's result.
    assert tested.exit_code == 0, f"exit {tested.exit_code}; the runner's log is {tested.log}"
    result = records.result
    assert (result["status"], result["exit"]) == ("passed", 0), result["infra"]
    assert result["runs_sent"] == result["claims_received"] == 61  # the setup request, 60 runs
    rows = {r["row"]: r for r in result["rows"]}
    assert set(rows) == {"baseline", "fault 1"}
    for name, fired in (("baseline", None), ("fault 1", True)):
        row = rows[name]
        assert (row["result"], row["done"], row["passed"]) == ("pass", 30, 30), row
        assert {d["outcome"] for d in row["run_details"]} == {"answered"}
        assert {d["fault_fired"] for d in row["run_details"]} == {fired}
    assert "Oct 30" in rows["baseline"]["run_details"][0]["answer_preview"]


def test_the_records_survive_the_jobs_pod(cluster: Cluster, tested: JobRun, tags: Tags) -> None:
    namespace = tested.namespace
    before = read_records(cluster, namespace)
    cluster.kubectl("delete", "pod", "reader", "-n", namespace, "--wait=true")
    cluster.kubectl("delete", "job", JOB, "-n", namespace, "--cascade=foreground", "--wait=true")
    assert pod_names(cluster, namespace, JOB_PODS) == []
    gone = cluster.kubectl("get", "pod", tested.runner_pod, "-n", namespace, check=False)
    assert gone.returncode != 0 and "NotFound" in gone.stderr, "the Job's pod is gone"
    # A new reader, so nothing that held the volume while the Job ran is still there.
    cluster.start_reader(namespace, tags)
    after = read_records(cluster, namespace)
    assert after == before
    shown = cluster.reader_exec(namespace, "scrimmage", "show", "--json", after.folder)
    result = json.loads(shown.stdout)["result"]  # what show read from the volume's result.json
    outcomes = {run: d["outcome"] for run, d in run_details(result).items()}
    assert outcomes == {run: d["outcome"] for run, d in run_details(before.result).items()}
    assert len(outcomes) == 60


def test_the_pods_read_their_secrets_from_secret_volumes(cluster: Cluster, tested: JobRun) -> None:
    namespace = tested.namespace
    # The runner read its token and its control key from Secret volumes too: it stops with
    # exit 2 when it refuses either, before any run.
    assert tested.exit_code == 0, f"exit {tested.exit_code}; the runner's log is {tested.log}"
    for agent in tested.agents:
        done = cluster.kubectl(
            "exec", agent, "-n", namespace, "--", "python3", "-c", SECRET_FILE_FACTS
        )
        facts = json.loads(done.stdout)
        # The kubelet's layout: a link into ..data, which links to a stamped folder beside it.
        assert facts["link"] == "..data/token", facts
        assert re.fullmatch(r"\.\.[^/]+", facts["data"]), facts
        assert (facts["uid"], facts["gid"], facts["mode"]) == (0, 10001, "0o440"), facts
        assert facts["read_only"], facts
        assert 10001 in facts["groups"], facts
        # Neither the environment kubectl exec gets nor the service's own holds the token.
        env = cluster.kubectl("exec", agent, "-n", namespace, "--", "env").stdout
        own = cluster.kubectl("exec", agent, "-n", namespace, "--", "cat", "/proc/1/environ")
        for text in (env, own.stdout.replace("\0", "\n")):
            assert (tested.token in text) is False, "the token is in the pod's environment"
            assert re.search(r"(?m)^SCRIMMAGE_TOKEN=", text) is None
            assert f"SCRIMMAGE_TOKEN_FILE={TOKEN_FILE}" in text  # positive control


def test_the_job_never_retries(
    cluster: Cluster, tags: Tags, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    committed = kubernetes.CONFIG_FILE.read_text()
    broken = committed.replace("runs = 30\n", 'runs = "thirty"\n')
    assert broken != committed
    (tmp_path / "scrimmage.toml").write_text(broken)
    monkeypatch.setattr(kubernetes, "CONFIG_FILE", tmp_path / "scrimmage.toml")
    with cluster.namespace(restricted=True) as namespace:
        wait_default_account(cluster, namespace)
        cluster.issue_control_certificate(namespace)
        cluster.create_token_secret(namespace)
        cluster.create_config_map(namespace)
        cluster.apply(namespace, ("service-accounts.yaml", "records-pvc.yaml"), tags)
        pod = start_job(cluster, namespace, tags)
        assert wait_ended(cluster, namespace, pod, timeout=180) == 2, "a mistake in the file"

        def failed() -> bool:
            job = cluster.json("get", "job", JOB, "-n", namespace)
            done = any(
                c["type"] == "Failed" and c["status"] == "True"
                for c in job.get("status", {}).get("conditions", [])
            )
            # A second pod ends the wait too: the assertion below then names it.
            return done or len(pod_names(cluster, namespace, JOB_PODS)) > 1

        cluster.wait_until("the Job to fail", failed, timeout=120)
        pods = pod_names(cluster, namespace, JOB_PODS)
        assert pods == [pod], f"the Job started {len(pods)} pods: {pods}"
        job = cluster.json("get", "job", JOB, "-n", namespace)
        assert job["status"].get("failed") == 1, job["status"]
        reasons = [c.get("reason") for c in job["status"]["conditions"] if c["type"] == "Failed"]
        assert reasons == ["BackoffLimitExceeded"], job["status"]


def test_the_docs_manifests_are_the_tested_ones() -> None:
    assert kubernetes_docs_problems() == []
