from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", env_ignore_empty=True
    )

    # The one setting that picks the model provider.
    provider: Literal["groq", "anthropic"] = "anthropic"
    groq_api_key: SecretStr = SecretStr("")
    groq_model: str = "openai/gpt-oss-120b"
    groq_base_url: str = "https://api.groq.com/openai/v1"
    anthropic_api_key: SecretStr = SecretStr("")
    anthropic_model: str = "claude-haiku-5-5"

    # Where each run writes its spans (OTLP JSON, one line per export).
    trace_file: Path = Path("runs/traces.jsonl")

    # A command the MCP servers start under, such as a wrapper, as a JSON list.
    # Each server then runs as prefix + [python, -m, module].
    mcp_command_prefix: list[str] = []
    # Names of variables the MCP servers get from this process's environment, as a
    # JSON list. The MCP SDK passes a server only a few variables (PATH, HOME, ...).
    mcp_pass_env: list[str] = []

    @model_validator(mode="after")
    def key_for_provider(self) -> "Settings":
        key = {"groq": self.groq_api_key, "anthropic": self.anthropic_api_key}
        if not key[self.provider].get_secret_value():
            raise ValueError(f"PROVIDER={self.provider} needs its API key set")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
