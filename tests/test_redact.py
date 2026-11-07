"""What redact removes from a recorded text."""

import base64
import time

import pytest

from scrimmage_core.redact import redact


@pytest.mark.design("redact.urls")
@pytest.mark.parametrize(
    ("text", "kept"),
    [
        ("for url 'https://a.b/c?api_key=SECRET'", "for url 'https://a.b/c?[removed]'"),
        ('see "http://a.b?x=SECRET" now', 'see "http://a.b?[removed]" now'),
        ("https://a.b/c#SECRET", "https://a.b/c#[removed]"),
        ("https://user:SECRET@a.b/c", "https://[removed]@a.b/c"),
        (
            "postgres://u:SECRET@db:5432/x?sslmode=require",
            "postgres://[removed]@db:5432/x?[removed]",
        ),
        ("https://a.b/c and https://d.e/f?y=SECRET", "https://a.b/c and https://d.e/f?[removed]"),
        ("'https://a.b/x?q=O'Brien&api_key=SECRET'", "'https://a.b/x?[removed]'"),
        ("https://a.b/x?q=back\\slash&k=SECRET end", "https://a.b/x?[removed] end"),
        ("'https://usr:pa'SECRET@a.b/x'", "'https://[removed]@a.b/x'"),
        ("(https://a.b/c?x=SECRET).", "(https://a.b/c?[removed])."),
        ("https://a.b/x?token=sk-" + "a" * 20 + " end", "https://a.b/x?[removed] end"),
        ("'https://a.b/c' and 'https://d.e/f'", "'https://a.b/c' and 'https://d.e/f'"),
        ("no url here? a=1", "no url here? a=1"),
        ("1.https://u:SECRET@c.d/y?k=SECRET", "1.https://[removed]@c.d/y?[removed]"),
        ("-ihttps://c.d/y?k=SECRET", "-ihttps://c.d/y?[removed]"),
        ("'https://a.b/x','https://u:SECRET@c.d/y'", "'https://a.b/x','https://[removed]@c.d/y'"),
        ("https://a.b/f/https://u:SECRET@c.d/y", "https://a.b/f/https://[removed]@c.d/y"),
        ("https://a.b,https://u:SECRET@c.d/y", "https://a.b,https://[removed]@c.d/y"),
    ],
)
def test_a_url_keeps_its_place_but_not_its_query_or_credentials(text: str, kept: str) -> None:
    assert redact(text) == kept


def test_a_quoted_bad_header_is_removed() -> None:
    assert "SECRET" not in redact("Illegal header value b'SECRET '")


def test_redacting_twice_changes_nothing_more() -> None:
    once = redact("'https://u:SECRET@a.b/c?x=SECRET'")
    assert "SECRET" not in once
    assert redact(once) == once


@pytest.mark.parametrize(
    "before", ["token=", "id_token_", "a-", "", "line\\n", "x.", "api.example.com."]
)
def test_a_jwt_is_removed_after_any_separator(before: str) -> None:
    jwt = "eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.c2lnbmF0dXJlMTIz"
    assert redact(before + jwt) == before + "[secret removed]"


def test_a_jwt_holding_a_key_shape_goes_whole() -> None:
    jwt = "eyJhbGciOiJIUzI1.eyJzsk-aaaaaaaaaaaaaaaaaaaa.c2lnbmF0dXJlMTIz"
    assert redact(jwt) == "[secret removed]"


def test_every_dotted_part_after_a_jwt_goes_with_it() -> None:
    jwt = "eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.c2lnbmF0dXJlMTIz.dGFnZ2dnZ2c.cGFydA"
    assert redact(f"eyJaaaaaaaaa.bbbbbbbbbb.{jwt}") == "[secret removed]"


@pytest.mark.parametrize(
    "body",
    [
        base64.urlsafe_b64encode(b"\n".join(b'{"n": %d}' % n for n in range(100_000))).decode(),
        base64.urlsafe_b64encode(b'~{"id":1}' * 200_000).decode(),
        "-eyJ" * 500_000,
    ],
    ids=["json lines", "aligned records", "repeated starts"],
)
def test_a_large_base64_body_is_not_slow(body: str) -> None:
    started = time.monotonic()
    redact(body)
    assert time.monotonic() - started < 5
