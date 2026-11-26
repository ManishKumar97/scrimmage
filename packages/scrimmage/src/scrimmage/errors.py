"""Scrimmage's own errors. None of them subclasses a library's error."""

DOCS_SITE = "https://manishkumar97.github.io/scrimmage/"


def docs_url(page: str) -> str:
    """The published page for a docs file, such as configuration. A wheel has no docs folder."""
    return f"{DOCS_SITE}{page}/"


class ScrimmageError(Exception):
    """The base of every error Scrimmage raises."""


class ScrimmageRefused(ScrimmageError):
    """A call the test neither mocks nor lists as safe to run for real."""


class ScrimmageTimeLimit(ScrimmageError):
    """The run passed its time limit, so the gate stops the call."""


class ScrimmageStopped(ScrimmageError):
    """The run was stopped while a call was going on. It is what the call's line says."""


class ScrimmageMockError(ScrimmageError):
    """A mock or a fault cannot be built as the test says. A mistake in the test.

    cause: the class name of the team's error that made the mock fail to build, if one did.
    Only the name, as the error's message can quote the value."""

    def __init__(self, message: str, *, cause: str | None = None) -> None:
        super().__init__(message)
        self.cause = cause


class ConfigError(ScrimmageError):
    """Bad values in scrimmage.toml. Each problem names its table and key."""

    def __init__(self, *problems: str) -> None:
        super().__init__("\n".join(problems))
        self.problems = problems
