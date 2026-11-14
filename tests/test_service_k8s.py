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

import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from k8s import cluster as kubernetes
from k8s import images
from k8s.cluster import Cluster, Tags
from k8s.workloads import (
    EXAMPLE_OUTCOMES,
    JOB,
    JOB_PODS,
    JobRun,
    pod_names,
    read_records,
    row_outcomes,
    run_details,
    run_job,
    start_job,
    wait_default_account,
    wait_ended,
)
from test_docs import kubernetes_docs_problems

pytestmark = [pytest.mark.slow, pytest.mark.kind]

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


@pytest.fixture(scope="module")
def tested(
    cluster: Cluster, tags: Tags, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[JobRun]:
    with cluster.namespace(restricted=True) as namespace:
        yield run_job(cluster, namespace, tags, tmp_path_factory.mktemp("k8s-job"))


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
    assert row_outcomes(result) == EXAMPLE_OUTCOMES
    rows = {r["row"]: r for r in result["rows"]}
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
            leaked = tested.token in text  # outside the assert, so a failure never prints it
            assert not leaked, "the token is in the pod's environment"
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
