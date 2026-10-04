"""#1415：叢集層的唯讀查詢——目前只有儲存（Longhorn）排程額度。

平台 monitor cron 定時呼叫 ``GET /api/cluster/storage-capacity``，在額度快滿時告警給系統
管理員（不必等到租戶開通卡住）。全部唯讀；運算在 ``src/services/storage_capacity.py``。
"""
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Query

from src.services.helm import KubectlException, KubernetesService
from src.services.storage_capacity import (
    CapacityDataError,
    LonghornCluster,
    capacity_report,
    parse_storage_classes,
    summarize_orphans,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/cluster", tags=["cluster"])

k8s_service = KubernetesService()


@router.get(
    "/storage-capacity",
    summary="Longhorn scheduling capacity per node, largest placeable volume, orphaned PVCs",
)
def storage_capacity(
    storage_class: Optional[str] = Query(
        None, max_length=253,
        description="StorageClass to size the largest placeable volume for "
                    "(default: the cluster default StorageClass)",
    ),
) -> Dict[str, Any]:
    """一律 200，帶 ``available``／``reason``：讀不到 Longhorn 是「這個叢集沒有／看不到」的
    狀態，交給呼叫端決定要不要提示，不是伺服器錯誤（形制同 ``/api/analytics/traffic``）。

    ``reason``：``longhorn_unreadable``（CRD 不存在、RBAC、逾時）、``unexpected_data``、
    ``no_longhorn_nodes``。孤兒 PVC 讀不到時 ``orphaned_pvcs`` 為 null，其餘照常回報。
    """
    try:
        snapshot = k8s_service.get_longhorn_capacity_snapshot()
    except (KubectlException, ValueError) as exc:
        logger.warning("Storage capacity report: Longhorn unreadable: %s",
                       getattr(exc, "stderr", None) or exc)
        return {"available": False, "reason": "longhorn_unreadable"}
    try:
        cluster = LonghornCluster.from_snapshot(snapshot["nodes"], snapshot["settings"])
        classes, default_class = parse_storage_classes(snapshot["storage_classes"])
    except CapacityDataError as exc:
        logger.warning("Storage capacity report: unexpected Longhorn data: %s", exc)
        return {"available": False, "reason": "unexpected_data"}
    if not cluster.nodes:
        return {"available": False, "reason": "no_longhorn_nodes"}
    try:
        orphans = summarize_orphans(
            k8s_service.list_orphaned_pvcs_all_namespaces(), classes, cluster.settings,
        )
    except (KubectlException, ValueError) as exc:
        logger.warning("Storage capacity report: orphaned PVCs unreadable: %s",
                       getattr(exc, "stderr", None) or exc)
        orphans = None
    return capacity_report(cluster, classes, default_class, storage_class, orphans)
