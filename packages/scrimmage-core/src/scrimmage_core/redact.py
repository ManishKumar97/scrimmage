"""Remove known secret patterns from anything that is recorded."""

import json
import re
from collections.abc import Iterator
from typing import NamedTuple, cast

REMOVED = "[secret removed]"
URL_PART_REMOVED = "[removed]"


class Secret(NamedTuple):
    """One kind of key. Every match holds one of the literals, so a text with none of them
    needs no regex pass. lowercase: the literal is looked for in the text in lower case."""

    pattern: str
    literals: tuple[str, ...]
    lowercase: bool = False


# A form-encoded or keyword field: password, token, secret, secret_access_key or api_key,
# with = or %3D. Any NAME_ prefix may come before the name, so DB_PASSWORD, GITHUB_TOKEN and
# client_secret match: a leak reaches a repository, and a false positive only makes a call
# unwritable. A name directly after a letter or digit, as in mytoken, is not a name, except
# the fixed list of glued env names: PGPASSWORD, PGPASS, MYSQL_PWD, authtoken, accesstoken.
# These prefixes are harmless and are kept: the paging names (next_page_token), is_, has_,
# top_, max_, min_, num_ and n_, and the tokenizer names (eos_token, pad_token). After a JSON
# \n the name also starts, and a backslash before it does not.
# The value is a quoted string up to its closing quote, a <...>, or a run of characters that
# is not a space, &, quote, bracket or backslash. A raw quote opens a value only when the name
# is not itself inside that quote, so the quote that closes a string is not taken for one.
# A marker left by an earlier removal is kept alone, and removed with what follows it.
# None, True, False, and a bare name in a call, such as f(token=tok), are code and are kept.
FIELD = (
    r"(?:passw(?:or)?d|token|secret(?:_access_key)?|api(?:_|%5f)key"
    r"|pgpass(?:word)?|mysql_pwd|authtoken|accesstoken)(?:=|%3d)"
)
HARMLESS = (
    "next|page|continuation|pagination|sync|cursor|is|has|top|max|min|num|n|eos|bos|pad|unk|sep|cls"
)
MARKER = r"\[(?:secret )?removed\]"
NAME = (
    r"(?:(?<![A-Za-z0-9_\\])|(?<=\\[nrt]))"
    rf"(?ai:(?:[A-Za-z0-9]+_)*{FIELD})"
)
# A quoted value may not hold another field name, so an unclosed quote that runs on to the
# next quote does not swallow the name of a later secret.
QUOTED = (
    r"""(?P<quote>["'])"""
    rf"(?!(?:(?!(?P=quote))[^\n])*?(?ai:{FIELD}))"
    r"""(?:\\.|(?!(?P=quote))[^\\\n])+(?P=quote)"""
)
# `inside` is set when the name follows a quote, so a quote after it closes that string.
FORM = (
    r"""(?:(?P<inside>(?<=["']))|(?<!["']))"""
    + NAME
    + rf"(?:{MARKER}[^&\s\"'<>\\]+"
    + rf"|(?(inside)(?!)|{QUOTED})"
    + rf"|(?!{MARKER})(?:<[^<>\"'\\]+>|(?P<value>(?(inside)|(?:\\?[\"']|<)?)[^&\s\"'<>\\]+)))"
)
# The harmless prefixes, checked on the match: next_page_token is found and then kept.
HARMLESS_NAME = re.compile(rf"(?:[A-Za-z0-9]+_)*(?:{HARMLESS})_{FIELD}", re.ASCII | re.IGNORECASE)
CODE_VALUES = ("None", "True", "False")
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")


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
    Secret(
        FORM,
        ("password", "passwd", "token", "secret", "api_key", "api%5fkey", "pgpass", "mysql_pwd"),
        True,
    ),
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


def code_value(found: re.Match[str]) -> bool:
    """A value that is code: None, True, False, or a bare name as an argument of a call."""
    value = found.group("value")
    if value is None:
        return False
    head = IDENTIFIER.match(value)
    if head is None:
        return False
    name, rest = head.group(), value[head.end() :]
    if name in CODE_VALUES and not rest[:1].isalnum():
        return True
    text, start = found.string, found.start()
    argument = text[start - 1 : start] == "(" or text[start - 2 : start] == ", "
    open_call = text.rfind("(", 0, start) > text.rfind(")", 0, start)
    return argument and open_call and rest[:1] in ("", ")", ",") and rest == value[len(name) :]


def remove_keys(text: str) -> tuple[str, int]:
    """The text with every key shape replaced, and how many were."""
    count = 0

    def replace(found: re.Match[str]) -> str:
        nonlocal count
        if HARMLESS_NAME.match(found.group()) or code_value(found):
            return found.group()
        count += 1
        return REMOVED

    return PATTERNS.sub(replace, text), count


def holds_candidate(text: str) -> bool:
    """Whether any pass could match: the text holds a string every match of that pass holds."""
    if "://" in text or "eyJ" in text or any(lit in text for lit in LITERALS):
        return True
    lowered = text.lower()
    return any(lit in lowered for lit in LOWER_LITERALS)


def plain(text: str) -> tuple[str, int]:
    """The three passes on text that is not read as JSON. Each pass runs only if the text, as
    the pass before left it, holds a string every match of that pass contains."""
    count = 0

    def url(found: re.Match[str]) -> str:
        nonlocal count
        kept = plain_url(found)
        count += kept != found.group()
        return kept

    jwts = keys = 0
    if "://" in text:
        text = URL.sub(url, text)
    if "eyJ" in text:
        text, jwts = JWT.subn(no_jwt, text)
    lowered = text.lower()
    if any(lit in text for lit in LITERALS) or any(lit in lowered for lit in LOWER_LITERALS):
        text, keys = remove_keys(text)
    return text, count + jwts + keys


class Number(str):
    """A JSON number or constant, kept exactly as it was written."""


class Members(list[tuple[str, object]]):
    """The members of a JSON object in order, repeated names included."""


def encode(node: object, item: str, colon: str, ascii_only: bool) -> str:
    if isinstance(node, Number):
        return str(node)
    if isinstance(node, str):
        return json.dumps(str.__str__(node), ensure_ascii=ascii_only)
    if isinstance(node, Members):
        members = (
            json.dumps(name, ensure_ascii=ascii_only)
            + colon
            + encode(value, item, colon, ascii_only)
            for name, value in node
        )
        return "{" + item.join(members) + "}"
    if isinstance(node, list):
        return (
            "["
            + item.join(encode(v, item, colon, ascii_only) for v in cast(list[object], node))
            + "]"
        )
    return json.dumps(node)


def structural(text: str) -> tuple[str, int] | None:
    """A JSON object or array, redacted string by string, so the result still parses and has
    the same keys and lengths. None if the text is not one. Only a change is written back,
    in the input's spacing where it can be told, and no text is ever returned that does not
    parse: if the writing fails, the whole text is replaced."""
    body = text.strip()
    if body[:1] not in ("{", "[", '"'):
        return None
    try:
        tree = json.loads(
            body,
            parse_int=Number,
            parse_float=Number,
            parse_constant=Number,
            object_pairs_hook=Members,
        )
    except (ValueError, RecursionError):
        return None
    count = 0

    def clean(value: str) -> str:
        nonlocal count
        kept, more = redacted(value)
        count += more
        return kept

    def rename(node: Members) -> Iterator[tuple[str, object]]:
        # Two names that redact to the same text would become one key, so a later one is
        # numbered. The count of keys is kept.
        taken = {name for name, _ in node}
        for name, value in node:
            kept = clean(name)
            if kept != name:
                base, number = kept, 2
                while kept in taken:
                    kept, number = f"{base} {number}", number + 1
                taken.add(kept)
            yield kept, walk(value)

    def walk(node: object) -> object:
        if isinstance(node, Number):
            return node
        if isinstance(node, str):
            return clean(node)
        if isinstance(node, Members):
            return Members(rename(node))
        if isinstance(node, list):
            return [walk(value) for value in cast(list[object], node)]
        return node

    try:
        cleaned = walk(tree)
        if count == 0:
            return text, 0
        spaced = ": " in body or ", " in body
        written = encode(cleaned, ", " if spaced else ",", ": " if spaced else ":", "\\u" in body)
        json.loads(written)
    except (ValueError, RecursionError):
        return json.dumps(REMOVED), max(count, 1)
    lead = text[: len(text) - len(text.lstrip())]
    return lead + written + text[len(text.rstrip()) :], count


def redacted(text: str) -> tuple[str, int]:
    """The text with secrets removed, and how many places were removed. A JSON object,
    array or string is read and its strings are redacted, so it stays valid JSON."""
    done: tuple[str, int] = (text, 0)
    if holds_candidate(text):
        found = structural(text)
        done = found if found is not None else plain(text)
    # A plain str, even if given a subclass, whose methods could carry the secret back.
    return str.__str__(done[0]), done[1]


def redact(text: str) -> str:
    return redacted(text)[0]
