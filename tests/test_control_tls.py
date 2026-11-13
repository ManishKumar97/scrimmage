"""TLS on the control channel: the settings that allow it, the SDK's certificate checks, the
server's deadlines, and a full service test over it.

Each timing test shortens the server's two limits (the read timeout and the whole-request
deadline) and keeps their ratio, so it takes seconds and checks the same rule.
"""

import asyncio
import base64
import contextlib
import http.client
import io
import ipaddress
import json
import os
import secrets
import shutil
import socket
import ssl
import subprocess
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

import test_service
from scrimmage_core import asgi_gate, bootstrap, cli, control, protocol, service_client, tls
from scrimmage_core import config as settings
from scrimmage_core import runner as running
from scrimmage_core.cert import CertFiles, make_certificates
from scrimmage_core.client import ClaimRefused, ControlClient, ControlError
from scrimmage_core.control import ControlServer
from scrimmage_core.records import RecordStore
from service_harness import Service, drive, eventually
from test_service import bench

__all__ = ["bench"]  # a fixture, used by name

TOKEN = secrets.token_hex(32)
RUN = "run-0123456789ab"
HOST = "127.0.0.1"


def pki(tmp_path: Path, name: str = "pki") -> CertFiles:
    return make_certificates(tmp_path / name, ips=(HOST,), names=("control.test",))


class Stopped(Exception):
    """The service's stop, which ends a process, raised in its place."""


@pytest.fixture
def stops(monkeypatch: pytest.MonkeyPatch) -> None:
    def stop(message: str) -> NoReturn:
        raise Stopped(message)

    monkeypatch.setattr(bootstrap, "service_stop", stop)


@pytest.fixture
def servers(tmp_path: Path) -> Iterator[list[ControlServer]]:
    made: list[ControlServer] = []
    yield made
    for server in made:
        server.stop()


def serve(
    servers: list[ControlServer], tmp_path: Path, files: CertFiles | None = None, **kwargs: Any
) -> ControlServer:
    """A control server on loopback that speaks TLS with the given files, already started."""
    context = None if files is None else tls.server_context(files.cert, files.key)
    server = ControlServer(
        TOKEN, RecordStore(tmp_path / f"records-{len(servers)}"), tls=context, **kwargs
    )
    server.start()
    servers.append(server)
    return server


def sdk(server: ControlServer, ca: Path, host: str = HOST) -> ControlClient:
    port = server.server.server_address[1]
    return ControlClient(f"https://{host}:{port}", TOKEN, tls.client_context(ca))


def port_of(server: ControlServer) -> int:
    return int(server.server.server_address[1])


def handshake(client: ssl.SSLContext, server: ssl.SSLContext, host: str) -> None:
    """A handshake in memory. Raises what the client's side raises."""
    to_server, to_client = ssl.MemoryBIO(), ssl.MemoryBIO()
    mine = client.wrap_bio(to_server, to_client, server_hostname=host)
    theirs = server.wrap_bio(to_client, to_server, server_side=True)
    for _ in range(10):
        done = [False, False]
        for i, side in enumerate((mine, theirs)):
            try:
                side.do_handshake()
                done[i] = True
            except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
                pass
        if all(done):
            return
    raise AssertionError("the handshake did not finish")


# --- the settings in scrimmage.toml -------------------------------------------------------

FILE = """[agent]
service = "http://127.0.0.1:9/chat"
body = {{message = "{{question}}"}}
answer = "reply"
{extra}

[[test]]
name = "t"
question = "q"
expect = ["x"]
"""


def write(tmp_path: Path, extra: str) -> Path:
    path = tmp_path / "scrimmage.toml"
    path.write_text(FILE.format(extra=extra))
    return path


def check(path: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    code = cli.main(["check", str(path)])
    return code, capsys.readouterr().err


TLS_LINE = 'control_tls = {cert = "pki/server.pem", key = "pki/server.key"}'


@pytest.mark.design("service.tls_control_needs_tls")
@pytest.mark.parametrize("address", ["172.17.0.1:8766", "10.0.0.5:8766", "[fd00::1]:8766"])
def test_control_beyond_loopback_without_control_tls_is_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], address: str
) -> None:
    code, err = check(write(tmp_path, f'control = "{address}"'), capsys)
    assert code == 2
    assert "control_tls" in err and "scrimmage cert" in err
    # The same address with a certificate is a valid file.
    assert check(write(tmp_path, f'control = "{address}"\n{TLS_LINE}'), capsys)[0] == 0


@pytest.mark.design("service.tls_control_needs_tls")
def test_control_tls_without_control_is_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, err = check(write(tmp_path, TLS_LINE), capsys)
    assert code == 2
    assert "control_tls: needs control" in err


@pytest.mark.design("service.tls_control_needs_tls")
def test_control_on_loopback_needs_no_certificate_and_may_have_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert check(write(tmp_path, 'control = "127.0.0.1:8766"'), capsys)[0] == 0
    assert check(write(tmp_path, f'control = "[::1]:8766"\n{TLS_LINE}'), capsys)[0] == 0


@pytest.mark.design("service.tls_control_address")
@pytest.mark.parametrize("address", ["0.0.0.0:8766", "[::]:8766", "[::ffff:0.0.0.0]:8766"])
def test_control_on_an_unspecified_address_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], address: str
) -> None:
    code, err = check(write(tmp_path, f'control = "{address}"\n{TLS_LINE}'), capsys)
    assert code == 2
    assert "not every address" in err


@pytest.mark.design("service.tls_control_address")
@pytest.mark.parametrize(
    "address", ["localhost:8766", "host.docker.internal:8766", "172.17.0.1", "172.17.0.1:0"]
)
def test_control_must_be_an_ip_literal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], address: str
) -> None:
    code, err = check(write(tmp_path, f'control = "{address}"\n{TLS_LINE}'), capsys)
    assert code == 2
    assert "is not an IP address and a port" in err


@pytest.mark.design("service.tls_control_address")
def test_control_works_only_with_service_and_reads_placeholders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CONTROL_PORT", "8766")
    path = write(tmp_path, f'control = "127.0.0.1:${{CONTROL_PORT}}"\n{TLS_LINE}')
    assert check(path, capsys)[0] == 0
    assert settings.load(path).agent.control == "127.0.0.1:8766"
    path.write_text(
        '[agent]\nfunction = "a.b:c"\ncontrol = "127.0.0.1:1"\n\n'
        '[[test]]\nname = "t"\nquestion = "q"\nexpect = ["x"]\n'
    )
    code, err = check(path, capsys)
    assert code == 2 and "control: works only with service" in err


@pytest.mark.design("service.tls_control_address")
def test_control_tls_takes_exactly_a_cert_and_a_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for bad, why in (
        ('control_tls = {cert = "a.pem"}', "key is required"),
        ('control_tls = {cert = "a.pem", key = "a.key", ca = "x"}', "ca: unknown setting"),
        ('control_tls = "pki"', "expected a table"),
    ):
        code, err = check(write(tmp_path, f'control = "172.17.0.1:8766"\n{bad}'), capsys)
        assert code == 2 and why in err, err


# --- the runner starts only with a good key and certificate --------------------------------


def runner_file(tmp_path: Path, cert: Path, key: Path) -> Path:
    token = tmp_path / "token"
    token.write_text(TOKEN)
    token.chmod(0o600)
    port = socket.socket()
    port.bind((HOST, 0))
    number = port.getsockname()[1]
    port.close()
    return write(
        tmp_path, f'control = "{HOST}:{number}"\ncontrol_tls = {{cert = "{cert}", key = "{key}"}}'
    )


def run_test(
    path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> tuple[int, str]:
    monkeypatch.setenv("SCRIMMAGE_TOKEN_FILE", str(path.parent / "token"))
    code = cli.main(["test", str(path)])
    return code, capsys.readouterr().err


@pytest.mark.design("service.tls_key_private")
def test_a_control_key_not_0600_is_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    files = pki(tmp_path)
    files.key.chmod(0o644)
    code, err = run_test(runner_file(tmp_path, files.cert, files.key), monkeypatch, capsys)
    assert code == 2
    assert "has mode 644; it must be 600" in err


@pytest.mark.design("service.tls_key_private")
def test_a_key_swapped_between_the_mode_check_and_the_load_is_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    files = pki(tmp_path)
    other = pki(tmp_path, "other")
    path = runner_file(tmp_path, files.cert, files.key)
    real = tls.server_context

    def swapped(cert: Path, key: Path) -> ssl.SSLContext:
        context = real(cert, key)  # loads the file that was checked
        new = key.with_suffix(".new")  # made before the old file goes, so its inode differs
        shutil.copy(other.key, new)
        new.chmod(0o600)
        os.replace(new, key)  # then a different 0600 file takes its place
        return context

    monkeypatch.setattr(tls, "server_context", swapped)
    code, err = run_test(path, monkeypatch, capsys)
    assert code == 2 and "changed while it was read" in err, err


def self_signed(tmp_path: Path, name: str, start: datetime, end: datetime) -> CertFiles:
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "x")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(end)
        .sign(key, hashes.SHA256())
    )
    folder = tmp_path / name
    folder.mkdir()
    out = CertFiles(folder / "ca.pem", folder / "server.pem", folder / "server.key")
    out.cert.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    out.key.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    out.key.chmod(0o600)
    return out


@pytest.mark.design("service.tls_cert_checked_at_start")
def test_an_expired_or_mismatched_certificate_is_exit_2_at_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    now = datetime.now(UTC)
    good = pki(tmp_path)
    agent = settings.load(runner_file(tmp_path, good.cert, good.key)).agent
    assert service_client.server_tls(agent, tmp_path) is not None  # a good pair is accepted
    expired = self_signed(tmp_path, "old", now - timedelta(days=9), now - timedelta(days=2))
    code, err = run_test(runner_file(tmp_path, expired.cert, expired.key), monkeypatch, capsys)
    assert code == 2 and "control_tls" in err and "expired" in err, err
    early = self_signed(tmp_path, "new", now + timedelta(days=2), now + timedelta(days=9))
    code, err = run_test(runner_file(tmp_path, early.cert, early.key), monkeypatch, capsys)
    assert code == 2 and "not yet valid" in err, err
    other = pki(tmp_path, "other")
    code, err = run_test(runner_file(tmp_path, good.cert, other.key), monkeypatch, capsys)
    assert code == 2 and "cannot be used together" in err, err
    code, err = run_test(
        runner_file(tmp_path, tmp_path / "missing.pem", good.key), monkeypatch, capsys
    )
    assert code == 2 and "control_tls" in err, err


# --- the SDK trusts one authority, by name, over TLS 1.3, with strict chain rules ----------


@pytest.fixture
def one_service(tmp_path: Path) -> Iterator[list[Service]]:
    made: list[Service] = []
    yield made
    for service in made:
        service.close()


async def refuses_the_app(scope: Any, receive: Any, send: Any) -> None:
    raise AssertionError("a run whose plan was not claimed must never reach the app")


@pytest.mark.design("service.tls_one_authority")
def test_the_sdk_refuses_a_certificate_not_from_the_given_authority(
    tmp_path: Path, servers: list[ControlServer], one_service: list[Service]
) -> None:
    ours, theirs = pki(tmp_path), pki(tmp_path, "other")
    server = serve(servers, tmp_path, theirs)
    with pytest.raises(ControlError, match="SSLCertVerificationError"):
        sdk(server, ours.ca).claim(RUN, {"pid": 1})
    # The server saw the SDK refuse its certificate, by OpenSSL's reason.
    eventually(lambda: server.stats.tls_failed == {"unknown ca": 1})
    # The same server, trusted: the request is answered, so the refusal was the authority.
    with pytest.raises(ClaimRefused):
        sdk(server, theirs.ca).claim(RUN, {"pid": 1})
    # In a service, the refusal is every run's 503 plan-unavailable.
    service = Service(
        tmp_path / "svc",
        server_tls=tls.server_context(theirs.cert, theirs.key),
        client_tls=tls.client_context(ours.ca),
    )
    one_service.append(service)
    name = service.open()
    app = asgi_gate.Entry(refuses_the_app)
    reply = asyncio.run(drive(app, headers=[service.header(name)]))
    assert reply.status == 503 and reply.headers["x-scrimmage-error"] == "plan-unavailable"


@pytest.mark.design("service.tls_one_authority")
def test_the_sdk_refuses_a_public_authority_and_never_loads_the_system_store(
    tmp_path: Path, servers: list[ControlServer], monkeypatch: pytest.MonkeyPatch
) -> None:
    system, ours = pki(tmp_path, "system"), pki(tmp_path, "ours")
    monkeypatch.setenv("SSL_CERT_FILE", str(system.ca))
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "no-such-folder"))
    server = serve(servers, tmp_path, system)
    # Positive control: a context that loads the default store does trust this server.
    default = ssl.create_default_context()
    handshake(default, tls.server_context(system.cert, system.key), HOST)
    with pytest.raises(ControlError, match="SSLCertVerificationError"):
        sdk(server, ours.ca).claim(RUN, {"pid": 1})


@pytest.mark.slow
@pytest.mark.skipif(
    shutil.which("openssl") is None, reason="openssl is not installed, so no hashed CA folder"
)
@pytest.mark.design("service.tls_one_authority")
def test_the_sdk_refuses_an_authority_in_the_system_folder(
    tmp_path: Path, servers: list[ControlServer], monkeypatch: pytest.MonkeyPatch
) -> None:
    system, ours = pki(tmp_path, "system"), pki(tmp_path, "ours")
    folder = tmp_path / "capath"
    folder.mkdir()
    shutil.copy(system.ca, folder / "system.pem")
    subprocess.run(["openssl", "rehash", str(folder)], check=True)  # the names OpenSSL looks up
    monkeypatch.setenv("SSL_CERT_DIR", str(folder))
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "no-such-file"))
    server = serve(servers, tmp_path, system)
    # Positive control: the default store, through the folder alone, trusts this server.
    handshake(ssl.create_default_context(), tls.server_context(system.cert, system.key), HOST)
    with pytest.raises(ControlError, match="SSLCertVerificationError"):
        sdk(server, ours.ca).claim(RUN, {"pid": 1})


@pytest.mark.design("service.tls_one_authority")
def test_the_sdk_refuses_a_certificate_for_another_name(
    tmp_path: Path, servers: list[ControlServer]
) -> None:
    files = pki(tmp_path)  # its names: 127.0.0.1 and control.test
    server = serve(servers, tmp_path, files)
    with pytest.raises(ControlError, match="SSLCertVerificationError"):
        sdk(server, files.ca, host="localhost").claim(RUN, {"pid": 1})
    with pytest.raises(ClaimRefused):
        sdk(server, files.ca, host=HOST).claim(RUN, {"pid": 1})


@pytest.mark.design("service.tls_one_authority")
def test_the_sdk_refuses_tls_1_2(tmp_path: Path, servers: list[ControlServer]) -> None:
    files = pki(tmp_path)
    old = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    old.maximum_version = ssl.TLSVersion.TLSv1_2
    old.load_cert_chain(files.cert, files.key)
    server = ControlServer(TOKEN, RecordStore(tmp_path / "records-old"), tls=old)
    server.start()
    servers.append(server)
    with pytest.raises(ControlError, match="SSLError"):
        sdk(server, files.ca).claim(RUN, {"pid": 1})
    # The same certificate on a TLS 1.3 server is reached.
    with pytest.raises(ClaimRefused):
        sdk(serve(servers, tmp_path, files), files.ca).claim(RUN, {"pid": 1})


def forge(tmp_path: Path, name: str, *, critical: bool, with_aki: bool) -> CertFiles:
    """An authority and a server certificate, each with one defect only strict mode refuses."""

    def usage(**on: bool) -> x509.KeyUsage:
        off = dict.fromkeys(
            (
                "digital_signature",
                "content_commitment",
                "key_encipherment",
                "data_encipherment",
                "key_agreement",
                "key_cert_sign",
                "crl_sign",
                "encipher_only",
                "decipher_only",
            ),
            False,
        )
        return x509.KeyUsage(**{**off, **on})

    now = datetime.now(UTC)
    ca_key, key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "forged authority")])
    leaf_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "forged server")])
    ca_ski = x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key())

    def builder(subject: x509.Name, issuer: x509.Name, public: Any) -> x509.CertificateBuilder:
        return (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(public)
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=1))
        )

    ca = (
        builder(ca_name, ca_name, ca_key.public_key())
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=critical)
        .add_extension(usage(key_cert_sign=True, crl_sign=True), critical=True)
        .add_extension(ca_ski, critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    leaf = (
        builder(leaf_name, ca_name, key.public_key())
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(usage(digital_signature=True), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(HOST))]),
            critical=False,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
    )
    if with_aki:
        leaf = leaf.add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ca_ski), critical=False
        )
    folder = tmp_path / name
    folder.mkdir()
    files = CertFiles(folder / "ca.pem", folder / "server.pem", folder / "server.key")
    files.ca.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    signed = leaf.sign(ca_key, hashes.SHA256())
    files.cert.write_bytes(signed.public_bytes(serialization.Encoding.PEM))
    files.key.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    files.key.chmod(0o600)
    return files


@pytest.mark.design("service.tls_strict")
@pytest.mark.parametrize(
    ("critical", "with_aki"),
    [(False, True), (True, False)],
    ids=["authority basic constraints not critical", "server certificate without a key id"],
)
def test_the_sdk_refuses_an_authority_that_only_strict_mode_rejects(
    tmp_path: Path, critical: bool, with_aki: bool
) -> None:
    sound = forge(tmp_path, "sound", critical=True, with_aki=True)
    handshake(tls.client_context(sound.ca), tls.server_context(sound.cert, sound.key), HOST)
    files = forge(tmp_path, "flawed", critical=critical, with_aki=with_aki)
    server = tls.server_context(files.cert, files.key)
    lenient = tls.client_context(files.ca)
    lenient.verify_flags &= ~ssl.VERIFY_X509_STRICT
    handshake(lenient, server, HOST)  # the chain verifies without strict mode
    with pytest.raises(ssl.SSLCertVerificationError):
        handshake(tls.client_context(files.ca), server, HOST)  # and is refused with it


# --- what the service does at its start --------------------------------------------------


@pytest.mark.design("service.tls_service_forms")
def test_http_beyond_loopback_stops_the_service_naming_https(stops: None) -> None:
    for url in ("http://172.17.0.1:8766", "http://control.test:8766"):
        with pytest.raises(Stopped, match=r"https://host:port.*SCRIMMAGE_CONTROL_CA"):
            bootstrap.control_context(url)
    assert bootstrap.control_context("http://127.0.0.1:8766") is None
    assert bootstrap.control_context("http://localhost:8766") is None


@pytest.mark.design("service.tls_service_forms")
def test_https_without_scrimmage_control_ca_stops_the_service_naming_it(
    tmp_path: Path, stops: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SCRIMMAGE_CONTROL_CA", raising=False)
    with pytest.raises(Stopped, match="SCRIMMAGE_CONTROL_CA must name"):
        bootstrap.control_context("https://172.17.0.1:8766")
    monkeypatch.setenv("SCRIMMAGE_CONTROL_CA", str(tmp_path / "missing.pem"))
    with pytest.raises(Stopped, match=r"SCRIMMAGE_CONTROL_CA: .*cannot be read"):
        bootstrap.control_context("https://172.17.0.1:8766")
    (tmp_path / "junk.pem").write_text("not a certificate")
    monkeypatch.setenv("SCRIMMAGE_CONTROL_CA", str(tmp_path / "junk.pem"))
    with pytest.raises(Stopped, match="holds 0 certificates"):
        bootstrap.control_context("https://172.17.0.1:8766")
    with pytest.raises(Stopped, match=r"must be http://127\.0\.0\.1:port"):
        bootstrap.control_context("ftp://172.17.0.1:8766")


@pytest.mark.design("service.tls_service_forms")
def test_a_ca_file_with_two_certificates_stops_the_service(
    tmp_path: Path, stops: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    one, two = pki(tmp_path, "one"), pki(tmp_path, "two")
    monkeypatch.setenv("SCRIMMAGE_CONTROL_CA", str(one.ca))
    assert bootstrap.control_context("https://172.17.0.1:8766") is not None
    bundle = tmp_path / "bundle.pem"
    bundle.write_bytes(one.ca.read_bytes() + two.ca.read_bytes())
    monkeypatch.setenv("SCRIMMAGE_CONTROL_CA", str(bundle))
    with pytest.raises(Stopped, match="holds 2 certificates"):
        bootstrap.control_context("https://172.17.0.1:8766")


@pytest.mark.design("service.tls_service_forms")
def test_a_token_file_owned_by_another_uid_names_the_user_setting(
    tmp_path: Path, stops: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = tmp_path / "token"
    token.write_text(TOKEN)
    token.chmod(0o600)
    me = os.getuid()
    assert bootstrap.read_token_file(str(token)) == TOKEN
    monkeypatch.setattr(os, "getuid", lambda: me + 1)
    with pytest.raises(Stopped) as raised:
        bootstrap.read_token_file(str(token))
    assert (
        f"the token file is owned by uid {me} and this process runs as uid {me + 1}; "
        "in Docker, set user: to the file owner's uid and gid"
    ) in str(raised.value)


# --- every request goes through the TLS connection ----------------------------------------


@pytest.mark.design("service.tls_every_request")
def test_every_control_request_kind_goes_through_the_tls_connection(
    tmp_path: Path, servers: list[ControlServer], monkeypatch: pytest.MonkeyPatch
) -> None:
    files = pki(tmp_path)
    server = serve(servers, tmp_path, files)
    server.open_run(RUN, "baseline", {"run": RUN}, service=True)
    client = sdk(server, files.ca)
    sent: list[tuple[str, str, str]] = []
    real = http.client.HTTPConnection.request

    def spy(self: http.client.HTTPConnection, method: str, url: str, *a: Any, **k: Any) -> None:
        sent.append((type(self).__name__, method, url))
        real(self, method, url, *a, **k)

    monkeypatch.setattr(http.client.HTTPConnection, "request", spy)
    assert client.claim(RUN, {"pid": 1}) is not None
    with pytest.raises(ControlError, match="service_run"):
        client.fetch_plan(RUN)  # a service run's plan is claimed, never fetched: still answered
    reservation = client.reserve(RUN, 0.01)
    assert isinstance(reservation, int)
    posts: dict[str, object] = {
        "/v1/budget/settle": {"reservation": reservation, "usd": 0.005},
        "/v1/records": {"run": RUN, "lines": [], "dropped": 0},
        "/v1/blobs": {"run": RUN, "sha256": protocol.digest(b"x"), "data": "eA=="},
    }
    for path, payload in posts.items():
        assert client.request("POST", path, payload)[0] == 200, path
    client.release(7)
    client.abandoned(RUN)
    assert {(m, u) for _, m, u in sent} == {
        ("POST", "/v1/claims"),
        ("GET", f"/v1/plans/{RUN}"),
        ("POST", "/v1/budget/reserve"),
        ("POST", "/v1/budget/settle"),
        ("POST", "/v1/records"),
        ("POST", "/v1/blobs"),
        ("POST", "/v1/budget/release"),
        ("POST", "/v1/abandoned"),
    }
    assert {kind for kind, _, _ in sent} == {"HTTPSConnection"}


# --- a stalled or slow client cannot hold the server -------------------------------------


@pytest.fixture
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    """The two limits at a fifth of their size, keeping their ratio."""
    monkeypatch.setattr(control, "READ_TIMEOUT_S", 2.0)
    monkeypatch.setattr(control, "REQUEST_DEADLINE_S", 3.0)


@pytest.mark.design("service.tls_deadlines")
def test_a_stalled_handshake_does_not_block_a_second_client_and_is_cut_at_the_deadline(
    tmp_path: Path, servers: list[ControlServer], quick: None
) -> None:
    files = pki(tmp_path)
    server = serve(servers, tmp_path, files)
    began = time.monotonic()
    stalled = socket.create_connection((HOST, port_of(server)))  # sends no ClientHello
    try:
        with pytest.raises(ClaimRefused):  # served: the run does not exist
            sdk(server, files.ca).claim(RUN, {"pid": 1})
        assert time.monotonic() - began < 1.0
        stalled.settimeout(control.REQUEST_DEADLINE_S + 2)
        try:
            data = stalled.recv(1)
        except ConnectionResetError:
            data = b""
        assert data == b""
        assert time.monotonic() - began < control.REQUEST_DEADLINE_S + 1.0
        assert server.stats.connections_cut == 1
    finally:
        stalled.close()


@pytest.mark.design("service.tls_deadlines")
def test_a_slow_drip_is_cut_at_the_whole_request_deadline(
    tmp_path: Path, servers: list[ControlServer], quick: None
) -> None:
    files = pki(tmp_path)
    server = serve(servers, tmp_path, files)
    context = tls.client_context(files.ca)
    raw = socket.create_connection((HOST, port_of(server)))
    with context.wrap_socket(raw, server_hostname=HOST) as conn:
        began = time.monotonic()
        cut_at = None
        for byte in b"POST /v1/claims HTTP/1.1 " * 20:
            try:
                conn.settimeout(5)
                conn.sendall(bytes([byte]))
                conn.settimeout(0.05)
                if conn.recv(1) == b"":
                    cut_at = time.monotonic()
                    break
            except TimeoutError:
                pass  # nothing came back: the server is still waiting
            except OSError:
                cut_at = time.monotonic()
                break
            time.sleep(1.0)  # under the 2 s read timeout, so only the deadline can cut this
            if time.monotonic() - began > 7:
                break
    assert cut_at is not None, "the connection was never cut"
    assert cut_at - began < control.REQUEST_DEADLINE_S + 1.5
    assert server.stats.connections_cut == 1


@pytest.mark.design("service.tls_deadlines")
def test_an_unsigned_large_body_is_refused_before_it_is_read(
    tmp_path: Path, servers: list[ControlServer], monkeypatch: pytest.MonkeyPatch
) -> None:
    files = pki(tmp_path)
    server = serve(servers, tmp_path, files)
    server.open_run(RUN, "baseline", {"run": RUN}, service=True)
    taken = [0]
    real = control._Reader.readinto

    def counted(self: control._Reader, buffer: Any) -> int:
        n = real(self, buffer)
        taken[0] += n
        return n

    monkeypatch.setattr(control._Reader, "readinto", counted)
    conn = http.client.HTTPSConnection(
        HOST, port_of(server), timeout=10, context=tls.client_context(files.ca)
    )
    conn.putrequest("POST", "/v1/blobs")
    conn.putheader("Content-Length", str(14 * 1024 * 1024))
    forged = protocol.request_headers("x" * 64, "POST", "/v1/blobs", b"", time.time())
    for name, value in forged.items():
        conn.putheader(name, value)
    conn.endheaders()
    conn.auto_open = 0  # a send after the close must fail, not open a second connection

    def push() -> None:  # the whole declared body, until the server stops listening
        chunk = b"x" * 65536
        with contextlib.suppress(OSError, http.client.HTTPException):
            for _ in range(14 * 1024 * 1024 // len(chunk)):
                conn.send(chunk)

    pusher = threading.Thread(target=push, daemon=True)
    pusher.start()
    reply = conn.getresponse()
    assert reply.status == 401 and json.loads(reply.read())["error"] == "bad_signature"
    conn.close()
    pusher.join(10)
    assert taken[0] < 64 * 1024
    # Positive control: a signed body of the same kind is read in full.
    data = os.urandom(5 * 1024 * 1024)
    payload = {
        "run": RUN,
        "sha256": protocol.digest(data),
        "data": base64.b64encode(data).decode(),
    }
    before = taken[0]
    assert sdk(server, files.ca).request("POST", "/v1/blobs", payload)[0] == 200
    assert taken[0] - before > 5 * 1024 * 1024


@pytest.mark.design("service.tls_deadlines")
def test_tls_failures_are_counted_by_reason_and_print_no_traceback(
    tmp_path: Path, servers: list[ControlServer], capfd: pytest.CaptureFixture[str]
) -> None:
    files = pki(tmp_path)
    server = serve(servers, tmp_path, files)
    with socket.create_connection((HOST, port_of(server))) as plain:  # HTTP to the TLS port
        plain.sendall(b"GET /v1/plans/x HTTP/1.1\r\nHost: x\r\n\r\n")
        plain.settimeout(5)
        with contextlib.suppress(OSError):
            plain.recv(1024)
    old = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    old.maximum_version = ssl.TLSVersion.TLSv1_2
    old.load_verify_locations(cafile=files.ca)
    with (
        socket.create_connection((HOST, port_of(server))) as raw,
        pytest.raises(ssl.SSLError),
    ):
        old.wrap_socket(raw, server_hostname=HOST)
    eventually(lambda: sum(server.stats.tls_failed.values()) == 2)
    assert all(reason and reason.islower() for reason in server.stats.tls_failed)
    assert "unsupported protocol" in server.stats.tls_failed
    assert capfd.readouterr().err == ""


# --- the runner's own numbers -------------------------------------------------------------


@pytest.mark.design("service.tls_runner_numbers")
def test_the_report_counts_runs_sent_and_claims_received(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quick: None
) -> None:
    files = pki(tmp_path)
    path = runner_file(tmp_path, files.cert, files.key)
    monkeypatch.setenv("SCRIMMAGE_TOKEN_FILE", str(tmp_path / "token"))
    runner = running.Runner(settings.load(path))
    runner.server.start()
    held: list[socket.socket] = []
    try:
        port = int(runner.server.server.server_address[1])
        for _ in range(control.MAX_CONNECTIONS):  # every thread held by a client that sends nothing
            held.append(socket.create_connection((HOST, port)))
        eventually(lambda: runner.server.stats.connections_cut == control.MAX_CONNECTIONS, 20)
        for _ in range(3):  # the service is down, so nothing claims
            runner.send("run-0123456789ab.00", "q")
        out = io.StringIO()
        report = running.build_report(runner.config, [], runner.folder, runner)
        running.print_report(report, out)
        assert "control server: runs sent 3, claims received 0" in out.getvalue()
        assert "64 connections cut by the timeouts" in out.getvalue()
        doc = running.report_json(report)
        assert doc["runs_sent"] == 3 and doc["claims_received"] == 0
        assert doc["control_cut"] == control.MAX_CONNECTIONS and doc["control_tls_failed"] == {}
    finally:
        for sock in held:
            sock.close()
        runner.server.stop()


# --- a whole service test over TLS --------------------------------------------------------


@pytest.mark.slow
@pytest.mark.design("service.tls_full_run")
def test_a_full_s7a_run_works_over_tls_on_loopback(
    bench: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S7a's four parallel runs on four workers, with the control channel on TLS."""
    files = pki(tmp_path)
    port = test_service.free_port()
    extra = (
        f'control = "{HOST}:{port}"\ncontrol_tls = {{cert = "{files.cert}", key = "{files.key}"}}'
    )
    monkeypatch.setattr(test_service, "FILE", test_service.FILE.replace("EXTRA", f"EXTRA\n{extra}"))
    service = bench.start(
        workers=4,
        SCRIMMAGE_CONTROL_URL=f"https://{HOST}:{port}",
        SCRIMMAGE_CONTROL_CA=str(files.ca),
    )
    workers = set(service.workers())
    assert len(workers) == 4, service.text
    code, out = bench.test(service.port, runs=4, parallel=4)
    assert code == 0, out
    records = {p.stem: p for p in (bench.root / "scrimmage-runs").glob("*/*.jsonl")}
    assert sum(name.startswith("run-") for name in records) == 4, sorted(records)
    for name, path in records.items():
        lines = [json.loads(x) for x in path.read_text().splitlines()]
        assert lines and {(x["run"], x["test"]) for x in lines} == {(name, "order-status")}, name
    # The setup request and the four runs each claimed their plan, over TLS.
    assert "control server: runs sent 5, claims received 5" in out
