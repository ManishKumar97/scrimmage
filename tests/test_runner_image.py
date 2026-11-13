"""The runner image: the product's container, with no team code and no secret in it.

Run them in one process: `pytest -n 0 -m docker tests/test_runner_image.py`, and check the
count with `tests/compose/check_junit.py`. Docker must work: a daemon that fails makes a test
fail, not skip.
"""

import json
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


# Run inside the image: the top-level entries of site-packages, and every startup file on
# sys.path outside scrimmage_core/_boot.
INSPECT = """
import json, os, sys, sysconfig
top = sorted(os.listdir(sysconfig.get_paths()["purelib"]))
startup = []
for base in sys.path:
    for root, _, files in os.walk(base) if os.path.isdir(base) else []:
        if root.endswith("scrimmage_core/_boot") or "/scrimmage_core/_boot/" in root + "/":
            continue
        for f in files:
            if f.endswith(".pth") or f.split(".")[0] in ("sitecustomize", "usercustomize"):
                startup.append(os.path.join(root, f))
print(json.dumps({"site_packages": top, "startup_files": sorted(set(startup))}))
"""


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

    # A lint on the Dockerfile text, not a guarantee: the image scan above is the guarantee.
    # No COPY or ADD at all, and the only RUN that mounts or reads another stage is the install.
    text = (images.RUNNER / "Dockerfile").read_text().replace("\\\n", " ")
    lines = [line.strip() for line in text.splitlines()]
    assert [x for x in lines if re.match(r"(COPY|ADD)\b", x, re.IGNORECASE)] == []
    mounting = [
        x for x in lines if re.match(r"RUN\b", x, re.IGNORECASE) and re.search(r"--mount|from=", x)
    ]
    assert len(mounting) == 1 and "pip install --no-index" in mounting[0], mounting

    # The positive controls: the same Dockerfile with a COPY of the token, and with a RUN
    # that bind-mounts the whole context and copies it, each with the canary in the context
    # and a weakened .dockerignore, must be found by the same scan, or "found nothing" above
    # means nothing.
    controls = {
        "copy": "COPY token /token\n",
        "bind": "RUN --mount=type=bind,target=/ctx cp -r /ctx /src\n",
    }
    for name, extra in controls.items():
        control = f"{image}-control-{name}"
        context = tmp_path / f"control-{name}"
        context.mkdir()
        dockerfile = (images.RUNNER / "Dockerfile").read_text().replace("USER ", extra + "USER ", 1)
        (context / "Dockerfile").write_text(dockerfile)
        shutil.copytree(images.RUNNER / "wheels", context / "wheels")
        # Not the image's .dockerignore: its `*token*` line would drop the canary on purpose.
        (context / ".dockerignore").write_text("*\n!Dockerfile\n!wheels/\n!token\n")
        (context / "token").write_text(harness.CANARIES["token"])
        try:
            harness.build(context, control)
            leaking = tmp_path / f"control-{name}.tar"
            harness.sh(["docker", "save", "-o", str(leaking), control], timeout=300)
            hits = scan_image(leaking, canaries)
            assert [h.file for h in hits] and all(h.file.endswith("token") for h in hits), name
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

    # pip list cannot see code that has no .dist-info, so read site-packages itself, and look
    # for startup files that would run code in every Python: a .pth, sitecustomize, usercustomize.
    found = json.loads(run(image, "-I", "-c", INSPECT, entrypoint="python").stdout)
    allowed = (
        r"README\.txt|pip|pip-[^/]+\.dist-info|scrimmage|scrimmage-[^/]+\.dist-info"
        r"|scrimmage_core|scrimmage_core-[^/]+\.dist-info"
    )
    stray = [x for x in found["site_packages"] if not re.fullmatch(allowed, x)]
    assert stray == [], f"in site-packages: {stray}"
    assert found["startup_files"] == []


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
