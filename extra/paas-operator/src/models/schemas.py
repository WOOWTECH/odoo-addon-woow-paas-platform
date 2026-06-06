"""Pydantic schemas for API requests and responses."""
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


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
    reset_values: bool = Field(
        default=False, description="Reset values to chart defaults"
    )
    reuse_values: bool = Field(
        default=True, description="Reuse last release values"
    )


class ReleaseRollbackRequest(BaseModel):
    """Request to rollback a Helm release."""

    revision: Optional[int] = Field(None, description="Target revision number. If not provided, rolls back to the previous revision.")


class NamespaceCreateRequest(BaseModel):
    """Request to create a namespace with resource quota."""

    name: str = Field(..., description="Namespace name (must start with paas-ws-)")
    cpu_limit: str = Field(default="2", description="CPU limit (e.g., '2' or '2000m')")
    memory_limit: str = Field(default="4Gi", description="Memory limit")
    storage_limit: str = Field(default="20Gi", description="Storage limit")


class TunnelCreateRequest(BaseModel):
    """Request to create a new dedicated Cloudflare Tunnel."""

    name: str = Field(
        ...,
        description="Tunnel name (e.g., 'smarthome-livingroom')",
    )
    hostname: str = Field(
        ...,
        description="Public hostname for the tunnel (e.g., 'ha.example.com')",
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
    hostname: str = Field(..., description="Configured public hostname")
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
    """Request to re-render a pod's NetworkPolicy egress allowlist."""

    project_workspace_ids: List[int] = Field(default_factory=list)


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

    routes: List[TunnelRouteSpec] = Field(..., min_length=1)
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
