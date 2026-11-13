"""scrimmage cert: a throwaway authority, name-constrained, and one server certificate."""

import ipaddress
import ssl
import stat
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from scrimmage_core import cert
from scrimmage_core.cert import CertError, CertFiles, make_certificates

NAME = "host.docker.internal"
IP = "172.17.0.1"


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def make(tmp_path: Path, **kwargs: Any) -> CertFiles:
    args: dict[str, Any] = {"ips": (IP,), "names": (NAME,)}
    args.update(kwargs)
    return make_certificates(tmp_path / "out", **args)


def load(path: Path) -> x509.Certificate:
    return x509.load_pem_x509_certificate(path.read_bytes())


def handshake(ca: Path, server_cert: Path, server_key: Path, host: str) -> None:
    """A full TLS 1.3 handshake in memory, strict, as the SDK's context will do it."""
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    server_ctx.load_cert_chain(server_cert, server_key)
    client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client_ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    client_ctx.load_verify_locations(cafile=ca)
    client_ctx.verify_flags |= ssl.VERIFY_X509_STRICT
    to_server, to_client = ssl.MemoryBIO(), ssl.MemoryBIO()
    client = client_ctx.wrap_bio(to_server, to_client, server_hostname=host)
    server = server_ctx.wrap_bio(to_client, to_server, server_side=True)
    done = [False, False]
    for _ in range(10):
        for index, side in enumerate((client, server)):
            if not done[index]:
                try:
                    side.do_handshake()
                    done[index] = True
                except ssl.SSLWantReadError:
                    pass
        if all(done):
            return
    raise AssertionError("the handshake did not finish")


class Captured:
    """The authority's key, caught as made, so a test can sign what the real run never does."""

    def __init__(self) -> None:
        self.keys: list[ec.EllipticCurvePrivateKey] = []


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> Captured:
    seen = Captured()
    real = ec.generate_private_key

    def spy(*args: Any, **kwargs: Any) -> ec.EllipticCurvePrivateKey:
        key = real(*args, **kwargs)
        seen.keys.append(key)
        return key

    monkeypatch.setattr(ec, "generate_private_key", spy)
    return seen


def test_cert_writes_three_files_with_their_modes_into_a_new_folder(tmp_path: Path) -> None:
    files = make(tmp_path)
    out = tmp_path / "out"
    assert sorted(p.name for p in out.iterdir()) == ["ca.pem", "server.key", "server.pem"]
    assert (files.ca, files.cert, files.key) == (
        out / "ca.pem",
        out / "server.pem",
        out / "server.key",
    )
    assert mode(out) == 0o700
    assert mode(files.ca) == 0o644
    assert mode(files.cert) == 0o644
    assert mode(files.key) == 0o600


def test_cert_writes_no_authority_key(tmp_path: Path) -> None:
    make(tmp_path)
    out = tmp_path / "out"
    assert not (out / "ca.key").exists()
    keys = [p for p in out.iterdir() if b"PRIVATE KEY" in p.read_bytes()]
    assert [p.name for p in keys] == ["server.key"]
    server_key = serialization.load_pem_private_key((out / "server.key").read_bytes(), None)
    server_public = load(out / "server.pem").public_key()
    assert server_key.public_key() == server_public
    assert load(out / "ca.pem").public_key() != server_public


def test_cert_refuses_an_existing_folder(tmp_path: Path) -> None:
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "keep").write_text("mine")
    with pytest.raises(CertError, match="already exists"):
        make(tmp_path)
    assert [p.name for p in (tmp_path / "out").iterdir()] == ["keep"]


def test_cert_refuses_a_link_in_place_of_the_folder(tmp_path: Path) -> None:
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "out").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(CertError, match="already exists"):
        make(tmp_path)
    assert list((tmp_path / "elsewhere").iterdir()) == []


def test_the_certificates_match_the_pinned_profile(tmp_path: Path) -> None:
    before = datetime.now(UTC)
    files = make(tmp_path, ips=(IP, "::1"), names=(NAME, "runner.internal"), days=30)
    after = datetime.now(UTC)
    ca, server = load(files.ca), load(files.cert)

    # key
    for certificate in (ca, server):
        public = certificate.public_key()
        assert isinstance(public, ec.EllipticCurvePublicKey)
        assert public.curve.name == "secp256r1"
    key = serialization.load_pem_private_key(files.key.read_bytes(), None)
    assert isinstance(key, ec.EllipticCurvePrivateKey)
    assert key.curve.name == "secp256r1"

    # basicConstraints
    ca_basic = ca.extensions.get_extension_for_class(x509.BasicConstraints)
    assert ca_basic.critical
    assert ca_basic.value.ca is True
    assert ca_basic.value.path_length == 0
    server_basic = server.extensions.get_extension_for_class(x509.BasicConstraints)
    assert server_basic.critical
    assert server_basic.value.ca is False

    # keyUsage
    ca_usage = ca.extensions.get_extension_for_class(x509.KeyUsage)
    assert ca_usage.critical
    assert (ca_usage.value.key_cert_sign, ca_usage.value.crl_sign) == (True, True)
    assert not any(
        (
            ca_usage.value.digital_signature,
            ca_usage.value.content_commitment,
            ca_usage.value.key_encipherment,
            ca_usage.value.data_encipherment,
            ca_usage.value.key_agreement,
        )
    )
    server_usage = server.extensions.get_extension_for_class(x509.KeyUsage)
    assert server_usage.critical
    assert server_usage.value.digital_signature is True
    assert not any(
        (
            server_usage.value.key_cert_sign,
            server_usage.value.crl_sign,
            server_usage.value.content_commitment,
            server_usage.value.key_encipherment,
            server_usage.value.data_encipherment,
            server_usage.value.key_agreement,
        )
    )

    # extendedKeyUsage
    with pytest.raises(x509.ExtensionNotFound):
        ca.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
    eku = server.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert list(eku) == [ExtendedKeyUsageOID.SERVER_AUTH]

    # nameConstraints
    with pytest.raises(x509.ExtensionNotFound):
        server.extensions.get_extension_for_class(x509.NameConstraints)
    constraints = ca.extensions.get_extension_for_class(x509.NameConstraints)
    assert constraints.critical
    assert constraints.value.excluded_subtrees is None
    permitted = constraints.value.permitted_subtrees or []
    assert {g.value for g in permitted if isinstance(g, x509.DNSName)} == {NAME, "runner.internal"}
    assert {g.value for g in permitted if isinstance(g, x509.IPAddress)} == {
        ipaddress.ip_network(IP + "/32"),
        ipaddress.ip_network("::1/128"),
    }
    assert len(permitted) == 4

    # the server's names
    san = server.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert sorted(san.get_values_for_type(x509.DNSName)) == [NAME, "runner.internal"]
    assert sorted(map(str, san.get_values_for_type(x509.IPAddress))) == ["172.17.0.1", "::1"]

    # identifiers
    ca_ski = ca.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
    assert ca_ski == x509.SubjectKeyIdentifier.from_public_key(ca.public_key())
    with pytest.raises(x509.ExtensionNotFound):
        ca.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier)
    server_ski = server.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
    assert server_ski == x509.SubjectKeyIdentifier.from_public_key(server.public_key())
    aki = server.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier).value
    assert aki.key_identifier == ca_ski.digest

    # issuer, signature, validity
    assert server.issuer == ca.subject
    assert ca.issuer == ca.subject
    assert server.subject != ca.subject
    server.verify_directly_issued_by(ca)
    assert isinstance(server.signature_hash_algorithm, hashes.SHA256)
    slack = timedelta(seconds=2)
    for certificate in (ca, server):
        started = certificate.not_valid_before_utc
        assert before - timedelta(minutes=5) - slack <= started <= after - timedelta(minutes=5)
        assert certificate.not_valid_after_utc - started == timedelta(days=30, minutes=5)
    assert ca.not_valid_after_utc == server.not_valid_after_utc
    assert ca.not_valid_before_utc == server.not_valid_before_utc


def test_days_default_to_30_and_are_capped_at_397(tmp_path: Path) -> None:
    files = make(tmp_path)
    server = load(files.cert)
    assert server.not_valid_after_utc - server.not_valid_before_utc == timedelta(days=30, minutes=5)
    for days in (0, 398):
        with pytest.raises(CertError, match="397"):
            make_certificates(tmp_path / f"d{days}", names=(NAME,), days=days)
    assert not (tmp_path / "d0").exists()
    make_certificates(tmp_path / "max", names=(NAME,), days=397)


def test_a_name_or_an_ip_is_required_and_must_be_valid(tmp_path: Path) -> None:
    with pytest.raises(CertError, match="at least one"):
        make_certificates(tmp_path / "out")
    with pytest.raises(CertError, match="not an IP"):
        make_certificates(tmp_path / "out", ips=("172.17.0",))
    with pytest.raises(CertError, match="not a host name"):
        make_certificates(tmp_path / "out", names=("a b",))
    assert not (tmp_path / "out").exists()


def test_the_server_certificate_verifies_under_x509_strict(tmp_path: Path) -> None:
    files = make(tmp_path)
    handshake(files.ca, files.cert, files.key, NAME)
    handshake(files.ca, files.cert, files.key, IP)
    with pytest.raises(ssl.SSLCertVerificationError):
        handshake(files.ca, files.cert, files.key, "other.internal")


def test_the_authority_is_name_constrained_to_the_given_names_and_their_subdomains(
    tmp_path: Path, captured: Captured
) -> None:
    files = make(tmp_path)
    assert len(captured.keys) == 2  # the authority's key first, the server's second
    ca_key, ca = captured.keys[0], load(files.ca)
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    ca_ski = ca.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
    leaf_key_file = tmp_path / "leaf.key"
    leaf_key_file.write_bytes(
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )

    def signed(san: x509.GeneralName) -> Path:
        now = datetime.now(UTC)
        leaf = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "leaf")]))
            .issuer_name(ca.subject)
            .public_key(leaf_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName([san]), critical=False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ca_ski),
                critical=False,
            )
            .sign(ca_key, hashes.SHA256())
        )
        path = tmp_path / f"leaf-{len(list(tmp_path.glob('leaf-*')))}.pem"
        path.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
        return path

    inside = signed(x509.DNSName("a." + NAME))
    handshake(files.ca, inside, leaf_key_file, "a." + NAME)
    inside_ip = signed(x509.IPAddress(ipaddress.ip_address(IP)))
    handshake(files.ca, inside_ip, leaf_key_file, IP)
    for outside, host in (
        (x509.DNSName("other.internal"), "other.internal"),
        (x509.IPAddress(ipaddress.ip_address("172.17.0.2")), "172.17.0.2"),
    ):
        bad = signed(outside)
        with pytest.raises(ssl.SSLCertVerificationError):
            handshake(files.ca, bad, leaf_key_file, host)


def test_cert_without_the_extra_says_how_to_install_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in [m for m in sys.modules if m == "cryptography" or m.startswith("cryptography.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "cryptography", None)  # an import now raises ImportError
    with pytest.raises(CertError) as raised:
        make(tmp_path)
    assert str(raised.value) == cert.INSTALL
    assert "scrimmage-core[cert]" in str(raised.value)
    assert not (tmp_path / "out").exists()
