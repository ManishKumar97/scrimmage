"""The runner image for the kind tests: stage it, build it, load it into a cluster.

The build follows the compose harness: wheels staged first, `--network none`, a tag that is
a hash of the context, and one build at a time under the harness's lock. Nothing is pushed.
"""

import shutil
from pathlib import Path

from compose import harness

RUNNER = harness.REPO / "images" / "runner"
FAKE_MODEL = Path(__file__).parent / "fake_model"


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


def stage_fake_model() -> None:
    """The server, the fake it runs and the fixtures it answers from, in the build context."""
    app = FAKE_MODEL / "app"
    shutil.rmtree(app, ignore_errors=True)
    tests = harness.REPO / "tests"
    (app / "fakes").mkdir(parents=True)
    for name in ("__init__.py", "openai_server.py"):
        shutil.copy(tests / "fakes" / name, app / "fakes" / name)
    shutil.copytree(tests / "fixtures" / "openai", app / "fixtures" / "openai")
    shutil.copy(FAKE_MODEL / "serve.py", app / "serve.py")


def ensure_fake_model_image() -> str:
    """Build the fake model image if its tag is not there yet, and return the tag."""
    with harness.locked("build"):
        stage_fake_model()
        tag = f"scrimmage-fake-model:{harness.context_hash(FAKE_MODEL)}"
        if not harness.image_exists(tag):
            harness.build(FAKE_MODEL, tag)
    return tag


def ensure_agent_image() -> str:
    """The acme order bot as a service, the image the Compose tests build and run. It has no
    USER line; the pod's securityContext sets the user."""
    return harness.ensure_images().service


def kind_load(tag: str, cluster: str) -> None:
    """Copy a local image into the cluster's nodes; the manifests use imagePullPolicy Never."""
    harness.sh(["kind", "load", "docker-image", tag, "--name", cluster], timeout=300)


def ensure_egress_image() -> str:
    """The agent image plus the egress audit hook, for the test that reads what each process
    connects to. The product image has no hook."""
    return harness.ensure_images().egress
