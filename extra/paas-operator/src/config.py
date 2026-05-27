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
    # Chart lives in a PRIVATE Gitea classic Helm repo (gap C decision): operator
    # fetches it statelessly via `helm install --repo <url> --version <ver>
    # --username/--password` (no `helm repo add`; OCI is unavailable in this env).
    # agent_runtime_chart is the bare chart NAME; repo URL / version / read creds
    # are separate env-overridable settings, set by the operator deployment (gap A).
    agent_runtime_chart: str = "agent-runtime"  # chart NAME (resolved via --repo)
    agent_runtime_repo_url: str = ""  # classic Helm repo URL (--repo), e.g. http://gitea-http.gitea.svc.cluster.local:3000/api/packages/woow-paas/helm
    agent_runtime_chart_version: str = ""  # chart version (--version); empty = latest in index. This is the CHART version (e.g. 0.1.0), NOT the image tag
    agent_runtime_repo_username: str = ""  # Gitea read:package username (--username); private repo auth
    agent_runtime_repo_password: str = ""  # Gitea read:package token (--password); private repo auth
    agent_runtime_image_tag: str = "1.0.0"  # pod image tag (appVersion) injected into values; 'latest' does not exist, only 1.0.0 is built
    odoo_base_url: str = ""  # ODOO_BASE_URL injected into pod env (skill-bundle fetch); stg = internal svc, e.g. http://stg-odoo-dev-sandbox-odoo.dev-sandbox-stg.svc.cluster.local:8069
    # F-PILOT-10 (A): open the agent pod's egress to the EXTERNAL LLM endpoint
    # (e.g. eugene-minimax.woowtech.io:443). The chart gates this behind
    # `agentRuntime.externalEgress.enabled`; default OFF (closed egress = secure),
    # explicit per-environment opt-in via env AGENT_RUNTIME_EXTERNAL_EGRESS_ENABLED.
    agent_runtime_external_egress_enabled: bool = False  # forward to chart when real-LLM pods need outbound

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
