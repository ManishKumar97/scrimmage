"""Derail's own errors. None of them subclasses a library's error."""


class DerailError(Exception):
    """The base of every error Derail raises."""


class DerailRefused(DerailError):
    """A call the test neither mocks nor lists as safe to run for real."""


class DerailTimeLimit(DerailError):
    """The run passed its time limit, so the gate stops the call."""


class DerailStopped(DerailError):
    """The run was stopped while a call was going on. It is what the call's line says."""


class DerailMockError(DerailError):
    """A mock or a fault cannot be built as the test says. A mistake in the test."""


class ConfigError(DerailError):
    """Bad values in derail.toml. Each problem names its table and key."""

    def __init__(self, *problems: str) -> None:
        super().__init__("\n".join(problems))
        self.problems = problems
