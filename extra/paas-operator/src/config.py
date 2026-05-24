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

    # PVC Lifecycle
    pvc_grace_period_days: int = 7  # Days before orphaned PVCs are deleted

    # Private Registry (for tenant namespace imagePullSecrets)
    registry_server: str = ""
    registry_username: str = ""
    registry_password: str = ""
    registry_secret_name: str = "paas-registry"

    # Agent Runtime (per-project pod) — design §2.11
    agent_runtime_chart: str = "agent-runtime"  # chart ref for helm install/upgrade
    agent_runtime_image_tag: str = "latest"  # pod image tag injected into values
    odoo_base_url: str = ""  # ODOO_BASE_URL injected into pod env (skill-bundle fetch)

    # Cloudflare Tunnel Integration
    cloudflare_enabled: bool = False  # Set to true to enable automatic route management
    cloudflare_api_token: str = ""  # API token with Cloudflare Tunnel:Edit and DNS:Edit permission
    cloudflare_account_id: str = ""  # Cloudflare account ID
    cloudflare_tunnel_id: str = ""  # Tunnel ID from Zero Trust dashboard
    cloudflare_zone_id: str = ""  # Zone ID for DNS management (found in domain overview)
    cloudflare_domain: str = ""  # Base domain (e.g., woowtech.io)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )


settings = Settings()
