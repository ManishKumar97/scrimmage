"""What redact removes from a recorded text."""

import base64
import random
import re
import sys
import time

import pytest

from scrimmage_core import redact as redact_module
from scrimmage_core.redact import redact, redacted


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
    # About 0.2 s on a quiet machine. A scan that backtracks takes minutes on a body like
    # this, so 30 s tells the two apart under any load.
    assert time.monotonic() - started < 30


@pytest.mark.design("record.secret_in_reply")
def test_redacted_counts_each_place_it_removed_and_gives_the_same_text() -> None:
    text = "key sk-proj-a1B2c3D4e5F6g7H8i9J0 at https://a.b/c?token=x and https://a.b/plain"
    assert redacted(text) == (redact(text), 2)
    assert redacted("nothing here") == ("nothing here", 0)


# One secret for each literal that lets a pass run. A new kind of secret needs its own.
SAMPLES = {
    "sk-": "sk-" + "a" * 20,
    "AKIA": "AKIA" + "A" * 16,
    "ASIA": "ASIA" + "B" * 16,
    "AIza": "AIza" + "c" * 35,
    "ghp_": "ghp_" + "d" * 36,
    "gho_": "gho_" + "d" * 36,
    "ghu_": "ghu_" + "d" * 36,
    "ghs_": "ghs_" + "d" * 36,
    "ghr_": "ghr_" + "d" * 36,
    "github_pat_": "github_pat_" + "e" * 22,
    "xox": "xoxb-" + "1" * 10,
    "xapp-": "xapp-" + "2" * 10,
    "bearer": "bearer " + "f" * 16,
    "authorization": "Authorization: Basic dXNlcjpwYXNz",
    "password": "password=hunter2",
    "passwd": "passwd=hunter2",
    "token": "token=hunter2",
    "secret": "secret=hunter2",
    "api_key": "api_key=hunter2",
    "api%5fkey": "api%5Fkey%3Dhunter2",
    "Illegal header": "Illegal header value b'x'",
}
JWT_SAMPLE = (
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
)


def reference(text: str) -> tuple[str, int]:
    """The three passes run unconditionally, on the product's own compiled expressions."""
    count = 0

    def url(found: re.Match[str]) -> str:
        nonlocal count
        kept = redact_module.plain_url(found)
        count += kept != found.group()
        return kept

    text = redact_module.URL.sub(url, text)
    text, jwts = redact_module.JWT.subn(redact_module.no_jwt, text)
    text, keys = redact_module.PATTERNS.subn(redact_module.REMOVED, text)
    return text, count + jwts + keys


def corpus() -> list[str]:
    rng = random.Random(20261108)
    pieces = [
        *SAMPLES.values(),
        *(literal for secret in redact_module.SECRETS for literal in secret.literals),
        JWT_SAMPLE,
        "eyJ",
        "://",
        "https://a.b/c?k=SECRET",
        "https://user:pw@host/x",
        "BeArEr " + "g" * 20,
        "BEARER\t" + "h" * 16,
        "bearer",
        "Authorization: Basic dXNlcjpwYXNz",
        "PASSWORD=hunter2",
        "api%5Fkey%3Dhunter2",
        "password=",
        "token=[secret removed]",
        "Bearer " + chr(0x17F) + "i" * 16,
        chr(0x17F),
        chr(0x130),
        chr(0x212A),
        "\ud800",
        "\n",
        "\\n",
        " ",
        "task-abc",
        "risk-abc",
        "sk",
        "-",
        "_",
        "xoxe." + SAMPLES["xox"],
        "plain words",
    ]
    out: list[str] = []
    for _ in range(5000):
        parts = [rng.choice(pieces) for _ in range(rng.randint(1, 12))]
        if rng.random() < 0.3:  # split a secret, or put something inside it
            i = rng.randrange(len(parts))
            at = rng.randrange(len(parts[i]) + 1)
            parts[i] = parts[i][:at] + rng.choice(["", " ", "x", "-", "://"]) + parts[i][at:]
        out.append(rng.choice(["", " ", "/"]).join(parts))
    return out


def test_redacted_gives_the_same_output_as_running_every_pass() -> None:
    for text in corpus():
        assert redacted(text) == reference(text), text


def test_every_kind_has_a_sample_and_each_sample_is_removed() -> None:
    literals = {literal for secret in redact_module.SECRETS for literal in secret.literals}
    assert set(SAMPLES) == literals, "a literal without a sample, or a sample without a literal"
    for secret in redact_module.SECRETS:
        assert any(re.fullmatch(secret.pattern, SAMPLES[lit]) for lit in secret.literals), secret
        for literal in secret.literals:
            sample = SAMPLES[literal]
            assert literal in (sample.lower() if secret.lowercase else sample), sample
            assert redacted(sample) == (redact_module.REMOVED, 1), sample


def test_every_letter_of_bearer_is_found_by_lowering_the_text() -> None:
    """The check lowers the text and looks for 'bearer'. A character that (?i:bearer) matches
    must lower to the letter it stands for, or a bearer token could be missed."""
    for letter in "bearer":
        matcher = re.compile(f"(?i:{letter})")
        for code in range(sys.maxunicode + 1):
            char = chr(code)
            if matcher.fullmatch(char):
                assert char.lower() == letter, (hex(code), letter)


def test_redacted_gives_a_plain_str_even_when_no_pass_runs() -> None:
    class Marked(str):
        pass

    for text in (Marked("nothing here"), Marked("sk-" + "a" * 20)):
        assert type(redacted(text)[0]) is str
        assert type(redact(text)) is str


@pytest.mark.design("security.redaction_shapes")
def test_the_ascii_only_patterns_match_no_letter_that_lowers_to_another() -> None:
    """Basic and the form fields are found by lowering the text. With the ASCII flag a
    letter matches only itself in either case, so no look-alike can slip past the check."""
    for letter in set("authorizationbasicpasswdtokensecretapi_key"):
        matcher = re.compile(f"(?ai:{letter})")
        for code in range(sys.maxunicode + 1):
            char = chr(code)
            if matcher.fullmatch(char):
                assert char.lower() == letter, (hex(code), letter)


NEW_LITERALS = {"authorization", "password", "passwd", "token", "secret", "api_key", "api%5fkey"}
B64 = "dXNlcjpwYXNz"
# The text, the part of it that is removed, and the secret that must not be left.
NEW_SECRETS = [
    ("Authorization: Basic dXNlcjpwYXNz", "Authorization: Basic dXNlcjpwYXNz", B64),
    ("authorization: basic dXNlcjpwYXNz==", "authorization: basic dXNlcjpwYXNz==", B64),
    ("-H 'Authorization: Basic dXNlcjpwYXNz'", "Authorization: Basic dXNlcjpwYXNz", B64),
    ('{"Authorization": "Basic dXNlcjpwYXNz"}', 'Authorization": "Basic dXNlcjpwYXNz', B64),
    (
        '{"Authorization ": "Basic dXNlcjpwYXNz"}',
        'Authorization ": "Basic dXNlcjpwYXNz',
        B64,
    ),
    ("password=hunter2", "password=hunter2", "hunter2"),
    ("PASSWORD=hunter2", "PASSWORD=hunter2", "hunter2"),
    ("passwd=hunter2&x=1", "passwd=hunter2", "hunter2"),
    ("token=hunter2", "token=hunter2", "hunter2"),
    ("access_token=hunter2", "access_token=hunter2", "hunter2"),
    ("secret=hunter2", "secret=hunter2", "hunter2"),
    ("client_secret=hunter2", "secret=hunter2", "hunter2"),
    ("api_key=hunter2", "api_key=hunter2", "hunter2"),
    ("API_KEY=hunter2", "API_KEY=hunter2", "hunter2"),
    ("api%5Fkey=hunter2", "api%5Fkey=hunter2", "hunter2"),
    ("password%3Dhunter2", "password%3Dhunter2", "hunter2"),
    ("password=hun%20ter%2B2", "password=hun%20ter%2B2", "hun%20ter%2B2"),
    ("grant_type=password&password=p%40ss&u=a", "password=p%40ss", "p%40ss"),
]


def without_new_passes(text: str) -> str:
    """The module as it was before: the same three passes, minus the two new secrets."""
    old = re.compile(
        "|".join(
            secret.pattern
            for secret in redact_module.SECRETS
            if not NEW_LITERALS & set(secret.literals)
        )
    )
    text = redact_module.URL.sub(redact_module.plain_url, text)
    text = redact_module.JWT.sub(redact_module.no_jwt, text)
    return old.sub(redact_module.REMOVED, text)


@pytest.mark.design("security.redaction_shapes")
@pytest.mark.parametrize(("text", "span", "value"), NEW_SECRETS)
def test_the_old_module_kept_each_new_secret_and_the_new_one_removes_it(
    text: str, span: str, value: str
) -> None:
    assert value in without_new_passes(text)
    assert redact(text) == text.replace(span, "[secret removed]")
    assert redact(redact(text)) == redact(text)


@pytest.mark.design("security.redaction_shapes")
def test_the_only_difference_from_the_old_module_is_the_new_secrets_removed() -> None:
    rng = random.Random(20261111)
    # texts from the old corpus, with none of the new kinds in them
    benign = [t for t in corpus() if not any(lit in t.lower() for lit in NEW_LITERALS)][:400]
    assert len(benign) == 400
    for _ in range(3000):
        parts: list[str] = []
        expected: list[str] = []
        for _ in range(rng.randint(1, 6)):
            if rng.random() < 0.5:
                text, span, _ = rng.choice(NEW_SECRETS)
                parts.append(text)
                expected.append(text.replace(span, redact_module.REMOVED))
            else:
                part = rng.choice(benign)
                parts.append(part)
                expected.append(part)
        # A newline ends an "Illegal header" line, and a # ends a bearer token, so a part
        # is judged alone: the old module already removed a field that a bearer ran into.
        text = "\n# ".join(parts)
        assert redact(text) == without_new_passes("\n# ".join(expected)), text


@pytest.mark.design("security.redaction_shapes")
@pytest.mark.parametrize(
    "text",
    [
        "password=",
        "password=&user=a",
        "password= ",
        "token=",
        "secret=\n",
        "please reset your password at the desk",
        "Reset your password at https://example.com/account",
        "the secret is out",
        "https://example.com/oauth/token",
        "https://example.com/api/token/refresh",
        "/v1/auth/token/lookup",
        "a token bucket limits the rate",
        "password: hunter2",
        "basic usage, see Authorization in the docs",
        "Authorization: Basic",
        "Authorization: Basic abc",
        "the basic dXNlcjpwYXNz is prose",
        "password=[secret removed]",
        "token=[removed]",
        "pa\u017f\u017fword=hunter2",  # long s is not an s
    ],
)
def test_prose_empty_values_and_paths_are_kept(text: str) -> None:
    assert redact(text) == text
