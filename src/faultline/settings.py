from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", env_ignore_empty=True
    )

    # The one setting that picks the model provider.
    provider: Literal["groq", "anthropic"] = "groq"
    # Empty means the default model of the chosen provider.
    model: str = ""
    groq_api_key: SecretStr = SecretStr("")
    groq_base_url: str = "https://api.groq.com/openai/v1"
    anthropic_api_key: SecretStr = SecretStr("")

    @model_validator(mode="after")
    def key_for_provider(self) -> "Settings":
        key = {"groq": self.groq_api_key, "anthropic": self.anthropic_api_key}
        if not key[self.provider].get_secret_value():
            raise ValueError(f"PROVIDER={self.provider} needs its API key set")
        return self


def get_settings() -> Settings:
    # The keys come from the environment, which pyright cannot see.
    return Settings()
