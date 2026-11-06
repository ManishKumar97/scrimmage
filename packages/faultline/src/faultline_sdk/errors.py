"""Faultline's own errors. None of them subclasses a library's error."""


class FaultlineError(Exception):
    """The base of every error Faultline raises."""


class FaultlineRefused(FaultlineError):
    """A call the test neither mocks nor lists as safe to run for real."""


class FaultlineTimeLimit(FaultlineError):
    """The run passed its time limit, so the gate stops the call."""


class FaultlineMockError(FaultlineError):
    """A mock or a fault cannot be built as the test says. A mistake in the test."""


class ConfigError(FaultlineError):
    """Bad values in faultline.toml. Each problem names its table and key."""

    def __init__(self, *problems: str) -> None:
        super().__init__("\n".join(problems))
        self.problems = problems
