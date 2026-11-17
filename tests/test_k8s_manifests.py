"""The committed manifests on a real cluster: the namespace's Pod Security labels hold, the agent
and the fake model start under them, a pod that breaks the standard fails fast, and the token
never reaches a command line. The Job against two replicas is another file's.

Like the cluster tests, these run only with SCRIMMAGE_KIND=1, one file at a time:

    SCRIMMAGE_KIND=1 SCRIMMAGE_LANE=2 test-slot uv run pytest -n 0 -m kind \
        tests/test_k8s_manifests.py
"""

import base64
import json
import subprocess
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from k8s import cluster as kubernetes
from k8s import images
from k8s.cluster import Cluster, ClusterError, Tags
from k8s.workloads import wait_default_account

pytestmark = [pytest.mark.slow, pytest.mark.kind]

HERE = Path(kubernetes.__file__).parent
LABELS = {"app.kubernetes.io/name": "broken"}
RESTRICTED = {
    "runAsNonRoot": True,
    "runAsUser": 10001,
    "seccompProfile": {"type": "RuntimeDefault"},
}


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


def container(**security: Any) -> dict[str, Any]:
    return {
        "name": "c",
        "image": "scrimmage-runner:none",
        "imagePullPolicy": "Never",
        "command": ["sleep", "infinity"],
        "securityContext": security,
    }


def pod(name: str, *, shipped: bool, volumes: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """A bare Pod: with `shipped` it keeps every field the standard asks for, without it only
    the container's privilege flag is set the way a careless manifest sets it."""
    good = {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}
    spec: dict[str, Any] = {
        "containers": [container(**(good if shipped else {"privileged": True}))],
    }
    if shipped:
        spec["securityContext"] = RESTRICTED
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "labels": LABELS},
        "spec": spec | ({"volumes": volumes} if volumes else {}),
    }


def deployment(name: str, template: dict[str, Any]) -> dict[str, Any]:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": LABELS},
            "template": {"metadata": {"labels": LABELS}, "spec": template["spec"]},
        },
    }


def apply_json(cluster: Cluster, namespace: str, document: dict[str, Any], *flags: str) -> Any:
    return cluster.kubectl(
        "apply", "-n", namespace, "-f", "-", *flags, input=json.dumps(document), check=False
    )


def test_every_manifest_applies_with_no_pod_security_warning(cluster: Cluster, tags: Tags) -> None:
    with cluster.namespace(restricted=True) as namespace:
        wait_default_account(cluster, namespace)
        shipped = sorted(p.name for p in (HERE / "manifests").glob("*.yaml"))
        shipped.remove("namespace.yaml")  # a cluster-scoped object, applied to make this one
        assert len(shipped) == 10
        # Admission runs on a server-side dry run, so every file is judged and none is made.
        # The accounts are made for real first: a pod naming one that is missing is refused.
        cluster.apply(namespace, ("service-accounts.yaml",), tags)
        for name in shipped:
            warnings = cluster.apply(namespace, (name,), tags, dry_run=True)
            assert "Warning" not in warnings, f"{name}:\n{warnings}"
        # Positive controls: the labels do refuse and warn. Without them the loop above would
        # pass on a namespace that checks nothing.
        careless = pod("careless", shipped=False)
        refused = apply_json(cluster, namespace, careless, "--dry-run=server")
        assert refused.returncode != 0, "a pod that breaks the standard was admitted"
        assert 'violates PodSecurity "restricted:' in refused.stderr, refused.stderr
        warned = apply_json(
            cluster, namespace, deployment("careless", careless), "--dry-run=server"
        )
        assert warned.returncode == 0, "enforce does not refuse the Deployment, only its pods"
        assert "Warning: would violate PodSecurity" in warned.stderr, warned.stderr
        # A Deployment warns under `enforce` alone, so the `warn` label is seen by itself: a
        # namespace with only it must warn, and admit the careless pod it does not enforce on.
        with cluster.namespace(restricted=True, modes=("warn",)) as warn_only:
            wait_default_account(cluster, warn_only)
            alone = apply_json(cluster, warn_only, careless, "--dry-run=server")
            assert alone.returncode == 0, "warn must not refuse"
            assert "Warning: would violate PodSecurity" in alone.stderr, alone.stderr
        # And the same pod as shipped, with every field the standard asks for, is admitted.
        kept = apply_json(cluster, namespace, pod("kept", shipped=True), "--dry-run=server")
        assert kept.returncode == 0 and "Warning" not in kept.stderr, kept.stderr


def test_the_agent_and_the_fake_model_become_ready(cluster: Cluster, tags: Tags) -> None:
    with cluster.namespace(restricted=True) as namespace:
        wait_default_account(cluster, namespace)
        warnings = cluster.deploy(namespace, tags)
        assert "Warning" not in warnings, warnings
        cluster.wait_ready(namespace, "fake-model", "acme-agent")
        for secret, keys in (
            (kubernetes.CONTROL_SECRET, ["tls.crt", "tls.key"]),
            (kubernetes.CA_SECRET, ["ca.crt"]),
        ):
            assert sorted(cluster.json("get", "secret", secret, "-n", namespace)["data"]) == keys
        agents = cluster.json(
            "get", "pods", "-n", namespace, "-l", "app.kubernetes.io/name=acme-agent"
        )
        names = [p["metadata"]["name"] for p in agents["items"]]
        assert len(names) == len(set(names)) == 2, "two replicas, two pods"
        for item in agents["items"]:
            ready = [c["status"] for c in item["status"]["conditions"] if c["type"] == "Ready"]
            assert ready == ["True"], f"{item['metadata']['name']} is not Ready"


def test_the_harness_fails_fast_on_a_refused_pod(cluster: Cluster) -> None:
    with cluster.namespace(restricted=True) as namespace:
        wait_default_account(cluster, namespace)
        # The Deployment is admitted, as the standard's `warn` shows; its pod is refused later.
        made = apply_json(cluster, namespace, deployment("broken", pod("p", shipped=False)))
        assert made.returncode == 0, made.stderr
        started = time.monotonic()
        with pytest.raises(ClusterError) as raised:
            cluster.wait_ready(namespace, "broken", timeout=120)
        seconds = time.monotonic() - started
        assert seconds < 10, f"the wait ran {seconds:.0f} s before it failed"
        assert "FailedCreate" in str(raised.value)
        assert "violates PodSecurity" in str(raised.value), "the event's own message is named"


def test_the_token_secret_comes_from_stdin(
    cluster: Cluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[tuple[list[str], str | None]] = []
    real = kubernetes.run

    def watching(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append((command, kwargs.get("input")))
        return real(command, **kwargs)

    monkeypatch.setattr(kubernetes, "run", watching)
    with cluster.namespace(restricted=True) as namespace:
        token = cluster.create_token_secret(namespace)
        cluster.control_secrets(namespace)
        stored = cluster.kubectl(
            "get", "secret", "scrimmage-token", "-n", namespace,
            "-o", "jsonpath={.data.token}",
        ).stdout  # fmt: skip
        assert len(token) == 64
        assert base64.b64decode(stored).decode() == token, "the Secret holds the token"
        for name in ("scrimmage-token", "scrimmage-control-tls", "scrimmage-ca-cert"):
            found = cluster.json("get", "secret", name, "-n", namespace)
            notes = found["metadata"].get("annotations", {})
            assert "kubectl.kubernetes.io/last-applied-configuration" not in notes, (
                f"{name} carries its own content in an annotation"
            )
    for command, _ in commands:
        assert token not in " ".join(command), f"the token is on a command line: {command[:6]}"
    # Positive control: the watcher does see the token, on one command's standard input.
    on_stdin = [c[:3] for c, i in commands if i and token in i]
    assert on_stdin == [["kubectl", "apply", "--server-side"]]


def event(namespace: str, name: str, owner: dict[str, str], when: str) -> dict[str, Any]:
    """A FailedCreate event about `owner`, a ReplicaSet named and, if it has one, with its uid."""
    return {
        "apiVersion": "v1",
        "kind": "Event",
        "metadata": {"name": name, "namespace": namespace},
        "type": "Warning",
        "reason": "FailedCreate",
        "message": f"Error creating: planted {name}",
        "involvedObject": {"kind": "ReplicaSet", "namespace": namespace, "apiVersion": "apps/v1"}
        | owner,
        "firstTimestamp": when,
        "lastTimestamp": when,
        "count": 1,
    }


def test_a_stale_failure_event_does_not_fail_a_healthy_wait(cluster: Cluster, tags: Tags) -> None:
    healthy = pod("healthy", shipped=True)
    healthy["spec"]["containers"][0]["image"] = tags.runner
    watched = (("deployment", "healthy"),)
    with cluster.namespace(restricted=True) as namespace:
        wait_default_account(cluster, namespace)
        made = apply_json(cluster, namespace, deployment("healthy", healthy))
        assert made.returncode == 0, made.stderr
        cluster.wait_ready(namespace, "healthy", timeout=120)
        # A rollout: the first ReplicaSet is now an earlier one, its failure long over.
        healthy["spec"]["containers"][0]["env"] = [{"name": "ROLLOUT", "value": "2"}]
        assert apply_json(cluster, namespace, deployment("healthy", healthy)).returncode == 0
        cluster.wait_ready(namespace, "healthy", timeout=120)
        revision = "deployment.kubernetes.io/revision"
        sets = sorted(
            cluster.json("get", "replicasets", "-n", namespace)["items"],
            key=lambda r: int(r["metadata"]["annotations"][revision]),
        )
        old, current = sets[0]["metadata"], sets[-1]["metadata"]
        assert old["name"] != current["name"], "the rollout made no second ReplicaSet"
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")  # after the rollout, so recent
        # Recent events, none about the Deployment's current ReplicaSet or its pods.
        for planted in (
            event(namespace, "g1-unrelated", {"name": "gone-long-ago"}, now),
            event(namespace, "g1-lookalike", {"name": "healthy-old-7d9f", "uid": "x-1"}, now),
            event(namespace, "g1-rollout", {"name": old["name"], "uid": old["uid"]}, now),
        ):
            assert apply_json(cluster, namespace, planted).returncode == 0
        assert cluster.bad_event(namespace, watched) is None
        cluster.kubectl("scale", "deployment", "healthy", "-n", namespace, "--replicas=2")
        cluster.wait_ready(namespace, "healthy", timeout=120)
        # An unscoped wait: the reader's, with the stale events still there.
        cluster.apply(namespace, ("service-accounts.yaml", "records-pvc.yaml"), tags)
        cluster.start_reader(namespace, tags)
        # Positive control: an event about the current ReplicaSet does fail the wait.
        about = {"name": current["name"], "uid": current["uid"]}
        assert (
            apply_json(
                cluster,
                namespace,
                event(namespace, "g1-now", about, datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")),
            ).returncode
            == 0
        )
        assert "planted g1-now" in str(cluster.bad_event(namespace, watched))


def test_the_reader_pod_reads_the_records_volume(cluster: Cluster, tags: Tags) -> None:
    writer = pod("writer", shipped=True)
    writer["spec"]["restartPolicy"] = "Never"
    writer["spec"]["securityContext"] = RESTRICTED | {"runAsGroup": 10001}
    writer["spec"]["terminationGracePeriodSeconds"] = 0
    writer["spec"]["containers"][0].update(
        image=tags.runner,
        command=[
            "python3",
            "-c",
            "import os; d = '/records/scrimmage-runs'; os.makedirs(d, mode=0o700, exist_ok=True);"
            "open(d + '/note.txt', 'w').write('written by a pod that is gone')",
        ],
        volumeMounts=[{"name": "records", "mountPath": "/records"}],
    )
    writer["spec"]["volumes"] = [
        {"name": "records", "persistentVolumeClaim": {"claimName": "scrimmage-records"}}
    ]
    with cluster.namespace(restricted=True) as namespace:
        wait_default_account(cluster, namespace)
        cluster.apply(namespace, ("service-accounts.yaml", "records-pvc.yaml"), tags)
        made = apply_json(cluster, namespace, writer)
        assert made.returncode == 0, made.stderr
        cluster.wait_until(
            "the writer to end",
            lambda: cluster.exit_code(namespace, "writer") is not None,
            timeout=120,
            namespace=namespace,
            watch=(("pod", "writer"),),
        )
        assert cluster.exit_code(namespace, "writer") == 0
        cluster.kubectl("delete", "pod", "writer", "-n", namespace, "--wait=true")
        cluster.start_reader(namespace, tags)
        text = cluster.reader_exec(namespace, "cat", "/records/scrimmage-runs/note.txt")
        assert text.stdout == "written by a pod that is gone"
        # The volume is mounted read-only in the reader, so it can change nothing.
        blocked = cluster.reader_exec(
            namespace, "touch", "/records/scrimmage-runs/more.txt", check=False
        )
        assert blocked.returncode != 0
        assert "Read-only file system" in blocked.stderr
