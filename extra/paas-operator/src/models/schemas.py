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

    revision: Optional[int] = Field(None, description="Target revision (0 = previous)")


class NamespaceCreateRequest(BaseModel):
    """Request to create a namespace with resource quota."""

    name: str = Field(..., description="Namespace name (must start with paas-ws-)")
    cpu_limit: str = Field(default="2", description="CPU limit (e.g., '2' or '2000m')")
    memory_limit: str = Field(default="4Gi", description="Memory limit")
    storage_limit: str = Field(default="20Gi", description="Storage limit")


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


class HealthResponse(BaseModel):
    """Health check response."""

    status: str
    timestamp: datetime
    helm_version: Optional[str] = None


class ErrorResponse(BaseModel):
    """Error response."""

    detail: str
    error_type: Optional[str] = None
