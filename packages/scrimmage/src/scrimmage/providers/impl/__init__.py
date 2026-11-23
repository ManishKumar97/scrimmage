"""The providers there are. Only the registry imports this package.

The providers are built on first call, not when the package loads: a provider's table module
opens the wire types through providers.impl.wire, so importing this package must not import a
table module back. Call providers() through the registry, which builds them once: a second
caller would get instances of its own.
"""

from scrimmage.providers.base import ModelProvider


def providers() -> tuple[ModelProvider, ...]:
    """Every provider there is, in the order a probe tries them."""
    from scrimmage.providers.impl.openai import OpenAI

    return (OpenAI(),)
