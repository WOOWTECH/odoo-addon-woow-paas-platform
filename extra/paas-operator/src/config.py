"""Application configuration."""
import os
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # API Security
    api_key: str = ""

    # Service Configuration
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "info"

    # Kubernetes
    namespace_prefix: str = "paas-ws-"

    # Helm
    helm_binary: str = "/usr/local/bin/helm"
    helm_timeout: int = 300  # seconds

    # CORS
    cors_origins: str = ""  # Comma-separated list of allowed origins, empty = block all external

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )


settings = Settings()
