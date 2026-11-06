"""What redact removes from a recorded text."""

import pytest

from faultline_sdk.redact import redact


@pytest.mark.design("redact.urls")
@pytest.mark.parametrize(
    ("text", "kept"),
    [
        ("for url 'https://a.b/c?api_key=SECRET'", "for url 'https://a.b/c'"),
        ('see "http://a.b?x=SECRET" now', 'see "http://a.b" now'),
        ("https://a.b/c#SECRET", "https://a.b/c"),
        ("https://user:SECRET@a.b/c", "https://a.b/c"),
        ("postgres://u:SECRET@db:5432/x?sslmode=require", "postgres://db:5432/x"),
        ("https://a.b/c and https://d.e/f?y=SECRET", "https://a.b/c and https://d.e/f"),
        ("no url here? a=1", "no url here? a=1"),
    ],
)
def test_a_url_keeps_its_place_but_not_its_query_or_credentials(text: str, kept: str) -> None:
    assert redact(text) == kept


def test_a_quoted_bad_header_is_removed() -> None:
    assert "SECRET" not in redact("Illegal header value b'SECRET '")
