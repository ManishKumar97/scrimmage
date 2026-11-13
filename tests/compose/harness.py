"""The Docker Compose test bed: stage a build context, build the images, bring a project up.

Everything here is local. The images are tagged by a hash of what the context holds and are
built once, under a lock in the system temp folder, so two test slots never build at once and
a second build is skipped when the tag exists. Each test brings up its own project, under a
name of its own, and takes it down in a `finally`. A lock also serialises the projects of
two test processes, as each one starts a runner and a service on 4 cores.

Nothing is fetched while an image builds: `stage()` downloads the wheels first, from the
versions in uv.lock, and the build runs with `--network none`.
"""

import contextlib
import fcntl
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from packaging.markers import Marker

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
SERVICE = HERE / "service"
HOOK = HERE / "egress_hook"
BIN = Path(sys.executable).parent
EXAMPLE = REPO / "tests" / "example"

BUILD_TIMEOUT_S = 600
UP_TIMEOUT_S = 60
SERVICE_PORT = 8765
# The service's own needs beyond scrimmage-core, which has no dependencies: the web stack and
# the model library tests/example/acme imports. Versions come from uv.lock, never a new resolve.
ROOTS = ("fastapi", "uvicorn", "openai")
# What the wheels are chosen for: the image's Python and platform.
TARGET_ENV = {
    "python_version": "3.12",
    "python_full_version": "3.12.0",
    "sys_platform": "linux",
    "platform_system": "Linux",
    "platform_machine": "x86_64",
    "platform_python_implementation": "CPython",
    "implementation_name": "cpython",
    "os_name": "posix",
    "extra": "",
}
# Files a careless build context would pick up. Fixed, so an image built by an earlier session
# and found by its tag was built with these same files beside the Dockerfile.
CANARIES = {
    "token": "CANARY-TOKEN-5d1f0a77c2e94b3b8c6a",
    ".env": "SCRIMMAGE_SECRET=CANARY-ENV-9b2e41c7d85f4a60b1d3",
    "server.key": "CANARY-KEY-3c7a90e1b4d24f58a6e2",
}


def locked_file(name: str) -> Path:
    return Path(tempfile.gettempdir()) / f"scrimmage-compose-{name}-{os.getuid()}.lock"


@contextlib.contextmanager
def locked(name: str) -> Iterator[None]:
    """An exclusive lock between test processes; the kernel frees it if the holder dies."""
    with open(locked_file(name), "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def sh(
    command: list[str],
    *,
    timeout: float,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    done = subprocess.run(
        command, capture_output=True, text=True, timeout=timeout, env=env, cwd=cwd, check=False
    )
    if check and done.returncode != 0:
        raise AssertionError(
            f"{' '.join(command)} exited {done.returncode}\n{done.stdout[-3000:]}\n"
            f"{done.stderr[-3000:]}"
        )
    return done


def require_docker() -> None:
    """Skip when there is no docker command. Fail when there is one and the daemon is unusable:
    a test that cannot run must not look like one that passed."""
    if shutil.which("docker") is None:
        pytest.skip("no docker command on this machine")
    if subprocess.run(["docker", "info"], capture_output=True, timeout=60).returncode != 0:
        pytest.fail("docker is installed but `docker info` fails, so this docker test cannot pass")


def bridge_gateway() -> str:
    """The address a container reaches its host at: the default bridge's gateway."""
    template = "{{(index .IPAM.Config 0).Gateway}}"
    out = sh(["docker", "network", "inspect", "bridge", "-f", template], timeout=30).stdout
    return out.strip()


# Staging


def runtime_pins() -> list[str]:
    """name==version for the closure of ROOTS in uv.lock, for the image's Python and platform."""
    lock = tomllib.loads((REPO / "uv.lock").read_text())
    packages = {p["name"]: p for p in lock["package"]}
    wanted: dict[str, str] = {}
    todo = list(ROOTS)
    while todo:
        name = todo.pop()
        if name in wanted:
            continue
        wanted[name] = packages[name]["version"]
        for edge in packages[name].get("dependencies", []):
            marker = edge.get("marker")
            if marker and not Marker(marker).evaluate(TARGET_ENV):
                continue
            todo.append(edge["name"])
            for extra in edge.get("extra", []):
                optional = packages[edge["name"]].get("optional-dependencies", {})
                todo += [o["name"] for o in optional.get(extra, [])]
    return sorted(f"{name}=={version}" for name, version in wanted.items())


def stage() -> None:
    """Fill wheels/ and app/ in the build context: the pinned wheels, the two workspace
    wheels built here, and the example app."""
    wheels = SERVICE / "wheels"
    wheels.mkdir(exist_ok=True)
    pins = "\n".join(runtime_pins()) + "\n"
    marker = wheels / "pins.txt"
    if not marker.exists() or marker.read_text() != pins:
        for old in wheels.glob("*.whl"):
            old.unlink()
        marker.write_text(pins)
        command = ["uvx", "pip", "download", "--only-binary=:all:", "--no-deps", "-q"]
        command += ["--python-version", "3.12", "--implementation", "cp"]
        for platform in ("manylinux_2_28_x86_64", "manylinux_2_17_x86_64", "manylinux2014_x86_64"):
            command += ["--platform", platform]
        command += ["-r", str(marker), "-d", str(wheels)]
        sh(command, timeout=BUILD_TIMEOUT_S)
    for old in wheels.glob("scrimmage*.whl"):
        old.unlink()
    for package in ("scrimmage", "scrimmage-core"):
        build = ["uv", "build", "--wheel", "--package", package, "--out-dir", str(wheels), "-q"]
        sh(build, timeout=BUILD_TIMEOUT_S, cwd=REPO)
    app = SERVICE / "app"
    shutil.rmtree(app, ignore_errors=True)
    shutil.copytree(EXAMPLE / "acme", app / "acme", ignore=shutil.ignore_patterns("__pycache__"))


def context_hash(context: Path) -> str:
    """A hash of what the context holds. A wheel counts by the files inside it, as a zip's
    own bytes change from one build to the next."""
    digest = hashlib.sha256()
    for path in sorted(p for p in context.rglob("*") if p.is_file()):
        relative = path.relative_to(context).as_posix()
        if relative.split("/")[0] not in ("Dockerfile", ".dockerignore", "wheels", "app"):
            continue  # not in the context: compose.yaml, canaries, this build's leftovers
        digest.update(relative.encode())
        if path.suffix == ".whl":
            with zipfile.ZipFile(path) as wheel:
                for info in sorted(wheel.infolist(), key=lambda i: i.filename):
                    digest.update(f"{info.filename}:{info.CRC}".encode())
        else:
            digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def image_exists(tag: str) -> bool:
    return subprocess.run(["docker", "image", "inspect", tag], capture_output=True).returncode == 0


def build(context: Path, tag: str, *args: str) -> None:
    """docker build with no network: everything it installs was staged first."""
    command = ["docker", "build", "-q", "--network", "none", "-t", tag, *args, str(context)]
    sh(command, timeout=BUILD_TIMEOUT_S)


@dataclass(frozen=True)
class Images:
    service: str  # the product image
    egress: str  # the product image plus the egress audit hook
    key: str


def ensure_images() -> Images:
    with locked("build"):
        stage()
        key = context_hash(SERVICE)
        hook = hashlib.sha256(key.encode())
        for path in (HERE / "egress.Dockerfile", *sorted(HOOK.glob("*.p*"))):
            hook.update(path.read_bytes())  # the .pth and the .py, not a stale __pycache__
        images = Images(
            f"scrimmage-compose-service:{key}",
            f"scrimmage-compose-egress:{hook.hexdigest()[:16]}",
            key,
        )
        written: list[Path] = []
        try:
            for name, text in CANARIES.items():
                (SERVICE / name).write_text(text)
                written.append(SERVICE / name)
            if not image_exists(images.service):
                build(SERVICE, images.service)
        finally:
            for path in written:
                path.unlink(missing_ok=True)
        if not image_exists(images.egress):
            args = ["-f", str(HERE / "egress.Dockerfile"), "--build-arg", f"BASE={images.service}"]
            build(HOOK, images.egress, *args)
    return images


# A project


@dataclass
class Certs:
    folder: Path

    @property
    def ca(self) -> Path:
        return self.folder / "ca.pem"

    @property
    def server_pem(self) -> Path:
        return self.folder / "server.pem"

    @property
    def server_key(self) -> Path:
        return self.folder / "server.key"


def make_certs(folder: Path, gateway: str) -> Certs:
    names = ["--ip", gateway, "--name", "host.docker.internal", "--out", str(folder)]
    sh([str(BIN / "scrimmage"), "cert", *names], timeout=60)
    return Certs(folder)


def clean_env() -> dict[str, str]:
    """This process's environment without any Scrimmage setting, so nothing leaks into a
    container's variables or the runner's."""
    return {k: v for k, v in os.environ.items() if not k.startswith("SCRIMMAGE_")}


class Project:
    """One compose project: its files, its variables, and `up` and `down`."""

    def __init__(
        self,
        folder: Path,
        image: str,
        *,
        gateway: str,
        certs: Certs,
        control_port: int,
        model_url: str,
        uid: int | None = None,
        ca: Path | None = None,
    ) -> None:
        self.name = f"scrimmage-t-{uuid.uuid4().hex[:10]}"
        self.folder = folder
        self.token_file = folder / "token"
        self.token_file.write_text(secrets.token_hex(32))
        self.token_file.chmod(0o600)
        self.egress_dir = folder / "egress"
        self.egress_dir.mkdir(mode=0o700)
        self.egress_log = self.egress_dir / "log"
        self.env = clean_env() | {
            "SCRIMMAGE_IMAGE": image,
            "SCRIMMAGE_UID": str(os.getuid() if uid is None else uid),
            "SCRIMMAGE_GID": str(os.getgid() if uid is None else uid),
            "SCRIMMAGE_TOKEN_FILE": str(self.token_file),
            "SCRIMMAGE_CA_FILE": str(ca or certs.ca),
            "SCRIMMAGE_CONTROL_PORT": str(control_port),
            "TEST_MODEL_URL": model_url,
            "TEST_EGRESS_DIR": str(self.egress_dir),
        }
        self.gateway = gateway

    def compose(
        self, *args: str, timeout: float = 120, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        files = ["-f", str(SERVICE / "compose.yaml"), "-f", str(SERVICE / "compose.test.yaml")]
        base = ["docker", "compose", "-p", self.name, "--project-directory", str(SERVICE), *files]
        return sh([*base, *args], timeout=timeout, env=self.env, check=check)

    def up(self) -> None:
        self.compose("up", "-d", "--pull", "never", "--no-build")

    def down(self) -> None:
        self.compose("down", "-v", "--remove-orphans", timeout=180, check=False)

    def logs(self) -> str:
        done = self.compose("logs", "--no-color", check=False)
        return done.stdout + done.stderr

    def state(self) -> str:
        """The agent container's state, such as running or exited; empty when there is none."""
        out = self.compose("ps", "-a", "--format", "json", check=False).stdout
        rows = [json.loads(line) for line in out.splitlines() if line.strip()]
        return str(rows[0]["State"]) if rows else ""

    def port(self) -> int:
        out = self.compose("port", "agent", str(SERVICE_PORT)).stdout.strip()
        return int(out.rsplit(":", 1)[1])

    def wait_ready(self) -> None:
        """Both workers have loaded the app and the port is bound; fails early if the
        container ends."""
        deadline = time.monotonic() + UP_TIMEOUT_S
        while True:
            text = self.logs()
            if text.count("Application startup complete") >= 2 and "Uvicorn running on" in text:
                return
            if self.state() in ("exited", "dead"):
                raise AssertionError(f"the container ended before it was ready:\n{text[-3000:]}")
            if time.monotonic() > deadline:
                raise AssertionError(f"not ready in {UP_TIMEOUT_S} s:\n{text[-3000:]}")
            time.sleep(0.5)

    def leftovers(self) -> dict[str, list[str]]:
        """What Docker still holds under this project's label."""
        label = f"label=com.docker.compose.project={self.name}"
        commands = {
            "containers": ["docker", "ps", "-a", "-q", "--filter", label],
            "networks": ["docker", "network", "ls", "-q", "--filter", label],
            "volumes": ["docker", "volume", "ls", "-q", "--filter", label],
        }
        return {k: sh(c, timeout=60).stdout.split() for k, c in commands.items()}


# What the runner is told


def example_tests() -> str:
    """The example's tests: the part of tests/example/scrimmage.toml from its first [[test]]."""
    text = (EXAMPLE / "scrimmage.toml").read_text()
    return text[text.index("[[test]]") :]


def service_file(
    published_port: int, gateway: str, control_port: int, certs: Certs, runs: int
) -> str:
    tools = [
        "acme.tools:get_order",
        "acme.tools:check_inventory",
        "acme.tools:refund_order",
        "acme.tools:get_stock",
    ]
    return (
        "[agent]\n"
        f'service     = "http://127.0.0.1:{published_port}/chat"\n'
        'body        = {message = "{question}", session = "{session}"}\n'
        'answer      = "reply"\n'
        f"tools       = {json.dumps(tools)}\n"
        'real        = ["tool:check_inventory", "process:uname"]\n'
        f'model_hosts = [{{host = "{gateway}", provider = "openai"}}]\n'
        f'control     = "{gateway}:{control_port}"\n'
        f'control_tls = {{cert = "{certs.server_pem}", key = "{certs.server_key}"}}\n'
        f"runs = {runs}\n"
        "record_grace_s = 0.2\n"
        "record_wait_s = 10\n"
        "run_timeout = 30\n\n" + example_tests()
    )


def run_scrimmage(
    project: Project, file: Path, *args: str, timeout: float = 300
) -> subprocess.CompletedProcess[str]:
    """`scrimmage ARGS file` as a user runs it: its own process, the token file in its
    environment, records beside the file."""
    env = clean_env() | {"SCRIMMAGE_TOKEN_FILE": str(project.token_file)}
    command = [str(BIN / "scrimmage"), *args, str(file)]
    return sh(command, timeout=timeout, env=env, cwd=file.parent, check=False)


# The egress log


def _address(raw: Any) -> tuple[str, int] | None:
    if not isinstance(raw, list) or len(raw) < 2:
        return None
    host, port = str(raw[0]), raw[1]
    host = host.removeprefix("::ffff:")
    return (host, port) if isinstance(port, int) else None


def egress_problems(
    records: list[dict[str, Any]],
    allowed: set[tuple[str, int]],
    names: set[str],
    known_children: tuple[list[str], ...] = (),
) -> list[str]:
    """Everything in an egress log that is not allowed.

    allowed: the (address, port) pairs a process may connect to or send to.
    names: the host names it may resolve.
    known_children: the argv of programs that are not Python, so write no start line.
    Three kinds of problem: a process whose hook came after Scrimmage's startup file, a network
    address or name outside the lists, and a child process no start line accounts for.
    """
    problems: list[str] = []
    starts = [r for r in records if r["event"] == "start"]
    for start in starts:
        if start["scrimmage_core"] or start["sitecustomize"]:
            problems.append(f"pid {start['pid']}: the hook came after Scrimmage's startup: {start}")
    for r in records:
        if r["event"] in ("connect", "sendto", "sendmsg"):
            if _address(r["address"]) not in allowed:
                problems.append(f"pid {r['pid']}: {r['event']} to {r['address']}")
        elif r["event"] == "getaddrinfo" and r["host"] not in names:
            problems.append(f"pid {r['pid']}: resolved {r['host']!r}")
    problems += _unwatched_children(records, known_children)
    return problems


def _unwatched_children(records: list[dict[str, Any]], known: tuple[list[str], ...]) -> list[str]:
    """A popen or spawn line of pid P is matched by a start line whose ppid is P; an exec line
    of pid P by a second start line of P. A child that wrote no start line, such as `python -S`
    or a program that is not Python, leaves its parent's line unmatched, unless the program is
    one of `known`. A child started below subprocess, as multiprocessing's spawn does, has no
    line of its own: `live_pids` finds one that is still running."""
    problems: list[str] = []
    pids = {r["pid"] for r in records}
    for pid in sorted(pids):
        own = [r for r in records if r["pid"] == pid]
        started = sum(1 for r in own if r["event"] == "start")
        execs = sum(1 for r in own if r["event"] == "exec")
        if started != 1 + execs:
            problems.append(f"pid {pid}: {execs} exec lines but {started} start lines")
        made = sum(1 for r in own if r["event"] in ("popen", "spawn") and r["argv"] not in known)
        children = sum(1 for r in records if r["event"] == "start" and r.get("ppid") == pid)
        if children < made:
            problems.append(f"pid {pid} started {made} children, only {children} wrote a start")
    return problems


def read_egress(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def live_pids(project: Project) -> set[int]:
    """The process ids alive in the agent container now, read from its /proc by the shell
    alone, so the reader starts no Python of its own. Its own pid is left out."""
    script = 'for p in /proc/[0-9]*; do echo "${p#/proc/}"; done; echo "self $$"'
    out = project.compose("exec", "-T", "agent", "sh", "-c", script, timeout=60).stdout
    lines = out.split()
    own = int(lines[lines.index("self") + 1])
    return {int(x) for x in lines if x.isdigit()} - {own}
