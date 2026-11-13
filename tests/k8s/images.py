"""The runner image for the kind tests: stage it, build it, load it into a cluster.

The build follows the compose harness: wheels staged first, `--network none`, a tag that is
a hash of the context, and one build at a time under the harness's lock. Nothing is pushed.
"""

import shutil
from pathlib import Path

from compose import harness

RUNNER = harness.REPO / "images" / "runner"


def stage_runner() -> None:
    wheels = RUNNER / "wheels"
    shutil.rmtree(wheels, ignore_errors=True)  # only the wheels staged now enter the build
    wheels.mkdir()
    harness.stage_workspace_wheels(wheels)


def runner_tag() -> str:
    return f"scrimmage-runner:{harness.context_hash(RUNNER)}"


def ensure_runner_image() -> str:
    """Build the runner image if its tag is not there yet, and return the tag. The canaries sit
    in the context while it builds, so the image shows that the context rules keep them out."""
    with harness.locked("build"):
        stage_runner()
        tag = runner_tag()
        if harness.image_exists(tag):
            return tag
        written: list[Path] = []
        try:
            for name, text in harness.CANARIES.items():
                (RUNNER / name).write_text(text)
                written.append(RUNNER / name)
            harness.build(RUNNER, tag)
        finally:
            for path in written:
                path.unlink(missing_ok=True)
    return tag


def kind_load(tag: str, cluster: str) -> None:
    """Copy a local image into the cluster's nodes; the manifests use imagePullPolicy Never."""
    harness.sh(["kind", "load", "docker-image", tag, "--name", cluster], timeout=300)
