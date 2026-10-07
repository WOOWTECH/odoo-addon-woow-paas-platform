"""API endpoints for namespace management."""
import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, status

from src.models.schemas import (
    BaselineNetpolBackfillRequest,
    NamespaceCreateRequest,
    NamespaceQuotaUpdateRequest,
)
from src.services.helm import KubernetesService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/namespaces", tags=["namespaces"])

k8s_service = KubernetesService()


@router.post(
    "",
    response_model=Dict[str, str],
    status_code=status.HTTP_201_CREATED,
    summary="Create a namespace with resource quota",
)
async def create_namespace(request: NamespaceCreateRequest):
    """Create a new Kubernetes namespace with resource quota.

    The namespace must start with 'paas-ws-' prefix for security.

    Args:
        request: Namespace creation parameters

    Returns:
        Creation confirmation message

    Raises:
        HTTPException: If namespace creation fails
    """
    try:
        result = k8s_service.create_namespace(
            name=request.name,
            cpu_limit=request.cpu_limit,
            memory_limit=request.memory_limit,
            storage_limit=request.storage_limit,
            workspace_id=request.workspace_id,
            ephemeral_storage_request=request.ephemeral_storage_request,
            ephemeral_storage_limit=request.ephemeral_storage_limit,
        )
        logger.info(f"Created namespace {request.name} with quota")
        return result

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except Exception as e:
        logger.error(f"Failed to create namespace: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create namespace: {str(e)}",
        )


@router.put(
    "/{name}/quota",
    response_model=Dict[str, str],
    summary="#278 A1 — 整組 hard overwrite ns ResourceQuota",
)
async def update_namespace_quota(name: str, request: NamespaceQuotaUpdateRequest):
    """整組覆寫既有 ns 的 ResourceQuota（值由 Odoo 依存活 service 集算好傳入）。"""
    try:
        result = k8s_service.update_namespace_quota(
            name, request.cpu_limit, request.memory_limit, request.storage_limit,
            ephemeral_storage_request=request.ephemeral_storage_request,
            ephemeral_storage_limit=request.ephemeral_storage_limit,
        )
        logger.info(f"Updated quota for namespace {name}")
        return result
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to update quota for {name}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update quota: {str(e)}",
        )


@router.post(
    "/{name}/baseline-netpol",
    response_model=Dict[str, Any],
    summary="#279 P5 backfill — apply baseline NP bundle + re-stamp managed labels",
)
async def backfill_baseline_netpol(name: str, request: BaselineNetpolBackfillRequest):
    """Idempotent backfill for a PRE-EXISTING CloudService namespace: apply the O4
    baseline NetworkPolicy bundle and re-stamp its managed labels (paas-managed + env
    + workspace-id) so leg-1 egress's namespaceSelector and the baseline NPs work on
    namespaces created before O4/O1. Safe to re-run.

    #1441：同一條路徑也回填 ``paas-baseline-egress`` 與 Pod Security 標籤——各自只在
    TENANT_BASELINE_EGRESS_ENABLED／TENANT_POD_SECURITY_ENABLED 打開時才套。"""
    try:
        pod_security = k8s_service.apply_pod_security(name)
        k8s_service._apply_baseline_netpol(name, request.workspace_id)
        labels = k8s_service.stamp_managed_ns_labels(
            name, workspace_id=request.workspace_id
        )
        logger.info("P5 baseline-netpol backfilled on %s (ws=%s)",
                    name, request.workspace_id)
        return {"action": "baseline-netpol-applied", "namespace": name,
                "labels": labels, "pod_security_labels": pod_security}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error("P5 baseline-netpol backfill failed on %s: %s", name, e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"baseline-netpol backfill failed: {str(e)}",
        )
