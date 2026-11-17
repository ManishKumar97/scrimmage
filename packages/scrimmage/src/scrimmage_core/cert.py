"""The certificates for the control channel: a throwaway authority and a server certificate.

The authority's key lives in this function's memory only. It signs the server certificate
and is dropped, so nothing on disk can sign another name. The authority is name-constrained
to the names and addresses given, so a leaked ca.pem cannot vouch for any other host.

This is the one module that imports cryptography, an optional extra. It is loaded only by
the scrimmage cert command, never into the team's process.
"""

import ipaddress
import os
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scrimmage_core.errors import ScrimmageError

INSTALL = 'scrimmage cert needs the cert extra; install it with: pip install "scrimmage[cert]"'
MAX_DAYS = 397
BACKDATE = timedelta(minutes=5)  # a clock a little behind the runner's still accepts it
CA_NAME = "Scrimmage throwaway authority"
SERVER_NAME = "Scrimmage control server"


LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


def valid_name(name: str) -> bool:
    """Whether name is dot-separated LDH labels: no wildcard, no empty label, at most 253."""
    return len(name) <= 253 and all(LABEL.fullmatch(label) for label in name.split("."))


class CertError(ScrimmageError):
    """The certificates cannot be made as asked: bad input, an existing folder, a missing extra."""


@dataclass(frozen=True)
class CertFiles:
    """Where the three files were written."""

    ca: Path
    cert: Path
    key: Path


def make_certificates(
    out: Path, *, ips: tuple[str, ...] = (), names: tuple[str, ...] = (), days: int = 30
) -> CertFiles:
    """Write ca.pem, server.pem and server.key into the new folder out (mode 0700)."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    except ImportError:
        raise CertError(INSTALL) from None

    if not ips and not names:
        raise CertError("give at least one --ip or --name")
    if not 1 <= days <= MAX_DAYS:
        raise CertError(f"--days must be from 1 to {MAX_DAYS}, not {days}")
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for text in ips:
        try:
            address = ipaddress.ip_address(text)
        except ValueError:
            raise CertError(f"--ip {text!r} is not an IP address") from None
        if address.is_unspecified:
            raise CertError(f"--ip {text!r} is the unspecified address; give the runner's own")
        if getattr(address, "scope_id", None):
            raise CertError(f"--ip {text!r} has a scope id; a certificate names no interface")
        addresses.append(address)
    for name in names:
        if not valid_name(name):
            raise CertError(
                f"--name {name!r} is not a host name: use letters, digits and hyphens in "
                "dot-separated labels, with no wildcard, no leading or trailing dot"
            )
    if out.exists() or out.is_symlink():
        raise CertError(f"{out} already exists; give a new folder, or remove it first")

    now = datetime.now(UTC).replace(microsecond=0)
    start, end = now - BACKDATE, now + timedelta(days=days)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    server_key = ec.generate_private_key(ec.SECP256R1())
    ca_subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CA_NAME)])
    server_subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, SERVER_NAME)])

    ca_ski = x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key())
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_subject)
        .issuer_name(ca_subject)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(end)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(ca_ski, critical=False)
        .add_extension(
            x509.NameConstraints(
                permitted_subtrees=[
                    *(x509.DNSName(name) for name in names),
                    *(
                        x509.IPAddress(
                            ipaddress.ip_network(
                                address.exploded + "/" + str(address.max_prefixlen)
                            )
                        )
                        for address in addresses
                    ),
                ],
                excluded_subtrees=None,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(server_subject)
        .issuer_name(ca_subject)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(end)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.SubjectAlternativeName(
                [*(x509.DNSName(name) for name in names), *(x509.IPAddress(a) for a in addresses)]
            ),
            critical=False,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(server_key.public_key()), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ca_ski), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    del ca_key  # the authority cannot sign again

    ca_pem = ca_cert.public_bytes(serialization.Encoding.PEM)
    server_pem = server_cert.public_bytes(serialization.Encoding.PEM)
    key_pem = server_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    files = CertFiles(out / "ca.pem", out / "server.pem", out / "server.key")
    try:
        out.mkdir(mode=0o700)
    except FileExistsError:
        raise CertError(f"{out} already exists; give a new folder, or remove it first") from None
    except OSError as error:
        raise CertError(f"cannot create {out}: {error.strerror or error}") from None
    # From here the folder is ours, so a failed write removes it and nothing else.
    try:
        folder = os.open(out, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            os.fchmod(folder, 0o700)  # the umask may have narrowed the mode, never widened it
            for name, data, mode in (
                ("ca.pem", ca_pem, 0o644),
                ("server.pem", server_pem, 0o644),
                ("server.key", key_pem, 0o600),
            ):
                fd = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    mode,
                    dir_fd=folder,
                )
                with os.fdopen(fd, "wb") as handle:
                    os.fchmod(handle.fileno(), mode)
                    handle.write(data)
        finally:
            os.close(folder)
    except OSError as error:
        shutil.rmtree(out, ignore_errors=True)
        raise CertError(f"cannot write {out}: {error.strerror or error}") from None
    return files
