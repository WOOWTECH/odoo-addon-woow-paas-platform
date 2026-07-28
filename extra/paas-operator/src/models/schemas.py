"""Pydantic schemas for API requests and responses."""
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class ReleaseStatus(str, Enum):
    """Helm release status."""

    DEPLOYED = "deployed"
    FAILED = "failed"
    PENDING_INSTALL = "pending-install"
    PENDING_UPGRADE = "pending-upgrade"
    PENDING_ROLLBACK = "pending-rollback"
    SUPERSEDED = "superseded"
    UNINSTALLED = "uninstalled"
    UNINSTALLING = "uninstalling"
    UNKNOWN = "unknown"


class PodPhase(str, Enum):
    """Kubernetes pod phase."""

    PENDING = "Pending"
    RUNNING = "Running"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    UNKNOWN = "Unknown"


# Request Models


class ExposeConfig(BaseModel):
    """Configuration for exposing a service via Cloudflare Tunnel."""

    enabled: bool = Field(default=False, description="Enable public access via Cloudflare Tunnel")
    subdomain: Optional[str] = Field(
        None,
        description="Custom subdomain (auto-generated if not provided)",
    )
    service_name: Optional[str] = Field(
        None,
        description="K8s service name to expose (auto-detected if not provided)",
    )
    service_port: Optional[int] = Field(
        None,
        description="Service port to expose (auto-detected if not provided)",
    )


class ReleaseCreateRequest(BaseModel):
    """Request to create a new Helm release."""

    namespace: str = Field(..., description="Target namespace for the release")
    name: str = Field(..., description="Release name")
    chart: str = Field(..., description="Chart reference (repo/chart or path)")
    version: Optional[str] = Field(None, description="Chart version")
    values: Optional[Dict[str, Any]] = Field(
        default_factory=dict, description="Helm values override"
    )
    sensitive_values: Optional[Dict[str, Any]] = Field(
        default_factory=dict,
        description="Sensitive Helm values merged in memory only",
    )
    create_namespace: bool = Field(
        default=False, description="Create namespace if not exists"
    )
    expose: Optional[ExposeConfig] = Field(
        default=None,
        description="Configuration for exposing service via Cloudflare Tunnel",
    )


class ReleaseUpgradeRequest(BaseModel):
    """Request to upgrade an existing Helm release."""

    chart: Optional[str] = Field(None, description="Chart reference (if changing)")
    version: Optional[str] = Field(None, description="Chart version")
    values: Optional[Dict[str, Any]] = Field(
        default_factory=dict, description="Helm values override"
    )
    sensitive_values: Optional[Dict[str, Any]] = Field(
        default_factory=dict,
        description="Sensitive Helm values merged in memory only",
    )
    reset_values: bool = Field(
        default=False, description="Reset values to chart defaults"
    )
    reuse_values: bool = Field(
        default=True, description="Reuse last release values"
    )


class ReleaseRollbackRequest(BaseModel):
    """Request to rollback a Helm release."""

    revision: Optional[int] = Field(None, description="Target revision number. If not provided, rolls back to the previous revision.")


class ReleaseResetConfigRequest(BaseModel):
    """#357：reset admin_password。載體由 ``admin_password_carrier`` 分流（#449）。

    **config_json 路（預設，mcp-server 家族）**：外科式 read-modify-write
    ``/data/config.json``，只替換 ``admin_password`` leaf，保留租戶 GUI 自填的
    connection / tools / mcp_server.env / token_history。比「砍整份 + re-seed」
    少一次完整 PVC 重置；敏感值走 ``sensitive_values``，不落持久化 values；
    main container 不掛 Secret、僅 seed-config 一次性讀取 → 應用永遠讀 config.json。

    **helm_secret 路（hermes-agent / open-design）**：密碼的權威載體是 chart 自己
    的 Secret，pod 以 env / initContainer 讀取，**沒有** ``/data/config.json``。
    走純 helm upgrade + 全部 Deployment rollout restart，不做 config.json exec
    patch —— 對這些 chart 做 exec patch 必定失敗（#449：patch 炸 → 備份未建成 →
    rollback 也炸 → 500，密碼永遠輪替不了）。

    任一子步失敗 → operator 報 5xx、不做半套。chart values 經 --values tmp file
    不印明文，cmdline secret value 走既有 _redact_cmd 遮罩；config_json 路的
    密碼只進 pod stdin、不寫檔、不 log。
    """

    values: Optional[Dict[str, Any]] = Field(
        default_factory=dict,
        description="Helm values to merge with the existing release (包含新 "
        "admin_password 走 sensitive_values，不落持久化 values).",
    )
    sensitive_values: Optional[Dict[str, Any]] = Field(
        default_factory=dict,
        description="Sensitive Helm values merged in memory only",
    )
    chart: Optional[str] = Field(None, description="Chart reference（如需升版）")
    version: Optional[str] = Field(None, description="Chart version（如需升版）")
    # #411 A 案接通（#449 一併補上）：接通前 client 傳的 reset_values 被 pydantic
    # 靜默忽略，只靠 reuse_values=False 讓 helm 走「無-flag 預設」（chart
    # values.yaml 為 base ≈ reset）。接通後升級為顯式 --reset-values；兩者對
    # helm 的實際語意相同（helm 3 的 default == ResetValues），故對既有呼叫端
    # 是行為中性的向後相容改動。
    reset_values: bool = Field(
        default=False, description="Reset values to chart defaults（顯式 --reset-values）"
    )
    reuse_values: bool = Field(default=True, description="複用最近 release values")
    # #449：密碼載體策略。**預設 config_json ＝ 既有行為**，這是「operator 先發版、
    # 平台後發版」順序安全的支點：舊平台不帶本欄 → 走原 saga，零行為變化。
    admin_password_carrier: Literal["config_json", "helm_secret"] = Field(
        default="config_json",
        description=(
            "密碼載體：config_json＝PVC 內 /data/config.json（mcp-server 家族，"
            "既有 saga）；helm_secret＝chart Secret（hermes-agent / open-design，"
            "純 helm upgrade + rollout restart）。未帶＝config_json。"
        ),
    )


class NamespaceCreateRequest(BaseModel):
    """Request to create a namespace with resource quota."""

    name: str = Field(..., description="Namespace name (must start with paas-ws-)")
    cpu_limit: str = Field(default="2", description="CPU limit (e.g., '2' or '2000m')")
    memory_limit: str = Field(default="4Gi", description="Memory limit")
    storage_limit: str = Field(default="20Gi", description="Storage limit")
    workspace_id: Optional[int] = Field(
        default=None,
        description="Odoo workspace id of the owning CloudService; stamped as the "
        "woow.tw/workspace-id NetworkPolicy label (#279 leg-1/leg-2 tenant isolation)",
    )


class NamespaceQuotaUpdateRequest(BaseModel):
    """#278 A1：整組覆寫 ns ResourceQuota（值由 Odoo 依存活 service 集算好傳入）。"""

    cpu_limit: str = Field(..., description="requests.cpu / limits.cpu")
    memory_limit: str = Field(..., description="requests.memory / limits.memory, e.g. 8Gi")
    storage_limit: str = Field(..., description="requests.storage, e.g. 100Gi")


class BaselineNetpolBackfillRequest(BaseModel):
    """#279 P5 backfill: re-apply the baseline NP bundle + re-stamp managed labels on
    a PRE-EXISTING CloudService namespace created before O4/O1."""

    workspace_id: int = Field(
        ..., description="Odoo workspace id of the owning CloudService (required — the "
        "baseline NP4 + leg-1 egress selectors key on woow.tw/workspace-id)",
    )


class RestampLabelsRequest(BaseModel):
    """#279 P5 backfill: re-stamp managed labels (paas-managed + env + optional
    workspace-id / reach-ws-*) on a pre-existing namespace — add-only (never deletes a
    reach label). Chiefly adds the env label a legacy agent ns lacks so it matches
    NP4's env predicate once #280 removes the chart from-less."""

    workspace_id: Optional[int] = Field(default=None)
    reach_workspace_ids: List[int] = Field(default_factory=list)


class TunnelCreateRequest(BaseModel):
    """Request to create a new dedicated Cloudflare Tunnel."""

    name: str = Field(
        ...,
        description="Tunnel name (e.g., 'smarthome-livingroom')",
    )
    hostname: Optional[str] = Field(
        None,
        description="Public hostname for the tunnel. Omit to create a bare "
        "tunnel (catch-all 404 ingress, no DNS record).",
    )
    service_url: str = Field(
        default="http://localhost:8123",
        description="Backend service URL the tunnel forwards to",
    )


# Response Models


class PodInfo(BaseModel):
    """Information about a pod."""

    name: str
    phase: PodPhase
    ready: str  # e.g., "1/1"
    restarts: int
    age: str


class VmiInfo(BaseModel):
    """KubeVirt VirtualMachineInstance status (#423 W4.1).

    ``ready`` is the VMI's ``Ready`` condition, NOT ``phase == Running``:
    a VMI reaches Running as soon as QEMU starts, while Ready additionally
    reflects that the virt-launcher probes are satisfied. The platform's
    readiness gate (Odoo ``workload_type == 'vm'`` branch) requires BOTH —
    see design §4.1.
    """

    name: str
    phase: str  # Pending / Scheduling / Scheduled / Running / Succeeded / Failed
    ready: bool
    node_name: Optional[str] = None
    # HAOS 18.1 ships qemu-guest-agent (W1 PoC R9). Not load-bearing for v1
    # readiness, but surfacing it lets the platform tell "QEMU up" from
    # "guest OS up" without another API round-trip.
    agent_connected: bool = False


class DataVolumeInfo(BaseModel):
    """CDI DataVolume status (#423 W4.1).

    ``progress`` is CDI's own import percentage string (e.g. ``"42.31%"``)
    or ``"N/A"`` before the importer starts. Surfaced so the UI can show a
    real progress bar during the long first-provision import instead of an
    opaque ``deploying`` (design §7.4).
    """

    name: str
    phase: str  # Pending / ImportScheduled / ImportInProgress / Succeeded / Failed
    progress: Optional[str] = None


class ReleaseRevision(BaseModel):
    """Helm release revision information."""

    revision: int
    updated: str
    status: ReleaseStatus
    chart: str
    app_version: str
    description: str


class RouteInfo(BaseModel):
    """Information about a Cloudflare Tunnel route."""

    hostname: str = Field(..., description="Public hostname (e.g., myapp.example.com)")
    service_url: str = Field(..., description="Internal service URL")
    enabled: bool = Field(default=True, description="Whether the route is active")


class ReleaseInfo(BaseModel):
    """Detailed information about a Helm release."""

    name: str
    namespace: str
    revision: int
    status: ReleaseStatus
    chart: str
    app_version: str
    updated: str
    description: Optional[str] = None
    values: Optional[Dict[str, Any]] = None
    route: Optional[RouteInfo] = Field(None, description="Cloudflare Tunnel route info if exposed")


class ReleaseStatusResponse(BaseModel):
    """Release status including pod information."""

    release: ReleaseInfo
    pods: List[PodInfo]
    pod_retrieval_error: Optional[str] = Field(
        None, description="Error message if pod retrieval failed"
    )
    # ---- #423 W4.2: VM-type workloads (opt-in via ?include_vm=true) --------
    # Default [] (not None) so existing clients that ignore these fields see
    # no behaviour change, and VM-aware clients can treat "empty" uniformly
    # regardless of whether they asked for it.
    vmis: List[VmiInfo] = Field(
        default_factory=list,
        description="KubeVirt VMIs for this release (only populated when include_vm=true)",
    )
    datavolumes: List[DataVolumeInfo] = Field(
        default_factory=list,
        description="CDI DataVolumes for this release (only populated when include_vm=true)",
    )
    vm_retrieval_error: Optional[str] = Field(
        None,
        description=(
            "Error message if VMI/DataVolume retrieval failed. Mirrors "
            "pod_retrieval_error: the caller MUST NOT read empty vmis as "
            "'no VM' while this is set — see the fail-closed rule in design §4.1."
        ),
    )


class ReleaseListResponse(BaseModel):
    """List of Helm releases."""

    releases: List[ReleaseInfo]


class ReleaseRevisionsResponse(BaseModel):
    """List of release revisions."""

    revisions: List[ReleaseRevision]


class NamespaceInfo(BaseModel):
    """Information about a namespace."""

    name: str
    status: str
    created: str
    labels: Optional[Dict[str, str]] = None


class TunnelConnectionInfo(BaseModel):
    """Connection details for a Cloudflare Tunnel connector."""

    connector_id: str = Field(..., description="Connector ID")
    type: str = Field(default="", description="Connection type")
    origin_ip: str = Field(default="", description="Origin IP address")
    opened_at: str = Field(default="", description="Connection opened timestamp")


class TunnelCreateResponse(BaseModel):
    """Response after creating a dedicated Cloudflare Tunnel."""

    tunnel_id: str = Field(..., description="Cloudflare Tunnel ID")
    tunnel_name: str = Field(..., description="Tunnel name")
    tunnel_token: str = Field(..., description="Tunnel token for cloudflared")
    hostname: Optional[str] = Field(
        None, description="Configured public hostname (None for a bare tunnel)"
    )
    dns_record_id: Optional[str] = Field(
        None, description="DNS record ID if created"
    )
    dns_failed: bool = Field(
        False,
        description=(
            "True when DNS was enabled but the record creation failed, so "
            "dns_record_id is None for an error reason (vs None because DNS "
            "is intentionally disabled). Lets the caller distinguish the two "
            "and alert/re-sync rather than silently treating the tunnel as "
            "fully reachable."
        ),
    )


class TunnelStatusResponse(BaseModel):
    """Status information for a Cloudflare Tunnel."""

    tunnel_id: str = Field(..., description="Cloudflare Tunnel ID")
    name: str = Field(..., description="Tunnel name")
    status: str = Field(..., description="Tunnel status (e.g., 'healthy', 'inactive')")
    connections: List[TunnelConnectionInfo] = Field(
        default_factory=list, description="Active tunnel connections"
    )
    created_at: Optional[str] = Field(
        None, description="Tunnel creation timestamp"
    )


class TunnelTokenResponse(BaseModel):
    """Response containing a tunnel token."""

    tunnel_id: str = Field(..., description="Cloudflare Tunnel ID")
    token: str = Field(..., description="Tunnel token for cloudflared")


class HealthResponse(BaseModel):
    """Health check response."""

    status: str
    timestamp: datetime
    helm_version: Optional[str] = None


class SidecarContainer(BaseModel):
    """Sidecar container specification for deployment patching."""

    name: str = Field(..., description="Container name")
    image: str = Field(..., description="Container image")
    ports: Optional[List[Dict[str, Any]]] = Field(
        default=None, description="Container ports"
    )
    env: Optional[List[Dict[str, Any]]] = Field(
        default=None, description="Environment variables"
    )
    resources: Optional[Dict[str, Any]] = Field(
        default=None, description="Resource requests/limits"
    )
    liveness_probe: Optional[Dict[str, Any]] = Field(
        default=None,
        alias="livenessProbe",
        description="Liveness probe configuration",
    )
    readiness_probe: Optional[Dict[str, Any]] = Field(
        default=None,
        alias="readinessProbe",
        description="Readiness probe configuration",
    )

    model_config = {"populate_by_name": True}


class SidecarPatchRequest(BaseModel):
    """Request to patch a deployment with a sidecar container."""

    deployment_name: Optional[str] = Field(
        None,
        description="Deployment name to patch (auto-detected from release if not provided)",
    )
    container: SidecarContainer = Field(
        ..., description="Sidecar container specification"
    )


class SidecarPatchResponse(BaseModel):
    """Response from sidecar patch operation."""

    message: str
    deployment: str
    namespace: str
    container_name: str
    mcp_service_name: Optional[str] = Field(
        None, description="Name of the K8s Service created for the sidecar"
    )
    mcp_endpoint_url: Optional[str] = Field(
        None, description="Public URL to reach the MCP sidecar endpoint (via Cloudflare)"
    )
    mcp_internal_url: Optional[str] = Field(
        None, description="Internal K8s URL for the MCP sidecar service"
    )


class N8nInitRequest(BaseModel):
    """Request to initialize an n8n instance after deployment."""

    owner_email: str = Field(
        default="admin@woowtech.io",
        description="Email for the n8n owner user",
    )
    owner_password: str = Field(
        ...,
        description="Password for the n8n owner user",
    )


class N8nInitResponse(BaseModel):
    """Response from n8n initialization."""

    success: bool
    api_key: Optional[str] = Field(None, description="Generated n8n API key (JWT)")
    owner_email: Optional[str] = None
    secret_patched: bool = Field(default=False, description="Whether the K8s Secret was updated with the real API key")
    pod_restarted: bool = Field(default=False, description="Whether the deployment was restarted to pick up the new API key")
    message: Optional[str] = None
    error: Optional[str] = None


class OdooInitResponse(BaseModel):
    """Response from Odoo module list initialization."""

    success: bool
    modules_added: int = Field(default=0, description="Number of new modules discovered")
    modules_updated: int = Field(default=0, description="Number of existing modules updated")
    message: Optional[str] = None
    error: Optional[str] = None


# --- Odoo Module Management ---


class OdooModuleStatesRequest(BaseModel):
    """Request to query Odoo module installation states."""

    module_names: List[str] = Field(..., description="List of module technical names to check")


class OdooModuleStateInfo(BaseModel):
    """Installation state and reverse dependencies for a single module."""

    state: str = Field(..., description="Module state: installed, uninstalled, uninstallable, etc.")
    dependents: List[str] = Field(default_factory=list, description="Installed modules that depend on this one")


class OdooModuleStatesResponse(BaseModel):
    """Response with module states."""

    success: bool
    modules: Dict[str, OdooModuleStateInfo] = Field(default_factory=dict)
    error: Optional[str] = None




class ErrorResponse(BaseModel):
    """Error response."""

    detail: str
    error_type: Optional[str] = None


# Agent Runtime (per-project pod) — design §2.11


class SpawnOrAttachRequest(BaseModel):
    """Request to spawn / attach / resume a project's agent-runtime pod."""

    project_hash: str = Field(..., description="K8s namespace hash (per-project)")
    project_id: int = Field(..., description="Authoritative project id (token claim)")
    channel_id: int = Field(..., description="Invoking discuss.channel id")
    invoker_user_id: int = Field(..., description="Invoking res.users id")
    invoker_role: str = Field(..., description="Invoker project_access role")
    project_workspace_ids: List[int] = Field(
        default_factory=list,
        description="Workspace ids the pod's NetworkPolicy egress may reach",
    )
    agent_template_snapshot: Dict[str, Any] = Field(default_factory=dict)
    mcp_snapshot: Dict[str, Any] = Field(default_factory=dict)
    skill_snapshot: List[Dict[str, Any]] = Field(default_factory=list)
    # F-PILOT-8 — per-tenant LLM credential (design §8.2). The operator never
    # queries the Odoo DB, so the tenant's ai.config api_key (+ optional model
    # config) rides the spawn request and the operator provisions it as a
    # per-release Secret. NB the model-config field is `llm_model_config`, NOT
    # `model_config` — the latter is a pydantic-reserved BaseModel attribute.
    api_key: Optional[str] = Field(
        default=None,
        description="Per-tenant LLM api_key, provisioned as a per-release Secret",
    )
    llm_model_config: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional LLM model config (model / temperature / ...)",
    )


class ResumeRequest(BaseModel):
    """Optional body for the standalone resume endpoint (re-sign token)."""

    project_id: Optional[int] = None
    project_workspace_ids: List[int] = Field(default_factory=list)


class InvalidateMcpRequest(BaseModel):
    """Request to invalidate a pod's mcp.json ConfigMap snapshot."""

    mcp_snapshot: Dict[str, Any] = Field(default_factory=dict)


class InvalidateSkillsRequest(BaseModel):
    """Request to invalidate a pod's skills.json ConfigMap snapshot.

    Symmetric with :class:`InvalidateMcpRequest`; the snapshot is the
    list[{name, version, sha256, description}] the Odoo controller builds.
    """

    skill_snapshot: List[Dict[str, Any]] = Field(default_factory=list)


class SyncWorkspacesRequest(BaseModel):
    """Request to re-render a project's tenant-isolation legs (#279 O5).

    leg-1 = pod NetworkPolicy egress allowlist (helm upgrade);
    leg-2 = agent-ns ``reach-ws-*`` labels (O3 reconcile primitive).
    """

    project_workspace_ids: List[int] = Field(default_factory=list)
    # GRANT path sets True: if the pod's helm release is gone, leg-1 egress can't be
    # applied, so the operator MUST fail-loud (409) instead of returning 200 after only
    # leg-2 — otherwise the caller commits a non-functional grant + an orphan reach
    # label. REVOKE leaves it False (a revoke against a gone pod is a no-op success).
    require_release: bool = Field(default=False)


# Security Access — multi-route dedicated tunnel sync (security-access P1)


class TunnelRouteSpec(BaseModel):
    """One desired ingress route on a dedicated tunnel."""

    subdomain: str = Field(..., description="Subdomain under the platform zone")
    service_url: str = Field(..., description="Backend service URL or http_status:NNN")
    http_host_header: bool = Field(
        True, description="Send the public hostname as HTTP Host header to origin"
    )


class TunnelRoutesSyncRequest(BaseModel):
    """Declarative full sync of a dedicated tunnel's routes."""

    routes: List[TunnelRouteSpec] = Field(..., min_length=0)
    comment: Optional[str] = Field(
        None, description="Canonical name stamped on DNS records for traceability"
    )


class TunnelRouteSyncResult(BaseModel):
    subdomain: str
    hostname: str
    dns_record_id: Optional[str] = None


class TunnelRoutesSyncResponse(BaseModel):
    tunnel_id: str
    routes: List[TunnelRouteSyncResult]


# Security Access — Cloudflare Access (Zero Trust) sync (security-access P2)


class AccessDecision(str, Enum):
    """Cloudflare Access policy decision. ``bypass`` skips authentication and
    does not support identity-based selectors (enforced below)."""

    ALLOW = "allow"
    DENY = "deny"
    BYPASS = "bypass"


# Selectors whose rule_type carries no value (e.g. "everyone").
_VALUELESS_RULE_TYPES = {"everyone"}
# Identity-based selectors a bypass policy may not use (Cloudflare rejects them).
_IDENTITY_RULE_TYPES = {"email_domain", "email_list"}


class AccessRuleSpec(BaseModel):
    """One selector rule inside a policy group (include / require / exclude).

    rule_value is per-type: domain (no @) / comma-separated emails / CIDR /
    comma-separated ISO-2 country codes; empty for ``everyone``.
    """

    rule_type: str = Field(
        ...,
        description="email_domain | email_list | ip_range | everyone | country",
    )
    rule_value: Optional[str] = Field(None, description="Selector value per type")

    @field_validator("rule_value", mode="before")
    @classmethod
    def _normalize_empty(cls, v):
        # Odoo reads an unset Char field back as Python ``False``; a valueless
        # selector (e.g. everyone) therefore arrives as JSON ``false``. Coerce
        # any falsy value (False / "" / None) to None so it is accepted instead
        # of 422'd as a non-string — defense in depth with the Odoo-side
        # normalization in _rules_by_group.
        return v or None


class AccessPolicySpec(BaseModel):
    """One desired Access policy (reusable) on an SA's Access App.

    Mirrors Cloudflare's native policy shape: include (OR) / require (AND) /
    exclude (NOT) selector groups + a decision. At least one Include rule is
    required; bypass policies may not carry identity-based selectors.
    """

    name: str = Field(..., description="Policy display name")
    decision: AccessDecision = Field(..., description="allow | deny | bypass")
    include: List[AccessRuleSpec] = Field(
        default_factory=list, description="OR group; at least one required"
    )
    require: List[AccessRuleSpec] = Field(
        default_factory=list, description="AND group"
    )
    exclude: List[AccessRuleSpec] = Field(
        default_factory=list, description="NOT group"
    )
    precedence: int = Field(1, description="Evaluation order; lower first")

    @model_validator(mode="after")
    def _validate_groups(self) -> "AccessPolicySpec":
        if not self.include:
            raise ValueError(
                "Access policy must contain at least one include rule")
        if self.decision == AccessDecision.BYPASS:
            for group in (self.include, self.require, self.exclude):
                if any(r.rule_type in _IDENTITY_RULE_TYPES for r in group):
                    raise ValueError(
                        "Bypass policies cannot use identity-based selectors "
                        "(email_domain / email_list)")
        return self


class AccessSyncRequest(BaseModel):
    """Declarative full-state sync of one SA's Cloudflare Access config.

    enabled=False → operator tears down the app + its reusable policies.
    """

    enabled: bool = Field(..., description="Master Access protection toggle")
    canonical_name: str = Field(
        ..., description="SA canonical name; stamped on app/policy names"
    )
    app_name: str = Field(..., description="Access Application display name")
    primary_hostname: str = Field(
        ..., description="App primary domain (SA primary hostname)"
    )
    protected_hostnames: List[str] = Field(
        default_factory=list,
        description="Hostnames to protect (app destinations); subset of routes",
    )
    session_duration: str = Field(
        "24h", description='Go duration string, e.g. "8h" / "24h"'
    )
    login_idp_types: List[str] = Field(
        default_factory=list,
        description='IdP types to allow, e.g. ["google", "onetimepin"]; '
                    "empty = all available",
    )
    policies: List[AccessPolicySpec] = Field(default_factory=list)
    existing_app_id: Optional[str] = Field(None)
    existing_policy_ids: Optional[List[str]] = Field(None)


class AccessSyncResponse(BaseModel):
    app_id: Optional[str] = Field(None, description="CF Access App id (None if disabled/torn down)")
    policy_ids: List[str] = Field(default_factory=list)
    allowed_idps: List[str] = Field(
        default_factory=list, description="Resolved IdP ids set on the app"
    )
    unresolved_idp_types: List[str] = Field(
        default_factory=list,
        description="Requested login methods with no matching account IdP "
                    "(silently dropped → login restriction widened to all IdPs)",
    )
    orphaned_policy_ids: List[str] = Field(
        default_factory=list,
        description="Old reusable policies that could not be deleted after the "
                    "app was repointed — permanent CF orphans to log/alert on",
    )


class IdentityProviderInfo(BaseModel):
    id: str
    name: str
    type: str


class IdentityProvidersResponse(BaseModel):
    identity_providers: List[IdentityProviderInfo]


# ==================== L4 reconciler（帳號級反向孤兒對帳）====================
# 設計 doc: docs/plans/2026-Q2/2026-06-25-l4-reconciler-design.md
# 首版「零自動刪除」：reconcilable / dry_run 的預設都偏向安全（不可回收 / dry-run）。


class OrphanKind(str, Enum):
    """L4 reconciler 反向發現的孤兒資源類別。enum 值是 operator↔Odoo wire 契約的一部分。"""

    TUNNEL = "tunnel"                  # cfd_tunnel（dedicated，CNAME 指自身）
    DNS_RECORD = "dns_record"          # zone DNS（指 shared / dedicated tunnel）
    ACCESS_APP = "access_app"          # Zero Trust Access application
    ACCESS_POLICY = "access_policy"    # reusable Access policy
    NAMESPACE = "namespace"            # k8s 租戶 namespace（Phase E）


class OrphanCandidate(BaseModel):
    """單筆疑似孤兒（dry-run inventory 的一列）。"""

    id: str = Field(..., description="CF / k8s 資源 id")
    name: str = Field(default="", description="顯示名（tunnel name / hostname / app name ...）")
    kind: OrphanKind = Field(..., description="資源類別")
    env_hint: str = Field(
        default="unknown", description="判讀的環境歸屬：prod / stg / unknown"
    )
    reconcilable: bool = Field(
        default=False,
        description="是否『結構上可被自動回收』。首版恆 False（dry-run-only、刪除權留人手）；"
                    "唯帶 owner marker 的資源未來可能升 True（仍受 DP-MARKER 等 gate）。",
    )
    reason: str = Field(
        default="", description="判定理由 / 人工審用說明（如 'no env token, manual only'）"
    )


class ReconcileRequest(BaseModel):
    """Odoo → operator：帶各類 stored-id 左集（白名單），請 operator 對 CF/k8s 右集取差集。"""

    stored_ids: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="Odoo 持有的 stored-id 左集，依 kind 分組"
                    "（如 {'tunnel': [...], 'dns_record': [...], 'access_app': [...]}）",
    )
    dry_run: bool = Field(
        default=True,
        description="首版恆 True；傳 False 由 endpoint 422 拒收（發現與刪除刻意分離）。",
    )


class ReconcileResponse(BaseModel):
    """operator → Odoo：dry-run 孤兒 inventory。"""

    dry_run: bool = Field(default=True, description="本次是否 dry-run（首版恆 True）")
    partial: bool = Field(
        default=False,
        description="completeness gate：任一類右集撈取失敗 → True，"
                    "且此時不得有任何 reconcilable=True 的候選。",
    )
    orphan_candidates: List[OrphanCandidate] = Field(
        default_factory=list, description="疑似孤兒清單（dry-run，供人工審）"
    )


# ── 方向 A：operator 唯讀 inventory wire（operator→Odoo 右集 ground truth）──────
# operator 對 CF 帳號級資源只做唯讀發現、回 raw 事實；差集 / 分類（env_hint /
# reconcilable / reason）/ 刪除全在 Odoo `src/services/reconciler.py`。故 operator 端
# wire 是未分類的 `InventoryResponse`，不是 `ReconcileResponse`（後者是 Odoo 端產出）。


class CfResource(BaseModel):
    """帳號級反向 inventory 的單筆 CF 資源（raw、未分類）。

    operator 只回『事實』（id / name / kind），**不**含 env_hint / reconcilable /
    reason — 那些是 Odoo reconciler.py 依 stored-id 左集 + 守衛規則算出的分類結果。
    """

    id: str = Field(..., description="CF 資源 id")
    name: str = Field(default="", description="顯示名（tunnel name / app name / policy name）")
    kind: OrphanKind = Field(..., description="資源類別")


class InventoryResponse(BaseModel):
    """operator → Odoo：帳號級 CF 資源 inventory（右集 ground truth，未分類、唯讀）。"""

    paas_env: str = Field(
        ...,
        description="operator 鎖定的環境（R3 env-pin 回拋，恆為 prod|stg；未鎖定時端點 503 不回此物件）",
    )
    shared_tunnel_id: str = Field(
        ...,
        description="operator 配置的 shared tunnel id（CLOUDFLARE_TUNNEL_ID，與 CloudflareService "
                    "同源）。Odoo reconciler 用它把 shared tunnel 本體標 protected（必出現在 "
                    "list_all_tunnels 右集、不在 SM/SA 左集 → 否則被誤判孤兒）。**取代**對完整 "
                    "32-char UUID 永不命中的 8-char 硬編 denylist。空值時 Odoo 端 fail-loud abort。",
    )
    partial: bool = Field(
        default=False,
        description="completeness gate：任一類 list-all 失敗 → True，該類資源缺漏。"
                    "下游差集**不得**把缺漏類當『無孤兒』，且 partial 時不得授權任何 reconcilable=True。",
    )
    failed_kinds: List[str] = Field(
        default_factory=list,
        description="撈取失敗的資源類別（partial 細節，供人工審看是哪一類沒撈到）",
    )
    resources: List[CfResource] = Field(
        default_factory=list, description="撈到的 CF 資源（未分類，右集 ground truth）"
    )
