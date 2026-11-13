"""The kind harness's own rules, with no cluster: what it refuses, and what it never touches."""

import tempfile
import tomllib
from pathlib import Path

import pytest

from k8s import cluster as kubernetes
from k8s.cluster import Cluster, ClusterError

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
