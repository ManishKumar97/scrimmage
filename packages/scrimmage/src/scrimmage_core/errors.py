"""Scrimmage's own errors. None of them subclasses a library's error."""


class ScrimmageError(Exception):
    """The base of every error Scrimmage raises."""


class ScrimmageRefused(ScrimmageError):
    """A call the test neither mocks nor lists as safe to run for real."""


class ScrimmageTimeLimit(ScrimmageError):
    """The run passed its time limit, so the gate stops the call."""


class ScrimmageStopped(ScrimmageError):
    """The run was stopped while a call was going on. It is what the call's line says."""


class ScrimmageMockError(ScrimmageError):
    """A mock or a fault cannot be built as the test says. A mistake in the test."""


class ConfigError(ScrimmageError):
    """Bad values in scrimmage.toml. Each problem names its table and key."""

    def __init__(self, *problems: str) -> None:
        super().__init__("\n".join(problems))
        self.problems = problems
