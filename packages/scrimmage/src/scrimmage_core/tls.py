"""The TLS settings of the control channel, in one place.

Both ends speak TLS 1.3 only. The client trusts the one authority it is given and nothing
else: it never loads the system's store. Python's ssl module, on OpenSSL, does the work; this
module only fixes the settings.
"""

import ssl
from pathlib import Path

from scrimmage_core.errors import ScrimmageError

CA_MAX_BYTES = 64 * 1024
BEGIN = "-----BEGIN CERTIFICATE-----"


class TlsError(ScrimmageError):
    """A certificate, a key or a trust file that cannot be used as the control channel needs."""


def _need_tls13() -> None:
    if not ssl.HAS_TLSv1_3:
        raise TlsError(
            f"this Python's OpenSSL ({ssl.OPENSSL_VERSION}) has no TLS 1.3, which the control "
            "channel requires; use a Python built on OpenSSL 1.1.1 or later"
        )


def client_context(ca: Path) -> ssl.SSLContext:
    """The SDK's context: TLS 1.3, the strict chain rules, and only the one authority in ca.
    The host name is checked against the host in the control URL, by name or by address."""
    _need_tls13()
    try:
        with ca.open("rb") as handle:
            text = handle.read(CA_MAX_BYTES + 1).decode("ascii", "replace")
    except OSError as exc:
        raise TlsError(f"{ca} cannot be read: {exc.strerror or exc}") from None
    if len(text) > CA_MAX_BYTES:
        raise TlsError(f"{ca} is over {CA_MAX_BYTES} bytes, so it is not an authority")
    found = text.count(BEGIN)
    if found != 1:
        raise TlsError(
            f"{ca} holds {found} certificates; it must hold exactly one, the authority "
            "scrimmage cert made, and never a bundle of public authorities"
        )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.verify_flags |= ssl.VERIFY_X509_STRICT
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    try:
        context.load_verify_locations(cafile=str(ca))  # never load_default_certs
    except ssl.SSLError as exc:
        raise TlsError(f"{ca} is not a certificate: {exc.reason or exc}") from None
    return context


def server_context(cert: Path, key: Path) -> ssl.SSLContext:
    """The runner's context: TLS 1.3, with the certificate chain and the key it signs with."""
    _need_tls13()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    try:
        context.load_cert_chain(str(cert), str(key))
    except ssl.SSLError as exc:
        raise TlsError(
            f"the certificate {cert} and the key {key} cannot be used together: {exc.reason or exc}"
        ) from None
    except OSError as exc:
        raise TlsError(f"cannot read the certificate or key: {exc.strerror or exc}") from None
    return context


def check_valid(context: ssl.SSLContext, cert: Path) -> None:
    """Raise TlsError unless the certificate is inside its dates. A handshake between the
    server context and a client that trusts the certificate alone shows what OpenSSL says
    of it, with no third-party library."""
    client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client.minimum_version = ssl.TLSVersion.TLSv1_3
    client.check_hostname = False
    client.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    try:
        client.load_verify_locations(cafile=str(cert))
    except (ssl.SSLError, OSError) as exc:
        raise TlsError(f"{cert} cannot be read as a certificate: {exc}") from None
    to_server, to_client = ssl.MemoryBIO(), ssl.MemoryBIO()
    mine = client.wrap_bio(to_server, to_client, server_hostname="scrimmage-check")
    theirs = context.wrap_bio(to_client, to_server, server_side=True)
    for _ in range(8):
        done = True
        for side in (mine, theirs):
            try:
                side.do_handshake()
            except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
                done = False
            except ssl.SSLCertVerificationError as exc:
                raise TlsError(
                    f"the certificate {cert} is not usable: {exc.verify_message}"
                ) from None
            except ssl.SSLError as exc:
                raise TlsError(f"the certificate {cert} is not usable: {exc}") from None
        if done:
            return
    raise TlsError(f"the certificate {cert} did not complete a test handshake")
