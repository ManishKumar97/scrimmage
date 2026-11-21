"""The Docker Compose harness's standalone pieces: JUnit check, image scanner, egress hook."""

import gzip
import hashlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import uuid
from pathlib import Path
from typing import cast

import pytest

from compose import check_junit
from compose.harness import egress_problems
from compose.image_scan import PEM_PRIVATE_KEY, UnreadableLayer, scan_image

SKIPPABLE_ZSTD = b"\x50\x2a\x4d\x18\x04\x00\x00\x00abcd\x28\xb5\x2f\xfd" + b"\x00" * 8
HOOK_DIR = Path(__file__).parent / "compose" / "egress_hook"
CANARY = "CANARY-7f3a9c1e-token"
REAL_PEM = (
    "-----BEGIN PRIVATE KEY-----\n"
    "MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQg" + "A" * 40 + "\n"
    "-----END PRIVATE KEY-----\n"
)


# check_junit


def _case(name: str, child: str = "") -> str:
    return f'<testcase classname="c" name="{name}">{child}</testcase>'


def _xml(tmp_path: Path, cases: list[str], suite_attrs: str = "", wrap: bool = True) -> Path:
    body = f'<testsuite name="p" {suite_attrs}>{"".join(cases)}</testsuite>'
    path = tmp_path / "r.xml"
    path.write_text(f"<testsuites>{body}</testsuites>" if wrap else body)
    return path


def _run_junit(path: Path, n: int = 6) -> int:
    return check_junit.main(["x", str(path), str(n)])


def _six(child_on_last: str = "") -> list[str]:
    return [_case(str(i)) for i in range(5)] + [_case("5", child_on_last)]


def test_junit_accepts_exactly_n_passed(tmp_path: Path) -> None:
    assert _run_junit(_xml(tmp_path, _six(), 'tests="6" skipped="0" failures="0" errors="0"')) == 0


def test_junit_accepts_a_bare_testsuite_root(tmp_path: Path) -> None:
    assert _run_junit(_xml(tmp_path, _six(), wrap=False)) == 0


@pytest.mark.parametrize(
    "child",
    [
        "<skipped/>",
        "<failure/>",
        "<error/>",
        "<rerunFailure/>",
        "<flakyFailure/>",
    ],
)
def test_junit_rejects_a_case_with_a_bad_child_whatever_the_suite_says(
    tmp_path: Path, child: str
) -> None:
    lying = 'tests="6" skipped="0" failures="0" errors="0"'
    assert _run_junit(_xml(tmp_path, _six(child), lying)) == 1


@pytest.mark.parametrize("attr", ['skipped="1"', 'failures="1"', 'errors="1"'])
def test_junit_rejects_a_suite_attribute_that_counts_a_problem(tmp_path: Path, attr: str) -> None:
    assert _run_junit(_xml(tmp_path, _six(), attr)) == 1


def test_junit_rejects_a_wrong_case_count_whatever_tests_says(tmp_path: Path) -> None:
    assert _run_junit(_xml(tmp_path, _six()[:5], 'tests="6"')) == 1
    assert _run_junit(_xml(tmp_path, [*_six(), _case("6")], 'tests="6"')) == 1


def test_junit_rejects_negative_counters(tmp_path: Path) -> None:
    assert _run_junit(_xml(tmp_path, _six(), 'tests="7" skipped="-1"')) == 1
    assert _run_junit(_xml(tmp_path, _six(), 'tests="-6"')) == 1


def test_junit_does_not_double_count_nested_suites(tmp_path: Path) -> None:
    inner = f'<testsuite name="in" tests="6">{"".join(_six())}</testsuite>'
    path = tmp_path / "n.xml"
    path.write_text(
        f'<testsuites><testsuite name="out" tests="12">{inner}</testsuite></testsuites>'
    )
    assert _run_junit(path) == 0


@pytest.mark.slow
def test_junit_rejects_an_xfail_that_passes_under_the_repo_config(tmp_path: Path) -> None:
    case = tmp_path / "test_x.py"
    case.write_text("import pytest\n\n@pytest.mark.xfail\ndef test_x():\n    pass\n")
    config = Path(__file__).parent.parent / "pyproject.toml"

    def run(*extra: str) -> Path:
        out = tmp_path / f"{len(extra)}.xml"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-c",
                str(config),
                "-n",
                "0",
                "-p",
                "no:cacheprovider",
                f"--junitxml={out}",
                str(case),
                *extra,
            ],
            cwd=tmp_path,
            capture_output=True,
            timeout=120,
        )
        return out

    assert _run_junit(run(), 1) == 1
    # Control: without xfail_strict the same file is written as a plain pass.
    assert _run_junit(run("-o", "xfail_strict=false"), 1) == 0


def test_junit_rejects_missing_empty_and_garbled_files(tmp_path: Path) -> None:
    empty = tmp_path / "e.xml"
    empty.write_text("<testsuites/>")
    garbled = tmp_path / "g.xml"
    garbled.write_text("not xml")
    for path in (tmp_path / "none.xml", empty, garbled):
        assert _run_junit(path) == 1
    assert check_junit.main(["x"]) == 2


# image scanner


def _tar(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _layer(files: dict[str, bytes], gz: bool = False) -> bytes:
    data = _tar(files)
    return gzip.compress(data, mtime=0) if gz else data


def _digest(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def _archive(tmp_path: Path, files: dict[str, bytes]) -> Path:
    path = tmp_path / f"{uuid.uuid4().hex}.tar"
    path.write_bytes(_tar(files))
    return path


def _image(tmp_path: Path, layers: list[bytes], extra: dict[str, bytes] | None = None) -> Path:
    """An OCI layout archive: index.json, one manifest, a config and the layers."""
    blobs = {f"blobs/sha256/{_digest(b)}": b for b in layers}
    config = b'{"architecture": "amd64"}'
    blobs[f"blobs/sha256/{_digest(config)}"] = config
    manifest = json.dumps(
        {
            "config": {"digest": f"sha256:{_digest(config)}"},
            "layers": [{"digest": f"sha256:{_digest(b)}"} for b in layers],
        }
    ).encode()
    blobs[f"blobs/sha256/{_digest(manifest)}"] = manifest
    index = json.dumps({"manifests": [{"digest": f"sha256:{_digest(manifest)}"}]}).encode()
    return _archive(tmp_path, {"index.json": index, **blobs, **(extra or {})})


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


def test_scanner_reads_the_classic_manifest_json_layout(tmp_path: Path) -> None:
    layer = _layer({"token": CANARY.encode()})
    manifest = json.dumps([{"Layers": ["abc/layer.tar"]}]).encode()
    image = _archive(tmp_path, {"manifest.json": manifest, "abc/layer.tar": layer})
    assert [h.file for h in scan_image(image, [CANARY])] == ["token"]


def test_scanner_finds_a_real_looking_pem_key(tmp_path: Path) -> None:
    image = _image(tmp_path, [_layer({"server.key": REAL_PEM.encode()})])
    hits = scan_image(image, [])
    assert [(h.file, h.what) for h in hits] == [("server.key", "pem private key")]


def test_scanner_finds_a_crlf_pem_key(tmp_path: Path) -> None:
    crlf = REAL_PEM.replace("\n", "\r\n").encode()
    assert PEM_PRIVATE_KEY.search(crlf)
    image = _image(tmp_path, [_layer({"server.key": crlf})])
    assert [h.what for h in scan_image(image, [])] == ["pem private key"]


def test_pem_regex_ignores_the_bare_marker_text() -> None:
    assert not PEM_PRIVATE_KEY.search(b"-----BEGIN PRIVATE KEY-----\n")
    assert not PEM_PRIVATE_KEY.search(b"PRIVATE KEY")


def test_scanner_searches_files_outside_the_layer_list_as_bytes(tmp_path: Path) -> None:
    image = _image(tmp_path, [_layer({"a": b"x"})], extra={"extra.json": CANARY.encode()})
    assert [h.file for h in scan_image(image, [CANARY])] == ["extra.json"]


def test_scanner_fails_when_the_image_lists_no_layers_at_all(tmp_path: Path) -> None:
    with pytest.raises(UnreadableLayer, match="cannot tell"):
        scan_image(_archive(tmp_path, {"config.json": b"{}"}), [CANARY])


def test_scanner_fails_when_a_listed_layer_is_missing(tmp_path: Path) -> None:
    manifest = json.dumps([{"Layers": ["gone/layer.tar"]}]).encode()
    with pytest.raises(UnreadableLayer, match="not in the archive"):
        scan_image(_archive(tmp_path, {"manifest.json": manifest}), [CANARY])


GZIP_OF_NON_TAR = gzip.compress(b"this is not a tar archive " + CANARY.encode() * 40, mtime=0)
_NOISE = b"".join(hashlib.sha256(bytes([i])).digest() for i in range(250))
CORRUPT_DEFLATE = gzip.compress(_tar({"a": _NOISE}), mtime=0)[:200] + b"\xff" * 64


@pytest.mark.parametrize(
    "blob",
    [
        b"\x28\xb5\x2f\xfd" + b"\x00" * 32,
        SKIPPABLE_ZSTD,
        gzip.compress(_tar({"a": b"x" * 5000}), mtime=0)[:60],
        CORRUPT_DEFLATE,
        GZIP_OF_NON_TAR,
        b"BZh9" + b"\x00" * 32,
        b"\xfd7zXZ\x00" + b"\x00" * 32,
    ],
    ids=[
        "zstd",
        "zstd-skippable",
        "truncated-gzip",
        "corrupt-deflate",
        "gzip-of-non-tar",
        "bz2",
        "xz",
    ],
)
def test_scanner_fails_on_a_listed_layer_it_cannot_read(tmp_path: Path, blob: bytes) -> None:
    with pytest.raises(UnreadableLayer):
        scan_image(_image(tmp_path, [blob]), [CANARY])


@pytest.mark.parametrize(
    "blob",
    [SKIPPABLE_ZSTD, GZIP_OF_NON_TAR, CORRUPT_DEFLATE],
    ids=["zstd-skippable", "gzip-of-non-tar", "corrupt-deflate"],
)
def test_scanner_fails_on_compressed_data_that_is_not_a_listed_layer(
    tmp_path: Path, blob: bytes
) -> None:
    image = _image(tmp_path, [_layer({"a": b"x"})], extra={"blobs/sha256/config": blob})
    with pytest.raises(UnreadableLayer, match="outside the layer list"):
        scan_image(image, [CANARY])


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


def _udp_port() -> tuple[socket.socket, int]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    return sock, sock.getsockname()[1]


@pytest.mark.slow
def test_hook_logs_udp_sendto_sendmsg_without_connect_and_getaddrinfo(tmp_path: Path) -> None:
    sock, port = _udp_port()
    with sock:
        code = (
            "import socket\n"
            "s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
            f"s.sendto(b'x', ('127.0.0.1', {port}))\n"
            f"s.sendmsg([b'y'], [], 0, ('127.0.0.1', {port}))\n"
            "socket.getaddrinfo('localhost', 80)\n"
        )
        records = _run_with_hook(tmp_path, code)
    by_kind = {r["event"]: r for r in records if r["event"] != "start"}
    assert by_kind["sendto"]["address"] == ["127.0.0.1", port]
    assert by_kind["sendmsg"]["address"] == ["127.0.0.1", port]
    assert (by_kind["getaddrinfo"]["host"], by_kind["getaddrinfo"]["port"]) == ("localhost", 80)
    assert "connect" not in by_kind


@pytest.mark.slow
def test_hook_logs_the_argv_of_every_child_it_starts(tmp_path: Path) -> None:
    code = (
        "import os, subprocess, sys\n"
        "subprocess.run([sys.executable, '-S', '-c', 'pass'], check=True)\n"
        "pid = os.posix_spawn(sys.executable, [sys.executable, '-S', '-c', 'pass'], os.environ)\n"
        "os.waitpid(pid, 0)\n"
        "os.execv(sys.executable, [sys.executable, '-S', '-c', 'pass'])\n"
    )
    records = _run_with_hook(tmp_path, code)
    children = [r for r in records if r["event"] in ("popen", "spawn", "exec")]
    assert [r["event"] for r in children] == ["popen", "spawn", "exec"]
    assert all("-S" in r["argv"] for r in children)  # type: ignore[operator]
    # The -S children load no .pth, so the only start line is the parent's.
    assert [r["event"] for r in records].count("start") == 1


@pytest.mark.slow
def test_hook_exits_70_when_the_log_cannot_be_written(tmp_path: Path) -> None:
    site_dir = tmp_path / "site"
    shutil.copytree(HOOK_DIR, site_dir, ignore=shutil.ignore_patterns("__pycache__"))
    code = f"import site; site.addsitedir({str(site_dir)!r}); print('RAN ON')"
    done = subprocess.run(
        [sys.executable, "-c", code],
        env={"EGRESS_AUDIT_LOG": str(tmp_path / "no" / "such" / "dir" / "log"), "PATH": ""},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode == 70
    assert "cannot install" in done.stderr
    assert "RAN ON" not in done.stdout


@pytest.mark.slow
def test_hook_makes_a_relative_log_path_absolute_at_install(tmp_path: Path) -> None:
    site_dir = tmp_path / "site"
    shutil.copytree(HOOK_DIR, site_dir, ignore=shutil.ignore_patterns("__pycache__"))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    code = (
        f"import site, os; site.addsitedir({str(site_dir)!r})\n"
        f"os.chdir({str(elsewhere)!r})\n"
        "import socket; socket.getaddrinfo('localhost', 1)\n"
    )
    subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        cwd=tmp_path,
        env={"EGRESS_AUDIT_LOG": "rel.log", "PATH": ""},
        timeout=30,
    )
    lines = (tmp_path / "rel.log").read_text().splitlines()
    assert [json.loads(line)["event"] for line in lines] == ["start", "getaddrinfo"]
    assert not (elsewhere / "rel.log").exists()


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
    assert records[0]["scrimmage"] is False
    assert records[0]["sitecustomize"] is False
    connects = [r for r in records if r["event"] == "connect"]
    assert [r["address"] for r in connects] == [["127.0.0.1", port]] * 2
    assert {r["pid"] for r in records} == {records[0]["pid"]}


@pytest.mark.slow
def test_hook_start_line_shows_a_module_that_was_already_loaded(tmp_path: Path) -> None:
    log = tmp_path / "egress.log"
    site_dir = tmp_path / "site"
    shutil.copytree(HOOK_DIR, site_dir, ignore=shutil.ignore_patterns("__pycache__"))
    (tmp_path / "scrimmage.py").write_text("")
    code = (
        f"import sys; sys.path.insert(0, {str(tmp_path)!r}); import scrimmage\n"
        f"import site; site.addsitedir({str(site_dir)!r})\n"
    )
    subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        env={"EGRESS_AUDIT_LOG": str(log), "PATH": ""},
        timeout=30,
    )
    start = json.loads(log.read_text().splitlines()[0])
    assert start["scrimmage"] is True


@pytest.mark.slow
def test_hook_is_silent_without_a_log_path(tmp_path: Path) -> None:
    site_dir = tmp_path / "site"
    shutil.copytree(HOOK_DIR, site_dir, ignore=shutil.ignore_patterns("__pycache__"))
    code = f"import site; site.addsitedir({str(site_dir)!r})"
    subprocess.run([sys.executable, "-c", code], check=True, env={"PATH": ""}, timeout=30)
    assert list(tmp_path.iterdir()) == [site_dir]


@pytest.mark.slow
def test_hook_start_line_names_the_parent_so_a_child_can_be_matched(tmp_path: Path) -> None:
    records = _run_with_hook(tmp_path, "pass\n")
    assert records[0]["event"] == "start"
    assert records[0]["ppid"] == os.getpid()


@pytest.mark.slow
def test_hook_logs_os_system_and_forks_and_the_start_line_holds_the_argv(tmp_path: Path) -> None:
    code = (
        "import os\n"
        "os.system('true')\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    os._exit(0)\n"
        "os.waitpid(pid, 0)\n"
    )
    records = _run_with_hook(tmp_path, code)
    kinds = [r["event"] for r in records if r["event"] != "start"]
    assert kinds == ["system", "fork"]
    assert records[1]["command"] == "true"
    assert cast(list[str], records[0]["argv"])[:2] == [sys.executable, "-c"]
    assert records[0]["executable"] == sys.executable


# egress log analysis

GW, CONTROL, MODEL = "172.17.0.1", 28000, 29000
ALLOWED = {(GW, CONTROL), (GW, MODEL)}
NAMES = {"host.docker.internal", GW}
PY = "/usr/local/bin/python"
MP = [PY, "-c", "from multiprocessing.spawn import spawn_main; spawn_main()"]
HIDDEN = [PY, "-S", "-c", "import socket"]


def _start(
    pid: int, ppid: int, argv: list[str] | None = None, *, late: bool = False
) -> dict[str, object]:
    return {
        "event": "start",
        "pid": pid,
        "ppid": ppid,
        "argv": argv or [PY, "-m", "x"],
        "executable": PY,
        "scrimmage": late,
        "sitecustomize": False,
    }


def _popen(pid: int, argv: list[str], executable: str | None = None) -> dict[str, object]:
    return {"pid": pid, "event": "popen", "executable": executable or argv[0], "argv": argv}


def _clean_log() -> list[dict[str, object]]:
    return [
        _start(1, 0),
        {"pid": 1, "event": "exec", "executable": PY, "argv": ["uvicorn"]},
        _start(1, 0),
        _start(8, 1, MP),  # multiprocessing's tracker and workers: a start line, no popen
        _start(9, 1, MP),
        {"pid": 8, "event": "getaddrinfo", "host": "host.docker.internal", "port": CONTROL},
        {"pid": 8, "event": "connect", "address": [GW, CONTROL]},
        {"pid": 8, "event": "sendto", "address": ["::ffff:" + GW, MODEL, 0, 0]},
        _popen(8, ["uname", "-p"]),
    ]


KNOWN = (("uname", ["uname", "-p"]),)


def _problems(records: list[dict[str, object]]) -> list[str]:
    return egress_problems(records, ALLOWED, NAMES, KNOWN)


def test_a_clean_egress_log_has_no_problem() -> None:
    assert _problems(_clean_log()) == []


@pytest.mark.parametrize(
    ("extra", "needle"),
    [
        ({"pid": 8, "event": "connect", "address": ["1.1.1.1", 53]}, "connect to ['1.1.1.1', 53]"),
        ({"pid": 8, "event": "connect", "address": [GW, 22]}, f"connect to ['{GW}', 22]"),
        ({"pid": 8, "event": "sendmsg", "address": ["8.8.8.8", 53]}, "sendmsg to"),
        ({"pid": 8, "event": "connect", "address": "/var/run/docker.sock"}, "docker.sock"),
        ({"pid": 8, "event": "getaddrinfo", "host": "example.com", "port": 443}, "resolved"),
        (_popen(8, ["curl", "example.com"]), "wrote no start line"),
        (_popen(1, HIDDEN), "wrote no start line"),
        (_popen(8, ["uname", "-a"]), "wrote no start line"),
        (_popen(8, ["uname", "-p"], "/tmp/uname"), "wrote no start line"),
        ({"pid": 1, "event": "spawn", "executable": PY, "argv": HIDDEN}, "wrote no start line"),
        ({"pid": 1, "event": "system", "command": "python3 -S -c x"}, "system 'python3 -S"),
        ({"pid": 1, "event": "fork"}, "fork"),
        ({"pid": 1, "event": "exec", "executable": PY, "argv": HIDDEN}, "2 exec lines but 2 start"),
    ],
)
def test_the_egress_check_names_each_kind_of_problem(extra: dict[str, object], needle: str) -> None:
    problems = _problems([*_clean_log(), extra])
    assert any(needle in p for p in problems), problems


def test_a_multiprocessing_child_does_not_pay_for_a_hidden_popen() -> None:
    # Pid 1 has two multiprocessing children with start lines; a third, hidden child of pid 1
    # started by subprocess must still be named.
    problems = _problems([*_clean_log(), _popen(1, HIDDEN)])
    assert problems == [f"pid 1: popen {HIDDEN} wrote no start line"]


def test_the_egress_check_names_a_hook_that_came_after_scrimmage() -> None:
    log = _clean_log()
    log[3] = _start(8, 1, MP, late=True)
    assert any("came after Scrimmage" in p for p in _problems(log))


def test_a_child_with_a_start_line_pairs_with_its_parents_popen() -> None:
    argv = [PY, "-c", "pass"]
    log = [*_clean_log(), _popen(8, argv), _start(20, 8, argv)]
    assert _problems(log) == []
    # One start line pays for one popen only.
    assert len(_problems([*log, _popen(8, argv)])) == 1


# Docker positive control


def _docker_usable() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True, timeout=60).returncode == 0


@pytest.mark.docker
@pytest.mark.slow
def test_scanner_finds_a_canary_in_a_real_image_and_nothing_without_it(tmp_path: Path) -> None:
    if not _docker_usable():
        pytest.fail("docker daemon is not usable, so this docker test cannot pass")
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
