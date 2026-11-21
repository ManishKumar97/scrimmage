"""The kind test cluster: made from the committed config, Calico enforcing policy, the control
certificates from `scrimmage cert` by default and from cert-manager as the option, and nothing
left behind on the user's machine.

These need Docker and a kind cluster, which takes minutes, so they run only with
SCRIMMAGE_KIND=1, one file at a time:

    SCRIMMAGE_KIND=1 SCRIMMAGE_LANE=2 uv run pytest -n 0 -m kind tests/test_k8s_cluster.py

With the variable set, a missing tool fails a test, never skips it.
"""

import base64
import contextlib
import hashlib
import ipaddress
import json
import os
import ssl
import subprocess
import time
import tomllib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from k8s import cluster as kubernetes
from k8s import images
from k8s.cluster import Cluster
from scrimmage import tls

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


@pytest.fixture(scope="module")
def cert_manager(cluster: Cluster) -> Cluster:
    """The session's cluster with the cert-manager option installed, for the option's tests."""
    cluster.cert_manager()
    return cluster


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
            watch=(("pod", "server"), ("pod", "client")),
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


def handshake(ca: Path, certificate: Path, key: Path, host: str = "scrimmage") -> str:
    """A TLS handshake between the SDK's client and server contexts, in memory."""
    client = tls.client_context(ca)
    server = tls.server_context(certificate, key)
    to_server, to_client = ssl.MemoryBIO(), ssl.MemoryBIO()
    mine = client.wrap_bio(to_server, to_client, server_hostname=host)
    theirs = server.wrap_bio(to_client, to_server, server_side=True)
    for _ in range(8):
        for side in (mine, theirs):
            with contextlib.suppress(ssl.SSLWantReadError, ssl.SSLWantWriteError):
                side.do_handshake()
    return mine.version() or ""


# Serves the control port with the runner's certificate, as the Job does, and says "ok" after
# each handshake. A connection whose handshake fails, such as the readiness probe's, is closed
# and the next one is served.
TLS_SERVER = """
import socket
from pathlib import Path
from scrimmage import tls
folder = Path("/run/scrimmage/tls")
context = tls.server_context(folder / "tls.crt", folder / "tls.key")
server = socket.create_server(("0.0.0.0", 7700))
while True:
    connection = server.accept()[0]
    try:
        with context.wrap_socket(connection, server_side=True) as wrapped:
            wrapped.sendall(b"ok")
    except OSError:
        connection.close()
"""
# Connects to each host in argv as the SDK does, with the SDK's client context and the
# authority the agent mounts, and prints the host, the TLS version and what the server said.
TLS_CLIENT = """
import socket, sys
from pathlib import Path
from scrimmage import tls
context = tls.client_context(Path("/run/scrimmage/ca/ca.pem"))
for host in sys.argv[1:]:
    with socket.create_connection((host, 7700), timeout=10) as raw:
        with context.wrap_socket(raw, server_hostname=host) as wrapped:
            print(host, wrapped.version(), wrapped.recv(2).decode())
"""
# How the Job and the agent mount the two Secrets: the same paths, items and modes.
RUNNER_TLS = {
    "secretName": kubernetes.CONTROL_SECRET,
    "defaultMode": 0o400,
    "items": [{"key": "tls.crt", "path": "tls.crt"}, {"key": "tls.key", "path": "tls.key"}],
}
AGENT_CA = {
    "secretName": kubernetes.CA_SECRET,
    "defaultMode": 0o444,
    "items": [{"key": "ca.crt", "path": "ca.pem"}],
}


def tls_pod(name: str, image: str, script: str, *, label: str, mount: str, secret: Any) -> str:
    """A pod of the runner image that runs script as the Job's user, with one Secret mounted
    read-only at mount. The runner's pod has a readiness probe on the control port."""
    container: dict[str, Any] = {
        "name": "c",
        "image": image,
        "imagePullPolicy": "Never",
        "command": ["python3", "-c", script],
        "volumeMounts": [{"name": "secret", "mountPath": mount, "readOnly": True}],
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        },
    }
    if label == "scrimmage-runner":
        container["ports"] = [{"containerPort": 7700, "name": "control"}]
        container["readinessProbe"] = {"tcpSocket": {"port": 7700}, "periodSeconds": 1}
    return json.dumps(
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": name, "labels": {"app.kubernetes.io/name": label}},
            "spec": {
                "terminationGracePeriodSeconds": 0,
                "automountServiceAccountToken": False,
                "securityContext": {
                    "runAsNonRoot": True,
                    "runAsUser": 10001,
                    "runAsGroup": 10001,
                    "fsGroup": 10001,
                    "seccompProfile": {"type": "RuntimeDefault"},
                },
                "containers": [container],
                "volumes": [{"name": "secret", "secret": secret}],
            },
        }
    )


def runner_endpoint_ready(cluster: Cluster, namespace: str) -> bool:
    """The Service scrimmage's EndpointSlice lists the runner pod as ready."""
    slices = cluster.json(
        "get", "endpointslices", "-n", namespace, "-l", "kubernetes.io/service-name=scrimmage"
    )
    return any(
        endpoint.get("targetRef", {}).get("name") == "runner"
        and endpoint.get("conditions", {}).get("ready") is True
        for item in slices["items"]
        for endpoint in item.get("endpoints") or []
    )


def connect(cluster: Cluster, namespace: str, *hosts: str) -> subprocess.CompletedProcess[str]:
    """TLS_CLIENT in the agent's pod. A refused connection, as kube-proxy gives while its
    rules catch up with the EndpointSlice, is tried again for up to 10 s; any other result,
    a refused certificate included, is returned as it is."""
    deadline = time.monotonic() + 10
    while True:
        done = cluster.kubectl(
            "exec", "agent", "-n", namespace, "--", "python3", "-c", TLS_CLIENT, *hosts,
            check=False,
        )  # fmt: skip
        if "ConnectionRefusedError" not in done.stderr or time.monotonic() > deadline:
            return done
        time.sleep(1)


def test_a_default_session_installs_no_cert_manager(
    cluster: Cluster, request: pytest.FixtureRequest
) -> None:
    def installed() -> bool:
        names = {n["metadata"]["name"] for n in cluster.json("get", "namespaces")["items"]}
        crd = cluster.json("get", "crd", "certificates.cert-manager.io", check=False)
        return "cert-manager" in names or crd is not None

    assert not installed(), "a default session installed cert-manager"
    # Positive control: the option's fixture installs it, and the same look sees it.
    request.getfixturevalue("cert_manager")
    assert installed()


def test_the_default_certificates_come_from_scrimmage_cert_and_pass_the_handshake(
    cluster: Cluster, runner_image: str
) -> None:
    with cluster.namespace() as namespace:
        cluster.control_secrets(namespace)
        runner = cluster.json("get", "secret", kubernetes.CONTROL_SECRET, "-n", namespace)
        agent = cluster.json("get", "secret", kubernetes.CA_SECRET, "-n", namespace)
        assert sorted(agent["data"]) == ["ca.crt"], "the agent's Secret must never hold a key"
        assert sorted(runner["data"]) == ["tls.crt", "tls.key"]
        assert runner["type"] == "kubernetes.io/tls"
        authority = x509.load_pem_x509_certificate(base64.b64decode(agent["data"]["ca.crt"]))
        server = x509.load_pem_x509_certificate(base64.b64decode(runner["data"]["tls.crt"]))
        # scrimmage cert's authority signed it, for one day, for the three names alone.
        assert authority.subject.rfc4514_string() == "CN=Scrimmage throwaway authority"
        assert server.issuer == authority.subject
        lifetime = server.not_valid_after_utc - server.not_valid_before_utc
        assert lifetime <= timedelta(days=kubernetes.CERT_DAYS, minutes=5), lifetime
        names = server.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        assert names.get_values_for_type(x509.DNSName) == list(kubernetes.control_names(namespace))
        assert names.get_values_for_type(x509.IPAddress) == []
        cluster.kubectl("apply", "-n", namespace, "-f", str(kubernetes.MANIFESTS / "services.yaml"))
        for name, script, label, mount, secret in (
            ("runner", TLS_SERVER, "scrimmage-runner", "/run/scrimmage/tls", RUNNER_TLS),
            ("agent", "import time; time.sleep(600)", "acme-agent", "/run/scrimmage/ca", AGENT_CA),
        ):
            pod = tls_pod(name, runner_image, script, label=label, mount=mount, secret=secret)
            cluster.kubectl("apply", "-n", namespace, "-f", "-", input=pod)
        cluster.wait_until(
            "both pods Ready",
            lambda: all(pod_ready(cluster, namespace, n) for n in ("runner", "agent")),
            timeout=120,
            namespace=namespace,
            watch=(("pod", "runner"), ("pod", "agent")),
        )
        # A Ready pod is not yet behind its Service: kube-proxy routes to it only once the
        # Service's EndpointSlice lists it as ready.
        cluster.wait_until(
            "the runner behind the Service scrimmage",
            lambda: runner_endpoint_ready(cluster, namespace),
            timeout=60,
        )
        # Each name the agent may connect by, through the Service `scrimmage` and cluster DNS.
        hosts = kubernetes.control_names(namespace)
        said = connect(cluster, namespace, *hosts)
        assert said.returncode == 0, said.stderr[-500:]
        assert said.stdout.splitlines() == [f"{host} TLSv1.3 ok" for host in hosts]
        # Positive control: the same client refuses a name of the Service the certificate
        # does not carry.
        other = connect(cluster, namespace, f"scrimmage.{namespace}")
        assert other.returncode != 0
        assert "CERTIFICATE_VERIFY_FAILED" in other.stderr, other.stderr[-500:]


def stand_in(authority: x509.Certificate, host: str) -> tuple[bytes, bytes, bytes]:
    """The PEM authority, certificate and key of a stand-in for `authority`: its subject, dates
    and extensions, the name constraint among them, under a key of its own, which signs a
    server certificate for `host`. The real authority's key is gone, so only a stand-in lets
    the authority's own constraint judge another name."""
    key = ec.generate_private_key(ec.SECP256R1())
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    ski = x509.SubjectKeyIdentifier.from_public_key(key.public_key())
    builder = (
        x509.CertificateBuilder()
        .subject_name(authority.subject)
        .issuer_name(authority.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(authority.not_valid_before_utc)
        .not_valid_after(authority.not_valid_after_utc)
    )
    for extension in authority.extensions:
        value = ski if isinstance(extension.value, x509.SubjectKeyIdentifier) else extension.value
        builder = builder.add_extension(value, extension.critical)
    ca = builder.sign(key, hashes.SHA256())
    now = datetime.now(UTC)
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "stand-in")]))
        .issuer_name(authority.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ski), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    pem = serialization.Encoding.PEM
    private = serialization.PrivateFormat.PKCS8
    return (
        ca.public_bytes(pem),
        leaf.public_bytes(pem),
        leaf_key.private_bytes(pem, private, serialization.NoEncryption()),
    )


def test_the_default_authority_refuses_a_name_outside_its_three(
    cluster: Cluster, tmp_path: Path
) -> None:
    tmp_path.chmod(0o700)
    with cluster.namespace() as namespace:
        cluster.control_secrets(namespace)
        agent = cluster.json("get", "secret", kubernetes.CA_SECRET, "-n", namespace)["data"]
    authority = x509.load_pem_x509_certificate(base64.b64decode(agent["ca.crt"]))
    # The SDK refuses another namespace's control name under the authority's constraint; one
    # of this namespace's names is the positive control.
    for host, refused in (("scrimmage.other.svc", True), (f"scrimmage.{namespace}.svc", False)):
        folder = tmp_path / host
        folder.mkdir()
        ca, certificate, key = folder / "ca.crt", folder / "tls.crt", folder / "tls.key"
        for path, data in zip((ca, certificate, key), stand_in(authority, host), strict=True):
            path.write_bytes(data)
        if refused:
            with pytest.raises(ssl.SSLCertVerificationError) as raised:
                handshake(ca, certificate, key, host)
            assert raised.value.verify_message == "permitted subtree violation", raised.value
        else:
            assert handshake(ca, certificate, key, host) == "TLSv1.3"
    found = authority.extensions.get_extension_for_class(x509.NameConstraints)
    assert found.critical
    assert found.value.permitted_subtrees == [
        x509.DNSName(name) for name in kubernetes.control_names(namespace)
    ]
    assert found.value.excluded_subtrees == [
        x509.IPAddress(ipaddress.ip_network("0.0.0.0/0")),
        x509.IPAddress(ipaddress.ip_network("::/0")),
    ]


def test_cert_manager_issues_a_certificate_the_sdk_accepts(
    cert_manager: Cluster, tmp_path: Path
) -> None:
    cluster = cert_manager
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


def write_secret(folder: Path, data: dict[str, str]) -> tuple[Path, Path, Path]:
    """The authority, the certificate and the key of a cert-manager Secret, as files."""
    ca, certificate, key = folder / "ca.crt", folder / "tls.crt", folder / "tls.key"
    for path in (ca, certificate, key):
        path.write_bytes(base64.b64decode(data[path.name]))
    return ca, certificate, key


def sign_outside(cluster: Cluster, name: str, **names: list[str]) -> dict[str, str]:
    """The data of a certificate the authority's Issuer signs for `names`, such as dnsNames or
    ipAddresses. cert-manager's CA Issuer does not check the constraints, so it signs any."""
    request = {
        "apiVersion": "cert-manager.io/v1",
        "kind": "Certificate",
        "metadata": {"name": name, "namespace": "scrimmage-pki"},
        "spec": {
            "secretName": name,
            "duration": "1h",
            "privateKey": {"algorithm": "ECDSA", "size": 256},
            "usages": ["server auth", "digital signature"],
            "issuerRef": {"name": "scrimmage-ca", "kind": "Issuer"},
            **names,
        },
    }
    cluster.kubectl("apply", "-f", "-", input=json.dumps(request))
    try:
        cluster.kubectl(
            "wait", "--for=condition=Ready", f"certificate/{name}",
            "-n", "scrimmage-pki", "--timeout=120s",
        )  # fmt: skip
        return cluster.json("get", "secret", name, "-n", "scrimmage-pki")["data"]
    finally:
        cluster.kubectl(
            "delete", f"certificate/{name}", f"secret/{name}",
            "-n", "scrimmage-pki", "--ignore-not-found",
        )  # fmt: skip


def test_the_authority_signs_only_names_under_its_constraints(
    cert_manager: Cluster, tmp_path: Path
) -> None:
    cluster = cert_manager
    tmp_path.chmod(0o700)
    with cluster.namespace() as namespace:
        cluster.issue_control_certificate(namespace)
        outside = sign_outside(cluster, "outside-name", dnsNames=["evil.example.com"])
        address = sign_outside(cluster, "outside-address", ipAddresses=["10.96.0.10"])
        control = cluster.json(
            "get", "secret", f"scrimmage-control-{namespace}", "-n", "scrimmage-pki"
        )["data"]
    # The host matches each certificate, so only the authority's constraint can refuse it.
    for case, data, host, reason in (
        ("name", outside, "evil.example.com", "permitted subtree violation"),
        ("address", address, "10.96.0.10", "excluded subtree violation"),
    ):
        (tmp_path / case).mkdir()
        ca, certificate, key = write_secret(tmp_path / case, data)
        with pytest.raises(ssl.SSLCertVerificationError) as refused:
            handshake(ca, certificate, key, host)
        assert refused.value.verify_message == reason, refused.value
    # Positive control: every name the control certificate carries is in a permitted subtree.
    (tmp_path / "control").mkdir()
    ca, certificate, key = write_secret(tmp_path / "control", control)
    for host in (
        "scrimmage",
        f"scrimmage.{namespace}.svc",
        f"scrimmage.{namespace}.svc.cluster.local",
    ):
        assert handshake(ca, certificate, key, host) == "TLSv1.3", host
    found = x509.load_pem_x509_certificate(ca.read_bytes()).extensions.get_extension_for_class(
        x509.NameConstraints
    )
    assert found.critical
    assert found.value.permitted_subtrees == [
        x509.DNSName("scrimmage"),
        x509.DNSName("svc"),
        x509.DNSName("cluster.local"),
    ]
    assert found.value.excluded_subtrees == [
        x509.IPAddress(ipaddress.ip_network("0.0.0.0/0")),
        x509.IPAddress(ipaddress.ip_network("::/0")),
    ]


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
