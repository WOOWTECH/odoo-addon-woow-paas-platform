"""API endpoints for namespace management."""
import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, status

from src.models.schemas import (
    BaselineNetpolBackfillRequest,
    NamespaceCreateRequest,
    NamespaceQuotaUpdateRequest,
)
from src.services.helm import KubectlException, KubernetesService, validate_namespace
from src.services.namespace_quota import (
    QUOTA_UNREADABLE, managed_quota_report, quota_report, unavailable_report,
)

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


# 一般 ``def``（不是 async）：FastAPI 會把它丟進 threadpool 跑，阻塞的 kubectl 不會卡住
# event loop。同檔其他端點是 async 卻直接呼叫阻塞的 kubectl——那是既有形狀，不要照抄。
@router.get("/{name}/quota", summary="#1647 — read ns ResourceQuota hard/used (read-only)")
def get_namespace_quota(name: str, include_managed: bool = False) -> Dict[str, Any]:
    """#1647：namespace ResourceQuota 的上限與已佔用量（純唯讀）。

    一律回 200：拿得到時 ``available`` 為 True、``resources`` 以 k8s 資源鍵為 key；沒有
    quota（含 namespace 不存在）或讀不到時 ``available`` 為 False 並帶 ``reason``——工作區
    頁開頁時同步呼叫，「讀不到」不該變成一個要呼叫端另外處理的 5xx。只有 namespace 不在
    允許前綴內才回 400。回應形狀見 ``namespace_quota.quota_report``。
    """
    # 兩段 try 不可合併：json.JSONDecodeError 是 ValueError 的子類，混在一起會把「kubectl
    # 輸出解析失敗」誤回成 400（那是 namespace 前綴不合專用的）。
    try:
        validate_namespace(name)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    try:
        # timeout 常數經 class 取、不經 k8s_service：單元測試會把 k8s_service 整個換成 mock。
        items = k8s_service.get_resource_quotas(
            name, timeout=KubernetesService.NS_QUOTA_READ_TIMEOUT_SECONDS)
    except (KubectlException, ValueError) as exc:
        logger.warning("Namespace quota unreadable for %s: %s", name,
                       getattr(exc, "stderr", None) or exc)
        report = unavailable_report(name, QUOTA_UNREADABLE)
        if include_managed:
            report["managed_quota"] = managed_quota_report(name, [], reason=QUOTA_UNREADABLE)
        return report
    report = quota_report(name, items)
    if include_managed:
        managed = managed_quota_report(name, items)
        if managed["reason"] == "managed_quota_not_found":
            # #1679：空 list 不能證明 namespace 不存在；另做一次明確探測，
            # 只有 NotFound 才回 False，其他失敗維持 None（未知）。
            managed["namespace_exists"] = k8s_service.namespace_exists(name)
        report["managed_quota"] = managed
    return report


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
