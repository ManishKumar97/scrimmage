"""What redact removes from a recorded text."""

import base64
import json
import random
import re
import sys
import time
from pathlib import Path
from typing import cast

import pytest

from scrimmage_core import capture, state
from scrimmage_core import redact as redact_module
from scrimmage_core.redact import redact, redacted

ROOT = Path(__file__).parents[1]


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
    "pgpass": "PGPASS=hunter2",
    "mysql_pwd": "MYSQL_PWD=hunter2",
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
    text, keys = redact_module.remove_keys(text)
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
        r"password=abc\"2zz",
        r"password=abc\'2zz",
        r"password=C:\new2zz",
        # a value kept on purpose, then an escape, then a secret field
        r"next_page_token=x\"",
        "next_page_token=x\\n",
        'next_page_token=x"',
        "f(token=tok)\\n",
        r"next_page_token=x\"password=hunter2",
        "f(token=tok)\\npassword=hunter2",
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


LOOKALIKES = "\u017f\u212a\u0130\u0131"  # long s, Kelvin sign, dotted and dotless i


@pytest.mark.design("security.redaction_shapes")
def test_a_lookalike_letter_never_matches_a_lowercase_pattern_the_pre_check_misses() -> None:
    """The pre-check lowers the text and looks for a literal. A pattern that a look-alike such
    as the long s can match, but whose literal the lowered text lacks, would leak the secret
    with no pass run. Each lowercase kind needs its ASCII flag."""
    tried = 0
    for secret in redact_module.SECRETS:
        if not secret.lowercase:
            continue
        for literal in secret.literals:
            sample = SAMPLES[literal]
            for at, letter in enumerate(sample):
                for mark in LOOKALIKES:
                    if re.fullmatch(f"(?i:{re.escape(letter)})", mark) is None:
                        continue
                    tried += 1
                    changed = sample[:at] + mark + sample[at + 1 :]
                    if redact_module.PATTERNS.search(changed):
                        lowered = changed.lower()
                        assert any(lit in lowered for lit in redact_module.LOWER_LITERALS), changed
    assert tried > 0


# Every name that is kept: the paging names, the is_ style prefixes and the tokenizer names.
HARMLESS_NAMES = [
    *(f"{word}_token" for word in ("next", "page", "continuation", "pagination", "sync", "cursor")),
    *(
        f"{word}_{kind}"
        for word in ("is", "has", "top", "max", "min", "num", "n")
        for kind in ("secret", "token")
    ),
    *(f"{word}_token" for word in ("eos", "bos", "pad", "unk", "sep", "cls")),
    "IS_SECRET",
    "DB_IS_SECRET",
    "Next_Page_Token",
]

NEW_LITERALS = {
    "authorization",
    "password",
    "passwd",
    "token",
    "secret",
    "api_key",
    "api%5fkey",
    "pgpass",
    "mysql_pwd",
}
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
    (
        "[(b'Authorization', b'Basic dXNlcjpwYXNz')]",
        "Authorization', b'Basic dXNlcjpwYXNz",
        B64,
    ),
    ("password=hunter2", "password=hunter2", "hunter2"),
    ("PASSWORD=hunter2", "PASSWORD=hunter2", "hunter2"),
    ("passwd=hunter2&x=1", "passwd=hunter2", "hunter2"),
    ("token=hunter2", "token=hunter2", "hunter2"),
    ("access_token=hunter2", "access_token=hunter2", "hunter2"),
    ("client_secret=hunter2", "client_secret=hunter2", "hunter2"),
    ("refresh_token=hunter2", "refresh_token=hunter2", "hunter2"),
    ("csrf_token=hunter2", "csrf_token=hunter2", "hunter2"),
    ("secret=hunter2", "secret=hunter2", "hunter2"),
    ("api_key=hunter2", "api_key=hunter2", "hunter2"),
    ("API_KEY=hunter2", "API_KEY=hunter2", "hunter2"),
    ("api%5Fkey=hunter2", "api%5Fkey=hunter2", "hunter2"),
    ("password%3Dhunter2", "password%3Dhunter2", "hunter2"),
    ("password=hun%20ter%2B2", "password=hun%20ter%2B2", "hun%20ter%2B2"),
    ("grant_type=password&password=p%40ss&u=a", "password=p%40ss", "p%40ss"),
    ("password='hunter2'", "password='hunter2'", "hunter2"),
    ('password="hunter2"', 'password="hunter2"', "hunter2"),
    ('password="two words"', 'password="two words"', "two words"),
    ("password=<hunter2>", "password=<hunter2>", "hunter2"),
    ('curl -d "token=abc"', "token=abc", "abc"),
    ("a\ntoken=abc", "token=abc", "abc"),
    ("password=hunter2, then", "password=hunter2,", "hunter2"),
    ("PGPASSWORD=hunter2", "PGPASSWORD=hunter2", "hunter2"),
    ("PGPASS=hunter2", "PGPASS=hunter2", "hunter2"),
    ("MYSQL_PWD=hunter2", "MYSQL_PWD=hunter2", "hunter2"),
    ("NGROK_AUTHTOKEN=hunter2", "NGROK_AUTHTOKEN=hunter2", "hunter2"),
    ("authtoken=hunter2", "authtoken=hunter2", "hunter2"),
    ("accesstoken=hunter2", "accesstoken=hunter2", "hunter2"),
    ("password=[secret removed]hunter2", "password=[secret removed]hunter2", "hunter2"),
    ("token=[removed]hunter2", "token=[removed]hunter2", "hunter2"),
    ("password='a\\b'", "password='a\\b'", "a\\b"),
    ("password='a\"b'", "password='a\"b'", 'a"b'),
    ('password="a\'b"', 'password="a\'b"', "a'b"),
    ("password=<unclosed more", "password=<unclosed", "unclosed"),
    ("user=admin, password=changeme, host=db", "password=changeme,", "changeme"),
    ("password=Summer.Time", "password=Summer.Time", "Summer.Time"),
    ("DB_HOST=db\\nDB_PASSWORD=hunter2\\n", "DB_PASSWORD=hunter2", "hunter2"),
    ("DB_PASSWORD=hunter2", "DB_PASSWORD=hunter2", "hunter2"),
    ("AWS_SECRET_ACCESS_KEY=abc", "AWS_SECRET_ACCESS_KEY=abc", "abc"),
    ("GITHUB_TOKEN=ghp_x", "GITHUB_TOKEN=ghp_x", "ghp_x"),
    ("MY_API_KEY=k", "MY_API_KEY=k", "k"),
    ("export DB_PASSWORD='x'", "DB_PASSWORD='x'", "'x'"),
    ("a_b_c_secret=zz", "a_b_c_secret=zz", "zz"),
    ("SYNC_PASSWORD=hunter2", "SYNC_PASSWORD=hunter2", "hunter2"),
    ("NEXT_PASSWORD=hunter2", "NEXT_PASSWORD=hunter2", "hunter2"),
    ("next_secret=hunter2", "next_secret=hunter2", "hunter2"),
    ("cursor_api_key=hunter2", "cursor_api_key=hunter2", "hunter2"),
    ("DB_SYNC_PASSWORD=hunter2", "DB_SYNC_PASSWORD=hunter2", "hunter2"),
    ("page_password=hunter2", "page_password=hunter2", "hunter2"),
    ("PAD_SECRET=hunter2", "PAD_SECRET=hunter2", "hunter2"),
    ("password=hun\\ter2zz", "password=hun\\ter2zz", "ter2zz"),
    ("PGPASSWORD=p\\$ss and more", "PGPASSWORD=p\\$ss", "p\\$ss"),
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
@pytest.mark.parametrize(
    ("text", "span", "value"), NEW_SECRETS, ids=[f"new-{n}" for n in range(len(NEW_SECRETS))]
)
def test_the_old_module_kept_each_new_secret_and_the_new_one_removes_it(
    text: str, span: str, value: str
) -> None:
    assert value in without_new_passes(text)
    # a text that is JSON as a whole is read as JSON, and each string is cleaned on its own
    expected = JSON_EXPECTED.get(text)
    if expected is None:
        assert not parses_as_container(text), text
        expected = text.replace(span, "[secret removed]")
    assert redact(text) == expected
    assert redact(redact(text)) == redact(text)


@pytest.mark.design("security.redaction_shapes")
@pytest.mark.parametrize(
    ("text", "span", "value"), NEW_SECRETS, ids=[f"new-{n}" for n in range(len(NEW_SECRETS))]
)
def test_each_new_secret_is_also_removed_in_the_middle_of_other_text(
    text: str, span: str, value: str
) -> None:
    """Behind a prefix, so a text that is itself JSON is judged as plain text. This is in
    addition to the test above, which runs every row as it is."""
    text = "log: " + text
    assert value in without_new_passes(text)
    assert redact(text) == text.replace(span, "[secret removed]")


def parses_as_container(text: str) -> bool:
    try:
        return isinstance(json.loads(text), (dict, list))
    except ValueError:
        return False


# What a row that is JSON as a whole comes out as: the same JSON, with the secret string removed.
JSON_EXPECTED = {
    '{"Authorization": "Basic dXNlcjpwYXNz"}': '{"Authorization": "[secret removed]"}',
    '{"Authorization ": "Basic dXNlcjpwYXNz"}': '{"Authorization ": "[secret removed]"}',
}
# Pairs in JSON: the name and the value are two strings, so no text pattern sees both.
JSON_PAIRS = [
    (
        '[["authorization", "Basic dXNlcjpwYXNz"]]',
        '[["authorization", "[secret removed]"]]',
    ),
    (
        '{"name": "Authorization", "value": "Basic dXNlcjpwYXNz"}',
        '{"name": "Authorization", "value": "[secret removed]"}',
    ),
    (
        '{"Proxy-Authorization": "Basic dXNlcjpwYXNzd29yZA=="}',
        '{"Proxy-Authorization": "[secret removed]"}',
    ),
    ('{"s": "p\\u0061ssword=hunter2"}', '{"s": "[secret removed]"}'),
    ('{"s": "password\\u003dhunter2"}', '{"s": "[secret removed]"}'),
    ('{"s": "x password=p\\u00e4ss"}', '{"s": "x [secret removed]"}'),
    # kept: an empty value, a counting name, a paging name, a marker, a word that is not a name
    ('{"token": "", "max_token": "3", "next_page_token": "abc"}', None),
    ('{"password": "[secret removed]", "author": "x"}', None),
    ('{"Authorization": "Basic realm"}', None),
    ('[["authorization", "Bearer"], ["token", null]]', None),
]


@pytest.mark.design("security.redaction_shapes")
@pytest.mark.parametrize(
    ("text", "expected"), JSON_PAIRS, ids=[f"pair-{n}" for n in range(len(JSON_PAIRS))]
)
def test_a_name_and_value_that_are_two_strings_in_json_are_judged_together(
    text: str, expected: str | None
) -> None:
    assert redact(text) == (text if expected is None else expected)
    json.loads(redact(text))


@pytest.mark.design("security.redaction_shapes")
def test_numbers_spacing_and_unicode_are_written_back_as_they_were() -> None:
    numbers = '{"n": [1e400, 0.10, 123456789012345678901234567890, -0.0, 1E+2], "s": "token=abc"}'
    assert redact(numbers) == numbers.replace("token=abc", "[secret removed]")
    spaced = '{"a": 1, "s": "token=abc", "b": [1, 2]}'
    assert redact(spaced) == '{"a": 1, "s": "[secret removed]", "b": [1, 2]}'
    compact = '{"a":1,"s":"token=abc","b":[1,2]}'
    assert redact(compact) == '{"a":1,"s":"[secret removed]","b":[1,2]}'
    raw = '{"s":"\u00e9 token=abc \u00e9"}'  # the letter itself
    assert redact(raw) == '{"s":"\u00e9 [secret removed] \u00e9"}'
    escaped = '{"s":"\\u00e9 token=abc \\u00e9"}'
    assert redact(escaped) == '{"s":"\\u00e9 [secret removed] \\u00e9"}'


@pytest.mark.design("security.redaction_shapes")
@pytest.mark.parametrize(
    "text",
    [
        "set it (PASSWORD=hunter2) then",
        "(a, PGPASSWORD=hunter2, b)",
        "password=True-Love99",
        "token=None:abc123",
        "password=False/xyz",
        "x = f(a) + (password=changeme)",
        "user=admin, password=changeme, host=db",
    ],
    ids=lambda text: text[:30],
)
def test_only_none_true_false_and_a_bare_argument_of_a_named_call_are_code(text: str) -> None:
    assert "[secret removed]" in redact(text)
    assert not any(secret in redact(text) for secret in ("hunter2", "Love99", "abc123", "xyz"))
    assert "changeme" not in redact(text)


@pytest.mark.design("security.redaction_shapes")
def test_redacted_json_text_is_still_json() -> None:
    """A reply body is redacted as text before it is read as JSON, so a cut that takes in a
    backslash or a quote would leave a body that does not parse."""
    for text, _span, _value in NEW_SECRETS:
        assert json.loads(redact(json.dumps(text))) == redact(text)
        held = {"a": text, "b": [text, 1], "c": {"d": text}}
        assert json.loads(redact(json.dumps(held))) == {
            "a": redact(text),
            "b": [redact(text), 1],
            "c": {"d": redact(text)},
        }


@pytest.mark.design("security.redaction_shapes")
def test_the_new_secrets_leave_what_the_other_passes_remove_as_it_was() -> None:
    """The benign texts hold none of the new literals, so they are redacted exactly as before.
    Next to a new secret the rest is still redacted as before. This does not show that a text
    holding a new literal changes only in the secret: the kept tests do."""
    rng = random.Random(20261112)
    # texts from the old corpus, with none of the new kinds in them
    benign = [t for t in corpus() if not any(lit in t.lower() for lit in NEW_LITERALS)][:400]
    assert len(benign) == 400
    for text in benign:
        assert redact(text) == without_new_passes(text), text
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
        text = "log:\n# " + "\n# ".join(parts)
        assert redact(text) == without_new_passes("log:\n# " + "\n# ".join(expected)), text


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
        *(f"{name}=abc" for name in HARMLESS_NAMES),
        "next_page_token=abc",
        "page_token=CgQ",
        "is_secret=true",
        "top_secret=false",
        "eos_token=2",
        "pad_token=0",
        "max_token=100",
        "mytoken=5",
        "def f(password=None):",
        "f(token=tok, n=1)",
        "f(token=tok1)",
        "x = f(a, password=secret_value)",
        "f(password=True) and token=False",
        '{"a": "password=", "b": "x"}',
        '{"password=": "x"}',
        '["token=", "hello"]',
        'see {"a": "password=", "b": "x"} here',
        'see {"password=": "x", "n": 1} here',
        'see ["token=", "hello"] here',
        '{"note": "send token="}',
        "password=[secret removed]",
        "authorization basic info",
        "Authorization: Basic realm",
        'password=""',
        "password=''",
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


SEEDED = [
    "password={v}",
    "PASSWORD={v}",
    "token={v}&x=1",
    "DB_PASSWORD='{v}'",
    'export API_KEY="{v}"',
    'curl -d "token={v}"',
    "client_secret={v}",
    "password%3D{v}",
    "AWS_SECRET_ACCESS_KEY={v}",
    "PGPASSWORD={v}",
    "password=<{v}>",
    "password=[secret removed]{v}",
    "password=C:\\new{v}",  # a backslash and a letter in a string, as in a Windows path
    'password=abc\\"{v}',
    "Authorization: Basic {b}",
    "Bearer {b}{b}",
    "sk-proj-{b}{b}",
    "https://u:{v}@a.b/c?k=1",
    "postgres://u:{v}@db/x",
]
JUNK = [
    '"',
    "'",
    "\\",
    "\n",
    "\t",
    "\r",
    "\\n",
    "\\\\",
    "é",
    "\N{LINE SEPARATOR}",
    "\U0001f600",
    ", ",
    ": ",
    "{",
    "[",
    "<",
    ">",
    "password=",
    "token=",
    "x",
    " ",
    "",
]
EMPTY_FIELDS = ["password=", "token=", "secret=", "api_key="]


def seeded_text(rng: random.Random, found: list[str]) -> str:
    parts: list[str] = []
    for _ in range(rng.randint(0, 5)):
        if rng.random() < 0.4:
            value = f"hunt{len(found)}x{rng.randrange(10**6)}"
            b64 = base64.b64encode(f"u:{value}pad".encode()).decode().rstrip("=")
            template = rng.choice(SEEDED)
            # the secret is the base64 text for the header shapes, else the plain value
            found.append(b64 if "{b}" in template else value)
            # a space on each side, so a name is never glued to a letter by the junk
            parts.append(" " + template.format(v=value, b=b64) + " ")
        else:
            parts.append(rng.choice(JUNK + EMPTY_FIELDS))
    return "".join(parts)


def seeded_pair(rng: random.Random, found: list[str]) -> object:
    """A header map, a list of pairs, or a name and a value: two strings in JSON."""
    value = f"hunt{len(found)}x{rng.randrange(10**6)}"
    b64 = base64.b64encode(f"u:{value}pad".encode()).decode().rstrip("=")
    name = rng.choice(
        ["Authorization", "authorization", "Proxy-Authorization", "HTTP_AUTHORIZATION"]
    )
    basic = f"Basic {b64}"
    shapes = [
        ({name: basic, "n": 1}, b64),
        ([[name, basic], ["x", "y"]], b64),
        ({"name": name, "value": basic}, b64),
        ({"field": name, "values": [basic, "other"]}, b64),
        ({name: [basic]}, b64),
        (["Host", "example.com", name, basic, "Accept", "*/*"], b64),
        ({"headers": [{"name": name, "value": basic}, {"name": "Host", "value": "h"}]}, b64),
        ([{"Key": name, "Value": [basic]}], b64),
        ([{"Name": name, "Value": basic}], b64),
        ({f"{name}:": basic}, b64),
        ([name, [basic]], b64),
    ]
    tree, secret = rng.choice(shapes)
    found.append(secret)
    if rng.random() < 0.4:
        # an object beside the Basic value, holding a second secret that must be cleaned too
        other = f"hunt{len(found)}y{rng.randrange(10**6)}"
        found.append(other)
        beside = {"k": f" password={other} ", "n": 1}
        if rng.random() < 0.5:
            tree = {name: [beside, basic]}
        else:
            tree = {"Key": name, "Value": [beside, basic]}
    return tree


def seeded_tree(rng: random.Random, found: list[str], depth: int = 0) -> object:
    kind = rng.random()
    if rng.random() < 0.08:
        return seeded_pair(rng, found)
    if depth >= 4 or kind < 0.45:
        if rng.random() < 0.15:
            return rng.choice([1, 2.5, -3, 1e10, True, False, None])
        return seeded_text(rng, found)
    if kind < 0.7:
        return [seeded_tree(rng, found, depth + 1) for _ in range(rng.randint(0, 4))]
    # Two names can redact to the same text: the walks number the later one.
    keys = [seeded_text(rng, found) or "k" for _ in range(rng.randint(0, 4))]
    return {key: seeded_tree(rng, found, depth + 1) for key in dict.fromkeys(keys)}


def shape(node: object) -> object:
    """The structure without the strings: key count and order, list lengths, other values."""
    if isinstance(node, dict):
        return [shape(v) for v in cast("dict[str, object]", node).values()]
    if isinstance(node, list):
        return ["list", *(shape(v) for v in cast("list[object]", node))]
    return "text" if isinstance(node, str) else node


@pytest.mark.design("security.redaction_shapes")
def test_redacted_json_parses_with_the_same_structure_and_no_seeded_secret() -> None:
    rng = random.Random(4646)
    cases = 0
    for _ in range(12_500):
        found: list[str] = []
        tree = seeded_tree(rng, found)
        if not isinstance(tree, (dict, list)):
            tree = [tree]
        for ascii_only in (True, False):
            for spaced in (True, False):
                text = json.dumps(
                    tree, ensure_ascii=ascii_only, separators=None if spaced else (",", ":")
                )
                out = redact(text)
                back = json.loads(out)
                assert shape(back) == shape(tree), text
                for secret in found:
                    assert secret not in out, (secret, text)
                cases += 1
    assert cases >= 20_000


@pytest.mark.design("security.redaction_shapes")
def test_json_with_a_field_name_at_the_end_of_a_string_keeps_its_strings_apart() -> None:
    for text in (
        '{"a": "password=", "b": "x"}',
        '{"password=": "x"}',
        '["token=", "hello"]',
        '{"a": "send token=", "n": 1}',
    ):
        assert redact(text) == text
    out = json.loads(redact('["token=abc", "hello", {"password=hunter2": 1, "token=zzz": 2}]'))
    assert out[1] == "hello" and len(out) == 3
    assert len(out[2]) == 2 and "hunter2" not in str(out) and "zzz" not in str(out)


@pytest.mark.design("security.redaction_shapes")
def test_a_text_that_parsed_as_json_still_parses_if_writing_it_back_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = json.dumps({"note": "token=abc", "n": 1})

    def broken(*args: object) -> str:
        return '{"note": '

    monkeypatch.setattr(redact_module, "encode", broken)
    assert json.loads(redact(text)) == "[secret removed]"

    def deep(*args: object) -> str:
        raise RecursionError

    monkeypatch.setattr(redact_module, "encode", deep)
    assert json.loads(redact(text)) == "[secret removed]"


@pytest.mark.design("security.redaction_shapes")
def test_the_same_pair_judgement_holds_for_python_values_and_json_text() -> None:
    """No seeded Basic pair survives, as JSON text or as Python values, and the shape of the
    tree is kept."""
    rng = random.Random(4747)
    cases = 0
    for _ in range(25_000):
        found: list[str] = []
        tree = seeded_tree(rng, found)
        cleaned, _removed = capture.clean(tree)
        assert shape(cleaned) == shape(tree), tree
        for secret in found:
            assert secret not in json.dumps(cleaned), (secret, tree)
        again, _count = redact_module.hide_header_credentials(tree)
        assert shape(again) == shape(tree)
        text = json.dumps(tree)
        out = redact(text)
        assert shape(json.loads(out)) == shape(tree), text
        for secret in found:
            assert secret not in out, (secret, text)
        cases += 2
    assert cases >= 50_000


@pytest.mark.design("security.redaction_shapes")
def test_objects_beside_a_basic_value_and_pascal_case_labels_on_both_paths() -> None:
    """A second seed, with a pair in every tree: an object in the same list as the Basic
    value keeps its numbers and has its own secrets cleaned, on JSON text and Python values."""
    rng = random.Random(5151)
    cases = 0
    for _ in range(30_000):
        found: list[str] = []
        tree = {"wrap": [seeded_pair(rng, found), seeded_tree(rng, found)]}
        cleaned, _removed = capture.clean(tree)
        assert shape(cleaned) == shape(tree), tree
        for secret in found:
            assert secret not in json.dumps(cleaned), (secret, tree)
        text = json.dumps(tree)
        out = redact(text)
        assert shape(json.loads(out)) == shape(tree), text
        for secret in found:
            assert secret not in out, (secret, text)
        cases += 2
    assert cases >= 60_000


# The shape, and the rules that report it: a key that names a secret is also caught by the
# key-name rule of capture, so those two are reported twice.
HEADER_SHAPES: list[tuple[object, list[str]]] = [
    ({"headers": [{"name": "Authorization", "value": "Basic dXNlcjpodW50ZXIy"}]}, ["pattern"]),
    ([["Authorization", "Basic dXNlcjpodW50ZXIy"]], ["pattern"]),
    ({"name": "authorization", "value": "Basic dXNlcjpodW50ZXIy"}, ["pattern"]),
    ({"field": "Proxy-Authorization", "values": ["Basic dXNlcjpodW50ZXIy"]}, ["pattern"]),
    ({"Authorization": ["Basic dXNlcjpodW50ZXIy"]}, ["pattern", "key_name"]),
    ({"rawHeaders": ["Host", "x", "Authorization", "Basic dXNlcjpodW50ZXIy"]}, ["pattern"]),
    ({"env": {"HTTP_AUTHORIZATION": "Basic dXNlcjpodW50ZXIy"}}, ["pattern", "key_name"]),
    ({"X-Forwarded-Authorization": "Basic dXNlcjpodW50ZXIy"}, ["pattern", "key_name"]),
    ([{"Key": "Authorization", "Value": ["Basic dXNlcjpodW50ZXIy"]}], ["pattern"]),
    ([{"Name": "Authorization", "Value": "Basic dXNlcjpodW50ZXIy"}], ["pattern"]),
    ({"Authorization:": "Basic dXNlcjpodW50ZXIy"}, ["pattern", "key_name"]),
    (["Authorization", ["Basic dXNlcjpodW50ZXIy"]], ["pattern"]),
    (
        {"Authorization": [{"note": "password=hunter2xyz", "n": 1}, "Basic dXNlcjpodW50ZXIy"]},
        ["pattern", "key_name", "key_name"],
    ),
]


@pytest.mark.design("security.redaction_shapes")
@pytest.mark.parametrize(
    ("value", "rules"), HEADER_SHAPES, ids=[f"header-{n}" for n in range(len(HEADER_SHAPES))]
)
def test_a_basic_header_is_removed_from_python_values_and_from_json_text(
    value: object, rules: list[str]
) -> None:
    secret = "dXNlcjpodW50ZXIy"
    cleaned, removed = capture.clean(value)
    assert secret not in json.dumps(cleaned)
    assert [r["rule"] for r in removed] == rules
    assert shape(cleaned) == shape(value)
    assert secret not in redact(json.dumps(value))


@pytest.mark.design("security.redaction_shapes")
def test_an_object_in_a_header_list_is_cleaned_and_keeps_its_numbers() -> None:
    """The object is a parsed JSON object, which is a list too. It must go through the walk."""
    text = '{"Authorization": [{"note": "password=hunter2xyz", "n": 1}, "Basic dXNlcjpodW50ZXIy"]}'
    assert redact(text) == (
        '{"Authorization": [{"note": "[secret removed]", "n": 1}, "[secret removed]"]}'
    )
    text = (
        '{"name": "Authorization", "values": '
        '[{"url": "https://u:pw@h/x?token=abc"}, "Basic dXNlcjpodW50ZXIy"]}'
    )
    assert redact(text) == (
        '{"name": "Authorization", "values": '
        '[{"url": "https://[removed]@h/x?[removed]"}, "[secret removed]"]}'
    )


@pytest.mark.design("security.redaction_shapes")
def test_headers_that_are_not_a_basic_credential_are_kept() -> None:
    for value in (
        {"Authorization": "Bearer"},
        {"Authorization": "Basic realm"},
        {"Host": "Basic dXNlcjpodW50ZXIy"},
        ["Accept", "Basic dXNlcjpodW50ZXIy"],
        {"name": "Host", "value": "Basic dXNlcjpodW50ZXIy"},
        {"author": "Basic dXNlcjpodW50ZXIy"},
    ):
        assert redact(json.dumps(value)) == json.dumps(value), value
        if "Authorization" not in value:  # that key is a secret name, so capture hides it whole
            cleaned, removed = capture.clean(value)
            assert cleaned == value and removed == [], value


@pytest.mark.design("security.redaction_shapes")
def test_a_json_key_named_password_is_left_to_the_recording_layer() -> None:
    """redact() judges patterns only. The side folder and the log previews use it alone, so
    a key-named value stays there; mocks, tool values and miss evidence also get the key rule."""
    text = '{"password": "hunter2", "token": "abc"}'
    assert redact(text) == text


@pytest.mark.design("security.redaction_shapes")
def test_an_escaped_letter_after_a_field_is_part_of_its_value() -> None:
    assert redact(r"log password=ab\u00e4cdef end") == "log [secret removed] end"
    assert redact("log password=ab\u00e4cdef end") == "log [secret removed] end"


@pytest.mark.design("security.redaction_shapes")
def test_json_is_read_only_after_a_pre_check_finds_something(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def never(*args: object, **kwargs: object) -> object:
        raise AssertionError("a clean text must not be parsed")

    monkeypatch.setattr(json, "loads", never)
    clean = json.dumps({"a": ["plain words", 1] * 40})
    assert redacted(clean) == (clean, 0)


@pytest.mark.design("security.redaction_shapes")
def test_a_marker_is_removed_with_what_sticks_to_it() -> None:
    assert redact("password=[secret removed]hunter2") == "[secret removed]"
    assert redact("token=[removed]hunter2") == "[secret removed]"
    assert redact("password=[secret removed]") == "password=[secret removed]"
    assert redact("password=[secret removed] x") == "password=[secret removed] x"


def test_spans_cover_exactly_what_redact_removes() -> None:
    # The corpus is plain text, as its reference needs; the JSON leak is added whole here.
    leaks = [
        ESCAPED_QUOTE_LEAK,
        DECODED_BACKSLASH_LEAK,
        json.dumps(ESCAPED_QUOTE_LEAK),
        f'"{ESCAPED_QUOTE_LEAK}"',
        *KEPT_BEFORE_SECRET,
        *(json.dumps({"log": text}) for text in KEPT_BEFORE_SECRET),
    ]
    for text in [*corpus(), *SAMPLES.values(), *leaks]:
        places = redact_module.replacements(text)
        built: list[str] = []
        at = 0
        for place in places:
            assert at <= place.start <= place.end <= len(text), text
            built += [text[at : place.start], place.text]
            at = place.end
        built.append(text[at:])
        assert "".join(built) == redact(text), text
        assert redact_module.spans(text) == [(p.start, p.end, p.rule) for p in places]


def test_a_span_names_its_rule() -> None:
    key = "ghp_" + "a" * 36
    text = f"token {key} and https://u:p@host/x?q=1 and Bearer {'b' * 20}"
    found = redact_module.spans(text)
    assert [rule for _, _, rule in found] == ["github-token", "url", "bearer-token"]
    start, end, _ = found[0]
    assert text[start:end] == key
    assert redact_module.spans("nothing here") == []


def test_a_secret_inside_a_url_query_is_one_span_of_the_url_rule() -> None:
    text = "see https://h/x?key=sk-" + "a" * 20 + " ok"
    assert [(r.rule, text[r.start : r.end]) for r in redact_module.replacements(text)] == [
        ("url", "://h/x?key=sk-aaaaaaaaaaaaaaaaaaaa")
    ]


def test_a_json_text_that_redaction_changes_is_one_span_and_kept_names_are_none() -> None:
    """A JSON text is written back whole, so its change is one place; a name that is found
    and kept, such as next_page_token, is no place at all."""
    text = '{"a": "token=abc", "n": 1, "b": "plain"}'
    # Only the string that the rules change is a place; "plain" and the number are not.
    assert redact_module.spans(text) == [(6, 17, "json")]
    assert redact_module.replacements(text)[0].text == '"[secret removed]"'
    assert redact_module.spans('{"a": "plain", "n": 1}') == []
    for kept in ("next_page_token=abc", "f(token=tok)", "def f(password=None):"):
        assert redact_module.spans(kept) == []
    assert [rule for _, _, rule in redact_module.spans("x DB_PASSWORD=hunter2")] == ["form-field"]
    header = "Authorization: Basic dXNlcjpwYXNzd29yZA=="
    assert [rule for _, _, rule in redact_module.spans(header)] == ["basic-credential"]


def test_two_names_that_redact_alike_are_numbered_on_both_paths() -> None:
    """The second must not overwrite the first, in a mock or in redacted JSON text."""
    names = {"token=abc": 1, "token=xyz": 2, "plain": 3}
    cleaned, _removed = capture.clean(names)
    assert cleaned == {"[secret removed]": 1, "[secret removed] 2": 2, "plain": 3}
    assert json.loads(redact(json.dumps(names))) == cleaned
    taken = {"[secret removed]", "[secret removed] 2"}
    assert redact_module.numbered("[secret removed]", "token=q", taken) == "[secret removed] 3"


def test_a_backslash_n_or_r_ends_a_value_and_other_backslashes_are_in_it() -> None:
    assert redact(r"password=ab\ncd") == r"[secret removed]\ncd"
    assert redact(r"password=ab\rcd") == r"[secret removed]\rcd"
    assert redact(r"password=ab\tcd ok") == "[secret removed] ok"
    assert redact(r"x password=ab\"cd") == "x [secret removed]"
    assert redact("password=ab\\$cd\\") == "[secret removed]"


# A value kept on purpose, an escape or a quote, then a secret field: the kept value must
# not take the secret in with it.
KEPT_BEFORE_SECRET = [
    r"next_page_token=abc\"password=hunter2",
    r"is_secret=True\"api_key=sk_live_zz",
    "next_page_token=Q2F\\npassword=hunter2",
    'next_page_token=abc"password=hunter2',
    "f(token=tok)\\npassword=hunter2",
]


@pytest.mark.design("security.redaction_shapes")
@pytest.mark.parametrize("text", KEPT_BEFORE_SECRET)
def test_a_kept_value_never_runs_on_into_a_secret_field_after_it(text: str) -> None:
    secret = text.rpartition("=")[2]
    kept = text.partition("=")[0]
    outs = {
        "plain": redact(text),
        "decoded": redacted(text, decoded=True)[0],
        "json": redact(json.dumps({"log": text})),
        "python value": json.dumps(capture.clean({"v": text})[0]),
        "preview": str(state.content(text)["preview"]),
        "miss": str(capture.shown_args({"v": text}, ["v"])),
    }
    for where, out in outs.items():
        assert secret not in out, (where, out)
        assert kept in out, (where, out)  # the kept name itself stays


ESCAPED_QUOTE_LEAK = r"password=abc\"2zz"  # a quote escaped inside a value
DECODED_BACKSLASH_LEAK = '{"q": "password=C:\\\\new2zz"}'  # the string is password=C:\new2zz


@pytest.mark.design("security.redaction_shapes")
@pytest.mark.parametrize("quote", ['"', "'"])
def test_an_escaped_quote_with_more_of_the_value_after_it_is_in_the_value(quote: str) -> None:
    text = ESCAPED_QUOTE_LEAK.replace('"', quote)
    assert redact(text) == "[secret removed]"
    assert redact(f"x {text} y") == "x [secret removed] y"
    # One that closes an escaped string still ends the value, so the text around it is kept.
    assert redact(r"{\"a\":\"password=abc\",\"b\":1}") == r"{\"a\":\"[secret removed]\",\"b\":1}"


@pytest.mark.design("security.redaction_shapes")
def test_a_backslash_n_in_a_string_read_out_of_json_is_in_the_value() -> None:
    """In JSON text \\n is a line break; once the string is read out it is a backslash and a
    letter, as in a Windows path."""
    assert json.loads(DECODED_BACKSLASH_LEAK)["q"] == "password=C:\\new2zz"
    assert json.loads(redact(DECODED_BACKSLASH_LEAK)) == {"q": "[secret removed]"}
    assert "2zz" not in redact(DECODED_BACKSLASH_LEAK)
    # A real line break read out of JSON still ends the value.
    broken = json.dumps({"q": "password=abc\nuser=bob"})
    assert json.loads(redact(broken)) == {"q": "[secret removed]\nuser=bob"}


@pytest.mark.design("security.redaction_shapes")
def test_a_quote_in_a_string_read_out_of_json_is_in_the_value() -> None:
    """A text that is one JSON string is read out first, and its \\" becomes a raw quote."""
    assert redact(f'"{ESCAPED_QUOTE_LEAK}"') == '"[secret removed]"'
    assert json.loads(redact(json.dumps({"q": 'password=abc"2zz'}))) == {"q": "[secret removed]"}
    # A name inside a quote is closed by that quote, and what follows is kept.
    assert json.loads(redact(json.dumps('say "token=abc" now'))) == 'say "[secret removed]" now'


def security_rule_names() -> set[str]:
    page = (ROOT / "docs" / "security.md").read_text()
    line = next(
        row for row in page.splitlines() if row.strip().startswith("Rule names in messages:")
    )
    return set(re.findall(r"`([a-z-]+)`", line))


def test_the_rule_names_on_the_security_page_are_the_ones_in_messages() -> None:
    """A refusal says which rule removes the text; the page lists every name it can use."""
    names = {secret.name for secret in redact_module.SECRETS} | {"url", "jwt", "json", "secret"}
    assert "" not in names
    assert security_rule_names() == names
