"""Check, once, on a real cluster, what the Kubernetes tests assume. Run it by hand:

    SCRIMMAGE_LANE=2 uv run python tests/k8s/probe.py

It makes the lane's cluster from the committed config, prints what each assumption turned out
to be, and deletes the cluster. A test never calls it; it is the record of what was seen.
"""

import base64
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from k8s import images
from k8s.cluster import Cluster, ClusterError, run, session

PY = ["python3", "-c"]
SECURITY = {
    "runAsNonRoot": True,
    "runAsUser": 10001,
    "runAsGroup": 10001,
    "fsGroup": 10001,
    "seccompProfile": {"type": "RuntimeDefault"},
}
CONTAINER_SECURITY = {
    "allowPrivilegeEscalation": False,
    "capabilities": {"drop": ["ALL"]},
}


def pod(name: str, tag: str, script: str, **spec: object) -> dict[str, object]:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name},
        "spec": {
            "restartPolicy": "Never",
            "securityContext": SECURITY,
            "containers": [
                {
                    "name": "c",
                    "image": tag,
                    "imagePullPolicy": "Never",
                    "command": [*PY, script],
                    "securityContext": CONTAINER_SECURITY,
                    **spec.pop("container", {}),  # type: ignore[arg-type]
                }
            ],
            **spec,
        },
    }


def run_pod(cluster: Cluster, namespace: str, body: dict[str, object]) -> str:
    """Apply a pod, wait until it ends, and return its output."""
    name = body["metadata"]["name"]  # type: ignore[index]
    cluster.kubectl("apply", "-n", namespace, "-f", "-", input=json.dumps(body))
    cluster.wait_until(
        f"pod {name} to end",
        lambda: (
            cluster.json("get", "pod", name, "-n", namespace)["status"]["phase"]  # type: ignore[index]
            in ("Succeeded", "Failed")
        ),
        timeout=120,
        namespace=namespace,
    )
    return cluster.kubectl("logs", name, "-n", namespace).stdout.strip()


def say(number: int, title: str, text: str) -> None:
    print(f"\n[{number}] {title}\n{text}", flush=True)


SECRET_FILE = """
import os
p = "/s/token"
print("lstat link:", os.path.islink(p), "->", os.readlink(p))
print("data link:", os.path.islink("/s/..data"), "->", os.readlink("/s/..data"))
fd = os.open(p, os.O_RDONLY)
st = os.fstat(fd)
print("owner", st.st_uid, "group", st.st_gid, "mode", oct(st.st_mode & 0o777))
print("read-only mount:", bool(os.fstatvfs(fd).f_flag & os.ST_RDONLY))
print("process uid", os.getuid(), "gid", os.getgid(), "groups", os.getgroups())
"""

DATA_DIR = """
import os
st = os.stat("/data")
print("folder owner", st.st_uid, "group", st.st_gid, "mode", oct(st.st_mode & 0o777))
os.mkdir("/data/made")
st = os.stat("/data/made")
print("made by this process: owner", st.st_uid, "group", st.st_gid)
"""

DIALER = """
import socket, sys, time
host, port, seconds = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
start = time.monotonic()
try:
    socket.create_connection((host, port), timeout=seconds).close()
    print("connected", round(time.monotonic() - start, 2))
except Exception as exc:
    print(type(exc).__name__, round(time.monotonic() - start, 2))
"""

SERVER = """
import http.server, socketserver
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
socketserver.TCPServer.allow_reuse_address = True
http.server.ThreadingHTTPServer(("", 8080), H).serve_forever()
"""


def dial(cluster: Cluster, namespace: str, pod_name: str, host: str, seconds: float) -> str:
    out = cluster.kubectl(
        "exec", pod_name, "-n", namespace, "--", "python3", "-c", DIALER, host, "8080", str(seconds)
    )
    return out.stdout.strip()


def probe_secret_volume(cluster: Cluster, namespace: str, tag: str) -> None:
    cluster.kubectl("create", "secret", "generic", "tok", "-n", namespace, "--from-literal=token=x")
    body = pod(
        "secret-file",
        tag,
        SECRET_FILE,
        volumes=[{"name": "s", "secret": {"secretName": "tok", "defaultMode": 0o400}}],
        container={"volumeMounts": [{"name": "s", "mountPath": "/s", "readOnly": True}]},
    )
    say(
        1,
        "a Secret file with defaultMode 0400 and fsGroup 10001",
        run_pod(cluster, namespace, body),
    )


HANDSHAKE = """
import ssl, sys
from pathlib import Path
if len(sys.argv) > 4:
    sys.path.insert(0, sys.argv[4])
from scrimmage_core import tls
ca, crt, key = (Path(p) for p in sys.argv[1:4])
client = tls.client_context(ca)
server = tls.server_context(crt, key)
to_server, to_client = ssl.MemoryBIO(), ssl.MemoryBIO()
mine = client.wrap_bio(to_server, to_client, server_hostname="scrimmage")
theirs = server.wrap_bio(to_client, to_server, server_side=True)
for _ in range(8):
    for side in (mine, theirs):
        try:
            side.do_handshake()
        except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
            pass
print(sys.version.split()[0], "strict handshake:", mine.version(), "ok")
"""


def probe_certificates(cluster: Cluster, namespace: str, tag: str) -> None:
    cluster.issue_control_certificate(namespace)
    data = cluster.json("get", "secret", f"scrimmage-control-{namespace}", "-n", "scrimmage-pki")
    folder = cluster.folder / "pki"
    folder.mkdir(mode=0o700)
    for key in ("tls.crt", "tls.key", "ca.crt"):
        (folder / key).write_bytes(base64.b64decode(data["data"][key]))
    ca = (folder / "ca.crt").read_text()
    say(
        2,
        "cert-manager's authority and server certificate",
        f"ca.crt holds {ca.count('BEGIN CERTIFICATE')} certificate(s)",
    )
    src = str(Path(__file__).parents[2] / "packages" / "scrimmage-core" / "src")
    for version in ("3.11", "3.12", "3.13", "3.14"):
        done = subprocess.run(
            [
                "uv",
                "run",
                "--no-project",
                "--python",
                version,
                "python",
                "-I",
                "-c",
                HANDSHAKE,
                str(folder / "ca.crt"),
                str(folder / "tls.crt"),
                str(folder / "tls.key"),
                src,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        print(f"    host: {done.stdout.strip() or done.stderr.strip()[-300:]}")
    done = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--user",
            "0",
            "--entrypoint",
            "python3",
            "-v",
            f"{folder}:/c:ro",
            tag,
            "-c",
            HANDSHAKE,
            "/c/ca.crt",
            "/c/tls.crt",
            "/c/tls.key",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    print(f"    runner image: {done.stdout.strip() or done.stderr.strip()[-300:]}")


def probe_name_constraints(cluster: Cluster) -> None:
    """Ask for an authority with nameConstraints, and see what cert-manager made of it."""
    manifest = json.dumps(
        {
            "apiVersion": "cert-manager.io/v1",
            "kind": "Certificate",
            "metadata": {"name": "probe-nc", "namespace": "scrimmage-pki"},
            "spec": {
                "isCA": True,
                "commonName": "probe-nc",
                "secretName": "probe-nc",
                "nameConstraints": {"critical": True, "permitted": {"dnsDomains": ["scrimmage"]}},
                "issuerRef": {"name": "scrimmage-selfsigned", "kind": "Issuer"},
            },
        }
    )
    applied = cluster.kubectl("apply", "-f", "-", input=manifest, check=False)
    time.sleep(15)
    cert = cluster.json("get", "certificate", "probe-nc", "-n", "scrimmage-pki", check=False)
    conditions = [
        (c["type"], c["status"], c.get("message", "")[:160])
        for c in (cert or {}).get("status", {}).get("conditions", [])
    ]
    extension: bool | None = None
    secret = cluster.json("get", "secret", "probe-nc", "-n", "scrimmage-pki", check=False)
    if secret:
        pem = base64.b64decode(secret["data"]["tls.crt"])
        text = run(["openssl", "x509", "-noout", "-text"], timeout=30, input=pem.decode()).stdout
        extension = "Name Constraints" in text
    cluster.kubectl(
        "delete",
        "certificate/probe-nc",
        "secret/probe-nc",
        "-n",
        "scrimmage-pki",
        "--ignore-not-found",
        check=False,
    )
    say(
        3,
        "nameConstraints on the authority",
        f"apply: {applied.returncode} {applied.stderr.strip()[:200]}\nconditions: {conditions}\n"
        f"the certificate carries the extension: {extension!r}",
    )


def probe_no_endpoints(cluster: Cluster, namespace: str, tag: str) -> None:
    cluster.kubectl(
        "apply",
        "-n",
        namespace,
        "-f",
        "-",
        input=json.dumps(
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": "empty"},
                "spec": {"selector": {"app": "nobody"}, "ports": [{"port": 8080}]},
            }
        ),
    )
    body = pod("dial-empty", tag, DIALER + "\n", container={"args": []})
    body["spec"]["containers"][0]["command"] = [*PY, DIALER, "empty", "8080", "5"]  # type: ignore[index]
    say(4, "a Service with no endpoints", run_pod(cluster, namespace, body))


def server_and_client(cluster: Cluster, namespace: str, tag: str) -> None:
    for name, label in (("server", "server"), ("client", "client")):
        script = SERVER if name == "server" else "import time; time.sleep(600)"
        body = pod(name, tag, script)
        body["metadata"]["labels"] = {"app": label}  # type: ignore[index]
        cluster.kubectl("apply", "-n", namespace, "-f", "-", input=json.dumps(body))
    cluster.kubectl(
        "wait",
        "--for=condition=Ready",
        "pod/server",
        "pod/client",
        "-n",
        namespace,
        "--timeout=120s",
    )
    cluster.kubectl(
        "apply",
        "-n",
        namespace,
        "-f",
        "-",
        input=json.dumps(
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": "server"},
                "spec": {"selector": {"app": "server"}, "ports": [{"port": 8080}]},
            }
        ),
    )


def probe_policy(cluster: Cluster, namespace: str) -> None:
    before = dial(cluster, namespace, "client", "server", 3)
    policy = json.dumps(
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": "deny"},
            "spec": {"podSelector": {"matchLabels": {"app": "server"}}, "policyTypes": ["Ingress"]},
        }
    )
    cluster.kubectl("apply", "-n", namespace, "-f", "-", input=policy)
    start = time.monotonic()
    first = dial(cluster, namespace, "client", "server", 2)  # a new connection, at once
    cluster.kubectl("delete", "networkpolicy", "deny", "-n", namespace)
    time.sleep(3)
    after = dial(cluster, namespace, "client", "server", 3)
    say(
        5,
        "a Calico NetworkPolicy drops, and holds for a new connection",
        f"before: {before}\nfirst try {time.monotonic() - start:.1f} s after apply: {first}\n"
        f"after delete: {after}",
    )


def probe_volume(cluster: Cluster, namespace: str, tag: str) -> None:
    cluster.kubectl(
        "apply",
        "-n",
        namespace,
        "-f",
        "-",
        input=json.dumps(
            {
                "apiVersion": "v1",
                "kind": "PersistentVolumeClaim",
                "metadata": {"name": "records"},
                "spec": {
                    "accessModes": ["ReadWriteOnce"],
                    "storageClassName": "standard",
                    "resources": {"requests": {"storage": "1Gi"}},
                },
            }
        ),
    )
    body = pod(
        "volume",
        tag,
        DATA_DIR,
        volumes=[{"name": "d", "persistentVolumeClaim": {"claimName": "records"}}],
        container={"volumeMounts": [{"name": "d", "mountPath": "/data"}]},
    )
    say(6, "local-path volume with fsGroup 10001", run_pod(cluster, namespace, body))


def probe_load(cluster: Cluster, namespace: str, tag: str) -> None:
    host = json.loads(run(["docker", "image", "inspect", tag], timeout=30).stdout)[0]
    out = run_pod(cluster, namespace, pod("loaded", tag, "print('image runs')"))
    status = cluster.json("get", "pod", "loaded", "-n", namespace)["status"]["containerStatuses"][0]
    inspected = run(
        [
            "docker",
            "exec",
            f"{cluster.name}-control-plane",
            "crictl",
            "inspecti",
            "-o",
            "json",
            status["image"],
        ],
        timeout=60,
    )
    node_layers = json.loads(inspected.stdout)["info"]["imageSpec"]["rootfs"]["diff_ids"]
    say(
        7,
        "kind load docker-image with Docker's containerd image store",
        f"pod ran with imagePullPolicy Never: {out}\nnode imageID: {status['imageID']}\n"
        f"host image id: {host['Id']}\n"
        f"the node's layers equal the host's: {node_layers == host['RootFS']['Layers']}",
    )


def main() -> int:
    print(f"kind: {run(['kind', 'version'], timeout=30).stdout.strip()}")
    tag = images.ensure_runner_image()
    with session((tag,)) as cluster, cluster.namespace() as namespace:
        print(f"nodes:\n{cluster.kubectl('get', 'nodes', '-o', 'wide').stdout}")
        pods = cluster.kubectl("get", "pods", "-A", "-o", "wide").stdout
        print(pods)
        probe_load(cluster, namespace, tag)
        probe_secret_volume(cluster, namespace, tag)
        probe_certificates(cluster, namespace, tag)
        probe_name_constraints(cluster)
        probe_no_endpoints(cluster, namespace, tag)
        server_and_client(cluster, namespace, tag)
        probe_policy(cluster, namespace)
        probe_volume(cluster, namespace, tag)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ClusterError as exc:
        print(f"probe failed: {exc}", file=sys.stderr)
        sys.exit(1)
