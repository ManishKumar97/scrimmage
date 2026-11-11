"""Remove known secret patterns from anything that is recorded."""

import re
from collections.abc import Callable
from typing import NamedTuple

REMOVED = "[secret removed]"
URL_PART_REMOVED = "[removed]"


class Secret(NamedTuple):
    """One kind of key. Every match holds one of the literals, so a text with none of them
    needs no regex pass. lowercase: the literal is looked for in the text in lower case.
    name is what a message calls the rule."""

    pattern: str
    literals: tuple[str, ...]
    lowercase: bool = False
    name: str = ""


SECRETS = (
    # OpenAI and Anthropic keys
    Secret(r"sk-(?:proj-|ant-|svcacct-)?[A-Za-z0-9_-]{16,}", ("sk-",), name="openai-key"),
    # AWS access key ids, long-term and temporary
    Secret(r"(?:AKIA|ASIA)[0-9A-Z]{16}", ("AKIA", "ASIA"), name="aws-access-key"),
    # Google API keys
    Secret(r"AIza[0-9A-Za-z_-]{35}", ("AIza",), name="google-api-key"),
    # GitHub tokens
    Secret(
        r"gh[pousr]_[A-Za-z0-9]{36,}", ("ghp_", "gho_", "ghu_", "ghs_", "ghr_"), name="github-token"
    ),
    # GitHub fine-grained tokens
    Secret(r"github_pat_[A-Za-z0-9_]{22,}", ("github_pat_",), name="github-token"),
    # Slack tokens, refresh tokens too
    Secret(r"(?:xoxe\.)?xox[abeprs]-[A-Za-z0-9-]{10,}", ("xox",), name="slack-token"),
    # Slack app-level tokens
    Secret(r"xapp-[A-Za-z0-9-]{10,}", ("xapp-",), name="slack-token"),
    # bearer tokens
    Secret(
        r"(?i:bearer)\s+[A-Za-z0-9._~+/=-]{16,}", ("bearer",), lowercase=True, name="bearer-token"
    ),
    # h11 quotes the bad header
    Secret(r"Illegal header (?:name|value) .*", ("Illegal header",), name="header-text"),
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


class Replacement(NamedTuple):
    """One place the rules change a text: where it was, which rule, and what the record has."""

    start: int
    end: int
    rule: str
    text: str


class Piece(NamedTuple):
    """A part of the text as the rules have left it so far. An unmodified part is the
    original from start to end; a replaced one has the rule that did it."""

    text: str
    start: int
    end: int
    rule: str | None


def cut(pieces: list[Piece], at: int) -> list[Piece]:
    """The pieces with a boundary at this position, which is in the text as it is now. An
    unmodified piece is split there; a replaced piece cannot be, and stays whole."""
    pos = 0
    for i, piece in enumerate(pieces):
        end = pos + len(piece.text)
        if pos < at < end and piece.rule is None:
            k = at - pos
            low = Piece(piece.text[:k], piece.start, piece.start + k, None)
            high = Piece(piece.text[k:], piece.start + k, piece.end, None)
            return [*pieces[:i], low, high, *pieces[i + 1 :]]
        pos = end
    return pieces


def replace_place(pieces: list[Piece], a: int, b: int, text: str, rule: str) -> list[Piece]:
    """Replace [a, b) of the text as it is now. A replaced piece the match only touches is
    taken whole: what it has outside the match stays beside the new text, and the place is
    the whole of what it came from."""
    pieces = cut(cut(pieces, a), b)
    starts: list[int] = []
    pos = 0
    for piece in pieces:
        starts.append(pos)
        pos += len(piece.text)
    inside = [i for i, p in enumerate(pieces) if starts[i] < b and starts[i] + len(p.text) > a]
    first, last = inside[0], inside[-1]
    head = pieces[first].text[: max(a - starts[first], 0)]
    tail = pieces[last].text[max(b - starts[last], 0) :]
    named = next((pieces[i].rule for i in inside if pieces[i].rule is not None), None)
    place = Piece(head + text + tail, pieces[first].start, pieces[last].end, named or rule)
    return [*pieces[:first], place, *pieces[last + 1 :]]


def secret_rule(found: str) -> str:
    """Which key a matched text is. The combined pattern takes the first that matches at a
    place, so the first whose own pattern matches the whole text is the one."""
    for secret in SECRETS:
        if re.fullmatch(secret.pattern, found):
            return secret.name
    return "secret"


def replacements(text: str) -> list[Replacement]:
    """Every place redact changes the text, in the text as given, each with its rule's name.

    redact runs its rules one after another, each on what the one before left, so a later
    match can sit on an earlier replacement. It is then one place here, and its text is
    what the rules left there.
    """
    text = str.__str__(text)
    pieces = [Piece(text, 0, len(text), None)]
    passes: list[tuple[re.Pattern[str], Callable[[re.Match[str]], tuple[str, str]]]] = [
        (URL, lambda m: (plain_url(m), "url")),
        (JWT, lambda m: (no_jwt(m), "jwt")),
        (PATTERNS, lambda m: (REMOVED, secret_rule(m.group()))),
    ]
    for pattern, make in passes:
        now = "".join(piece.text for piece in pieces)
        found = [(m.start(), m.end(), *make(m)) for m in pattern.finditer(now)]
        # Last first, so a place already made does not move the ones before it.
        for a, b, new, rule in reversed(found):
            if new != now[a:b]:
                pieces = replace_place(pieces, a, b, new, rule)
    return [Replacement(p.start, p.end, p.rule, p.text) for p in pieces if p.rule is not None]


def spans(text: str) -> list[tuple[int, int, str]]:
    """Each place redact would change the text, as (start, end, rule name)."""
    return [(r.start, r.end, r.rule) for r in replacements(text)]
