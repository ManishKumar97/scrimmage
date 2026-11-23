"""The registry: which provider a request is for, found per call.

It is built once, from the providers there are and the hosts the config names, before the first
run, and carried on the run's plan. One agent can call two providers, and one host can speak
two formats, so a request is resolved by its host and path, each time.

The providers themselves are loaded on first use, not at import: a provider reads replies with
the changes module, which imports the config, which imports this module."""

import functools
from dataclasses import dataclass

from scrimmage.providers.base import ModelProvider


@functools.cache
def builtin_providers() -> tuple[ModelProvider, ...]:
    """Every provider there is, in the order a probe tries them."""
    from scrimmage.providers.impl import providers

    return providers()


@dataclass(frozen=True)
class ProviderRegistry:
    """providers: every provider there is. listed: the (host, provider name) pairs the config
    names under model_hosts; a host may be listed once for each provider."""

    providers: tuple[ModelProvider, ...]
    listed: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        known = {p.name for p in self.providers}
        for host, name in self.listed:
            if name not in known:
                raise ValueError(f"model host {host!r} names the unknown provider {name!r}")

    def get(self, name: str) -> ModelProvider:
        for provider in self.providers:
            if provider.name == name:
                return provider
        raise ValueError(f"unknown provider {name!r}")

    def candidates(self, host: str) -> tuple[ModelProvider, ...]:
        """The providers that own this host: those the config names for it, in the order it
        names them, then those it is a built-in host of."""
        named = [self.get(name) for h, name in self.listed if h == host]
        built_in = [p for p in self.providers if host in p.hosts]
        return tuple(dict.fromkeys([*named, *built_in]))

    def resolve(self, host: str, path: str, method: str) -> tuple[ModelProvider, str] | None:
        """The provider and op a request to this host, path and method is, or None. When a host
        speaks two formats the path picks: the first provider whose op_for knows it."""
        for provider in self.candidates(host):
            op = provider.op_for(path, method)
            if op is not None:
                return provider, op
        return None

    def probe(self, path: str, method: str) -> tuple[ModelProvider, str] | None:
        """A request to a host no one named, tried against every provider: how a gateway not
        named yet is found."""
        for provider in self.providers:
            op = provider.op_for(path, method)
            if op is not None:
                return provider, op
        return None

    def recognizes(self, path: str, method: str) -> bool:
        """Whether this is a model's path on any provider, whatever the host."""
        return self.probe(path, method) is not None

    def is_built_in_host(self, host: str) -> bool:
        return any(host in p.hosts for p in self.providers)


@functools.lru_cache(maxsize=64)
def registry_for(listed: tuple[tuple[str, str], ...] = ()) -> ProviderRegistry:
    """The registry for these model hosts, built once for each distinct set."""
    return ProviderRegistry(builtin_providers(), listed)


def built_in() -> ProviderRegistry:
    """The providers' own hosts, with no model_hosts."""
    return registry_for()
