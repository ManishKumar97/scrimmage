"""The kind test cluster: made from the committed config, used by one session, deleted after.

kind is a test cluster, the one the Kubernetes project tests itself on. Nothing here touches
the user's kubeconfig or Helm files: every kubectl and helm call gets files in the session's
own folder. One cluster exists per lane, named `scrimmage-<lane>`, and a session deletes only
the cluster of its own name.
"""

import contextlib
import fcntl
import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
import tomllib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from k8s import images

HERE = Path(__file__).parent
PINS: dict[str, Any] = tomllib.loads((HERE / "versions.toml").read_text())
KIND_CONFIG = HERE / "kind.yaml"
MANIFESTS = HERE / "manifests"
CONFIG_FILE = HERE / "scrimmage.toml"
TOKEN_SECRET = "scrimmage-token"
CONFIG_MAP = "scrimmage-config"
# What every test namespace holds before its workloads start: the accounts, the volume claim,
# the Services and the policies. A workload applied first would fail on a missing account.
FOUNDATION = ("service-accounts.yaml", "records-pvc.yaml", "services.yaml", "network-policies.yaml")
WORKLOADS = ("fake-model.yaml", "agent.yaml")
PSA_MODES = ("enforce", "warn", "audit")
POD_CIDR = "192.168.0.0/16"
MEMORY_NEEDED_KB = 6 * 1024 * 1024
ROOM_WAIT_S = 1200.0
# An event with one of these reasons means a workload will never start; waiting is no use.
FAIL_FAST_EVENTS = frozenset({"FailedCreate", "FailedScheduling", "ErrImageNeverPull"})
TOOLS = ("kind", "kubectl", "helm", "docker")
# S8 clusters get a Docker network of their own. The network named `kind` is what other kind
# clusters on the machine, such as an AIOpsLab test bed, are addressed by, so it is never
# shared, and never removed.
NETWORK = "scrimmage-kind"


def parse_time(text: str) -> datetime:
    """An API timestamp, with or without fractions, as a time. Compared as text, an eventTime
    with microseconds sorts wrong against one without."""
    return datetime.fromisoformat(text)


def when(metadata: dict[str, Any]) -> datetime:
    return parse_time(metadata["creationTimestamp"])


def event_time(item: dict[str, Any]) -> datetime:
    """When an event last happened: lastTimestamp, else eventTime, else the time it was made."""
    for key in ("lastTimestamp", "eventTime"):
        if item.get(key):
            return parse_time(item[key])
    return when(item["metadata"])


class ClusterError(AssertionError):
    """The cluster cannot be made or used. An AssertionError, so a test shows it as a failure."""


@dataclass(frozen=True)
class Tags:
    """The image tags the manifests name as <image>:TAG, each one built and loaded first."""

    runner: str
    agent: str
    fake_model: str
    egress: str | None = None  # the agent plus the egress audit hook; only one test needs it

    def render(self, text: str) -> str:
        for placeholder, tag in (
            ("scrimmage-runner:TAG", self.runner),
            ("scrimmage-compose-service:TAG", self.agent),
            ("scrimmage-fake-model:TAG", self.fake_model),
            ("scrimmage-compose-egress:TAG", self.egress),
        ):
            if tag is not None:
                text = text.replace(placeholder, tag)
        return text


def lane() -> str:
    value = os.environ.get("SCRIMMAGE_LANE", "1")
    if not re.fullmatch(r"[a-z0-9]{1,8}", value):
        raise ClusterError(f"SCRIMMAGE_LANE={value!r} must be 1 to 8 letters or digits")
    return value


def cluster_name(which: str | None = None) -> str:
    return f"scrimmage-{which or lane()}"


def scratch_name(role: str) -> str:
    """A name for a cluster a test makes beside the session's. It holds a hyphen, which a lane
    cannot, so it never equals another lane's cluster, and it differs by lane."""
    if not re.fullmatch(r"[a-z0-9]{1,4}", role):
        raise ClusterError(f"scratch role {role!r} must be 1 to 4 letters or digits")
    return f"{lane()}-{role}"


def is_ours(name: str) -> bool:
    """The session's own cluster, or one of its scratch clusters."""
    return name == cluster_name() or name.startswith(f"{cluster_name()}-")


def lock_file(name: str) -> Path:
    return Path(tempfile.gettempdir()) / f"{name}.lock"


@contextlib.contextmanager
def locked(name: str) -> Iterator[None]:
    """An exclusive lock between processes; the kernel frees it if the holder dies."""
    with open(lock_file(name), "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def run(
    command: list[str],
    *,
    timeout: float,
    env: dict[str, str] | None = None,
    input: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        done = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, env=env, input=input
        )
    except subprocess.TimeoutExpired:
        raise ClusterError(f"{' '.join(command)} took over {timeout:.0f} s") from None
    if check and done.returncode != 0:
        raise ClusterError(
            f"{' '.join(command)} exited {done.returncode}\n{done.stdout[-3000:]}\n"
            f"{done.stderr[-3000:]}"
        )
    return done


def require_kind_version(said: str) -> None:
    """Refuse a kind older than the pinned one, naming the pinned one."""
    wanted = PINS["kind"]["version"]
    found = re.search(r"v(\d+)\.(\d+)\.(\d+)", said)
    need = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", wanted)
    assert need is not None
    if found is None or tuple(map(int, found.groups())) < tuple(map(int, need.groups())):
        raise ClusterError(f"kind is {said.strip()!r}; this harness needs {wanted} or newer")


def require_tools() -> None:
    """With SCRIMMAGE_KIND=1 a missing tool is a failure, never a skip."""
    for tool in TOOLS:
        if shutil.which(tool) is None:
            raise ClusterError(f"{tool} is not installed, and SCRIMMAGE_KIND=1 needs it")
    require_kind_version(run(["kind", "version"], timeout=30).stdout)


def memory_available_kb() -> int:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1])
    raise ClusterError("/proc/meminfo has no MemAvailable line")


def running_clusters() -> list[str]:
    return run(["kind", "get", "clusters"], timeout=60).stdout.split()


def route_overlap(routes: list[dict[str, Any]], cidr: str = POD_CIDR) -> str | None:
    """The first host route that overlaps the pod range, as text, or None."""
    pods = ipaddress.ip_network(cidr)
    for route in routes:
        destination = route.get("dst", "")
        if destination == "default":
            continue
        try:
            network = ipaddress.ip_network(destination, strict=False)
        except ValueError:
            continue
        if network.version == pods.version and network.overlaps(pods):
            return f"{destination} (dev {route.get('dev', '?')})"
    return None


def check_pod_range_is_free() -> None:
    routes = json.loads(run(["ip", "-j", "route"], timeout=30).stdout)
    clash = route_overlap(routes)
    if clash:
        raise ClusterError(f"the host has a route {clash} inside {POD_CIDR}, the pod range")


def remove_network(name: str = NETWORK) -> None:
    """Remove a Docker network S8 made. The network named `kind` is never removed."""
    if name == "kind":
        raise ClusterError("refusing to remove the Docker network named kind")
    run(["docker", "network", "rm", name], timeout=60, check=False)


def remove_network_if_unused() -> None:
    """The last S8 cluster to go takes the network. Docker refuses while a node is on it."""
    with locked("scrimmage-kind"):
        if not any(c.startswith("scrimmage-") for c in running_clusters()):
            remove_network()


class Cluster:
    """One lane's cluster and the session folder that holds its kubeconfig and Helm files."""

    def __init__(self, which: str | None = None) -> None:
        self.name = cluster_name(which)
        self.keep = os.environ.get("SCRIMMAGE_KIND_KEEP") == "1"
        self.folder = Path(tempfile.mkdtemp(prefix="scrimmage-kind-"))
        self.kubeconfig = self.folder / "kubeconfig"

    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["KUBECONFIG"] = str(self.kubeconfig)
        env["KUBECACHEDIR"] = str(self.folder / "kube-cache")  # not ~/.kube/cache
        env["HELM_CACHE_HOME"] = str(self.folder / "helm-cache")
        env["HELM_CONFIG_HOME"] = str(self.folder / "helm-config")
        env["HELM_DATA_HOME"] = str(self.folder / "helm-data")
        return env

    def kind_env(self) -> dict[str, str]:
        """kind's environment: the session's kubeconfig, which kind honours as well as the
        --kubeconfig flag, and the network of its own."""
        return {**self.env(), "KIND_EXPERIMENTAL_DOCKER_NETWORK": NETWORK}

    def kubectl(
        self, *args: str, input: str | None = None, timeout: float = 120, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        for arg in args:  # let kubectl's own --timeout message come first
            if found := re.fullmatch(r"--timeout=(\d+)s", arg):
                timeout = int(found.group(1)) + 30
        return run(["kubectl", *args], timeout=timeout, env=self.env(), input=input, check=check)

    def helm(self, *args: str, timeout: float = 600) -> subprocess.CompletedProcess[str]:
        return run(["helm", *args], timeout=timeout, env=self.env())

    def json(self, *args: str, check: bool = True) -> Any:
        done = self.kubectl(*args, "-o", "json", check=check)
        return json.loads(done.stdout) if done.returncode == 0 else None

    def create(self) -> None:
        run(
            [
                "kind",
                "create",
                "cluster",
                "--name",
                self.name,
                "--config",
                str(KIND_CONFIG),
                "--kubeconfig",
                str(self.kubeconfig),
            ],
            timeout=600,
            env=self.kind_env(),
        )  # no --wait: the node is not Ready until Calico runs

    def delete(self) -> None:
        """Delete the cluster of this session's name. Its --kubeconfig keeps kind away from
        the user's file."""
        run(
            [
                "kind",
                "delete",
                "cluster",
                "--name",
                self.name,
                "--kubeconfig",
                str(self.kubeconfig),
            ],
            timeout=300,
            env=self.kind_env(),
            check=False,
        )

    def wait_until(
        self,
        what: str,
        check: Callable[[], bool],
        *,
        timeout: float,
        namespace: str | None = None,
        watch: tuple[tuple[str, str], ...] = (),
    ) -> None:
        """Ask `check` every 2 s. `watch` lists the workloads being waited for, as
        ("deployment" | "pod", name) in `namespace`. An event that says one of them will never
        start stops the wait at once, naming its message; no other event counts, and with
        nothing watched no event does."""
        deadline = time.monotonic() + timeout
        while True:
            if check():
                return
            if namespace and watch and (event := self.bad_event(namespace, watch)):
                raise ClusterError(f"waiting for {what} in {namespace}: {event}")
            if time.monotonic() > deadline:
                raise ClusterError(f"{what} was not ready after {timeout:.0f} s")
            time.sleep(2)

    def scopes(
        self, namespace: str, watch: tuple[tuple[str, str], ...]
    ) -> list[tuple[set[str], datetime]]:
        """For each watched workload, the uids an event may be about and the time before which
        an event is old news. A Deployment's scope is its newest ReplicaSet, by revision, and
        that ReplicaSet's pods, since an earlier rollout's failure is over. A pod's is its own."""
        found: list[tuple[set[str], datetime]] = []
        for kind, name in watch:
            if kind == "pod":
                pod = self.json("get", "pod", name, "-n", namespace, check=False)
                if pod:
                    found.append(({pod["metadata"]["uid"]}, when(pod["metadata"])))
                continue
            deployment = self.json("get", "deployment", name, "-n", namespace, check=False)
            if not deployment:
                continue
            uid = deployment["metadata"]["uid"]
            sets = [
                r
                for r in (self.json("get", "replicasets", "-n", namespace, check=False) or {}).get(
                    "items", []
                )
                if any(o["uid"] == uid for o in r["metadata"].get("ownerReferences", []))
            ]
            if not sets:
                continue
            newest = max(
                sets,
                key=lambda r: int(
                    r["metadata"].get("annotations", {}).get("deployment.kubernetes.io/revision", 0)
                ),
            )
            uids = {newest["metadata"]["uid"]}
            for pod in (self.json("get", "pods", "-n", namespace, check=False) or {}).get(
                "items", []
            ):
                if any(o["uid"] in uids for o in pod["metadata"].get("ownerReferences", [])):
                    uids.add(pod["metadata"]["uid"])
            found.append((uids, when(newest["metadata"])))
        return found

    def bad_event(self, namespace: str, watch: tuple[tuple[str, str], ...]) -> str | None:
        scopes = self.scopes(namespace, watch)
        events = self.json("get", "events", "-n", namespace, check=False)
        for item in (events or {}).get("items", []):
            if item.get("reason") not in FAIL_FAST_EVENTS:
                continue
            uid = item.get("involvedObject", {}).get("uid")
            happened = event_time(item)
            if any(uid in uids and happened >= since for uids, since in scopes):
                return f"{item['reason']}: {item.get('message', '')}"
        return None

    def nodes_ready(self) -> bool:
        nodes = self.json("get", "nodes", check=False)
        items = (nodes or {}).get("items", [])
        return bool(items) and all(
            any(c["type"] == "Ready" and c["status"] == "True" for c in n["status"]["conditions"])
            for n in items
        )

    def daemonset_ready(self, name: str, namespace: str) -> bool:
        ds = self.json("get", "daemonset", name, "-n", namespace, check=False)
        status = (ds or {}).get("status", {})
        return status.get("desiredNumberScheduled", 0) > 0 and status.get(
            "numberReady", 0
        ) == status.get("desiredNumberScheduled")

    def install_calico(self) -> None:
        calico = PINS["calico"]
        chart = [
            calico["chart"],
            "--repo",
            calico["repo"],
            "--version",
            calico["version"],
            "--namespace",
            "tigera-operator",
            "--values",
            str(HERE / "calico-values.yaml"),
        ]
        # The operator makes its own CRDs when it starts, and Helm refuses a release that holds
        # an object of a kind that does not exist yet. So the operator goes in alone first.
        self.helm(
            "install", "calico", *chart, "--create-namespace", "--set", "installation.enabled=false"
        )
        self.wait_until(
            "the operator's CRDs",
            lambda: (
                self.kubectl(
                    "get", "crd", "installations.operator.tigera.io", check=False
                ).returncode
                == 0
            ),
            timeout=120,
        )
        self.kubectl(
            "wait",
            "--for=condition=Established",
            "crd/installations.operator.tigera.io",
            "--timeout=60s",
        )
        self.helm("upgrade", "calico", *chart)
        self.wait_until(
            "calico-node",
            lambda: self.daemonset_ready("calico-node", "calico-system"),
            timeout=180,
        )
        self.wait_until("every node Ready", self.nodes_ready, timeout=180)

    def install_cert_manager(self) -> None:
        cm = PINS["cert-manager"]
        self.helm(
            "install",
            "cert-manager",
            cm["chart"],
            "--repo",
            cm["repo"],
            "--version",
            cm["version"],
            "--namespace",
            "cert-manager",
            "--create-namespace",
            "--set",
            "crds.enabled=true",
            "--wait",
            "--timeout",
            "300s",
        )
        self.kubectl(
            "wait",
            "--for=condition=Available",
            "deployment/cert-manager-webhook",
            "-n",
            "cert-manager",
            "--timeout=120s",
        )

    def make_authority(self) -> None:
        """Apply the authority, retrying while the webhook comes up behind its Service."""
        manifest = (HERE / "pki-authority.yaml").read_text()
        deadline = time.monotonic() + 120
        while True:
            done = self.kubectl("apply", "-f", "-", input=manifest, check=False)
            if done.returncode == 0:
                break
            if time.monotonic() > deadline:
                raise ClusterError(f"cert-manager did not accept the authority: {done.stderr}")
            time.sleep(3)
        self.kubectl(
            "wait",
            "--for=condition=Ready",
            "certificate/scrimmage-ca",
            "issuer/scrimmage-ca",
            "-n",
            "scrimmage-pki",
            "--timeout=120s",
        )

    def begin(self) -> None:
        """What a session does before it creates anything: check the tools, the memory and the
        pod range, then delete a cluster of this name that a killed session left. Never another
        name."""
        require_tools()
        self.reserve_room()
        check_pod_range_is_free()
        self.delete()

    def bare(self) -> None:
        """A cluster with nothing installed, for a test of the harness itself. It takes the
        global lock around creating, as a session does."""
        with locked("scrimmage-kind"):
            self.begin()
            self.create()

    def up(self, load: tuple[str, ...] = ()) -> None:
        with locked("scrimmage-kind"):  # held until the pods are scheduled
            self.begin()
            self.create()
            self.install_calico()
            self.install_cert_manager()
        for tag in load:
            images.kind_load(tag, self.name)

    def reserve_room(self) -> None:
        """One S8 cluster at a time unless MemAvailable is over 6 GB. Another kind cluster is
        never stopped or deleted: if it is not ours, this fails naming it."""
        deadline = time.monotonic() + ROOM_WAIT_S
        while memory_available_kb() < MEMORY_NEEDED_KB:
            others = [c for c in running_clusters() if c != self.name and not is_ours(c)]
            foreign = [c for c in others if not c.startswith("scrimmage-")]
            free_gb = memory_available_kb() / 1024 / 1024
            if foreign:
                raise ClusterError(
                    f"the kind cluster {foreign[0]} is running and only {free_gb:.1f} GB of "
                    "memory is available; not starting another. Scrimmage never stops it"
                )
            if not others:
                return
            if time.monotonic() > deadline:
                raise ClusterError(f"{others[0]} still holds the memory after {ROOM_WAIT_S:.0f} s")
            time.sleep(10)

    def down(self) -> None:
        if self.keep:
            print(
                f"\nkept the cluster {self.name}; use it with KUBECONFIG={self.kubeconfig}\n"
                f"delete it with: kind delete cluster --name {self.name} "
                f"--kubeconfig {self.kubeconfig}"
            )
            return
        self.delete()
        shutil.rmtree(self.folder, ignore_errors=True)
        remove_network_if_unused()

    @contextlib.contextmanager
    def namespace(
        self, *, restricted: bool = False, modes: tuple[str, ...] = PSA_MODES
    ) -> Iterator[str]:
        """A namespace of its own, deleted after the test. `restricted` makes it from the
        committed template, which enforces, warns and audits the Pod Security restricted
        standard; without it the namespace is bare, for a test of the cluster itself. `modes`
        keeps only those of the three, so a test can see what one label does alone."""
        name = f"s8-{secrets.token_hex(4)}"
        if restricted:
            template = (MANIFESTS / "namespace.yaml").read_text().replace("NAMESPACE", name)
            left_out = tuple(f"pod-security.kubernetes.io/{m}" for m in PSA_MODES if m not in modes)
            template = "".join(
                line
                for line in template.splitlines(keepends=True)
                if not line.strip().startswith(left_out)
            )
            self.kubectl("apply", "-f", "-", input=template)
        else:
            self.kubectl("create", "namespace", name)
        try:
            yield name
        finally:
            self.kubectl(
                "delete", "namespace", name, "--wait=false", "--ignore-not-found", check=False
            )

    def apply(
        self,
        namespace: str,
        files: tuple[str, ...],
        tags: Tags,
        *,
        dry_run: bool = False,
    ) -> str:
        """Server-side apply of committed manifests, with the image tags filled in. Returns what
        kubectl printed on stderr, where the Pod Security warnings are. `dry_run` runs admission
        and creates nothing."""
        text = "\n---\n".join(tags.render((MANIFESTS / name).read_text()) for name in files)
        command = ["apply", "--server-side", "-n", namespace, "-f", "-"]
        if dry_run:
            command.append("--dry-run=server")
        return self.kubectl(*command, input=text).stderr

    def create_token_secret(self, namespace: str) -> str:
        """The token Secret, made from stdin. The token is on no command line and in no file."""
        token = secrets.token_hex(32)
        secret = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": TOKEN_SECRET, "namespace": namespace},
            "stringData": {"token": token},
        }
        # Server-side: a client-side apply writes the whole object, token included, into an
        # annotation.
        self.kubectl("apply", "--server-side", "-f", "-", input=json.dumps(secret))
        return token

    def create_config_map(self, namespace: str) -> None:
        """The Job's file, scrimmage.toml, as the ConfigMap the Job mounts."""
        config = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": CONFIG_MAP, "namespace": namespace},
            "data": {"scrimmage.toml": CONFIG_FILE.read_text()},
        }
        self.kubectl("apply", "-f", "-", input=json.dumps(config))

    def deploy(self, namespace: str, tags: Tags, agent: str = "agent.yaml") -> str:
        """Everything except the Job and the reader: the certificate and its two Secrets, the
        token, the file, the accounts, the volume claim, the Services, the policies, the fake
        model and the agent, from `agent`, a file of the Deployment acme-agent. Returns every
        warning kubectl printed, which is empty when the namespace's standard accepts it all.
        Returns before any pod is Ready; see wait_ready."""
        self.issue_control_certificate(namespace)
        self.create_token_secret(namespace)
        self.create_config_map(namespace)
        warnings = self.apply(namespace, FOUNDATION, tags)
        workloads = tuple(agent if name == "agent.yaml" else name for name in WORKLOADS)
        return warnings + self.apply(namespace, workloads, tags)

    def deployment_ready(self, namespace: str, name: str) -> bool:
        found = self.json("get", "deployment", name, "-n", namespace, check=False) or {}
        wanted = found.get("spec", {}).get("replicas", 1)
        status = found.get("status", {})
        return (
            status.get("readyReplicas", 0) == wanted
            and status.get("updatedReplicas", 0) == wanted
            and status.get("replicas", 0) == wanted
        )

    def wait_ready(self, namespace: str, *names: str, timeout: float = 180) -> None:
        """Every named Deployment has all its replicas Ready. An event that says a pod will
        never start fails the wait at once."""
        self.wait_until(
            f"Deployments {', '.join(names)}",
            lambda: all(self.deployment_ready(namespace, name) for name in names),
            timeout=timeout,
            namespace=namespace,
            watch=tuple(("deployment", name) for name in names),
        )

    def pod_ready(self, namespace: str, name: str) -> bool:
        pod = self.json("get", "pod", name, "-n", namespace, check=False)
        conditions = (pod or {}).get("status", {}).get("conditions", [])
        return any(c["type"] == "Ready" and c["status"] == "True" for c in conditions)

    def start_reader(self, namespace: str, tags: Tags) -> None:
        """The reader pod, which holds the records volume read-only and runs nothing else."""
        self.apply(namespace, ("reader.yaml",), tags)
        self.wait_until(
            "the reader pod",
            lambda: self.pod_ready(namespace, "reader"),
            timeout=120,
            namespace=namespace,
            watch=(("pod", "reader"),),
        )

    def reader_exec(
        self, namespace: str, *command: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        """A command in the reader pod. kubectl exec keeps stdout and stderr apart."""
        return self.kubectl("exec", "reader", "-n", namespace, "--", *command, check=check)

    def exit_code(self, namespace: str, pod: str) -> int | None:
        """The exit code of the pod's first container, from the pod's status; None while it
        has not ended. It is never read from the pod's log."""
        found = self.json("get", "pod", pod, "-n", namespace, check=False) or {}
        for status in found.get("status", {}).get("containerStatuses", []):
            ended = status.get("state", {}).get("terminated")
            return int(ended["exitCode"]) if ended else None
        return None

    def issue_control_certificate(self, namespace: str) -> None:
        """A server certificate for scrimmage.<namespace>, from the authority, then the two
        Secrets a test namespace holds: the runner's with the key, the agent's with only the
        authority. The key passes through this process's memory, never a file."""
        self.make_authority()  # applying it again changes nothing
        text = (HERE / "pki-control.yaml").read_text().replace("NAMESPACE", namespace)
        self.kubectl("apply", "-f", "-", input=text)
        source = f"scrimmage-control-{namespace}"
        self.kubectl(
            "wait",
            "--for=condition=Ready",
            f"certificate/{source}",
            "-n",
            "scrimmage-pki",
            "--timeout=120s",
        )
        data = self.json("get", "secret", source, "-n", "scrimmage-pki")["data"]
        for name, keys in (
            ("scrimmage-control-tls", ("tls.crt", "tls.key")),
            ("scrimmage-ca-cert", ("ca.crt",)),
        ):
            secret = {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": name, "namespace": namespace},
                "data": {key: data[key] for key in keys},
            }
            self.kubectl("apply", "--server-side", "-f", "-", input=json.dumps(secret))


@contextlib.contextmanager
def port_forward(
    cluster: Cluster, namespace: str, target: str, port: int, timeout: float = 30
) -> Iterator[int]:
    """`kubectl port-forward` from a free local port to `port` in `target`, such as
    deployment/fake-model, for as long as the block runs. Yields the local port. It reaches
    the pod's loopback through the kubelet, so no NetworkPolicy applies to it. Its output goes
    to files in the session's folder, so no pipe fills while nobody reads it. The process is
    this one's own child, stopped by its pid."""
    out = cluster.folder / f"port-forward-{secrets.token_hex(4)}.out"
    err = out.with_suffix(".err")
    with open(out, "w") as stdout, open(err, "w") as stderr:
        forward = subprocess.Popen(
            ["kubectl", "port-forward", "-n", namespace, target, f":{port}"],
            stdout=stdout,
            stderr=stderr,
            env=cluster.env(),
        )
    try:
        deadline = time.monotonic() + timeout
        # "Forwarding from 127.0.0.1:PORT -> port"
        while (found := re.search(r"127\.0\.0\.1:(\d+) ->", out.read_text())) is None:
            if forward.poll() is not None or time.monotonic() > deadline:
                said = f"{out.read_text()}{err.read_text()}"
                raise ClusterError(f"kubectl port-forward to {target} did not start: {said}")
            time.sleep(0.1)
        yield int(found.group(1))
    finally:
        forward.kill()
        forward.wait(timeout=30)


@contextlib.contextmanager
def scratch(role: str) -> Iterator[Cluster]:
    """A bare cluster beside the session's, named by lane and role, under a lock of its own
    name so two sessions never share it, and deleted after."""
    name = scratch_name(role)
    with locked(f"scrimmage-kind-{name}"):
        cluster = Cluster(name)
        try:
            cluster.bare()
            yield cluster
        finally:
            cluster.down()


@contextlib.contextmanager
def session(load: tuple[str, ...] = ()) -> Iterator[Cluster]:
    """The lane's lock for the whole session, a cluster, and its deletion in a finally."""
    require_tools()
    with locked(f"scrimmage-kind-{lane()}"):
        cluster = Cluster()
        try:
            cluster.up(load)
            yield cluster
        finally:
            cluster.down()
