"""The Docker Compose harness's standalone pieces: JUnit check, image scanner, egress hook."""

import gzip
import io
import json
import shutil
import socket
import subprocess
import sys
import tarfile
import uuid
from pathlib import Path

import pytest

from compose import check_junit
from compose.image_scan import PEM_PRIVATE_KEY, UnreadableLayer, scan_image

HOOK_DIR = Path(__file__).parent / "compose" / "egress_hook"
CANARY = "CANARY-7f3a9c1e-token"
REAL_PEM = (
    "-----BEGIN PRIVATE KEY-----\n"
    "MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQg" + "A" * 40 + "\n"
    "-----END PRIVATE KEY-----\n"
)


# check_junit


def _junit(tmp_path: Path, **counts: int) -> Path:
    attrs = {"tests": 6, "skipped": 0, "failures": 0, "errors": 0} | counts
    text = " ".join(f'{k}="{v}"' for k, v in attrs.items())
    path = tmp_path / "r.xml"
    path.write_text(f'<?xml version="1.0"?><testsuites><testsuite name="p" {text}/></testsuites>')
    return path


def test_junit_accepts_exactly_n_passed(tmp_path: Path) -> None:
    assert check_junit.main(["x", str(_junit(tmp_path)), "6"]) == 0


@pytest.mark.parametrize(
    "counts",
    [{"skipped": 1}, {"failures": 1}, {"errors": 1}, {"tests": 5}, {"tests": 7}],
)
def test_junit_rejects_anything_but_n_passed(tmp_path: Path, counts: dict[str, int]) -> None:
    assert check_junit.main(["x", str(_junit(tmp_path, **counts)), "6"]) == 1


def test_junit_accepts_a_bare_testsuite_root(tmp_path: Path) -> None:
    path = tmp_path / "r.xml"
    path.write_text('<testsuite tests="2" skipped="0" failures="0" errors="0"/>')
    assert check_junit.main(["x", str(path), "2"]) == 0


def test_junit_rejects_missing_empty_and_garbled_files(tmp_path: Path) -> None:
    empty = tmp_path / "e.xml"
    empty.write_text("<testsuites/>")
    garbled = tmp_path / "g.xml"
    garbled.write_text("not xml")
    for path in (tmp_path / "none.xml", empty, garbled):
        assert check_junit.main(["x", str(path), "6"]) == 1
    assert check_junit.main(["x"]) == 2


# image scanner


def _layer(files: dict[str, bytes], gz: bool = False) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return gzip.compress(buf.getvalue()) if gz else buf.getvalue()


def _image(tmp_path: Path, layers: list[bytes]) -> Path:
    path = tmp_path / f"{uuid.uuid4().hex}.tar"
    with tarfile.open(path, "w") as tar:
        for i, blob in enumerate(layers):
            info = tarfile.TarInfo(f"blobs/sha256/{i:064d}")
            info.size = len(blob)
            tar.addfile(info, io.BytesIO(blob))
        manifest = b'{"layers": []}'
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest)
        tar.addfile(info, io.BytesIO(manifest))
    return path


def test_scanner_finds_nothing_in_a_clean_image(tmp_path: Path) -> None:
    image = _image(tmp_path, [_layer({"bin/app": b"hello PRIVATE KEY in a binary"})])
    assert scan_image(image, [CANARY]) == []


def test_scanner_finds_a_canary_in_a_plain_layer(tmp_path: Path) -> None:
    image = _image(tmp_path, [_layer({"a": b"x"}), _layer({"token": CANARY.encode()})])
    hits = scan_image(image, [CANARY])
    assert [(h.file, h.what) for h in hits] == [("token", f"canary {CANARY!r}")]


def test_scanner_reads_gzip_layers_that_a_raw_grep_misses(tmp_path: Path) -> None:
    layer = _layer({"etc/secret": CANARY.encode()}, gz=True)
    image = _image(tmp_path, [layer])
    assert CANARY.encode() not in image.read_bytes()
    assert [h.file for h in scan_image(image, [CANARY])] == ["etc/secret"]


def test_scanner_finds_a_real_looking_pem_key(tmp_path: Path) -> None:
    image = _image(tmp_path, [_layer({"server.key": REAL_PEM.encode()})])
    hits = scan_image(image, [])
    assert [(h.file, h.what) for h in hits] == [("server.key", "pem private key")]


def test_pem_regex_ignores_the_bare_marker_text() -> None:
    assert not PEM_PRIVATE_KEY.search(b"-----BEGIN PRIVATE KEY-----\n")
    assert not PEM_PRIVATE_KEY.search(b"PRIVATE KEY")


def test_scanner_searches_the_manifest_and_config_bytes_too(tmp_path: Path) -> None:
    path = tmp_path / "m.tar"
    data = CANARY.encode()
    with tarfile.open(path, "w") as tar:
        info = tarfile.TarInfo("config.json")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    assert [h.file for h in scan_image(path, [CANARY])] == ["config.json"]


def test_scanner_fails_on_a_layer_it_cannot_read(tmp_path: Path) -> None:
    zstd_blob = b"\x28\xb5\x2f\xfd" + b"\x00" * 32
    with pytest.raises(UnreadableLayer):
        scan_image(_image(tmp_path, [zstd_blob]), [CANARY])
    truncated = gzip.compress(_layer({"a": b"x" * 5000}))[:60]
    with pytest.raises(UnreadableLayer):
        scan_image(_image(tmp_path, [truncated]), [CANARY])


def test_scanner_rejects_an_empty_canary(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="empty canary"):
        scan_image(_image(tmp_path, [_layer({"a": b"x"})]), [""])


# egress hook


def _run_with_hook(tmp_path: Path, code: str) -> list[dict[str, object]]:
    log = tmp_path / "egress.log"
    site_dir = tmp_path / "site"
    shutil.copytree(HOOK_DIR, site_dir, ignore=shutil.ignore_patterns("__pycache__"))
    bootstrap = f"import site; site.addsitedir({str(site_dir)!r})\n{code}"
    subprocess.run(
        [sys.executable, "-c", bootstrap],
        check=True,
        env={"EGRESS_AUDIT_LOG": str(log), "PATH": ""},
        timeout=30,
    )
    return [json.loads(line) for line in log.read_text().splitlines()]


@pytest.mark.slow
def test_hook_logs_a_start_line_first_then_each_connect(tmp_path: Path) -> None:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        code = (
            "import socket, threading\n"
            f"socket.create_connection(('127.0.0.1', {port})).close()\n"
            "def go():\n"
            f"    socket.create_connection(('127.0.0.1', {port})).close()\n"
            "t = threading.Thread(target=go)\n"
            "t.start(); t.join()\n"
        )
        records = _run_with_hook(tmp_path, code)
    assert records[0]["event"] == "start"
    assert records[0]["scrimmage_core"] is False
    assert records[0]["sitecustomize"] is False
    connects = [r for r in records if r["event"] == "connect"]
    assert [r["address"] for r in connects] == [["127.0.0.1", port]] * 2
    assert {r["pid"] for r in records} == {records[0]["pid"]}


@pytest.mark.slow
def test_hook_start_line_shows_a_module_that_was_already_loaded(tmp_path: Path) -> None:
    log = tmp_path / "egress.log"
    site_dir = tmp_path / "site"
    shutil.copytree(HOOK_DIR, site_dir, ignore=shutil.ignore_patterns("__pycache__"))
    (tmp_path / "scrimmage_core.py").write_text("")
    code = (
        f"import sys; sys.path.insert(0, {str(tmp_path)!r}); import scrimmage_core\n"
        f"import site; site.addsitedir({str(site_dir)!r})\n"
    )
    subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        env={"EGRESS_AUDIT_LOG": str(log), "PATH": ""},
        timeout=30,
    )
    start = json.loads(log.read_text().splitlines()[0])
    assert start["scrimmage_core"] is True


@pytest.mark.slow
def test_hook_is_silent_without_a_log_path(tmp_path: Path) -> None:
    site_dir = tmp_path / "site"
    shutil.copytree(HOOK_DIR, site_dir, ignore=shutil.ignore_patterns("__pycache__"))
    code = f"import site; site.addsitedir({str(site_dir)!r})"
    subprocess.run([sys.executable, "-c", code], check=True, env={"PATH": ""}, timeout=30)
    assert list(tmp_path.iterdir()) == [site_dir]


# Docker positive control


def _docker_usable() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True, timeout=60).returncode == 0


@pytest.mark.docker
@pytest.mark.slow
def test_scanner_finds_a_canary_in_a_real_image_and_nothing_without_it(tmp_path: Path) -> None:
    if not _docker_usable():
        pytest.skip("docker daemon is not available")
    canary = f"CANARY-{uuid.uuid4().hex}"
    tags = {name: f"scrimmage-scan-{name}-{uuid.uuid4().hex[:8]}" for name in ("with", "without")}
    try:
        for name, tag in tags.items():
            context = tmp_path / name
            context.mkdir()
            (context / "filler").write_text("filler\n")
            lines = ["FROM scratch", "COPY filler /filler"]
            if name == "with":
                (context / "token").write_text(canary)
                lines.append("COPY token /token")
            (context / "Dockerfile").write_text("\n".join(lines) + "\n")
            subprocess.run(
                ["docker", "build", "-q", "-t", tag, str(context)],
                check=True,
                capture_output=True,
                timeout=300,
            )
            subprocess.run(
                ["docker", "save", "-o", str(tmp_path / f"{name}.tar"), tag],
                check=True,
                capture_output=True,
                timeout=300,
            )
        with_hits = scan_image(tmp_path / "with.tar", [canary])
        assert [h.file for h in with_hits] == ["token"]
        assert scan_image(tmp_path / "without.tar", [canary]) == []
    finally:
        subprocess.run(["docker", "rmi", "-f", *tags.values()], capture_output=True, timeout=120)
