"""Endpoints that hand out credentials, metadata or secrets.

A reply from one is the value most certain to be a secret, and a mock of it would put a
live token in a file meant for git, so scrimmage record never writes one. In a test it runs
for real, as it did in the recording. It sits beside provider_openai.py's model hosts,
as the design puts its credential table next to the model address table.

The host and path are compared in one form, so a trailing dot, another spelling of an IP
address, a quoted letter or a doubled slash cannot slip past a pattern.
"""

import fnmatch
import ipaddress
import re
import urllib.parse

# AWS services whose replies are credentials or secrets, in every partition and FIPS form.
AWS = {
    "sts": "AWS STS",
    "secretsmanager": "AWS Secrets Manager",
    "ssm": "AWS Systems Manager Parameter Store",
    "kms": "AWS KMS",
}
AWS_HOSTS = tuple(
    (f"{service}{fips}.*.{suffix}", "*", name)
    for service, name in AWS.items()
    for fips in ("", "-fips")
    for suffix in ("amazonaws.com", "amazonaws.com.cn")
)
# (host pattern, path pattern, what it is). "*" matches any run of characters, dots and
# slashes included. Paths are compared without a trailing slash.
ENDPOINTS: tuple[tuple[str, str, str], ...] = (
    ("sts.amazonaws.com", "*", "AWS STS"),
    ("sts.amazonaws.com.cn", "*", "AWS STS"),
    *AWS_HOSTS,
    ("169.254.169.254", "*", "EC2 instance metadata"),
    ("fd00:ec2::254", "*", "EC2 instance metadata over IPv6"),
    ("169.254.170.2", "*", "ECS task metadata"),
    ("169.254.170.23", "*", "EKS Pod Identity"),
    ("fd00:ec2::23", "*", "EKS Pod Identity over IPv6"),
    ("metadata.google.internal", "*", "Google Cloud metadata"),
    ("metadata", "*", "Google Cloud metadata"),
    ("oauth2.googleapis.com", "*", "Google OAuth"),
    ("sts.googleapis.com", "*", "Google STS"),
    ("secretmanager.googleapis.com", "*", "Google Secret Manager"),
    ("login.microsoftonline.com", "*", "Microsoft Entra ID"),
    ("*.vault.azure.net", "*", "Azure Key Vault"),
    ("*.vault.azure.cn", "*", "Azure Key Vault"),
    ("*.vault.usgovcloudapi.net", "*", "Azure Key Vault"),
    ("token.actions.githubusercontent.com", "*", "GitHub Actions OIDC tokens"),
    ("*", "/v1/secret/*", "a HashiCorp Vault secret"),
    ("*", "/v1/auth/*", "a HashiCorp Vault login"),
    ("*", "/v1/sys/*", "the HashiCorp Vault system API"),
    ("*", "/v1/identity/*", "a HashiCorp Vault identity"),
    ("*", "/v1/*/data/*", "a HashiCorp Vault key-value secret on any mount"),
    ("*", "/v1/*/creds/*", "HashiCorp Vault dynamic credentials on any mount"),
    ("*", "*/token", "a token endpoint, such as OAuth 2.0's, Okta's or Azure's MSI"),
    ("*", "*/login/oauth/access_token", "GitHub's OAuth token endpoint"),
)
SLASHES = re.compile(r"/{2,}")
NUMBER = re.compile(r"0x[0-9a-f]+|[0-9]+")  # an IPv4 address written as one number


def host_form(host: str) -> str:
    """The host as compared: lower case, no trailing dot, an IP address in its usual form."""
    host = host.lower().rstrip(".").strip("[]")
    try:
        value: int | str = int(host, 0) if NUMBER.fullmatch(host) else host
        return str(ipaddress.ip_address(value))
    except ValueError:
        return host


def path_form(path: str) -> str:
    """The path as compared: unquoted, single slashes, no trailing slash."""
    return SLASHES.sub("/", urllib.parse.unquote(path)).rstrip("/") or "/"


def credential(host: str, path: str) -> str | None:
    """What the endpoint is, if a request to it fetches a credential or a secret."""
    host, path = host_form(host), path_form(path)
    for host_pattern, path_pattern, what in ENDPOINTS:
        if fnmatch.fnmatchcase(host, host_pattern) and fnmatch.fnmatchcase(path, path_pattern):
            return what
    return None
