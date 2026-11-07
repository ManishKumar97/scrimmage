"""Endpoints that hand out credentials, metadata or secrets.

A reply from one is the value most certain to be a secret, and a mock of it would put a
live token in a file meant for git, so scrimmage record never writes one. In a test it runs
for real, as it did in the recording. It sits beside provider_openai.py's model hosts,
as the design puts its credential table next to the model address table.
"""

import fnmatch

# (host pattern, path prefix, path ending, what it is). "*" in a host matches any part.
ENDPOINTS: tuple[tuple[str, str, str, str], ...] = (
    ("sts.amazonaws.com", "", "", "AWS STS"),
    ("sts.*.amazonaws.com", "", "", "AWS STS"),
    ("169.254.169.254", "", "", "EC2 instance metadata"),
    ("169.254.170.2", "", "", "ECS task metadata"),
    ("169.254.170.23", "", "", "EKS pod identity"),
    ("fd00:ec2::254", "", "", "EC2 instance metadata over IPv6"),
    ("metadata.google.internal", "", "", "Google Cloud metadata"),
    ("oauth2.googleapis.com", "", "", "Google OAuth"),
    ("sts.googleapis.com", "", "", "Google STS"),
    ("login.microsoftonline.com", "", "", "Microsoft Entra ID"),
    ("secretsmanager.*.amazonaws.com", "", "", "AWS Secrets Manager"),
    ("ssm.*.amazonaws.com", "", "", "AWS Systems Manager Parameter Store"),
    ("kms.*.amazonaws.com", "", "", "AWS KMS"),
    ("secretmanager.googleapis.com", "", "", "Google Secret Manager"),
    ("*.vault.azure.net", "", "", "Azure Key Vault"),
    ("*", "/v1/secret/", "", "a HashiCorp Vault secret"),
    ("*", "/v1/auth/", "", "a HashiCorp Vault login"),
    ("*", "/v1/sys/", "", "the HashiCorp Vault system API"),
    ("*", "/v1/identity/", "", "a HashiCorp Vault identity"),
    ("*", "", "/oauth/token", "an OAuth 2.0 token endpoint"),
    ("*", "", "/oauth2/token", "an OAuth 2.0 token endpoint"),
    ("*", "", "/oauth2/v2.0/token", "an OAuth 2.0 token endpoint"),
)


def credential(host: str, path: str) -> str | None:
    """What the endpoint is, if a request to it fetches a credential or a secret."""
    for pattern, prefix, ending, what in ENDPOINTS:
        if not fnmatch.fnmatchcase(host, pattern):
            continue
        if path.startswith(prefix) and path.endswith(ending):
            return what
    return None
