from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", env_ignore_empty=True
    )

    groq_api_key: SecretStr
    groq_base_url: str = "https://api.groq.com/openai/v1"
    model: str = "openai/gpt-oss-120b"


def get_settings() -> Settings:
    # The key comes from the environment, which pyright cannot see.
    return Settings()  # pyright: ignore[reportCallIssue]
