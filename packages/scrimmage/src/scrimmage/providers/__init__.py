"""Model providers behind one interface.

Every module of scrimmage outside this package knows a provider only through `ModelProvider`,
`ServerChains` and `ProviderRegistry`: none imports a provider's own module, its class or a
name the registry does not export. A provider is a class in `providers.impl`, over the pure
functions of its own table module (provider_openai for OpenAI), and an entry in the registry.
This file imports nothing, so reading a provider's types costs no provider module.
"""
