"""The kind harness's own rules, with no cluster: what it refuses, and what it never touches."""

import json
import subprocess
import tempfile
import tomllib
from pathlib import Path
from typing import Any

import pytest

from compose import harness
from k8s import cluster as kubernetes
from k8s.cluster import Cluster, ClusterError
from k8s.workloads import MANIFEST_FILES, WORKLOAD_KINDS, documents, pod_spec
from scrimmage.config import load as load_config

HERE = Path(kubernetes.__file__).parent


def test_an_older_kind_is_refused_naming_the_pinned_one() -> None:
    with pytest.raises(ClusterError, match=r"v0\.33\.0"):
        kubernetes.require_kind_version("kind v0.26.0 go1.23 linux/amd64")
    kubernetes.require_kind_version("kind v0.33.0 go1.26 linux/amd64")
    kubernetes.require_kind_version("kind v0.34.2 go1.26 linux/amd64")


def test_the_node_image_is_the_same_in_the_config_and_in_the_pins() -> None:
    pinned = tomllib.loads((HERE / "versions.toml").read_text())["node"]["image"]
    assert f"image: {pinned}" in (HERE / "kind.yaml").read_text()
    assert "@sha256:" in pinned, "the node image is pinned by digest"


def test_a_host_route_inside_the_pod_range_is_named() -> None:
    routes = [{"dst": "default", "dev": "eth0"}, {"dst": "192.168.5.0/24", "dev": "wg0"}]
    assert kubernetes.route_overlap(routes) == "192.168.5.0/24 (dev wg0)"
    assert kubernetes.route_overlap([{"dst": "default"}, {"dst": "172.18.0.0/16"}]) is None


def test_the_lane_names_the_cluster_and_a_bad_lane_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SCRIMMAGE_LANE", raising=False)
    assert kubernetes.cluster_name() == "scrimmage-1"
    monkeypatch.setenv("SCRIMMAGE_LANE", "2")
    assert kubernetes.cluster_name() == "scrimmage-2"
    monkeypatch.setenv("SCRIMMAGE_LANE", "../x")
    with pytest.raises(ClusterError, match="SCRIMMAGE_LANE"):
        kubernetes.lane()


def test_a_cluster_that_is_not_ours_makes_the_memory_guard_fail_and_it_is_never_stopped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(kubernetes, "memory_available_kb", lambda: 1024 * 1024)  # 1 GB
    monkeypatch.setattr(kubernetes, "running_clusters", lambda: ["aiopslab", "scrimmage-3"])
    calls: list[list[str]] = []

    def no_command(command: list[str], **_: object) -> None:
        calls.append(command)

    monkeypatch.setattr(kubernetes, "run", no_command)
    with pytest.raises(ClusterError, match="aiopslab"):
        Cluster("2").reserve_room()
    assert calls == [], "the guard must fail, never stop or delete a cluster"


def test_enough_memory_lets_a_second_lane_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(kubernetes, "memory_available_kb", lambda: 8 * 1024 * 1024)
    monkeypatch.setattr(kubernetes, "running_clusters", lambda: ["aiopslab", "scrimmage-3"])
    Cluster("2").reserve_room()


def test_two_lanes_make_scratch_clusters_of_different_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCRIMMAGE_LANE", "2")
    two = kubernetes.scratch_name("kc")
    monkeypatch.setenv("SCRIMMAGE_LANE", "3")
    three = kubernetes.scratch_name("kc")
    assert two != three
    assert "-" in two, "a lane cannot hold a hyphen, so no lane's own cluster has this name"
    assert not kubernetes.is_ours(
        kubernetes.cluster_name(two)
    )  # lane 3's session never counts lane 2's scratch as its own
    monkeypatch.setenv("SCRIMMAGE_LANE", "2")
    assert kubernetes.is_ours(kubernetes.cluster_name(two))
    assert kubernetes.is_ours("scrimmage-2")
    assert not kubernetes.is_ours("scrimmage-22")


def test_create_and_delete_name_the_session_kubeconfig(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    seen: list[tuple[list[str], dict[str, str]]] = []

    def record(command: list[str], **kwargs: object) -> None:
        env = kwargs["env"]
        assert isinstance(env, dict)
        seen.append((command, env))

    monkeypatch.setattr(kubernetes, "run", record)
    cluster = Cluster("2")
    cluster.create()
    cluster.delete()
    assert len(seen) == 2
    for command, env in seen:
        at = command.index("--kubeconfig")
        assert command[at + 1] == str(cluster.kubeconfig)
        assert env["KUBECONFIG"] == str(cluster.kubeconfig)
        assert env["KIND_EXPERIMENTAL_DOCKER_NETWORK"] == "scrimmage-kind"


def test_cleanup_never_removes_the_network_named_kind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    calls: list[list[str]] = []

    def record(command: list[str], **_: object) -> None:
        calls.append(command)

    monkeypatch.setattr(kubernetes, "run", record)
    with pytest.raises(ClusterError, match="kind"):
        kubernetes.remove_network("kind")
    assert calls == []
    monkeypatch.setattr(kubernetes, "running_clusters", lambda: [])
    kubernetes.remove_network_if_unused()
    assert calls == [["docker", "network", "rm", "scrimmage-kind"]]
    calls.clear()
    monkeypatch.setattr(kubernetes, "running_clusters", lambda: ["scrimmage-3"])
    kubernetes.remove_network_if_unused()
    assert calls == [], "another lane's cluster is still on the network"


def test_a_session_does_not_wait_for_its_own_scratch_cluster(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setenv("SCRIMMAGE_LANE", "2")
    monkeypatch.setattr(kubernetes, "memory_available_kb", lambda: 1024 * 1024)
    monkeypatch.setattr(kubernetes, "running_clusters", lambda: ["scrimmage-2-kp", "scrimmage-2"])
    Cluster("2-mn").reserve_room()  # returns at once; waiting would hang the test


@pytest.mark.slow  # it runs the real scrimmage cert
def test_control_secrets_runs_scrimmage_cert_then_two_kubectl_creates_and_removes_the_folder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """scrimmage cert really runs; each kubectl call is recorded with what the folder held."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    real = kubernetes.run
    calls: list[tuple[list[str], list[str]]] = []
    cluster = Cluster("2")
    out = cluster.folder / "cert-ns1"

    def recording(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        held = sorted(p.name for p in out.iterdir()) if out.exists() else []
        calls.append((command, held))
        if command[0] == "kubectl":
            return subprocess.CompletedProcess(command, 0, "", "")
        return real(command, **kwargs)

    monkeypatch.setattr(kubernetes, "run", recording)
    cluster.control_secrets("ns1")
    names = ["scrimmage", "scrimmage.ns1.svc", "scrimmage.ns1.svc.cluster.local"]
    assert [c for c, _ in calls] == [
        [
            kubernetes.scrimmage_command(), "cert",
            *(arg for name in names for arg in ("--name", name)),
            "--days", "1", "--out", str(out),
        ],
        [
            "kubectl", "create", "secret", "tls", "scrimmage-control-tls", "-n", "ns1",
            "--cert", str(out / "server.pem"), "--key", str(out / "server.key"),
        ],
        [
            "kubectl", "create", "secret", "generic", "scrimmage-ca-cert", "-n", "ns1",
            f"--from-file=ca.crt={out / 'ca.pem'}",
        ],
    ]  # fmt: skip
    # The two creates read the three files scrimmage cert wrote, and then the folder is gone.
    assert [held for _, held in calls[1:]] == [["ca.pem", "server.key", "server.pem"]] * 2
    assert not out.exists(), "the folder with the server's key was left on disk"
    assert cluster.folder.is_dir(), "only the certificate folder goes, not the session's"


def test_a_failed_create_still_removes_the_certificate_folder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    cluster = Cluster("2")

    def refusing(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if command[0] == "kubectl":
            raise ClusterError("kubectl create secret exited 1")
        out = Path(command[command.index("--out") + 1])  # scrimmage cert's folder, faked
        out.mkdir()
        (out / "server.key").write_text("key")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(kubernetes, "run", refusing)
    with pytest.raises(ClusterError, match="exited 1"):
        cluster.control_secrets("ns1")
    assert not (cluster.folder / "cert-ns1").exists()


# The committed manifests, read without a cluster.


def containers() -> list[tuple[str, dict[str, Any]]]:
    found: list[tuple[str, dict[str, Any]]] = []
    for path in MANIFEST_FILES:
        for document in documents(path):
            if document["kind"] in WORKLOAD_KINDS:
                name = f"{path.name}/{document['metadata']['name']}"
                found += [(name, c) for c in pod_spec(document)["containers"]]
    return found


def test_the_manifests_are_the_files_the_harness_applies() -> None:
    found = {(d["kind"], d["metadata"]["name"]) for p in MANIFEST_FILES for d in documents(p)}
    assert {
        ("Deployment", "acme-agent"),
        ("Deployment", "fake-model"),
        ("Job", "scrimmage-test"),
        ("Pod", "reader"),
    } <= found
    applied = {*kubernetes.FOUNDATION, *kubernetes.WORKLOADS, "runner-job.yaml", "reader.yaml"}
    # Two more Deployments of acme-agent, each applied in place of agent.yaml by one test.
    in_place = {"agent-pythonpath.yaml", "agent-egress.yaml"}
    assert applied | in_place | {"namespace.yaml"} == {p.name for p in MANIFEST_FILES}


def test_every_container_has_resource_requests_and_a_memory_limit() -> None:
    found = containers()
    assert len(found) == 6, "three agents, runner, fake model and reader"
    for name, container in found:
        resources = container.get("resources", {})
        assert {"cpu", "memory"} <= set(resources.get("requests", {})), f"{name}: no requests"
        assert "memory" in resources.get("limits", {}), f"{name}: no memory limit"


def test_every_image_is_a_tag_the_harness_fills_and_is_never_pulled() -> None:
    tags = kubernetes.Tags(
        "scrimmage-runner:a1",
        "scrimmage-compose-service:b2",
        "scrimmage-fake-model:c3",
        "scrimmage-compose-egress:d4",
    )
    for name, container in containers():
        assert container["image"].endswith(":TAG"), f"{name}: {container['image']}"
        assert container["imagePullPolicy"] == "Never", f"{name} may pull a same-named image"
        assert tags.render(container["image"]).split(":")[1] in ("a1", "b2", "c3", "d4"), name


def test_the_manifests_hold_what_pod_security_does_not_check() -> None:
    """The restricted standard asks none of these, so a manifest could drop one unnoticed."""
    specs: list[tuple[str, dict[str, Any]]] = []
    for path in MANIFEST_FILES:
        for document in documents(path):
            name = f"{path.name}/{document['metadata']['name']}"
            if document["kind"] == "ServiceAccount":
                assert document["automountServiceAccountToken"] is False, name
            elif document["kind"] in WORKLOAD_KINDS:
                specs.append((name, pod_spec(document)))
    assert len(specs) == 6
    for name, spec in specs:
        assert spec["automountServiceAccountToken"] is False, f"{name}: mounts an API token"
        for container in spec["containers"]:
            assert container["securityContext"]["readOnlyRootFilesystem"] is True, name
        secrets = [v for v in spec.get("volumes", []) if "secret" in v]
        if secrets:
            assert spec["securityContext"].get("fsGroup"), f"{name}: no fsGroup for its Secrets"
    (agent,) = (s for n, s in specs if n.startswith("agent.yaml"))
    ca = next(v["secret"] for v in agent["volumes"] if v["name"] == "ca")
    assert ca["secretName"] == "scrimmage-ca-cert"
    assert ca["items"] == [{"key": "ca.crt", "path": "ca.pem"}], "only the authority, never a key"


def test_the_namespace_enforces_warns_and_audits_the_restricted_standard() -> None:
    (namespace,) = documents(HERE / "manifests" / "namespace.yaml")
    labels = namespace["metadata"]["labels"]
    image = tomllib.loads((HERE / "versions.toml").read_text())["node"]["image"]
    minor = "v" + ".".join(image.split(":")[1].split("@")[0].lstrip("v").split(".")[:2])
    for mode in ("enforce", "warn", "audit"):
        assert labels[f"pod-security.kubernetes.io/{mode}"] == "restricted"
        assert labels[f"pod-security.kubernetes.io/{mode}-version"] == minor, "the cluster's minor"


def test_the_jobs_file_is_valid_and_its_tests_are_the_examples() -> None:
    config = load_config(HERE / "scrimmage.toml", {"POD_IP": "10.0.0.5"})
    assert config.agent.service == "http://acme-agent:8000/chat"
    assert config.agent.control == "10.0.0.5:7700"
    assert [m.host for m in config.agent.model_hosts] == ["fake-model"]
    example = (harness.EXAMPLE / "scrimmage.toml").read_text()
    committed = (HERE / "scrimmage.toml").read_text()
    assert committed.endswith(example[example.index("[[test]]") :])


def test_a_crd_with_no_conditions_yet_is_waited_for_not_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The Calico CRD flake: right after the operator makes its CRD, the object has no
    status.conditions. That counts as not yet established, and the next look finds it."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    replies = [
        None,  # not found yet
        {"metadata": {"name": "x"}},  # stored, no status at all
        {"status": {"conditions": None}},  # the reply the flake met
        {"status": {"conditions": [{"type": "NamesAccepted", "status": "True"}]}},
        {"status": {"conditions": [{"type": "Established", "status": "True"}]}},
    ]
    asked: list[list[str]] = []

    def kubectl_get(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        asked.append(command)
        reply = replies[len(asked) - 1]
        code = 1 if reply is None else 0
        return subprocess.CompletedProcess(command, code, json.dumps(reply or {}), "")

    monkeypatch.setattr(kubernetes, "run", kubectl_get)
    monkeypatch.setattr(kubernetes.time, "sleep", lambda _: None)
    cluster = Cluster("2")
    cluster.wait_until(
        "the CRD", lambda: cluster.crd_established("installations.operator.tigera.io"), timeout=60
    )
    assert len(asked) == len(replies)
    assert all(c[:3] == ["kubectl", "get", "crd"] for c in asked), asked
