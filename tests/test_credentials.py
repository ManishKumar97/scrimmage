"""Credential, metadata and secret-store endpoints: never saved, in any spelling."""

import httpx2
import pytest

from scrimmage_core import credentials, http_gate
from scrimmage_core.redact import redacted
from test_record_lines import Server, Sink, gated, sink, web

__all__ = ["sink", "web"]  # the fixtures, used by name


@pytest.mark.design("record.credential_at_gate")
@pytest.mark.parametrize(
    ("url", "what"),
    [
        ("http://169.254.169.254/latest/api/token", "EC2 instance metadata"),
        ("https://sts.us-east-1.amazonaws.com/", "AWS STS"),
        ("https://vault.acme.internal/v1/secret/data/db", "a HashiCorp Vault secret"),
        ("https://login.acme.internal/oauth2/v2.0/token", "a token endpoint"),
        ("https://keys.vault.azure.net/secrets/db", "Azure Key Vault"),
    ],
)
def test_a_credential_request_is_never_saved_inside_a_tool_or_out(
    web: Sink, url: str, what: str
) -> None:
    def credentials(name: str) -> str:
        with httpx2.Client(transport=httpx2.MockTransport(Server())) as client:
            return client.get(url).text

    gated(credentials)(name="a")
    with httpx2.Client(transport=httpx2.MockTransport(Server())) as client:
        client.get(url)
    found = [line for line in web.calls() if line["kind"] == "http"]
    assert [line["recorded"]["code"] for line in found] == ["credential", "credential"]
    assert what in found[0]["recorded"]["why"]
    assert "parent" in found[0] and "parent" not in found[1]


# Credential endpoints, each spelling of each one.


@pytest.mark.design("record.credential_at_gate")
@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/api/token",
        "http://[fd00:ec2::254]/latest/meta-data/",
        "http://[fd00:ec2:0::254]/latest/meta-data/",
        "http://[FD00:EC2::254]/latest/meta-data/",
        "http://[fd00:ec2::23]/v1/credentials",
        "http://2852039166/latest/meta-data/",
        "http://0xa9fea9fe/latest/",
        "http://169.254.169.254:80/metadata/identity/oauth2/token?api-version=2018",
        "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
        "http://METADATA.GOOGLE.INTERNAL/computeMetadata/v1/",
        "http://metadata.google.internal./computeMetadata/v1/",
        "http://metadata/computeMetadata/v1/",
        "https://vault.acme:8200/v1/secret/data/db",
        "https://vault.acme:8200/v1/kv/data/db",
        "https://vault.acme:8200/v1/database/creds/app",
        "https://vault.acme:8200/v1/%73ecret/data/db",
        "https://vault.acme:8200//v1/secret/data/db",
        "https://vault.acme:8200/v1/auth/token/lookup-self",
        "https://secretsmanager.us-east-1.amazonaws.com/",
        "https://secretsmanager-fips.us-east-1.amazonaws.com/",
        "https://ssm.eu-west-1.amazonaws.com/",
        "https://sts-fips.us-east-1.amazonaws.com/",
        "https://sts.amazonaws.com.cn/",
        "https://kms.cn-north-1.amazonaws.com.cn/",
        "https://secretmanager.googleapis.com/v1/projects/p/secrets/s/versions/latest:access",
        "https://kv1.vault.azure.net/secrets/x",
        "https://kv1.vault.azure.cn/secrets/x",
        "https://login.acme.com/oauth/token",
        "https://login.acme.com/oauth/token/",
        "https://acme.okta.com/oauth2/default/v1/token",
        "https://github.com/login/oauth/access_token",
        "https://token.actions.githubusercontent.com/",
        "http://127.0.0.1:41056/msi/token",
        "https://oauth2.googleapis.com/token",
        "https://login.microsoftonline.com/t/oauth2/v2.0/token",
    ],
)
def test_every_spelling_of_a_credential_endpoint_is_found(url: str) -> None:
    parsed = httpx2.URL(url)
    assert credentials.credential(http_gate.host_of(parsed), http_gate.path_of(parsed)), url


@pytest.mark.design("record.credential_at_gate")
@pytest.mark.parametrize(
    "url",
    ["https://inventory.acme.internal/stock/SKU-42", "https://s3.us-east-1.amazonaws.com/b/k",
     "https://api.acme.internal/v1/orders", "https://api.acme.internal/tokens/list",
     "http://169.254.169.25/x", "https://metadata.acme.internal/"],
)  # fmt: skip
def test_an_ordinary_endpoint_is_not_a_credential_one(url: str) -> None:
    parsed = httpx2.URL(url)
    assert credentials.credential(http_gate.host_of(parsed), http_gate.path_of(parsed)) is None


@pytest.mark.design("record.secret_in_reply")
def test_an_sts_reply_has_its_temporary_key_id_removed() -> None:
    reply = (
        "<AssumeRoleResponse><Credentials><AccessKeyId>ASIAIOSFODNN7EXAMPLE</AccessKeyId>"
        "</Credentials></AssumeRoleResponse>"
    )
    cleaned, count = redacted(reply)
    assert "ASIAIOSFODNN7EXAMPLE" not in cleaned and count == 1
