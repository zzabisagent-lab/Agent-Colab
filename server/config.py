"""Runtime configuration for the Agent-Colab server.

Precedence (development plan §8.2): emergency env > encrypted runtime setting > setup default >
built-in default. Phase 0 implements the env and built-in layers; runtime settings arrive in
Phase 4.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PRODUCT_NAME = "Agent-Colab"


class Settings(BaseSettings):
    """Process settings. Secret values are never logged or echoed."""

    model_config = SettingsConfigDict(env_prefix="AGENT_COLAB_", extra="ignore")

    instance_name: str = PRODUCT_NAME
    base_url: str = "http://127.0.0.1:8080"
    bind_host: str = "127.0.0.1"
    bind_port: int = 8080
    database_url: str | None = Field(default=None, repr=False)
    bootstrap_state_path: str = "/var/lib/agent-colab/bootstrap/state.json"
    default_timezone: str = "UTC"
    default_language: str = "en"
    log_json: bool = True
    master_key_id: str = "mk-local-1"
    master_key_b64: str | None = Field(default=None, repr=False)


def get_settings() -> Settings:
    return Settings()
