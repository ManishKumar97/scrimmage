"""The runner image: the product's container, with no team code and no secret in it.

Run them in one process: `pytest -n 0 -m docker tests/test_runner_image.py`, and check the
count with `tests/compose/check_junit.py`. Docker must work: a daemon that fails makes a test
fail, not skip.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from compose import harness
from compose.image_scan import scan_image
from k8s import images

pytestmark = [pytest.mark.slow, pytest.mark.docker]


@pytest.fixture(scope="session")
def image() -> str:
    harness.require_docker()
    return images.ensure_runner_image()


def run(image: str, *args: str, entrypoint: str | None = None) -> subprocess.CompletedProcess[str]:
    command = ["docker", "run", "--rm", "--network", "none"]
    if entrypoint is not None:
        command += ["--entrypoint", entrypoint]
    return harness.sh([*command, image, *args], timeout=120)


def test_the_runner_image_runs_as_uid_10001(image: str) -> None:
    assert run(image, "-u", entrypoint="id").stdout.strip() == "10001"
    assert run(image, "-g", entrypoint="id").stdout.strip() == "10001"
    assert "scrimmage" in run(image, "--help").stdout


def test_the_runner_image_holds_no_secret(image: str, tmp_path: Path) -> None:
    canaries = list(harness.CANARIES.values())
    archive = tmp_path / "runner.tar"
    harness.sh(["docker", "save", "-o", str(archive), image], timeout=300)
    assert scan_image(archive, canaries) == []

    # The Dockerfile copies the wheels and nothing else, so no canary can enter by a COPY.
    copies = [
        line
        for line in (images.RUNNER / "Dockerfile").read_text().splitlines()
        if re.match(r"\s*(COPY|ADD)\b", line, re.IGNORECASE)
    ]
    assert copies == ["COPY wheels/ /wheels/"]

    # The positive control: the same Dockerfile with `COPY token /token`, and the canary in
    # the context, must be found by the same scan, or "found nothing" above means nothing.
    control = f"{image}-control"
    context = tmp_path / "control-context"
    context.mkdir()
    shutil.copy(images.RUNNER / "Dockerfile", context / "Dockerfile")
    shutil.copytree(images.RUNNER / "wheels", context / "wheels")
    # Not the image's .dockerignore: its `*token*` line would drop the canary on purpose.
    (context / ".dockerignore").write_text("*\n!Dockerfile\n!wheels/\n!token\n")
    with (context / "Dockerfile").open("a") as dockerfile:
        dockerfile.write("COPY token /token\n")
    (context / "token").write_text(harness.CANARIES["token"])
    try:
        harness.build(context, control)
        leaking = tmp_path / "control.tar"
        harness.sh(["docker", "save", "-o", str(leaking), control], timeout=300)
        assert [h.file for h in scan_image(leaking, canaries)] == ["token"]
    finally:
        subprocess.run(["docker", "rmi", "-f", control], capture_output=True, timeout=120)


def test_the_runner_image_holds_no_agent_code(image: str) -> None:
    probe = harness.sh(
        ["docker", "run", "--rm", "--network", "none", "--entrypoint", "test", image, "-e", "/app"],
        timeout=120,
        check=False,
    )
    assert probe.returncode == 1, "the image has an /app folder"
    listing = run(image, "-m", "pip", "list", "--format=freeze", entrypoint="python").stdout
    names = sorted(line.split("==")[0] for line in listing.splitlines())
    assert names == ["pip", "scrimmage", "scrimmage-core"]


def test_the_runner_image_builds_with_no_network(image: str) -> None:
    """A second build, without the cache and with the network off, from the staged context."""
    tag = f"{image}-nonet"
    try:
        with harness.locked("build"):
            images.stage_runner()
            harness.build(images.RUNNER, tag, "--no-cache")
        assert harness.image_exists(tag)
    finally:
        subprocess.run(["docker", "rmi", "-f", tag], capture_output=True, timeout=120)
