"""Runtime settings from env + .env."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    anthropic_api_key: str = Field(default="")
    model: str = Field(default="claude-opus-4-7")
    max_input_chars: int = Field(
        default=200_000,
        description="Hard cap on diff characters sent to the model. Long diffs are truncated.",
    )


def get_settings() -> Settings:
    return Settings()
