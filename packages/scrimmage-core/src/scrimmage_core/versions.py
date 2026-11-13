"""The version rule between the SDK in a service and the runner.

Both sides read the version of the installed `scrimmage-core` and compare releases, with no
`packaging` dependency. The major is the first number; while it is 0, the first two, since a
0.x release may change anything. A different major is refused. The same major with another
minor, patch, pre-release or local part is allowed, and said once.
"""

import functools
import re
from dataclasses import dataclass
from enum import Enum

RELEASE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")
UNKNOWN = "unknown"


class Verdict(Enum):
    SAME = "same"  # the same release: nothing to say
    WARN = "warn"  # the same major, another release: allowed, and said once
    UNREADABLE = "unreadable"  # a version that is not an installed scrimmage-core release
    MAJOR = "major"  # another major: refused


@dataclass(frozen=True)
class Version:
    major: tuple[int, ...]
    release: tuple[int, int, int]
    rest: str  # what follows the release: `.dev1`, `rc2`, `+local`


def parse(text: object) -> Version | None:
    """The release at the start of text, or None when there is none."""
    if not isinstance(text, str):
        return None
    found = RELEASE.match(text)
    if found is None:
        return None
    first, second, third = int(found[1]), int(found[2]), int(found[3] or 0)
    return Version((first,) if first else (0, second), (first, second, third), text[found.end() :])


def judge(sdk: object, runner: str) -> Verdict:
    """How the SDK's version stands against the runner's."""
    theirs, ours = parse(sdk), parse(runner)
    if theirs is None or ours is None:
        return Verdict.UNREADABLE
    if theirs.major != ours.major:
        return Verdict.MAJOR
    same = theirs.release == ours.release and theirs.rest == ours.rest
    return Verdict.SAME if same else Verdict.WARN


@functools.cache
def _installed() -> str:
    # Imported here: every process the SDK starts in loads this module, and the metadata
    # reader is heavy, so only the process that needs a version pays for it.
    import importlib.metadata

    try:
        return importlib.metadata.version("scrimmage-core")
    except importlib.metadata.PackageNotFoundError:
        return UNKNOWN


def installed() -> str:
    """The version of scrimmage-core in this process, or `unknown` when it is not installed."""
    return _installed()
