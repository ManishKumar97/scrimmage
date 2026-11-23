"""The providers there are. Only the registry imports this package."""

from scrimmage.providers.base import ModelProvider
from scrimmage.providers.impl.openai import OpenAI

PROVIDERS: tuple[ModelProvider, ...] = (OpenAI(),)
