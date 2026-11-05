"""Faultline's own errors. None of them subclasses a library's error."""


class FaultlineError(Exception):
    """The base of every error Faultline raises."""


class FaultlineRefused(FaultlineError):
    """A call the test neither mocks nor lists as safe to run for real."""


class FaultlineTimeLimit(FaultlineError):
    """The run passed its time limit, so the gate stops the call."""


class FaultlineMockError(FaultlineError):
    """A mock's value does not fit the tool's type. A mistake in the test."""


class ConfigError(FaultlineError):
    """A bad value in faultline.toml. The message names its table and key."""
