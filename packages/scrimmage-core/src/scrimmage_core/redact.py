"""Remove known secret patterns from anything that is recorded."""

import re
from typing import NamedTuple

REMOVED = "[secret removed]"
URL_PART_REMOVED = "[removed]"


class Secret(NamedTuple):
    """One kind of key. Every match holds one of the literals, so a text with none of them
    needs no regex pass. lowercase: the literal is looked for in the text in lower case."""

    pattern: str
    literals: tuple[str, ...]
    lowercase: bool = False


# A form-encoded or keyword field: password, token, secret or api_key, with = or %3D. The
# name starts at a boundary, so mytoken, is_secret, next_page_token and top_secret are kept;
# only client_, refresh_, id_, csrf_, auth_ and access_ may come before it with a _. After a
# JSON \n the name also starts. The value is a quoted string up to its closing quote, a <...>
# or a run of characters that is not a space, &, quote, bracket or backslash, so a field cut
# from JSON text leaves the JSON valid. A value already removed is left, and so is a bare
# name followed by , or ), which is code: f(password=None).
FORM = (
    r"(?:(?<![A-Za-z0-9_])|(?<=\\[nrt]))"
    r"(?ai:(?:(?:client|refresh|id|csrf|auth|access)_)?"
    r"(?:passw(?:or)?d|token|secret|api(?:_|%5f)key)(?:=|%3d))"
    r"(?!\[(?:secret )?removed\](?:[&\s\"'<>\\]|$))"
    r"(?:(?P<quote>\\?[\"'])[^\"'\\]+(?P=quote)"
    r"|<[^<>\"'\\]+>"
    r"|(?![A-Za-z_.]+[),])(?:\\?[\"'])?[^&\s\"'<>\\]+)"
)


SECRETS = (
    # OpenAI and Anthropic keys
    Secret(r"sk-(?:proj-|ant-|svcacct-)?[A-Za-z0-9_-]{16,}", ("sk-",)),
    # AWS access key ids, long-term and temporary
    Secret(r"(?:AKIA|ASIA)[0-9A-Z]{16}", ("AKIA", "ASIA")),
    # Google API keys
    Secret(r"AIza[0-9A-Za-z_-]{35}", ("AIza",)),
    # GitHub tokens
    Secret(r"gh[pousr]_[A-Za-z0-9]{36,}", ("ghp_", "gho_", "ghu_", "ghs_", "ghr_")),
    # GitHub fine-grained tokens
    Secret(r"github_pat_[A-Za-z0-9_]{22,}", ("github_pat_",)),
    # Slack tokens, refresh tokens too
    Secret(r"(?:xoxe\.)?xox[abeprs]-[A-Za-z0-9-]{10,}", ("xox",)),
    # Slack app-level tokens
    Secret(r"xapp-[A-Za-z0-9-]{10,}", ("xapp-",)),
    # bearer tokens
    Secret(r"(?i:bearer)\s+[A-Za-z0-9._~+/=-]{16,}", ("bearer",), lowercase=True),
    # a Basic credential in an Authorization header, as a header line, JSON, a command line
    # or a list of byte pairs. ASCII only, so the lowered text holds the literal.
    Secret(
        r"(?ai:authorization)(?:\W|b(?=[\"'])){1,8}(?ai:basic)\s+[A-Za-z0-9+/_-]{8,}={0,2}",
        ("authorization",),
        lowercase=True,
    ),
    Secret(FORM, ("password", "passwd", "token", "secret", "api_key", "api%5fkey"), True),
    # h11 quotes the bad header
    Secret(r"Illegal header (?:name|value) .*", ("Illegal header",)),
)
PATTERNS = re.compile("|".join(secret.pattern for secret in SECRETS))
LITERALS = tuple(lit for secret in SECRETS if not secret.lowercase for lit in secret.literals)
LOWER_LITERALS = tuple(lit for secret in SECRETS if secret.lowercase for lit in secret.literals)


# A JWT. Each run of token characters is tried once, from its first eyJ, with no backing up,
# so a large base64 body stays linear. What comes before it, such as the n of an escaped
# newline, is kept.
TOKEN = "[A-Za-z0-9_-]"
JWT = re.compile(
    rf"(?<!{TOKEN})((?>{TOKEN}*?eyJ)){TOKEN}{{8,}}+\.{TOKEN}{{8,}}+\.{TOKEN}{{8,}}+(?:\.{TOKEN}+)*+"
)

# A query or a password may hold a raw ' or \, so only these end a URL. A quote or a bracket
# that closes the URL is given back after the cut.
URL = re.compile(r"(?<=[A-Za-z0-9+.-])://[^\s\"<>]+")
CLOSING = ")'\\,.;:"
LOGIN = re.compile(r"(?<=://)[^/?#\s]*@")


def plain_url(found: re.Match[str]) -> str:
    """A URL with its query, fragment and credentials marked as removed, not silently gone."""
    text = found.group()
    url = text.rstrip(CLOSING)
    kept, *cut = re.split(r"([?#])", url, maxsplit=1)
    query = f"{cut[0]}{URL_PART_REMOVED}" if cut else ""
    return f"{LOGIN.sub(f'{URL_PART_REMOVED}@', kept)}{query}{text[len(url) :]}"


def no_jwt(found: re.Match[str]) -> str:
    return found.group(1).removesuffix("eyJ") + REMOVED


def redacted(text: str) -> tuple[str, int]:
    """The text with secrets removed, and how many places were removed."""
    count = 0

    def url(found: re.Match[str]) -> str:
        nonlocal count
        kept = plain_url(found)
        count += kept != found.group()
        return kept

    # Each pass runs only if the text, as the pass before left it, holds a string every
    # match of that pass contains. A text that has none is returned as it is.
    jwts = keys = 0
    if "://" in text:
        text = URL.sub(url, text)
    if "eyJ" in text:
        text, jwts = JWT.subn(no_jwt, text)
    lowered = text.lower()
    if any(lit in text for lit in LITERALS) or any(lit in lowered for lit in LOWER_LITERALS):
        text, keys = PATTERNS.subn(REMOVED, text)
    # A plain str, even if given a subclass, whose methods could carry the secret back.
    return str.__str__(text), count + jwts + keys


def redact(text: str) -> str:
    return redacted(text)[0]
