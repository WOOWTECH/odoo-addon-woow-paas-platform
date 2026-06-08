"""Application configuration."""
import os
from pydantic import Field
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
    # Task 1a: probe SHAPE selector forwarded to the chart's top-level `probe.style`
    # when set. Empty (default) => operator forwards NOTHING => chart keeps its own
    # default (probe.style=tcpSocket), so image <= 1.1.x and existing pods are
    # unaffected. Set "httpGet" (per-environment opt-in via env
    # AGENT_RUNTIME_PROBE_STYLE) once the pod image is >= 1.2.0, which serves
    # /livez + /readyz; chart 0.2.0 fail-closes on any value other than
    # tcpSocket/httpGet. Mirrors the externalEgress forward pattern.
    agent_runtime_probe_style: str = ""  # "" = no forward; "httpGet" for image >= 1.2.0
    # skill bash sidecar (Drop B): forward the chart's `skillRunner.enabled` so the
    # agent-runtime pod cold-starts WITH the bash-runner sidecar. Default OFF — the
    # sidecar requires the Drop B agent-runtime image (bash_bridge daemon) + the
    # sidecar-aware chart; enabling it on an older image would crashloop. Explicit
    # per-environment opt-in via env AGENT_RUNTIME_SKILL_RUNNER_ENABLED, flipped at
    # the coordinated Drop B land. Mirrors the externalEgress forward pattern.
    agent_runtime_skill_runner_enabled: bool = False  # forward skillRunner.enabled to chart
    # issue #155 stopgap: SKILL_BUNDLE_TOKEN is signed at pod spawn/resume and baked
    # into the pod env; its exp was hard-coded to 3600s and no in-repo reap trigger
    # exists, so a pod outliving 1h 401'd forever on skill-bundle fetch. Make the TTL
    # config-driven; default 7 days (604800s) so a long-lived pod keeps a valid token.
    # The real fix (401 -> refresh) is a separate design. Env-overridable per
    # environment via SKILL_BUNDLE_TOKEN_TTL_SECONDS, set by the operator deployment
    # (the operator's own Deployment env lives cross-repo in paas-app-bundle-cd Fleet
    # CD values — NOT rendered by any chart in this repo).
    # SECURITY (F-GC-1 re-review): SKILL_BUNDLE_TOKEN is injected as a *plain env*
    # value into the pod spec (not secretKeyRef), so it is readable via
    # `kubectl get pod -o yaml` / `describe`. F-GC-1 was filed minor/pilot-acceptable
    # on the premise that the token was short-lived (exp 1h) + re-signed on resume,
    # so the plaintext exposure window was ≤1h. This default TTL (7 days) widens that
    # window from ≤1h to ≤7d — a 168x increase — which invalidates the "≤1h so
    # acceptable" premise. F-GC-1's risk rating (plain env vs secretKeyRef) MUST be
    # re-reviewed and may need its severity raised; see follow-ups.md F-GC-1.
    # gt=0 rejects non-positive TTLs (negative / 0 would sign an already-expired
    # token → every skill-bundle fetch 401s); fail closed at config load.
    skill_bundle_token_ttl_seconds: int = Field(
        default=604800, gt=0  # 7 days; env SKILL_BUNDLE_TOKEN_TTL_SECONDS
    )

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
