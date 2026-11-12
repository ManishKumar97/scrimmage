"""Remove known secret patterns from anything that is recorded."""

import json
import re
from collections.abc import Callable, Iterator
from typing import NamedTuple, cast

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
NAMES = (
    r"(?:passw(?:or)?d|token|secret(?:_access_key)?|api(?:_|%5f)key"
    r"|pgpass(?:word)?|mysql_pwd|authtoken|accesstoken)"
)
FIELD = NAMES + r"(?:=|%3d)"
# Before any field name: is_secret, top_password, max_token. Before token only: the paging
# names (next_page_token) and the tokenizer names (eos_token). SYNC_PASSWORD is a password.
HARMLESS = "is|has|top|max|min|num|n"
HARMLESS_TOKEN = "next|page|continuation|pagination|sync|cursor|eos|bos|pad|unk|sep|cls"
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
# What may follow an escape that stays in a value: more of the value. An escaped quote before
# a space, a bracket, a comma, a colon or the end closes an escaped string instead, as the
# second \" in {\"a\":\"b\"} does.
# Nor before a field name: there a new field starts, as it would after a value that ended, so a
# value kept on purpose, such as next_page_token=x, never runs on into a secret after it.
GOES_ON = r"""(?=[^&\s"'<>\\,;:()\[\]{}])""" + rf"(?!(?ai:(?:[A-Za-z0-9]+_)*{FIELD}))"


def form(decoded: bool) -> str:
    """The form-field rule. A backslash is in the value, except before a quote, n or r.

    An escaped quote with more of the value after it is in the value, as in abc\\"2zz. In raw
    text a \\n or \\r is a JSON escape, a line break, and ends the value. In a string read out
    of JSON every escape is already undone, so there a \\n with more of the value after it is
    a backslash and a letter, as in C:\\new, and is in the value too. A raw quote there is a
    character as well, as \\" became one when the string was read, unless the name sits inside
    a quote, which it then closes."""
    escaped = r"(?:\\[\"'nr]|(?(inside)(?!)|[\"']))" if decoded else r"\\[\"']"
    return (
        # `inside` is set when the name follows a quote, so a quote after it closes that string.
        r"""(?:(?P<inside>(?<=["']))|(?<!["']))"""
        + NAME
        + rf"(?:{MARKER}[^&\s\"'<>\\]+"
        + rf"|(?(inside)(?!)|{QUOTED})"
        + rf"|(?!{MARKER})(?:<[^<>\"'\\]+>|(?P<value>(?(inside)|(?:\\?[\"']|<)?)"
        + rf"(?:\\u[0-9a-fA-F]{{4}}|{escaped}{GOES_ON}|\\(?![\"'nr])|[^&\s\"'<>\\])+))"
        + ")"
    )


FORM = form(decoded=False)
# The harmless prefixes, checked on the match: next_page_token is found and then kept.
HARMLESS_NAME = re.compile(
    rf"(?:[A-Za-z0-9]+_)*(?:(?:{HARMLESS})_{FIELD}|(?:{HARMLESS_TOKEN})_token(?:=|%3d))",
    re.ASCII | re.IGNORECASE,
)
CODE_VALUES = ("None", "True", "False")
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")
# A Basic credential in an Authorization header, as a header line, JSON, a command line or a
# list of byte pairs. ASCII only, so the lowered text holds the literal.
BASIC = r"(?ai:authorization)(?:\W|b(?=[\"'])){1,8}(?ai:basic)\s+[A-Za-z0-9+/_-]{8,}={0,2}"
BASIC_RE = re.compile(BASIC)
# In a JSON object or list the name and the value are separate strings, so the rule that
# needs both is tried on the pair: an Authorization header with a Basic value. The name may
# end in authorization after _ or -, as in HTTP_AUTHORIZATION and X-Forwarded-Authorization.
AUTH_NAME = re.compile(r"(?:.*[_-])?authorization", re.ASCII | re.IGNORECASE)
LABELS = ("name", "key", "header", "field")


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
    Secret(BASIC, ("authorization",), lowercase=True, name="basic-credential"),
    Secret(
        FORM,
        ("password", "passwd", "token", "secret", "api_key", "api%5fkey", "pgpass", "mysql_pwd"),
        True,
        "form-field",
    ),
    # h11 quotes the bad header
    Secret(r"Illegal header (?:name|value) .*", ("Illegal header",), name="header-text"),
)
PATTERNS = re.compile("|".join(secret.pattern for secret in SECRETS))
# The same rules for a string read out of JSON: only the form-field rule differs.
DECODED_PATTERNS = re.compile(
    "|".join(form(decoded=True) if s.pattern == FORM else s.pattern for s in SECRETS)
)
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
    """A value that is code: None, True or False alone, or a bare name as an argument of a
    call whose name is right before its parenthesis, as in f(token=tok)."""
    value = found.group("value")
    if value is None:
        return False
    head = IDENTIFIER.match(value)
    if head is None:
        return False
    name, rest = head.group(), value[head.end() :]
    if rest[:1] not in ("", ")", ","):
        return False
    if name in CODE_VALUES:
        return True
    text, start = found.string, found.start()
    if not (text[start - 1 : start] == "(" or text[start - 2 : start] == ", "):
        return False
    opening = text.rfind("(", 0, start)
    named_call = opening > text.rfind(")", 0, start) and (
        text[opening - 1 : opening].isalnum() or text[opening - 1 : opening] == "_"
    )
    return named_call and "." not in name


def kept_as_is(found: re.Match[str]) -> bool:
    """A match the rules find and leave: a harmless name such as next_page_token, or code."""
    return HARMLESS_NAME.match(found.group()) is not None or code_value(found)


def remove_keys(text: str, decoded: bool = False) -> tuple[str, int]:
    """The text with every key shape replaced, and how many were. decoded: the text is a
    string read out of JSON."""
    count = 0

    def replace(found: re.Match[str]) -> str:
        nonlocal count
        if kept_as_is(found):
            return found.group()
        count += 1
        return REMOVED

    return (DECODED_PATTERNS if decoded else PATTERNS).sub(replace, text), count


ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")


def holds_candidate(text: str) -> bool:
    """Whether any pass could match: the text holds a string every match of that pass holds."""
    if "\\u" in text:  # a JSON escape can hide a letter: p\u0061ssword
        text = ESCAPE.sub(lambda found: chr(int(found.group(1), 16)), text)
    if "://" in text or "eyJ" in text or any(lit in text for lit in LITERALS):
        return True
    lowered = text.lower()
    return any(lit in lowered for lit in LOWER_LITERALS)


def plain(text: str, decoded: bool = False) -> tuple[str, int]:
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
        text, keys = remove_keys(text, decoded)
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


def basic_value(value: object) -> bool:
    return (
        isinstance(value, str)
        and not isinstance(value, Number)
        and BASIC_RE.search("authorization: " + value) is not None
    )


def hide_basic(value: object) -> tuple[object, int]:
    """A Basic value, or each Basic string in a list of strings, replaced. A parsed JSON
    object is a Members, which is a list too, and is left to the walk."""
    if isinstance(value, list) and not isinstance(value, Members):
        items = [hide_basic(item) for item in cast(list[object], value)]
        return [item for item, _ in items], sum(more for _, more in items)
    if isinstance(value, str) and basic_value(value):
        return REMOVED, 1
    return value, 0


def authorization(name: object) -> bool:
    """Whether a name is an Authorization header's: a trailing colon and spaces are ignored."""
    return isinstance(name, str) and AUTH_NAME.fullmatch(name.strip().rstrip(":")) is not None


def hide_header_credentials(node: object) -> tuple[object, int]:
    """The node with Basic credentials removed from the header-like pairs directly in it,
    and how many. One judgement for JSON text and for Python values: the callers walk the
    tree and call it on each dict and list. It reads a name beside a value (a header map,
    whose value may be a list), {"name": ..., "value": ...} and {"field": ..., "values":
    [...]}, and a name followed by a value in any list, as in Node's rawHeaders. A node with
    nothing to remove is returned as it is."""
    if isinstance(node, list) and not isinstance(node, Members):
        items: list[object] = list(cast(list[object], node))
        found = 0
        for at in range(len(items) - 1):
            name = items[at]
            if authorization(name):
                items[at + 1], more = hide_basic(items[at + 1])
                found += more
        return (items, found) if found else (cast(object, node), 0)
    if isinstance(node, Members):
        pairs = list(node)
    elif isinstance(node, dict):
        pairs = list(cast(dict[str, object], node).items())
    else:
        return node, 0
    labelled = any(name.lower() in LABELS and authorization(label) for name, label in pairs)
    out: list[tuple[str, object]] = []
    found = 0
    for name, value in pairs:
        named = authorization(name) or (labelled and name.lower() in ("value", "values"))
        value, more = hide_basic(value) if named else (value, 0)
        found += more
        out.append((name, value))
    if not found:
        return cast(object, node), 0
    return cast(object, Members(out) if isinstance(node, Members) else dict(out)), found


def numbered(kept: str, name: str, taken: set[str]) -> str:
    """The name a key has after redaction. Two names that redact to the same text would
    become one key, so a later one is numbered ("[secret removed] 2") and the count of keys is
    kept. taken holds every name in the object and is added to."""
    if kept != name:
        base, number = kept, 2
        while kept in taken:
            kept, number = f"{base} {number}", number + 1
        taken.add(kept)
    return kept


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
        kept, more = redacted(value, decoded=True)
        count += more
        return kept

    def rename(node: Members) -> Iterator[tuple[str, object]]:
        taken = {name for name, _ in node}
        for name, value in node:
            yield numbered(clean(name), name, taken), walk(value)

    def walk(node: object) -> object:
        nonlocal count
        if isinstance(node, Number):
            return node
        if isinstance(node, str):
            return clean(node)
        if isinstance(node, Members):
            node, hidden = hide_header_credentials(node)
            count += hidden
            return Members(rename(cast(Members, node)))
        if isinstance(node, list):
            node, hidden = hide_header_credentials(cast(object, node))
            count += hidden
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


def redacted(text: str, *, decoded: bool = False) -> tuple[str, int]:
    """The text with secrets removed, and how many places were removed. A JSON object,
    array or string is read and its strings are redacted, so it stays valid JSON. decoded:
    the text is a string read out of JSON, so a backslash in it is only a character."""
    done: tuple[str, int] = (text, 0)
    if holds_candidate(text):
        found = structural(text)
        done = found if found is not None else plain(text, decoded)
    # A plain str, even if given a subclass, whose methods could carry the secret back.
    return str.__str__(done[0]), done[1]


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


STRING_LITERAL = re.compile(r'"(?:[^"\\]|\\.)*"')


def document_strings(node: object) -> Iterator[str]:
    """Every string of a parsed JSON text, names and values, in the order of the text."""
    if isinstance(node, Number):
        return
    if isinstance(node, str):
        yield node
    elif isinstance(node, Members):
        for name, value in node:
            yield name
            yield from document_strings(value)
    elif isinstance(node, list):
        for value in cast(list[object], node):
            yield from document_strings(value)


def json_places(text: str) -> list[Replacement] | None:
    """For a text that is JSON as a whole: each string literal that redaction changes, as a
    place in the text, rule "json". None if the text is not JSON as a whole. If the strings
    cannot be lined up with the literals, the whole text is the one place."""
    read = structural(text)
    if read is None:
        return None
    if read[0] == text:
        return []
    body, lead = text.strip(), len(text) - len(text.lstrip())

    def parsed(source: str) -> object:
        return json.loads(
            source,
            parse_int=Number,
            parse_float=Number,
            parse_constant=Number,
            object_pairs_hook=Members,
        )

    try:
        before = list(document_strings(parsed(body)))
        after = list(document_strings(parsed(read[0].strip())))
    except (ValueError, RecursionError):
        before, after = [], []
    literals = list(STRING_LITERAL.finditer(body))
    if not before or len(before) != len(after) or len(before) != len(literals):
        return [Replacement(0, len(text), "json", read[0])]
    ascii_only = "\\u" in body
    return [
        Replacement(
            lead + found.start(),
            lead + found.end(),
            "json",
            json.dumps(new, ensure_ascii=ascii_only),
        )
        for found, old, new in zip(literals, before, after, strict=True)
        if old != new
    ]


def replacements(text: str) -> list[Replacement]:
    """Every place redact changes the text, in the text as given, each with its rule's name.

    redact runs its rules one after another, each on what the one before left, so a later
    match can sit on an earlier replacement. It is then one place here, and its text is
    what the rules left there.
    """
    text = str.__str__(text)
    if holds_candidate(text):
        # A JSON text is read and written back as a whole, so its places are the strings
        # in it that the rules change, each judged on its own.
        places = json_places(text)
        if places is not None:
            return places
    pieces = [Piece(text, 0, len(text), None)]
    passes: list[tuple[re.Pattern[str], Callable[[re.Match[str]], tuple[str, str]]]] = [
        (URL, lambda m: (plain_url(m), "url")),
        (JWT, lambda m: (no_jwt(m), "jwt")),
        (PATTERNS, lambda m: (m.group() if kept_as_is(m) else REMOVED, secret_rule(m.group()))),
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
