"""The acme service in a Docker container, with the runner on the host, over TLS.

Each test brings up its own compose project (a unique name, taken down in a `finally`), a fake
model on the host at the bridge address, and a runner that listens there too. Run them in one
process: `pytest -n 0 -m docker tests/test_service_compose.py`, and check the count with
`tests/compose/check_junit.py`. Docker must work: a daemon that fails makes a test fail, not skip.
"""

import json
import re
import shutil
import socket
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from compose import harness
from compose.harness import Certs, Images, Project
from compose.image_scan import scan_image
from conftest import ACME
from fakes.openai_server import FakeOpenAI
from ports import free_port

pytestmark = [pytest.mark.slow, pytest.mark.docker]

ACTIVE = re.compile(r"scrimmage: active in service mode \(pid (\d+)\)")
NON_PYTHON = (
    ("uname", ["uname", "-p"]),
)  # what the example's `real = ["process:uname"]` lets openai run


@pytest.fixture(scope="session")
def images() -> Images:
    harness.require_docker()
    return harness.ensure_images()


@dataclass
class Bench:
    project: Project
    fake: FakeOpenAI
    certs: Certs
    gateway: str
    control_port: int
    folder: Path

    def up(self) -> None:
        self.project.up()
        self.project.wait_ready()

    def file(self, runs: int = 3) -> Path:
        path = self.folder / "service.toml"
        text = harness.service_file(
            self.project.port(), self.gateway, self.control_port, self.certs, runs
        )
        path.write_text(text)
        return path


@contextmanager
def running(
    image: str,
    *,
    uid: int | None = None,
    other_ca: bool = False,
    folder: Path | None = None,
) -> Iterator[Bench]:
    """A project that is down again, whatever happens, when the block ends."""
    own = folder is None
    folder = folder or Path(tempfile.mkdtemp(prefix="scrimmage-compose-"))
    gateway = harness.bridge_gateway()
    certs = harness.make_certs(folder / "cert", gateway)
    ca = harness.make_certs(folder / "other-cert", gateway).ca if other_ca else None
    fake = FakeOpenAI(ACME, host=gateway)
    fake.start()
    control_port = free_port(also=gateway)
    project = Project(
        folder,
        image,
        gateway=gateway,
        certs=certs,
        control_port=control_port,
        model_url=fake.base_url,
        uid=uid,
        ca=ca,
    )
    with harness.locked("run"):
        try:
            yield Bench(project, fake, certs, gateway, control_port, folder)
        finally:
            project.down()
            fake.stop()
            if own:
                shutil.rmtree(folder, ignore_errors=True)


def record_lines(records: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for path in sorted(records.glob("*.jsonl"))
        for line in path.read_text().splitlines()
    ]


def test_the_service_in_a_container_runs_a_test_over_tls(images: Images, tmp_path: Path) -> None:
    with running(images.service, folder=tmp_path) as bench:
        bench.up()
        done = harness.run_scrimmage(bench.project, bench.file(3), "test", "--json")
        assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
        result = json.loads(done.stdout)
        rows = {r["row"]: r for r in result["rows"]}
        assert set(rows) == {"baseline", "fault 1"}
        for name, fired in (("baseline", None), ("fault 1", True)):
            row = rows[name]
            assert (row["result"], row["done"], row["passed"]) == ("pass", 3, 3), row
            assert [d["outcome"] for d in row["run_details"]] == ["answered"] * 3
            assert [d["fault_fired"] for d in row["run_details"]] == [fired] * 3
        assert "unable" in rows["fault 1"]["run_details"][0]["answer_preview"].lower()
        assert "Oct 30" in rows["baseline"]["run_details"][0]["answer_preview"]

        # The setup request and 6 runs were all claimed, and the ping was verified TLS.
        assert result["runs_sent"] == result["claims_received"] == 7
        ping = result["control_ping"]
        assert (ping["reached"], ping["tls"]) == (True, "verified")
        assert ping["url"] == f"https://host.docker.internal:{bench.control_port}"

        # Two workers, seen from inside the container: their own pids and the container's host
        # name, not the pids or the name of this machine.
        pids = {int(m.group(1)) for m in ACTIVE.finditer(bench.project.logs())}
        assert len(pids) == 2
        lines = record_lines(Path(result["records"]))
        procs = {(x["proc"]["host"], x["proc"]["pid"]) for x in lines if "proc" in x}
        hosts = {host for host, _ in procs}
        assert {pid for _, pid in procs} <= pids
        assert len(hosts) == 1 and hosts != {socket.gethostname()}


def test_the_container_connects_only_to_the_control_address_and_the_model_host(
    images: Images, tmp_path: Path
) -> None:
    with running(images.egress, folder=tmp_path) as bench:
        bench.up()
        done = harness.run_scrimmage(bench.project, bench.file(2), "test", "--json")
        assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
        result = json.loads(done.stdout)
        gateway, control = bench.gateway, bench.control_port
        model = int(bench.fake.base_url.split(":")[2].split("/")[0])
        records = harness.read_egress(bench.project.egress_log)
        alive = harness.live_pids(bench.project)

        # Positive controls: the hook saw the control channel once per claimed run at least,
        # and the model; and it was in place before Scrimmage's startup file in every process.
        connects = [r for r in records if r["event"] == "connect"]
        to_control = [r for r in connects if r["address"] == [gateway, control]]
        to_model = [r for r in connects if r["address"] == [gateway, model]]
        assert len(to_control) >= result["claims_received"] > 0
        model_calls = [
            x for x in record_lines(Path(result["records"])) if x.get("call") == "model:openai"
        ]
        assert model_calls and len(bench.fake.paths) == len(model_calls)
        assert len(to_model) >= 1
        starts = [r for r in records if r["event"] == "start"]
        assert len(starts) >= 5, starts  # the supervisor twice (exec), the tracker, two workers
        late = [s for s in starts if s["scrimmage_core"] or s["sitecustomize"]]
        assert late == [], f"the hook came after Scrimmage's startup file: {late}"

        # Nothing else: every address, every name, every child.
        allowed = {(gateway, control), (gateway, model)}
        names = {"host.docker.internal", gateway}
        problems = harness.egress_problems(records, allowed, names, NON_PYTHON)
        # The one excused program is the system's uname, not one earlier on the PATH.
        which = bench.project.compose("exec", "-T", "agent", "sh", "-c", "command -v uname")
        assert which.stdout.strip() == "/usr/bin/uname"
        assert problems == []
        # A child started below subprocess leaves no line of its own, so every process still
        # alive in the container must have written a start line.
        started = {r["pid"] for r in starts}
        assert alive and alive <= started, (alive, started)


def test_the_image_holds_no_secret(images: Images, tmp_path: Path) -> None:
    canaries = list(harness.CANARIES.values())
    for name, tag in (("service", images.service), ("egress", images.egress)):
        archive = tmp_path / f"{name}.tar"
        harness.sh(["docker", "save", "-o", str(archive), tag], timeout=300)
        assert scan_image(archive, canaries) == [], name

    # The positive control: the Dockerfile with `COPY token /token`, and the canary in the
    # context, must be found by the same scan, or "found nothing" above means nothing.
    control = f"scrimmage-compose-control:{images.key}"
    context = tmp_path / "control-context"
    context.mkdir()
    for name in ("Dockerfile", ".dockerignore"):
        shutil.copy(harness.SERVICE / name, context / name)
    for name in ("wheels", "app"):
        shutil.copytree(harness.SERVICE / name, context / name)
    # Not the service's .dockerignore: its `*token*` line would drop the canary on purpose.
    (context / ".dockerignore").write_text("*\n!Dockerfile\n!wheels/\n!app/\n!token\n")
    (context / "Dockerfile").write_text(
        (context / "Dockerfile").read_text() + "COPY token /token\n"
    )
    (context / "token").write_text(harness.CANARIES["token"])
    try:
        harness.build(context, control)
        leaking = tmp_path / "control.tar"
        harness.sh(["docker", "save", "-o", str(leaking), control], timeout=300)
        hits = scan_image(leaking, canaries)
        assert [h.file for h in hits] == ["token"]
    finally:
        subprocess.run(["docker", "rmi", "-f", control], capture_output=True, timeout=120)


def test_a_container_with_the_wrong_ca_fails_the_probe_with_exit_3(
    images: Images, tmp_path: Path
) -> None:
    with running(images.service, other_ca=True, folder=tmp_path) as bench:
        bench.up()
        done = harness.run_scrimmage(bench.project, bench.file(1), "check", "--service")
        text = done.stdout + done.stderr
        assert done.returncode == 3, text
        assert "the service cannot reach the control server" in text, text
        assert "CERTIFICATE_VERIFY_FAILED" in text and "SCRIMMAGE_CONTROL_CA" in text, text


def test_a_container_run_as_another_uid_names_the_user_setting(
    images: Images, tmp_path: Path
) -> None:
    """Root reads the file and meets the owner check; a non-root uid cannot open the 0600 file
    at all. Both must name the file's uid, the process's uid and the user: setting."""
    for uid in (0, 1234):
        folder = tmp_path / f"uid-{uid}"
        folder.mkdir()
        with running(images.service, uid=uid, folder=folder) as bench:
            bench.project.up()
            for _ in range(60):
                if bench.project.state() in ("exited", "dead"):
                    break
                time.sleep(0.5)
            logs = bench.project.logs()
            assert bench.project.state() == "exited", logs
            owner = bench.project.token_file.stat().st_uid
            said = f"the token file is owned by uid {owner} and this process runs as uid {uid}"
            assert said in logs, logs
            assert "in Docker, set user: to the file owner's uid and gid" in logs


def test_compose_down_leaves_no_container_network_or_volume(images: Images, tmp_path: Path) -> None:
    with running(images.service, folder=tmp_path) as bench:
        bench.up()
        up = bench.project.leftovers()
        # The positive control: the filter finds what is there while the project is up.
        assert all(len(found) == 1 for found in up.values()), up
        bench.project.down()
        assert bench.project.leftovers() == {"containers": [], "networks": [], "volumes": []}
