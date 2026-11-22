"""Model providers behind one interface.

The gate, the changes, the run's state, the config and init know a provider only through
`ModelProvider` and `ProviderRegistry`: none imports a provider's own module. A provider is
a module of pure functions, and a strategy class in this package that implements the
interface by calling them. This file imports nothing, so reading a provider's types costs
no provider module.
"""
