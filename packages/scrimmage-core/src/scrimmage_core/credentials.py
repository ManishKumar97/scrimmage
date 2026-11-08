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
from collections.abc import Mapping

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
# (host pattern, path pattern, what it is). In a host, "*" matches any run of characters,
# dots included. In a path, "*" at either end matches any run, slashes included, and a "*"
# between two slashes matches one segment. Paths are compared without a trailing slash. A
# rule for any host has no name: it matches by its path alone, so it is named by that.
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
    ("*", "/v1/secret/*", ""),
    ("*", "/v1/auth/*", ""),
    ("*", "/v1/sys/*", ""),
    ("*", "/v1/identity/*", ""),
    ("*", "/v1/*/data/*", ""),
    ("*", "/v1/*/creds/*", ""),
    ("*", "*/token", ""),
    ("*", "*/login/oauth/access_token", ""),
)
# The header every HashiCorp Vault client sends. A secret mount can sit at any depth under a
# namespace, as /v1/team/kv/data/db does, and by its path alone that looks like an ordinary
# /v1/stations/KSEA/data/latest, so the header says what the path cannot.
VAULT_TOKEN = "x-vault-token"
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


def path_rule(pattern: str) -> re.Pattern[str]:
    """A path pattern as a regex: a "*" at either end is any run, one inside is a segment."""
    parts = pattern.split("*")
    out: list[str] = []
    for i, part in enumerate(parts):
        out.append(re.escape(part))
        if i < len(parts) - 1:
            end = (i == 0 and not part) or (i == len(parts) - 2 and not parts[-1])
            out.append(".*" if end else "[^/]+")
    return re.compile("".join(out))


RULES = tuple((host, path_rule(path), path, what) for host, path, what in ENDPOINTS)


def credential(host: str, path: str) -> str | None:
    """What the endpoint is, if a request to it fetches a credential or a secret."""
    host, path = host_form(host), path_form(path)
    for host_pattern, rule, path_pattern, what in RULES:
        if fnmatch.fnmatchcase(host, host_pattern) and rule.fullmatch(path):
            return what or f"an endpoint that matches a credential path pattern ({path_pattern})"
    return None


def carries_vault_token(headers: Mapping[str, str]) -> bool:
    """Whether a request carries a Vault token, which names it a request to Vault's API."""
    return VAULT_TOKEN in {name.lower() for name in headers}
