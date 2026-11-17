"""The version rule between the SDK in a service and the runner.

Before 1.0 both sides run the same Scrimmage version. Each reads the version of the
installed `scrimmage` package; anything but an equal, known version is refused.
"""

import functools

UNKNOWN = "unknown"


@functools.cache
def _installed() -> str:
    # Imported here: every process the SDK starts in loads this module, and the metadata
    # reader is heavy, so only the process that needs a version pays for it.
    import importlib.metadata

    try:
        return importlib.metadata.version("scrimmage")
    except importlib.metadata.PackageNotFoundError:
        return UNKNOWN


def installed() -> str:
    """The version of scrimmage in this process, or `unknown` when it is not installed."""
    return _installed()


def same(sdk: object) -> bool:
    """True only for this process's own version, and only when it is known: `unknown`, from a
    Scrimmage with no package metadata, matches nothing, not even itself."""
    return sdk == installed() and sdk != UNKNOWN


def shown(text: object) -> str:
    """A version as a message may show it: printable characters only, and short."""
    return "".join(c for c in str(text) if c.isprintable())[:64]
