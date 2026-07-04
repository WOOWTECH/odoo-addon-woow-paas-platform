"""Application configuration."""
import os
from pydantic import Field, field_validator
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

    # Deploy environment (L1): prod/stg discriminator stamped onto tenant namespace
    # labels (L2, #279) + future account-level reconciler env filtering. Injected
    # per-environment via the operator Deployment env (cross-repo paas-app-bundle-cd
    # Fleet CD values — NOT a chart in this repo, mirrors the SKILL_BUNDLE_TOKEN_TTL
    # note above). Whitelist-validated fail-closed; consumers omit the label when
    # empty. Do NOT DERIVE the stamp value from namespace_prefix (kept a separate
    # field) — but the F6 startup guard (assert_env_guard) DOES cross-check the two as
    # independent signals to reject an env/prefix swap.
    #
    # F6 (#279 cross-env leak guard) — REVERSES the earlier "operator must NOT crash
    # on an unset env" stance, which was the fail-open hole: the workspace int id in
    # the #279 NP labels is per-Odoo-DB, NOT cluster-unique, and prod+stg share one
    # woow-k3s cluster, so an env-LESS label collides workspace ids across envs (a stg
    # agent reaches a prod CloudService of the same numeric id). An unset PAAS_ENV on a
    # real deploy is therefore fail-OPEN and the operator now FAILS FAST at startup
    # (assert_env_guard, called from main.lifespan) unless the dev/local opt-out is set.
    paas_env: str = Field(default="")  # env PAAS_ENV; "" | "prod" | "stg"
    # Dev/local opt-out for the F6 startup guard: allow an empty PAAS_ENV to boot
    # (single-env dev has no cross-env collision, so an omitted label is safe). Real
    # prod/stg NEVER set this (they set PAAS_ENV). The guard additionally refuses to
    # honor the opt-out when NAMESPACE_PREFIX names a real env (contains stg/prod), so
    # a real overlay that both forgot PAAS_ENV *and* set this can't silently re-open
    # the hole.
    allow_unset_paas_env: bool = Field(default=False)  # env ALLOW_UNSET_PAAS_ENV (dev only)

    @field_validator("paas_env")
    @classmethod
    def _validate_paas_env(cls, v: str) -> str:
        allowed = {"", "prod", "stg"}
        if v not in allowed:
            raise ValueError(
                f"PAAS_ENV must be one of {sorted(allowed)} (exact, lowercase, "
                f"no surrounding whitespace); got {v!r}"
            )
        return v

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )


# --- F6 (#279) startup env guard ----------------------------------------------
# The workspace int id baked into the #279 NetworkPolicy labels is per-Odoo-DB, NOT
# cluster-unique, and prod+stg share ONE woow-k3s cluster. So a tenant namespace whose
# #279 labels omit / carry the wrong paas.woowtech.io/env would let a stg agent reach a
# prod CloudService of the same numeric workspace id (cross-env tenant leak). This guard
# fails the operator fast at startup on a PAAS_ENV misconfig, rather than silently
# stamping colliding labels once O4 makes those labels load-bearing.


def _env_hint_from_prefix(namespace_prefix: str) -> str | None:
    """Independent env hint derived from NAMESPACE_PREFIX, used ONLY to cross-check
    PAAS_ENV in :func:`assert_env_guard` (never as the stamp source — that is a
    separate field by design). Grounded in the live operator deployments:
    ``paas-ws-`` = prod, ``paas-ws-stg-`` = stg, ``paas-ws-test*`` = integration/dev.

    Returns ``'dev' | 'stg' | 'prod' | None`` (None = genuinely ambiguous). dev is
    checked first so ``paas-ws-test-op-`` is never mistaken for a real env; the bare
    ``paas-ws-`` default maps to prod (the prod deployment's actual prefix).
    """
    p = namespace_prefix.lower()
    if "test" in p or "dev" in p:
        return "dev"
    if "stg" in p:
        return "stg"
    if "prod" in p or p == "paas-ws-":
        return "prod"
    return None


def assert_env_guard(paas_env: str, namespace_prefix: str, allow_unset: bool) -> None:
    """F6 fail-fast on a PAAS_ENV misconfig that would produce cross-env-colliding
    #279 NP labels. Raises :class:`RuntimeError` to abort boot; returns None if OK.

    - PRESENCE: an empty PAAS_ENV on a real deploy is fail-open → abort, UNLESS the
      dev/local opt-out (``allow_unset``) is set. The opt-out is itself refused when
      NAMESPACE_PREFIX explicitly names a real env (contains ``stg``/``prod``), so a
      real overlay that both forgot PAAS_ENV and set the opt-out can't re-open the hole.
      (Bare ``paas-ws-`` is tolerated on the opt-out path because local/dev commonly
      runs the default prefix — see the documented residual for prod-bare + opt-out.)
    - CORRECTNESS: a set PAAS_ENV that contradicts the prefix's independent env hint
      (e.g. PAAS_ENV=prod on a ``paas-ws-stg-`` deploy, or PAAS_ENV=stg on the bare
      prod prefix) is an env/prefix swap → abort.
    """
    if not paas_env:
        if not allow_unset:
            raise RuntimeError(
                "PAAS_ENV is empty. On the shared prod/stg cluster an env-less #279 "
                "tenant-isolation label collides workspace ids across environments "
                "(cross-env leak). Set PAAS_ENV=prod|stg, or ALLOW_UNSET_PAAS_ENV=true "
                "for dev/local (single-env, no collision)."
            )
        p = namespace_prefix.lower()
        if "stg" in p or "prod" in p:
            raise RuntimeError(
                f"ALLOW_UNSET_PAAS_ENV is set but NAMESPACE_PREFIX={namespace_prefix!r} "
                f"names a real environment — refusing to disable the F6 env guard on a "
                f"real deploy. Set PAAS_ENV explicitly instead."
            )
        return  # genuine dev/local: empty env + non-real-named prefix
    hint = _env_hint_from_prefix(namespace_prefix)
    if hint and hint != "dev" and hint != paas_env:
        raise RuntimeError(
            f"PAAS_ENV={paas_env!r} contradicts NAMESPACE_PREFIX={namespace_prefix!r} "
            f"(env hint {hint!r}) — an env/prefix swap would stamp #279 labels for the "
            f"wrong environment (cross-env leak). Refusing to start."
        )


settings = Settings()
