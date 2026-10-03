"""#1348：開通前的工作區儲存配額檢查（PVC 數量與 requests.storage）。

背景：刪除服務時 PVC 依 #317 保留（標 ``paas.woowtech.io/orphaned-at``，由 pvc-cleanup
cron 過了保留期才刪），但保留中的 PVC **仍計入** 工作區 namespace 的 ResourceQuota
（``persistentvolumeclaims`` 與 ``requests.storage``）。租戶刪開幾次就把配額吃滿，下一次
``helm install`` 在 PVC admission 被 ``exceeded quota`` 拒絕，平台只拿到泛用的
「Helm installation failed」——租戶看不出原因，也無法自助排除。

本模組只做**檢查與說明**，不改保留機制（把孤兒 PVC 排除於配額屬大設計，見 #1189、#396）：

* :func:`summarize` 把 ``kubectl get resourcequota``／``kubectl get pvc`` 的 JSON 收斂成
  一份用量摘要（含保留中的孤兒 PVC 數量、容量與最早釋放時間）；
* :func:`exhausted_resources` 判斷哪個儲存配額**已經**用滿（used ≥ hard）；
* :func:`build_quota_error_detail` 產生回給平台的結構化 ``detail``（``error_code`` 固定，
  平台據此轉成租戶看得懂的訊息）；
* :func:`is_storage_quota_rejection` 認 helm stderr 裡的 quota 拒絕（剩餘額度不夠本次
  chart 用的情況，事前檢查只看得到「已滿」，看不到 chart 要幾顆）。

全部是純函式，不呼叫 kubectl——取資料在 ``KubernetesService.get_storage_quota_snapshot``。
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional

#: 平台（woow_paas_platform ``cloud_service._execute_deploy``）認這個字串，改名要兩邊一起改。
STORAGE_QUOTA_ERROR_CODE = "workspace_storage_quota_exhausted"

#: 本檢查關心的兩個 quota 資源。
PVC_COUNT_RESOURCE = "persistentvolumeclaims"
STORAGE_RESOURCE = "requests.storage"
STORAGE_QUOTA_RESOURCES = (PVC_COUNT_RESOURCE, STORAGE_RESOURCE)

#: 與 ``api/releases.py`` ``build_orphan_pvc_labels`` 同一組 label（#317／#208）。
ORPHANED_AT_LABEL = "paas.woowtech.io/orphaned-at"
GRACE_PERIOD_DAYS_LABEL = "paas.woowtech.io/grace-period-days"
RELEASE_NAME_LABEL = "paas.woowtech.io/release-name"

_BINARY_SUFFIXES = {
    "Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3,
    "Ti": 1024 ** 4, "Pi": 1024 ** 5, "Ei": 1024 ** 6,
}
_DECIMAL_SUFFIXES = {
    "n": Decimal("1e-9"), "u": Decimal("1e-6"), "m": Decimal("1e-3"),
    "k": 10 ** 3, "M": 10 ** 6, "G": 10 ** 9, "T": 10 ** 12, "P": 10 ** 15, "E": 10 ** 18,
}
_QUANTITY_RE = re.compile(r"^([+-]?[0-9.]+(?:[eE][+-]?[0-9]+)?)([A-Za-z]{0,2})$")

# helm／kubectl 對 quota 拒絕的固定措辭（k8s apiserver quota admission，
# plugin/pkg/admission/resourcequota）：
#   persistentvolumeclaims "x" is forbidden: exceeded quota: <name>, requested: persistentvolumeclaims=1,
#   used: persistentvolumeclaims=10, limited: persistentvolumeclaims=10
# 同時超過多項時 requested 段是逗號串接（requested: persistentvolumeclaims=1,requests.storage=10Gi）。
# 只看 requested 段：used／limited 段列的是同一組資源，看 requested 才不會把別的 quota 誤認。
_QUOTA_REQUESTED_RE = re.compile(
    r"exceeded quota: [^\n]*?requested: ([^\n]*?)(?:, used: |$)",
    re.IGNORECASE | re.MULTILINE,
)


def parse_quantity(value: Any) -> Optional[Decimal]:
    """k8s resource quantity（``10``、``95Gi``、``100G``、``1.5Gi``、``1e3``）→ Decimal。

    看不懂回 ``None``——呼叫端一律把「看不懂」當成「不知道」，不據此擋人。
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return Decimal(str(value))
    if not isinstance(value, str):
        return None
    match = _QUANTITY_RE.match(value.strip())
    if not match:
        return None
    number, suffix = match.groups()
    try:
        base = Decimal(number)
    except InvalidOperation:
        return None
    if not suffix:
        return base
    if suffix in _BINARY_SUFFIXES:
        return base * _BINARY_SUFFIXES[suffix]
    if suffix in _DECIMAL_SUFFIXES:
        return base * Decimal(_DECIMAL_SUFFIXES[suffix])
    return None


def _format_gib(num_bytes: Decimal) -> str:
    gib = num_bytes / Decimal(1024 ** 3)
    text = f"{gib:.1f}".rstrip("0").rstrip(".")
    return f"{text}Gi"


def _pvc_requested_bytes(pvc: Dict[str, Any]) -> Decimal:
    spec = pvc.get("spec") or {}
    requests = (spec.get("resources") or {}).get("requests") or {}
    return parse_quantity(requests.get("storage")) or Decimal(0)


def _orphan_release_epoch(labels: Dict[str, str]) -> Optional[int]:
    """孤兒 PVC 預計被 pvc-cleanup cron 刪掉的時間（epoch 秒）；label 不完整回 None。"""
    try:
        orphaned_at = int(labels.get(ORPHANED_AT_LABEL, ""))
        grace_days = int(labels.get(GRACE_PERIOD_DAYS_LABEL, ""))
    except ValueError:
        return None
    return orphaned_at + grace_days * 86400


def summarize(
    quota_items: Iterable[Dict[str, Any]], pvc_items: Iterable[Dict[str, Any]]
) -> Dict[str, Any]:
    """把 namespace 的 ResourceQuota 與 PVC 收斂成儲存配額摘要。

    多個 quota 都宣告同一資源時取「最緊」的那一個（剩餘量最小）——admission 是每個
    quota 都要過，最緊的那個決定擋不擋。

    Returns:
        ``{"quotas": {resource: {"used": str, "hard": str, "quota": name}},
        "orphaned_pvc_count": int, "orphaned_storage": "95Gi",
        "orphaned_releases": [..], "next_release_epoch": int | None}``
    """
    quotas: Dict[str, Dict[str, Any]] = {}
    for item in quota_items or []:
        status = item.get("status") or {}
        hard = status.get("hard") or (item.get("spec") or {}).get("hard") or {}
        used = status.get("used") or {}
        name = (item.get("metadata") or {}).get("name", "")
        for resource in STORAGE_QUOTA_RESOURCES:
            hard_q = parse_quantity(hard.get(resource))
            if hard_q is None:
                continue
            used_q = parse_quantity(used.get(resource, "0")) or Decimal(0)
            current = quotas.get(resource)
            if current is None or hard_q - used_q < current["_headroom"]:
                quotas[resource] = {
                    "used": str(used.get(resource, "0")),
                    "hard": str(hard.get(resource)),
                    "quota": name,
                    "_headroom": hard_q - used_q,
                }

    orphan_count = 0
    orphan_bytes = Decimal(0)
    orphan_releases: List[str] = []
    release_epochs: List[int] = []
    for pvc in pvc_items or []:
        labels = (pvc.get("metadata") or {}).get("labels") or {}
        if ORPHANED_AT_LABEL not in labels:
            continue
        orphan_count += 1
        orphan_bytes += _pvc_requested_bytes(pvc)
        release = labels.get(RELEASE_NAME_LABEL)
        if release and release not in orphan_releases:
            orphan_releases.append(release)
        epoch = _orphan_release_epoch(labels)
        if epoch is not None:
            release_epochs.append(epoch)

    return {
        "quotas": quotas,
        "orphaned_pvc_count": orphan_count,
        "orphaned_storage": _format_gib(orphan_bytes),
        "orphaned_releases": sorted(orphan_releases),
        "next_release_epoch": min(release_epochs) if release_epochs else None,
    }


def exhausted_resources(summary: Dict[str, Any]) -> List[str]:
    """已經用滿（剩餘 ≤ 0）的儲存配額資源，順序固定（PVC 數量在前）。"""
    quotas = summary.get("quotas") or {}
    return [
        resource
        for resource in STORAGE_QUOTA_RESOURCES
        if resource in quotas and quotas[resource]["_headroom"] <= 0
    ]


def build_quota_error_detail(
    namespace: str, summary: Optional[Dict[str, Any]], resources: List[str]
) -> Dict[str, Any]:
    """回給平台的結構化 ``detail``（經 ``ErrorResponse.detail`` 的 dict 形，#531）。

    ``summary`` 為 None（helm 已被 quota 擋、但事後讀不到用量）時仍回固定的
    ``error_code``，平台照樣給租戶看得懂的訊息，只是少了數字。
    """
    quotas = (summary or {}).get("quotas") or {}
    usage = {
        resource: {"used": quotas[resource]["used"], "hard": quotas[resource]["hard"]}
        for resource in STORAGE_QUOTA_RESOURCES
        if resource in quotas
    }
    detail: Dict[str, Any] = {
        "error_code": STORAGE_QUOTA_ERROR_CODE,
        "message": (
            f"Workspace storage quota exhausted in {namespace} "
            f"({', '.join(resources) or 'storage'}); "
            "PVCs retained from deleted services still count against the quota"
        ),
        "namespace": namespace,
        "exhausted": list(resources),
        "usage": usage,
    }
    if summary is not None:
        detail.update({
            "orphaned_pvc_count": summary.get("orphaned_pvc_count", 0),
            "orphaned_storage": summary.get("orphaned_storage", "0Gi"),
            "orphaned_releases": summary.get("orphaned_releases", []),
            "next_release_epoch": summary.get("next_release_epoch"),
        })
    return detail


def is_storage_quota_rejection(stderr: Optional[str]) -> List[str]:
    """helm stderr 是否為 PVC 數量／storage 的 quota 拒絕；回被擋的資源（空 list＝不是）。"""
    if not stderr:
        return []
    found = set()
    for match in _QUOTA_REQUESTED_RE.finditer(stderr):
        for pair in match.group(1).split(","):
            found.add(pair.split("=", 1)[0].strip().lower())
    return [r for r in STORAGE_QUOTA_RESOURCES if r in found]
