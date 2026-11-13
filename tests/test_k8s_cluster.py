"""The kind test cluster: made from the committed config, Calico enforcing policy, cert-manager
issuing a certificate the SDK accepts, and nothing left behind on the user's machine.

These need Docker and a kind cluster, which takes minutes, so they run only with
SCRIMMAGE_KIND=1, one file at a time:

    SCRIMMAGE_KIND=1 SCRIMMAGE_LANE=2 test-slot uv run pytest -n 0 -m kind tests/test_k8s_cluster.py

With the variable set, a missing tool fails a test, never skips it.
"""

import base64
import contextlib
import hashlib
import json
import os
import ssl
import tomllib
from collections.abc import Iterator
from pathlib import Path

import pytest

from k8s import cluster as kubernetes
from k8s import images
from k8s.cluster import Cluster
from scrimmage_core import tls

pytestmark = [pytest.mark.slow, pytest.mark.kind]

HERE = Path(kubernetes.__file__).parent
DIALER = (
    "import socket, sys\n"
    "try:\n"
    "    socket.create_connection((sys.argv[1], 8080), timeout=float(sys.argv[2])).close()\n"
    "    print('connected')\n"
    "except Exception as exc:\n"
    "    print(type(exc).__name__)\n"
)
SERVER = (
    "import http.server\n"
    "class H(http.server.BaseHTTPRequestHandler):\n"
    "    def do_GET(self):\n"
    "        self.send_response(200); self.end_headers(); self.wfile.write(b'ok')\n"
    "http.server.ThreadingHTTPServer(('', 8080), H).serve_forever()\n"
)


def user_files() -> list[Path]:
    """The files kind, kubectl and Helm would write if they ignored the session's folder."""
    home = Path.home()
    return [
        home / ".kube" / "config",
        home / ".kube" / "cache",
        home / ".cache" / "helm",
        home / ".config" / "helm",
        home / ".local" / "share" / "helm",
    ]


def snapshot(paths: list[Path]) -> dict[str, tuple[str, int]]:
    """Each file under the paths: its hash and its modification time."""
    seen: dict[str, tuple[str, int]] = {}
    for path in paths:
        files = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
        for file in files:
            digest = hashlib.sha256(file.read_bytes()).hexdigest()
            seen[str(file)] = (digest, file.stat().st_mtime_ns)
    return seen


@pytest.fixture(scope="module")
def runner_image() -> str:
    return images.ensure_runner_image()


@pytest.fixture(scope="module")
def cluster(runner_image: str) -> Iterator[Cluster]:
    with kubernetes.session((runner_image,)) as made:
        yield made


def pod_json(name: str, image: str, script: str, *args: str, label: str) -> str:
    return json.dumps(
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": name, "labels": {"app": label}},
            "spec": {
                "terminationGracePeriodSeconds": 0,
                "containers": [
                    {
                        "name": "c",
                        "image": image,
                        "imagePullPolicy": "Never",
                        "command": ["python3", "-c", script, *args],
                    }
                ],
            },
        }
    )


def test_the_cluster_comes_up_from_the_committed_config_with_every_node_ready(
    cluster: Cluster,
) -> None:
    nodes = cluster.json("get", "nodes")["items"]
    assert len(nodes) == 1, "kind.yaml names one node"
    for node in nodes:
        ready = [c["status"] for c in node["status"]["conditions"] if c["type"] == "Ready"]
        assert ready == ["True"], f"{node['metadata']['name']} is not Ready"
    # The node runs the image kind.yaml names, and versions.toml names the same one.
    config = (HERE / "kind.yaml").read_text()
    pinned = tomllib.loads((HERE / "versions.toml").read_text())["node"]["image"]
    assert pinned in config
    version = pinned.split(":")[1].split("@")[0]
    assert nodes[0]["status"]["nodeInfo"]["kubeletVersion"] == version


def test_calico_is_the_network_plugin(cluster: Cluster) -> None:
    nodes = len(cluster.json("get", "nodes")["items"])
    daemonset = cluster.json("get", "daemonset", "calico-node", "-n", "calico-system")["status"]
    assert daemonset["numberReady"] == daemonset["desiredNumberScheduled"] == nodes
    pool = kubernetes.ipaddress.ip_network(kubernetes.POD_CIDR)
    pods = cluster.json("get", "pods", "-n", "kube-system", "-l", "k8s-app=kube-dns")["items"]
    assert pods, "no CoreDNS pod to read an address from"
    for pod in pods:
        # Only Calico writes this annotation; the plugin kind ships, on the same range, does not.
        annotation = pod["metadata"].get("annotations", {}).get("cni.projectcalico.org/podIP")
        assert annotation, f"{pod['metadata']['name']} has no Calico address annotation"
        address = kubernetes.ipaddress.ip_interface(annotation).ip
        assert str(address) == pod["status"]["podIP"]
        assert address in pool, f"{pod['metadata']['name']} has {address}, outside {pool}"


def pod_ready(cluster: Cluster, namespace: str, name: str) -> bool:
    pod = cluster.json("get", "pod", name, "-n", namespace, check=False)
    conditions = (pod or {}).get("status", {}).get("conditions", [])
    return any(c["type"] == "Ready" and c["status"] == "True" for c in conditions)


def dial(cluster: Cluster, namespace: str, host: str) -> str:
    done = cluster.kubectl(
        "exec", "client", "-n", namespace, "--", "python3", "-c", DIALER, host, "3"
    )
    return done.stdout.strip()


def test_a_network_policy_is_enforced(cluster: Cluster, runner_image: str) -> None:
    with cluster.namespace() as namespace:
        for name, script in (("server", SERVER), ("client", "import time; time.sleep(600)")):
            cluster.kubectl(
                "apply", "-n", namespace, "-f", "-",
                input=pod_json(name, runner_image, script, label=name),
            )  # fmt: skip
        cluster.wait_until(
            "both pods Ready",
            lambda: all(pod_ready(cluster, namespace, n) for n in ("server", "client")),
            timeout=120,
            namespace=namespace,
        )
        server = cluster.json("get", "pod", "server", "-n", namespace)["status"]["podIP"]
        assert dial(cluster, namespace, server) == "connected", "no policy: the first try must pass"
        policy = {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": "deny"},
            "spec": {"podSelector": {"matchLabels": {"app": "server"}}, "policyTypes": ["Ingress"]},
        }
        cluster.kubectl("apply", "-n", namespace, "-f", "-", input=json.dumps(policy))
        seen: list[str] = []

        def blocked() -> bool:
            seen.append(dial(cluster, namespace, server))
            return seen[-1] == "TimeoutError"

        # A few seconds for Calico to program the rule; after that every new connection times out.
        for _ in range(4):
            if blocked():
                break
        else:
            raise AssertionError(f"the deny policy did not hold a new connection: {seen}")
        assert blocked(), "once blocked, a new connection must stay blocked"
        cluster.kubectl("delete", "networkpolicy", "deny", "-n", namespace)
        cluster.wait_until(
            "the policy to go",
            lambda: dial(cluster, namespace, server) == "connected",
            timeout=30,
        )


def handshake(ca: Path, certificate: Path, key: Path) -> str:
    """A TLS handshake between the SDK's client and server contexts, in memory."""
    client = tls.client_context(ca)
    server = tls.server_context(certificate, key)
    to_server, to_client = ssl.MemoryBIO(), ssl.MemoryBIO()
    mine = client.wrap_bio(to_server, to_client, server_hostname="scrimmage")
    theirs = server.wrap_bio(to_client, to_server, server_side=True)
    for _ in range(8):
        for side in (mine, theirs):
            with contextlib.suppress(ssl.SSLWantReadError, ssl.SSLWantWriteError):
                side.do_handshake()
    return mine.version() or ""


def test_cert_manager_issues_a_certificate_the_sdk_accepts(
    cluster: Cluster, tmp_path: Path
) -> None:
    with cluster.namespace() as namespace:
        cluster.issue_control_certificate(namespace)
        runner = cluster.json("get", "secret", "scrimmage-control-tls", "-n", namespace)["data"]
        agent = cluster.json("get", "secret", "scrimmage-ca-cert", "-n", namespace)["data"]
        assert sorted(agent) == ["ca.crt"], "the agent's Secret must never hold a key"
        assert sorted(runner) == ["tls.crt", "tls.key"]
        tmp_path.chmod(0o700)
        ca, certificate, key = (tmp_path / n for n in ("ca.crt", "tls.crt", "tls.key"))
        ca.write_bytes(base64.b64decode(agent["ca.crt"]))
        certificate.write_bytes(base64.b64decode(runner["tls.crt"]))
        key.write_bytes(base64.b64decode(runner["tls.key"]))
        assert ca.read_text().count("BEGIN CERTIFICATE") == 1
        assert handshake(ca, certificate, key) == "TLSv1.3"


def test_the_harness_never_touches_the_users_kubeconfig(tmp_path: Path) -> None:
    before = snapshot(user_files())
    assert before, "no file of the user's to watch: the check would prove nothing"
    cert_manager = kubernetes.PINS["cert-manager"]
    with kubernetes.scratch("kc") as scratch:
        assert snapshot(user_files()) == before, "kind create wrote the user's files"
        scratch.kubectl("get", "nodes")
        scratch.helm(
            "show", "chart", "--repo", cert_manager["repo"], "--version", cert_manager["version"],
            cert_manager["chart"],
        )  # fmt: skip
        assert snapshot(user_files()) == before, "kubectl or helm wrote the user's files"
        # Positive control: kind with no --kubeconfig, in an empty home, does write a file there.
        home = tmp_path / "home"
        home.mkdir()
        env = {**os.environ, "HOME": str(home)}
        env.pop("KUBECONFIG", None)
        kubernetes.run(
            ["kind", "export", "kubeconfig", "--name", scratch.name], timeout=60, env=env
        )
        assert (home / ".kube" / "config").is_file(), "the control did not see a write"
        # A dropped --kubeconfig on create or delete is caught by the fast test
        # test_create_and_delete_name_the_session_kubeconfig in test_k8s_harness.py.
    assert snapshot(user_files()) == before


def test_a_session_deletes_only_its_own_cluster() -> None:
    with kubernetes.scratch("kp") as kept:
        with kubernetes.scratch("mn"):  # a session's start, and its end on leaving
            pass
        assert kept.name in kubernetes.running_clusters(), "another cluster was deleted"
    assert kept.name not in kubernetes.running_clusters()
