"""Remove known secret patterns from anything that is recorded."""

import base64
import functools
import json
import re
from collections.abc import Callable, Iterator, Sequence
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
# \n, \r, \t or \uXXXX escape the name also starts, and a backslash before it does not. The
# escape is a break whatever it holds: Go writes & as \u0026, and an escaped letter before a
# name only removes more. An escape inside a name, as in p\u0061ssword=, is not read as one
# in plain text: no JSON writer escapes an ASCII letter, and a string read out of JSON has it
# undone already. A URL-encoded comma, &, ;, space or | (%2C, %26, %3B, %20, %7C) is a break
# too. An encoded letter, as in %70assword=, is not: RFC 3986 says no encoder writes one.
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
# The hex digits of a URL-encoded comma, &, ;, space or |, after its %.
ENCODED_BREAK = "(?:2[06cC]|3[bB]|7[cC])"
# A name starts after the hex digits of an encoded break, not on them, unless a _ follows
# them, as in %2C_password=, where the digits are read as a NAME_ prefix.
NAME = (
    rf"(?:(?<![A-Za-z0-9_\\])(?!(?<=%){ENCODED_BREAK}(?!_))|(?<=\\[nrt])|(?<=\\u[0-9a-fA-F]{{4}})"
    rf"|(?<=%{ENCODED_BREAK}))"
    rf"(?ai:(?:[A-Za-z0-9]+_)*{FIELD})"
)
# A quoted value may not hold another field name, so an unclosed quote that runs on to the
# next quote does not swallow the name of a later secret.
QUOTED = (
    r"""(?P<quote>["'])"""
    rf"(?!(?:(?!(?P=quote))[^\n])*?(?ai:{FIELD}))"
    r"""(?:\\.|(?!(?P=quote))[^\\\n])+(?P=quote)"""
)
# Where a key shape other than a form field can begin: each of them starts with its literal.
SHAPE_START = (
    r"sk-|AKIA|ASIA|AIza|gh[pousr]_|github_pat_|xox|xapp-|Illegal header"
    r"|(?ai:bearer|authorization)"
)
# Where any key shape can begin, a field name included. Every match of the patterns starts at
# one, so a search for the next one never reads past the next match.
SHAPE_OR_NAME = rf"{SHAPE_START}|{NAME}"
# One step of an unquoted value, and of the value after a marker.
STEP = r"""\\u[0-9a-fA-F]{4}|\\(?!["'nr])|[^&\s"'<>\\]"""
MARKED_STEP = r"""[^&\s"'<>\\]"""


def bounded(step: str) -> str:
    """A run of steps that stops before a field name or a key shape, or one step if one
    begins at once. key_removals reads on from there with the plain run to where the value
    ends, so a removed value is the whole run, and each pattern match reads only up to the
    next name: a run of kept names, as in is_secret=1,is_secret=1, stays linear."""
    return rf"(?:(?:(?!{SHAPE_OR_NAME})(?:{step}))+|(?:{step}))"


# `inside` is set when the name follows a quote, so a quote after it closes that string.
FORM = (
    r"""(?:(?P<inside>(?<=["']))|(?<!["']))"""
    + rf"(?P<field>{NAME})"
    + rf"(?:(?P<marked>{MARKER}{bounded(MARKED_STEP)})"
    + rf"|(?(inside)(?!)|{QUOTED})"
    + rf"|(?!{MARKER})(?:<[^<>\"'\\]+>|(?P<value>(?(inside)|(?:\\?[\"']|<)?)"
    + rf"{bounded(STEP)}))"
    + ")"
)
# The rest of a value from where the pattern stopped. A run stops at the same place from any
# step inside it, so one read serves every name inside one run.
VALUE_RUN = re.compile(rf"(?:{STEP})*")
MARKED_RUN = re.compile(rf"(?:{MARKED_STEP})*")
SHAPE_OR_NAME_RE = re.compile(SHAPE_OR_NAME)
# Where FORM's value ends, a removed value may go on past an escape, as in abc\"2zz. It goes
# on only when more of the value follows the escape: one before a space, a bracket, a comma,
# a colon or the end closes an escaped string instead, as the second \" in {\"a\":\"b\"}.
GOES_ON = r"""(?=[^&\s"'<>\\,;:()\[\]{}])"""


def continuation(escape: str) -> re.Pattern[str]:
    """What a value goes on with where FORM's value ends: escapes with more of the value
    after them, and the value between them. Each step reads only the text ahead, so where it
    stops does not depend on where it started, as long as it starts between two steps."""
    step = rf"\\u[0-9a-fA-F]{{4}}|(?:{escape}){GOES_ON}|\\(?![\"'nr])|[^&\s\"'<>\\]"
    return re.compile(rf"(?:{step})*")


# In raw text an escaped quote goes on, and a \n or \r is a line break that ends the value.
# In a string read out of JSON, or a Python string, every escape is already undone: a \n
# there is a backslash and a letter, as in C:\new, and a raw quote is a character, as \"
# became one, unless the name sits inside a quote, which the quote then closes.
GOES_ON_RAW = continuation(r"\\[\"']")
GOES_ON_DECODED = continuation(r"\\[\"'nr]|[\"']")
GOES_ON_DECODED_INSIDE = continuation(r"\\[\"'nr]")
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


# The names a literal is made of. A name that holds another (pgpassword holds password, and
# authtoken, accesstoken hold token) is left out.
FIELD_LITERAL_NAMES = (
    "password",
    "passwd",
    "token",
    "secret",
    "secret_access_key",
    "api_key",
    "api%5fkey",
    "pgpass",
    "mysql_pwd",
)

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
        r"(?P<bearer>(?i:bearer)\s+[A-Za-z0-9._~+/=-]{16,})",
        ("bearer",),
        lowercase=True,
        name="bearer-token",
    ),
    Secret(BASIC, ("authorization",), lowercase=True, name="basic-credential"),
    # FIELD has the name and then = or %3d with nothing between, so each literal is a name and
    # its separator. A bare "token" would hold in every OpenAI body (max_tokens), and the
    # regex would run on every request. pgpassword= holds password=, so it needs none.
    Secret(
        FORM,
        tuple(f"{name}{sep}" for name in FIELD_LITERAL_NAMES for sep in ("=", "%3d")),
        True,
        "form-field",
    ),
    # h11 quotes the bad header
    Secret(r"Illegal header (?:name|value) .*", ("Illegal header",), name="header-text"),
)
PATTERNS = re.compile("|".join(secret.pattern for secret in SECRETS))
LITERALS = tuple(lit for secret in SECRETS if not secret.lowercase for lit in secret.literals)
LOWER_LITERALS = tuple(lit for secret in SECRETS if secret.lowercase for lit in secret.literals)
# Every form-field literal holds = or %3d, so a text with neither skips all of them.
FIELD_LITERALS = tuple(
    lit for secret in SECRETS if secret.name == "form-field" for lit in secret.literals
)
OTHER_LOWER_LITERALS = tuple(lit for lit in LOWER_LITERALS if lit not in FIELD_LITERALS)


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


PARENTHESIS = re.compile(r"[()]")


class Parentheses:
    """The last ( and the last ) before a place in the text. The key pass asks at places in
    order, so each part of the text is read once: a run of names in a call, as in
    a,token=t,a,token=t, stays linear, where a search back from each name would not."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.read_to = 0
        self.opening = self.closing = -1

    def before(self, at: int) -> tuple[int, int]:
        if at < self.read_to:  # never asked out of order by the key pass; kept correct anyway
            return self.text.rfind("(", 0, at), self.text.rfind(")", 0, at)
        for found in PARENTHESIS.finditer(self.text, self.read_to, at):
            if found.group() == "(":
                self.opening = found.start()
            else:
                self.closing = found.start()
        self.read_to = at
        return self.opening, self.closing


def code_value(found: re.Match[str], end: int, parentheses: Parentheses) -> bool:
    """A value that is code: None, True or False alone, or a bare name as an argument of a
    call whose name is right before its parenthesis, the argument right after the parenthesis
    or after ", ": f(token=tok) and f(a=1, token=tok). After a bare comma, as in
    f(a=1,token=tok), the value is removed: a removal is the safe side. end: where the value
    ends, with the first character it goes on with, when that is more than the match holds.
    parentheses: where the last ( and ) before the match are."""
    if found.start("value") < 0:
        return False
    text = found.string
    head = IDENTIFIER.match(text, found.start("value"), end)
    if head is None:
        return False
    name = head.group()
    if text[head.end() : min(head.end() + 1, end)] not in ("", ")", ","):
        return False
    if name in CODE_VALUES:
        return True
    start = found.start()
    if not (text[start - 1 : start] == "(" or text[start - 2 : start] == ", "):
        return False
    opening, closing = parentheses.before(start)
    named_call = opening > closing and (
        text[opening - 1 : opening].isalnum() or text[opening - 1 : opening] == "_"
    )
    return named_call and "." not in name


class Reads:
    """The last read of each run pattern: from where, and to where. A run stops at the same
    place from any step inside it, so one read is enough for every name inside one run, and
    the whole pass stays linear. One per pattern, as names inside and outside a quote can
    alternate, each reading with its own continuation."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.last: dict[re.Pattern[str], tuple[int, int]] = {}

    def end(self, pattern: re.Pattern[str], at: int) -> int:
        since, to = self.last.get(pattern, (0, 0))
        if not since <= at < to:
            to = cast(re.Match[str], pattern.match(self.text, at)).end()
            self.last[pattern] = (at, to)
        return to


def key_removals(text: str, decoded: bool = False) -> list[tuple[int, int, str]]:
    """Each place the key shapes remove, as (start, end, its first match's text), in order.

    A removed value is the whole of it, as the pattern without its bound reads it, and also
    takes what it goes on with past an escape, as in abc\"2zz; a secret found in that is
    joined into the same place. A match that is kept, such as next_page_token=x or code, keeps
    only what comes before the first field name or key shape in it, so in
    next_page_token=ab,password=x the password is read again, as any other text is, and a kept
    value never shields a secret in it or after it.
    """
    places: list[tuple[int, int, str]] = []
    reads = Reads(text)
    parentheses = Parentheses(text)
    at = 0
    while (found := PATTERNS.search(text, at)) is not None:
        # The pattern stops a value before a field name or a key shape; its run goes on.
        # resume: where the whole match ends. end: where a removed place ends, past an escape
        # with more of the value after it. code_end: what code_value reads.
        resume = end = code_end = found.end()
        if found.start("value") >= 0:
            resume = reads.end(VALUE_RUN, resume)
            goes_on = GOES_ON_RAW
            if decoded:
                inside = found.group("inside") is not None
                goes_on = GOES_ON_DECODED_INSIDE if inside else GOES_ON_DECODED
            end = reads.end(goes_on, resume)
            # Its first character is all code_value reads past the value: an escape is never
            # part of a name.
            code_end = min(end, resume + 1)
        elif found.start("marked") >= 0:
            resume = end = reads.end(MARKED_RUN, resume)
        at = resume
        if found.start("bearer") >= 0:
            # A Bearer token's characters can start a key shape or a field name, as the
            # Authorization in Bearer QQQAuthorization: Basic ...: the whole token is removed,
            # and the text from the first one that starts in it is read again, so what starts
            # there and goes on past the token joins it.
            shape = SHAPE_OR_NAME_RE.search(text, found.start() + 1)
            if shape is not None and shape.start() < found.end():
                at = shape.start()
        field = found.group("field")
        if field is not None:
            # The text from the first field name or key shape inside a form field is read
            # again. A kept field, a harmless name such as next_page_token or code, ends
            # there. A removed one does not, and what is found there joins it, as a Bearer
            # token that starts inside the value and goes on past its end.
            shape = SHAPE_OR_NAME_RE.search(text, found.start() + 1)
            bound = len(text) if shape is None else shape.start()
            if HARMLESS_NAME.match(field) is not None or code_value(found, code_end, parentheses):
                at = min(found.end(), bound)
                continue
            at = min(resume, bound)
        if places and found.start() < places[-1][1]:  # it starts in a place already removed
            start, last, first = places[-1]
            places[-1] = (start, max(last, end), first)
        else:
            places.append((found.start(), end, found.group()))
    return places


def remove_keys(text: str, decoded: bool = False) -> tuple[str, int]:
    """The text with every key shape replaced, and how many places were. decoded: the text is
    a string read out of JSON."""
    out: list[str] = []
    at = 0
    places = key_removals(text, decoded)
    for start, end, _first in places:
        out += [text[at:start], REMOVED]
        at = end
    out.append(text[at:])
    return "".join(out), len(places)


UNICODE_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")
# The two-character JSON escapes other than the backslash, which read_json handles first.
SIMPLE_ESCAPES = (
    ('\\"', '"'),
    ("\\/", "/"),
    ("\\b", "\b"),
    ("\\f", "\f"),
    ("\\n", "\n"),
    ("\\r", "\r"),
    ("\\t", "\t"),
)
PAIR = "\x00"  # stands for an escaped backslash while the other escapes are read
READS = 6  # reads of a JSON string inside a JSON string, then the passes run to be safe
# The reads cost at most a few passes over the text, but a large text that is mostly
# backslashes costs more to read than the passes it might save, so it runs them at once.
READ_MAX_LENGTH = 65536
READ_MAX_BACKSLASHES = 4096


def unicode_char(found: re.Match[str]) -> str:
    """The character a \\uXXXX escape stands for. A NUL is left as written: it is PAIR."""
    code = int(found.group(1), 16)
    return chr(code) if code else found.group()


def read_json(text: str) -> str:
    """The text read once as the inside of a JSON string, left to right: an escaped
    backslash is one backslash, and the character after it is not read as an escape. Pairs
    of backslashes go first, which is the same left to right, as a run of them pairs up from
    its start and an odd one out then escapes the next character."""
    paired = "\\\\" in text
    if paired:
        text = text.replace("\\\\", PAIR)
    for escape, char in SIMPLE_ESCAPES:
        if escape in text:
            text = text.replace(escape, char)
    if "\\u" in text:
        text = UNICODE_ESCAPE.sub(unicode_char, text)
    return text.replace(PAIR, "\\") if paired else text


def holds_literal(text: str) -> bool:
    if "://" in text or "eyJ" in text or any(lit in text for lit in LITERALS):
        return True
    lowered = text.lower()
    if any(lit in lowered for lit in OTHER_LOWER_LITERALS):
        return True
    return ("=" in lowered or "%3d" in lowered) and any(lit in lowered for lit in FIELD_LITERALS)


def holds_candidate(text: str) -> bool:
    """Whether any pass could match: the text holds a string every match of that pass holds.
    A JSON escape can hide a letter or the = (p\\u0061ssword, password\\u003d), and a JSON string
    inside a JSON string hides it deeper, so the text is also read as a JSON string again
    and again until it stops changing, and each reading is looked at. Whatever the reading
    cannot settle cheaply or surely, it answers True, so it never costs more than the passes."""
    backslashes = text.count("\\")
    if len(text) > READ_MAX_LENGTH and backslashes > READ_MAX_BACKSLASHES:
        return True  # before any scan: the passes scan the text themselves
    if holds_literal(text):
        return True
    if not backslashes:
        return False
    if PAIR in text:
        return True
    current = text
    for _ in range(READS):
        read = read_json(current)
        if read == current:
            return False
        if holds_literal(read):
            return True
        if "\\" not in read:
            return False  # nothing left to read
        current = read
    return True  # still changing after READS: too deep to rule out


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


SCRUB_MIN = 8  # a shorter value would match too much text that is not it
# What URL encoding keeps as it is, as RFC 3986 and urllib.parse.quote have it. Written here, as
# this module is read by code that must not import a network library.
UNRESERVED = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-~")


def percent_encoded(data: bytes, safe: bytes = b"", *, plus: bool = False) -> str:
    """data URL-encoded, as quote(data, safe) does, or quote_plus with plus."""
    kept = UNRESERVED | frozenset(safe)
    out: list[str] = []
    for byte in data:
        if plus and byte == 0x20:
            out.append("+")
        elif byte in kept:
            out.append(chr(byte))
        else:
            out.append(f"%{byte:02X}")
    return "".join(out)


def lower_hex(encoded: str) -> str:
    """A URL encoding with its hex digits in lower case, as some encoders write it."""
    return re.sub(r"%[0-9A-F]{2}", lambda found: found.group(0).lower(), encoded)


@functools.lru_cache(maxsize=256)
def scrub_forms(value: str) -> tuple[str, ...]:
    """Every form a value can take in recorded text, longest first: as written, JSON-escaped
    once and twice (a JSON string inside a JSON body, as tool arguments in a model request),
    URL-encoded in upper and lower case hex, and in base64 at each of the three offsets a byte
    string can put it at. A base64 form leaves off its edge characters, which the neighbouring
    bytes decide."""
    data = value.encode("utf-8", "surrogatepass")
    once = json.dumps(value)[1:-1]
    encoded_forms = [
        percent_encoded(data),
        percent_encoded(data, b"/"),
        percent_encoded(data, plus=True),
    ]
    forms = {
        value,
        once,
        json.dumps(once)[1:-1],
        json.dumps(value, ensure_ascii=False)[1:-1],
        *encoded_forms,
        *(lower_hex(form) for form in encoded_forms),
    }
    for offset in range(3):
        encoded = base64.b64encode(b"\0" * offset + data + b"\0\0").decode()
        first = -(-8 * offset // 6)  # the first character made of the value's bits alone
        last = (8 * (offset + len(data)) - 6) // 6  # and the last
        core = encoded[first : last + 1]
        forms |= {core, core.replace("+", "-").replace("/", "_")}  # standard and URL-safe
    return tuple(sorted((f for f in forms if len(f) >= SCRUB_MIN), key=len, reverse=True))


def remove_values(text: str, scrub: Sequence[str]) -> tuple[str, int]:
    """The text with each of the caller's own values removed, in every form, and how many
    places were removed. A value under SCRUB_MIN characters is not removed."""
    count = 0
    for value in scrub:
        if len(value) < SCRUB_MIN:
            continue
        for form in scrub_forms(value):
            if form in text:
                count += text.count(form)
                text = text.replace(form, REMOVED)
    return text, count


def scrubbed(value: object, scrub: Sequence[str]) -> object:
    """Every text in a value, at any depth, without the caller's own values."""
    if not scrub:
        return value
    if isinstance(value, str):
        return remove_values(value, scrub)[0]
    if isinstance(value, dict):
        return {k: scrubbed(v, scrub) for k, v in cast(dict[object, object], value).items()}
    if isinstance(value, list | tuple):
        return [scrubbed(item, scrub) for item in cast(list[object], value)]
    return value


def redacted(text: str, *, decoded: bool = False, scrub: Sequence[str] = ()) -> tuple[str, int]:
    """The text with secrets removed, and how many places were removed. The caller's own
    values go first, then the patterns. A JSON object, array or string is read and its
    strings are redacted, so it stays valid JSON. decoded: the text is a string read out of
    JSON, so a backslash in it is only a character."""
    text, removed = remove_values(text, scrub) if scrub else (text, 0)
    done: tuple[str, int] = (text, 0)
    if holds_candidate(text):
        found = structural(text)
        done = found if found is not None else plain(text, decoded)
    # A plain str, even if given a subclass, whose methods could carry the secret back.
    return str.__str__(done[0]), done[1] + removed


def redact(text: str, scrub: Sequence[str] = ()) -> str:
    return redacted(text, scrub=scrub)[0]


def redact_decoded(text: str, scrub: Sequence[str] = ()) -> str:
    """redact for a Python value's string, such as the agent's answer: it has no escapes left
    in it, so a backslash in it is only a character."""
    return redacted(text, decoded=True, scrub=scrub)[0]


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
    passes: list[Callable[[str], list[tuple[int, int, str, str]]]] = [
        lambda now: [(m.start(), m.end(), plain_url(m), "url") for m in URL.finditer(now)],
        lambda now: [(m.start(), m.end(), no_jwt(m), "jwt") for m in JWT.finditer(now)],
        # The same places remove_keys removes.
        lambda now: [(a, b, REMOVED, secret_rule(first)) for a, b, first in key_removals(now)],
    ]
    for places in passes:
        now = "".join(piece.text for piece in pieces)
        found = places(now)
        # Last first, so a place already made does not move the ones before it.
        for a, b, new, rule in reversed(found):
            if new != now[a:b]:
                pieces = replace_place(pieces, a, b, new, rule)
    return [Replacement(p.start, p.end, p.rule, p.text) for p in pieces if p.rule is not None]


def spans(text: str) -> list[tuple[int, int, str]]:
    """Each place redact would change the text, as (start, end, rule name)."""
    return [(r.start, r.end, r.rule) for r in replacements(text)]
