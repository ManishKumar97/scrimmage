"""The cluster's defences around a test: the network policies let only the agent's pods reach the
control port and the runner reach only the agent, the runner's account can do nothing, every
pod meets the restricted Pod Security standard, the agent's processes connect only where they
must, and an agent started without `scrimmage-run`, with PYTHONPATH set by hand, runs the same
test as one started with it.

Like the other cluster tests, these run only with SCRIMMAGE_KIND=1, one file at a time:

    SCRIMMAGE_KIND=1 SCRIMMAGE_LANE=2 test-slot uv run pytest -n 0 -m kind \
        tests/test_k8s_security.py
"""

import copy
import json
import re
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from compose import harness
from k8s import cluster as kubernetes
from k8s import images
from k8s.cluster import Cluster, Tags
from k8s.workloads import (
    AGENT_PODS,
    EXAMPLE_OUTCOMES,
    MANIFEST_FILES,
    WORKLOAD_KINDS,
    documents,
    pod_names,
    pod_spec,
    read_records,
    row_outcomes,
    run_details,
    run_job,
    wait_default_account,
)

pytestmark = [pytest.mark.slow, pytest.mark.kind]

MANIFESTS = kubernetes.MANIFESTS
CONTROL_PORT = 7700
# Accepts every connection and closes it: a stand-in for a server on the control port.
LISTEN = f"""
import socket
server = socket.create_server(("0.0.0.0", {CONTROL_PORT}))
while True:
    server.accept()[0].close()
"""
# Connects once to argv[1]:argv[2] within argv[3] seconds and prints "connected", or the
# error's class: TimeoutError when a policy drops the packets, since Calico drops, not refuses.
CONNECT = """
import socket, sys
try:
    socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=float(sys.argv[3])).close()
    print("connected")
except OSError as exc:
    print(type(exc).__name__)
"""
CONNECT_S = "4"
DROPPED = "TimeoutError"
TOKEN_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
# The rules RBAC gives every authenticated account, so a token can ask what it may do.
DISCOVERY = {
    "selfsubjectreviews.authentication.k8s.io",
    "selfsubjectaccessreviews.authorization.k8s.io",
    "selfsubjectrulesreviews.authorization.k8s.io",
}
EGRESS_LOG = "/run/egress/log"


@pytest.fixture(scope="module")
def tags() -> Tags:
    return Tags(
        runner=images.ensure_runner_image(),
        agent=images.ensure_agent_image(),
        fake_model=images.ensure_fake_model_image(),
        egress=images.ensure_egress_image(),
    )


@pytest.fixture(scope="module")
def cluster(tags: Tags) -> Iterator[Cluster]:
    assert tags.egress is not None
    with kubernetes.session((tags.runner, tags.agent, tags.fake_model, tags.egress)) as made:
        yield made


def manifest_pod(file: str, name: str) -> dict[str, Any]:
    """A bare Pod made from the pod template of the one workload in a committed file."""
    (document,) = (d for d in documents(MANIFESTS / file) if d["kind"] in WORKLOAD_KINDS)
    labels = (
        document["metadata"].get("labels", {})
        if document["kind"] == "Pod"
        else document["spec"]["template"]["metadata"]["labels"]
    )
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "labels": dict(labels)},
        "spec": copy.deepcopy(pod_spec(document)),
    }


def listener(name: str, labels: dict[str, str]) -> dict[str, Any]:
    """A pod from the runner image that listens on the control port and holds nothing else:
    no volume, no setting, no account but the namespace's default."""
    pod = manifest_pod("runner-job.yaml", name)
    pod["metadata"]["labels"] = labels
    spec = pod["spec"]
    spec["serviceAccountName"] = "default"
    spec.pop("volumes")
    container = spec["containers"][0]
    for key in ("args", "env", "volumeMounts"):
        container.pop(key)
    container["command"] = ["python3", "-c", LISTEN]
    container["resources"] = {"requests": {"cpu": "25m", "memory": "32Mi"}}
    return pod


def runner_stand_in() -> dict[str, Any]:
    """The Job's own pod, as committed, with the runner's labels, account, volumes and
    settings; only its command is the listener, so it holds the control port without a test."""
    pod = manifest_pod("runner-job.yaml", "runner")
    container = pod["spec"]["containers"][0]
    container.pop("args")
    container["command"] = ["python3", "-c", LISTEN]
    return pod


def start_pod(cluster: Cluster, namespace: str, pod: dict[str, Any], tags: Tags) -> str:
    name = pod["metadata"]["name"]
    cluster.kubectl("apply", "-n", namespace, "-f", "-", input=tags.render(json.dumps(pod)))
    cluster.wait_until(
        f"the pod {name}",
        lambda: cluster.pod_ready(namespace, name),
        timeout=120,
        namespace=namespace,
        watch=(("pod", name),),
    )
    return name


def connect(cluster: Cluster, namespace: str, pod: str, host: str, port: int) -> str:
    """What one connection from inside `pod` to host:port gives: "connected" or the error."""
    done = cluster.kubectl(
        "exec", pod, "-n", namespace, "--",
        "python3", "-c", CONNECT, host, str(port), CONNECT_S,
    )  # fmt: skip
    return done.stdout.strip()


def pod_ip(cluster: Cluster, namespace: str, pod: str) -> str:
    return str(cluster.json("get", "pod", pod, "-n", namespace)["status"]["podIP"])


def service_ip(cluster: Cluster, namespace: str, service: str) -> str:
    return str(cluster.json("get", "service", service, "-n", namespace)["spec"]["clusterIP"])


def deleted(cluster: Cluster, namespace: str) -> None:
    """Wait until a namespace the test is done with is gone, so its pods' CPU requests are free
    before the next test schedules its own."""
    cluster.kubectl("wait", "--for=delete", f"namespace/{namespace}", "--timeout=180s", check=False)


@dataclass(frozen=True)
class Stack:
    """The agent and the fake model as shipped, with a stand-in for the runner's pod, in a
    namespace under the committed policies; and a pod in a second namespace with no policy."""

    namespace: str
    agent: str  # one of the agent's replicas
    runner: str
    stranger: str  # in the namespace, with a label no policy names
    outside_namespace: str
    outsider: str  # in the second namespace, with the agent's label


@pytest.fixture(scope="module")
def stack(cluster: Cluster, tags: Tags) -> Iterator[Stack]:
    with cluster.namespace(restricted=True) as namespace, cluster.namespace() as outside:
        wait_default_account(cluster, namespace)
        wait_default_account(cluster, outside)
        cluster.deploy(namespace, tags)
        cluster.wait_ready(namespace, "fake-model", "acme-agent")
        runner = start_pod(cluster, namespace, runner_stand_in(), tags)
        stranger = start_pod(
            cluster, namespace, listener("stranger", {"app.kubernetes.io/name": "stranger"}), tags
        )
        # The second namespace is bare: no policy and no Pod Security label. Its pod keeps the
        # namespace's default account and its API token, as a careless pod does.
        outsider = listener("outsider", {"app.kubernetes.io/name": "acme-agent"})
        del outsider["spec"]["automountServiceAccountToken"]
        start_pod(cluster, outside, outsider, tags)
        yield Stack(
            namespace=namespace,
            agent=pod_names(cluster, namespace, AGENT_PODS)[0],
            runner=runner,
            stranger=stranger,
            outside_namespace=outside,
            outsider="outsider",
        )


def stranger_egress(namespace: str) -> dict[str, Any]:
    """Every egress for the stranger, so only the runner's own ingress rule can stop it."""
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "stranger-egress", "namespace": namespace},
        "spec": {
            "podSelector": {"matchLabels": {"app.kubernetes.io/name": "stranger"}},
            "policyTypes": ["Egress"],
            "egress": [{}],
        },
    }


def test_only_the_agents_pods_reach_the_control_port(
    cluster: Cluster, stack: Stack, tags: Tags
) -> None:
    namespace, outside = stack.namespace, stack.outside_namespace
    control = f"scrimmage.{namespace}.svc.cluster.local"
    cluster.kubectl("apply", "-f", "-", input=json.dumps(stranger_egress(namespace)))
    try:
        assert connect(cluster, namespace, stack.agent, "scrimmage", CONTROL_PORT) == "connected"
        # The stranger may send anywhere, so what stops it is the runner's ingress rule.
        assert connect(cluster, namespace, stack.stranger, "scrimmage", CONTROL_PORT) == DROPPED
        # Another namespace's pod with the agent's own label: the rule names this namespace.
        assert connect(cluster, outside, stack.outsider, control, CONTROL_PORT) == DROPPED
        # A pod that no rule names is closed too: default-deny is what closes it.
        stranger = pod_ip(cluster, namespace, stack.stranger)
        assert connect(cluster, outside, stack.outsider, stranger, CONTROL_PORT) == DROPPED
        # Positive control: with the policies removed, the stranger and the outsider connect,
        # so the drops above are the policies' and not a broken path.
        cluster.kubectl("delete", "networkpolicy", "--all", "-n", namespace)
        cluster.wait_until(
            "the stranger to connect with no policy",
            lambda: (
                connect(cluster, namespace, stack.stranger, "scrimmage", CONTROL_PORT)
                == "connected"
            ),
            timeout=30,
        )
        assert connect(cluster, outside, stack.outsider, control, CONTROL_PORT) == "connected"
        assert connect(cluster, outside, stack.outsider, stranger, CONTROL_PORT) == "connected"
    finally:
        cluster.kubectl("delete", "networkpolicy", "--all", "-n", namespace)
        cluster.apply(namespace, ("network-policies.yaml",), tags)
    # The committed policies are back for the tests after this one.
    cluster.wait_until(
        "the policies to hold again",
        lambda: connect(cluster, namespace, stack.stranger, "scrimmage", CONTROL_PORT) == DROPPED,
        timeout=30,
    )


def test_the_runner_reaches_only_the_agent_and_dns(cluster: Cluster, stack: Stack) -> None:
    namespace = stack.namespace
    runner = stack.runner
    # The names resolve, so DNS is open to it, and the agent's port answers.
    assert connect(cluster, namespace, runner, "acme-agent", 8000) == "connected"
    assert connect(cluster, namespace, runner, "fake-model", 8080) == DROPPED
    assert connect(cluster, namespace, runner, "1.1.1.1", 443) == DROPPED
    # Positive controls: the agent reaches the fake model, and a pod with no policy reaches
    # 1.1.1.1:443, so both paths work for whoever is allowed them.
    assert connect(cluster, namespace, stack.agent, "fake-model", 8080) == "connected"
    outside = stack.outside_namespace
    assert connect(cluster, outside, stack.outsider, "1.1.1.1", 443) == "connected"


def permissions(cluster: Cluster, namespace: str, account: str) -> list[str]:
    """What `kubectl auth can-i --list` says an account may do, one rule a line."""
    done = cluster.kubectl(
        "auth", "can-i", "--list", "-n", namespace,
        f"--as=system:serviceaccount:{namespace}:{account}",
    )  # fmt: skip
    lines = done.stdout.splitlines()
    assert lines[0].split()[0] == "Resources", lines[:2]
    return sorted(" ".join(line.split()) for line in lines[1:] if line.strip())


def has_token_dir(cluster: Cluster, namespace: str, pod: str) -> bool:
    done = cluster.kubectl("exec", pod, "-n", namespace, "--", "ls", TOKEN_DIR, check=False)
    return done.returncode == 0


def test_the_runners_service_account_can_do_nothing(cluster: Cluster, stack: Stack) -> None:
    namespace = stack.namespace
    runner = permissions(cluster, namespace, "scrimmage-runner")
    # Only the rules every account has: discovery, and asking what it may do.
    resources = {r.split()[0] for r in runner if not r.startswith("[")}
    assert resources <= DISCOVERY, runner
    # The same as an account made now with nothing bound to it.
    cluster.kubectl("create", "serviceaccount", "nobody", "-n", namespace)
    assert runner == permissions(cluster, namespace, "nobody")
    # Positive control: a Role bound to an account shows in its list.
    cluster.kubectl("create", "serviceaccount", "granted", "-n", namespace)
    cluster.kubectl(
        "create", "role", "read-secrets", "--verb=get", "--resource=secrets", "-n", namespace
    )
    cluster.kubectl(
        "create", "rolebinding", "read-secrets", "--role=read-secrets",
        f"--serviceaccount={namespace}:granted", "-n", namespace,
    )  # fmt: skip
    granted = permissions(cluster, namespace, "granted")
    assert any(r.split()[0] == "secrets" and "[get]" in r for r in granted), granted

    # No API token: the account and the Job's own pod both say not to mount one, and none is.
    account = cluster.json("get", "serviceaccount", "scrimmage-runner", "-n", namespace)
    assert account.get("automountServiceAccountToken") is False, account
    pod = cluster.json("get", "pod", stack.runner, "-n", namespace)
    assert pod["spec"]["serviceAccountName"] == "scrimmage-runner"
    mounted = [m["mountPath"] for m in pod["spec"]["containers"][0]["volumeMounts"]]
    assert TOKEN_DIR not in mounted, mounted
    assert not has_token_dir(cluster, namespace, stack.runner), "the runner's pod has a token"
    # Positive control: the pod in the bare namespace, with the default account, has one.
    assert has_token_dir(cluster, stack.outside_namespace, stack.outsider)


def without_run_as_non_root(pod: dict[str, Any]) -> dict[str, Any]:
    stripped = copy.deepcopy(pod)
    stripped["spec"].get("securityContext", {}).pop("runAsNonRoot", None)
    for container in stripped["spec"]["containers"]:
        container.get("securityContext", {}).pop("runAsNonRoot", None)
    return stripped


def create(
    cluster: Cluster, namespace: str, pod: dict[str, Any], tags: Tags, *flags: str
) -> subprocess.CompletedProcess[str]:
    return cluster.kubectl(
        "create", "-n", namespace, "-f", "-", *flags,
        input=tags.render(json.dumps(pod)), check=False,
    )  # fmt: skip


def test_every_pod_passes_the_restricted_standard(cluster: Cluster, tags: Tags) -> None:
    (template,) = documents(MANIFESTS / "namespace.yaml")
    version = template["metadata"]["labels"]["pod-security.kubernetes.io/enforce-version"]
    refusal = f'violates PodSecurity "restricted:{version}"'
    workloads = [
        p.name for p in MANIFEST_FILES if any(d["kind"] in WORKLOAD_KINDS for d in documents(p))
    ]
    assert len(workloads) == 6, workloads
    with cluster.namespace(restricted=True) as namespace:
        wait_default_account(cluster, namespace)
        cluster.apply(namespace, ("service-accounts.yaml",), tags)
        # The agent's pod, bare, with no Deployment around it: refused at create without
        # runAsNonRoot, naming the standard and the field; admitted as shipped.
        agent = manifest_pod("agent.yaml", "bare-agent")
        agent["metadata"]["labels"] = {"app.kubernetes.io/name": "bare-agent"}
        refused = create(cluster, namespace, without_run_as_non_root(agent), tags)
        assert refused.returncode != 0, "a pod without runAsNonRoot was admitted"
        assert refusal in refused.stderr and "runAsNonRoot != true" in refused.stderr, (
            refused.stderr
        )
        admitted = create(cluster, namespace, agent, tags)
        assert admitted.returncode == 0, admitted.stderr
        assert "Warning" not in admitted.stderr, admitted.stderr
        cluster.kubectl("delete", "pod", "bare-agent", "-n", namespace, "--wait=false")
        # Every other pod the manifests make, judged the same way on a server-side dry run.
        for file in workloads:
            pod = manifest_pod(file, "bare")
            pod["metadata"]["labels"] = {"app.kubernetes.io/name": "bare"}
            judged = create(cluster, namespace, pod, tags, "--dry-run=server")
            assert judged.returncode == 0 and "Warning" not in judged.stderr, (file, judged.stderr)
            stripped = create(
                cluster, namespace, without_run_as_non_root(pod), tags, "--dry-run=server"
            )
            assert stripped.returncode != 0, f"{file}: admitted without runAsNonRoot"
            assert refusal in stripped.stderr, (file, stripped.stderr)
    deleted(cluster, namespace)


def egress_log(cluster: Cluster, namespace: str, pod: str) -> list[dict[str, Any]]:
    text = cluster.kubectl("exec", pod, "-n", namespace, "--", "cat", EGRESS_LOG).stdout
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_the_agents_pods_connect_only_to_the_control_service_and_the_model(
    cluster: Cluster, tags: Tags, tmp_path: Path
) -> None:
    with cluster.namespace(restricted=True) as namespace:
        tested = run_job(cluster, namespace, tags, tmp_path, agent="agent-egress.yaml")
        assert tested.exit_code == 0, f"exit {tested.exit_code}; the runner's log is {tested.log}"
        result = read_records(cluster, namespace).result
        control = (service_ip(cluster, namespace, "scrimmage"), CONTROL_PORT)
        model = (service_ip(cluster, namespace, "fake-model"), 8080)
        to_control = 0
        for agent in tested.agents:
            records = egress_log(cluster, namespace, agent)
            # Positive controls: the hook saw this replica's control requests and its model
            # calls, and it was in place before Scrimmage's startup file in every process.
            connects = [tuple(r["address"]) for r in records if r["event"] == "connect"]
            assert connects.count(model) >= 1, (agent, connects[:5])
            assert connects.count(control) >= 1, (agent, connects[:5])
            to_control += connects.count(control)
            starts = [r for r in records if r["event"] == "start"]
            assert len(starts) >= 5, starts  # scrimmage-run, uvicorn, the tracker, two workers
            late = [s for s in starts if s["scrimmage_core"] or s["sitecustomize"]]
            assert late == [], f"the hook came after Scrimmage's startup file: {late}"
            # Nothing else: every address, every name, every child.
            problems = harness.egress_problems(
                records, {control, model}, {"scrimmage", "fake-model"}, harness.NON_PYTHON
            )
            assert problems == [], agent
            # A child started below subprocess leaves no line of its own, so every process
            # alive in the pod must have written a start line.
            out = cluster.kubectl(
                "exec", agent, "-n", namespace, "--", "sh", "-c", harness.LIVE_PIDS
            ).stdout
            alive = harness.parse_live_pids(out)
            assert alive and alive <= {r["pid"] for r in starts}, (agent, alive)
        assert to_control >= result["claims_received"] > 0
    deleted(cluster, namespace)


def test_scrimmage_path_in_the_agent_image_names_a_folder_there(tags: Tags) -> None:
    said = harness.sh(
        ["docker", "run", "--rm", "--network", "none", tags.agent, "scrimmage", "path"],
        timeout=120,
    ).stdout
    assert said.endswith("\n") and said.count("\n") == 1, f"one line: {said!r}"
    folder = said.strip()
    assert folder.startswith("/") and folder.endswith("/scrimmage_core/_boot"), folder
    # In that image, the folder holds Scrimmage's startup file, so PYTHONPATH can name it.
    check = harness.sh(
        [
            "docker", "run", "--rm", "--network", "none", tags.agent,
            "test", "-f", f"{folder}/sitecustomize.py",
        ],
        timeout=120,
        check=False,
    )  # fmt: skip
    assert check.returncode == 0, f"no sitecustomize.py in {folder} in {tags.agent}"


def pythonpath_of(deployment: dict[str, Any]) -> tuple[list[str], str]:
    (container,) = deployment["spec"]["template"]["spec"]["containers"]
    env = {e["name"]: e.get("value") for e in container.get("env", [])}
    return container["command"], env.get("PYTHONPATH", "")


def test_a_deployment_that_sets_pythonpath_itself_runs_the_same_test(
    cluster: Cluster, tags: Tags, tmp_path: Path
) -> None:
    with cluster.namespace(restricted=True) as namespace:
        tested = run_job(cluster, namespace, tags, tmp_path, agent="agent-pythonpath.yaml")
        # First, since a test that stopped at the probe writes no result.json to read.
        assert tested.exit_code == 0, f"exit {tested.exit_code}; the runner's log is {tested.log}"
        records = read_records(cluster, namespace)
        result = records.result
        details = run_details(result)
        assert len(details) == 60
        # The plan each run was served under is the runner's: the service's Scrimmage claimed
        # every run and digested the plan its run state held.
        for run in details:
            summary = records.summaries[run]
            assert re.fullmatch(r"[0-9a-f]{64}", str(summary["plan_digest"])), (run, summary)
            assert summary["claimed_plan_digest"] == summary["plan_digest"], (run, summary)
        served = {records.summaries[run]["claimed_by"]["host"] for run in details}
        assert served == set(tested.agents) and len(served) == 2, served
        assert row_outcomes(result) == EXAMPLE_OUTCOMES
        # The fake model answers by whether a tool result starts with "error", so outcomes
        # alone would pass with no mock applied. Each run's own get_order line shows the gate
        # served the row's mock or its fault.
        rows = {d["run"]: row["row"] for row in result["rows"] for d in row["run_details"]}
        for run, row in rows.items():
            calls = [x for x in records.lines if x.get("run") == run and x.get("call")]
            orders = [x for x in calls if x["call"] == "tool:get_order"]
            assert orders, (run, [x["call"] for x in calls])
            if row == "baseline":
                assert {(x["action"], x["result"]["preview"]) for x in orders} == {
                    ("mock", "shipped, Oct 30")
                }, (run, orders)
            else:
                assert {(x["action"], x.get("fault", {}).get("do")) for x in orders} == {
                    ("fault", "timeout")
                }, (run, orders)
        # And this Deployment really is the one without scrimmage-run, with the folder the
        # image's own `scrimmage path` prints first on its PYTHONPATH.
        deployment = cluster.json("get", "deployment", "acme-agent", "-n", namespace)
        command, pythonpath = pythonpath_of(deployment)
        assert command[0] == "uvicorn", command
        said = harness.sh(
            ["docker", "run", "--rm", "--network", "none", tags.agent, "scrimmage", "path"],
            timeout=120,
        ).stdout.strip()
        assert pythonpath.split(":")[0] == said, (pythonpath, said)
    deleted(cluster, namespace)
