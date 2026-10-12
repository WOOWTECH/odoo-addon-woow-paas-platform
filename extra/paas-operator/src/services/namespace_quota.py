"""#1647：工作區 namespace ResourceQuota 的上限與已佔用量報表（唯讀）。

背景：2026-10-10 正式站一台服務被手動把 CPU limit 調到超過 namespace 的 ``limits.cpu``
上限，``Recreate`` 的 Deployment 砍掉舊 pod 之後新 pod 被 quota admission 拒絕，停了近六
小時——操作的人事前不知道上限是多少，平台前台與後台也沒有任何地方看得到。整條鏈缺的是
「讀」：operator 只有建立與覆寫 quota 的端點。

本模組把 ``kubectl get resourcequota -o json`` 的 items 收斂成一份報表，給
``GET /api/namespaces/{name}/quota``（``api/namespaces.py``）回傳：

* 數字是 ResourceQuota 的 ``status.hard``／``status.used``——**已被佔用的配額**（各容器
  宣告的 requests／limits 與 PVC 的 requests 加總），不是即時使用率；
* 多個 quota 宣告同一資源時取最緊的那一個（:func:`storage_quota.tightest_quota_usage`）；
* 「沒有 quota」與「讀不到」各有自己的 ``reason``，不共用一個空值
  （docs/tracking/pitfalls/operator-route-none-conflates-disabled-and-failed.md 同一類）。

全部是純函式，不呼叫 kubectl——取資料在 ``KubernetesService.get_resource_quotas``。
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Iterable, List

from src.services.storage_quota import parse_quantity, tightest_quota_usage

#: 平台（woow_paas_platform ``src/services/workspace_quota.py``）認這兩個字串，改名要兩邊
#: 一起改。
#: namespace 內沒有可用的 ResourceQuota（namespace 還不存在也落在這裡，見 quota_report）。
QUOTA_NOT_FOUND = "quota_not_found"
#: kubectl 失敗、逾時或回應解析不了——不知道有沒有 quota。
QUOTA_UNREADABLE = "quota_unreadable"

_UNIT_CORES = "cores"
_UNIT_BYTES = "bytes"
_UNIT_COUNT = "count"

#: 以位元組計的資源（去掉 ``requests.``／``limits.`` 前綴之後的名字）。
_BYTE_RESOURCES = ("memory", "storage", "ephemeral-storage")


def resource_unit(resource: str) -> str:
    """quota 資源鍵的單位：``cores``（CPU）、``bytes``（記憶體／儲存）、``count``（物件數）。

    鍵的形狀有三種：``limits.cpu``、``persistentvolumeclaims``，以及帶 StorageClass 或
    擴充資源前綴的 ``<sc>.storageclass.storage.k8s.io/requests.storage``。所以先取最後
    一個 ``/`` 之後的片段，再去掉 ``requests.``／``limits.`` 前綴才判斷。
    """
    name = str(resource).rsplit("/", 1)[-1]
    for prefix in ("requests.", "limits."):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    if name == "cpu":
        return _UNIT_CORES
    if name in _BYTE_RESOURCES or name.startswith("hugepages-"):
        return _UNIT_BYTES
    return _UNIT_COUNT


def unavailable_report(namespace: str, reason: str) -> Dict[str, Any]:
    """拿不到用量時的報表：形狀與成功時相同，``available`` 為 False 並帶 ``reason``。"""
    return {
        "namespace": namespace,
        "available": False,
        "reason": reason,
        "quotas": [],
        "resources": {},
    }


def managed_quota_report(namespace: str, quota_items: Iterable[Dict[str, Any]],
                         reason: str | None = None) -> Dict[str, Any]:
    """#1678: one managed object, independent of the legacy tightest summary.

    observed_at is the read time, not quota-controller reconciliation time.
    An empty list cannot distinguish an absent namespace from an absent quota;
    namespace_exists remains unknown and must not authorize skipping a check.
    """
    report = {
        "namespace": namespace, "name": f"{namespace}-quota", "uid": None,
        "resource_version": None, "observed_at": datetime.now(timezone.utc).isoformat(),
        "namespace_exists": None, "available": False,
        "reason": reason or "managed_quota_not_found", "hard": {}, "used": {},
    }
    if reason:
        return report
    matches = [item for item in quota_items
               if (item.get("metadata") or {}).get("name") == report["name"]]
    if len(matches) != 1:
        return report
    item = matches[0]
    metadata = item.get("metadata") or {}
    status = item.get("status") or {}
    report.update({
        "namespace": metadata.get("namespace"), "uid": metadata.get("uid"),
        "resource_version": metadata.get("resourceVersion"),
        "hard": status.get("hard") or {}, "used": status.get("used") or {},
        "spec_hard": (item.get("spec") or {}).get("hard") or {},
    })
    resources = ("requests.cpu", "limits.cpu", "requests.memory", "limits.memory",
                 "requests.storage")
    if not all(isinstance(metadata.get(key), str) and metadata[key]
               for key in ("namespace", "name", "uid", "resourceVersion")):
        report["reason"] = "managed_quota_identity_incomplete"
    elif metadata["namespace"] != namespace:
        report["reason"] = "managed_quota_namespace_mismatch"
    elif _is_scoped(item):
        report["reason"] = "managed_quota_scoped"
    elif not all(isinstance(report[key], dict) and
                 all(resource in report[key] for resource in resources)
                 for key in ("hard", "used", "spec_hard")):
        report["reason"] = "managed_quota_incomplete"
    elif not all(_valid_quantity(report[key][resource])
                 for key in ("hard", "used", "spec_hard") for resource in resources):
        report["reason"] = "managed_quota_invalid_quantity"
    elif any(parse_quantity(report["hard"][resource]) !=
             parse_quantity(report["spec_hard"][resource]) for resource in resources):
        report["reason"] = "managed_quota_status_pending"
    else:
        report.update(available=True, reason=None)
    return report


def _valid_quantity(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        quantity = parse_quantity(value)
        return quantity is not None and quantity.is_finite() and quantity >= 0
    except ArithmeticError:
        return False


def _is_scoped(item: Dict[str, Any]) -> bool:
    """帶 ``scopes``／``scopeSelector`` 的 quota 只計部分 pod（例如只計 BestEffort），
    它的 hard 不是整個工作區的上限，不能拿來顯示。"""
    spec = item.get("spec") or {}
    return bool(spec.get("scopes")) or bool(spec.get("scopeSelector"))


def _numeric(quantity: Decimal, unit: str) -> Any:
    """Decimal → JSON 數字：cores 留三位小數（millicore），bytes 與 count 取整數。"""
    if unit == _UNIT_CORES:
        return float(round(quantity, 3))
    return int(quantity)


def quota_report(namespace: str, quota_items: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """namespace 的 ResourceQuota items → 每個資源的上限與已佔用量。

    Args:
        namespace: 只用來填回應的 ``namespace`` 欄位。
        quota_items: ``kubectl get resourcequota -o json`` 的 ``items``。

    Returns:
        ``{"namespace", "available": True, "reason": None, "quotas": [name, ...],
        "resources": {resource: {"hard": str, "used": str, "hard_value": number,
        "used_value": number, "unit": "cores"|"bytes"|"count", "ratio": float|None,
        "quota": name}}}``。``resources`` 以 k8s 資源鍵為 key（依鍵排序）；``ratio`` 是
        used／hard（四位小數，超額時照實大於 1，hard 為 0 時 None）。

        沒有任何可用的 quota 時回 :func:`unavailable_report`＋``QUOTA_NOT_FOUND``。
        namespace 不存在與 namespace 內沒有 quota 兩種情況 kubectl 都回空 items，這裡
        不區分（對呼叫端都是「沒有上限可以顯示」）。
    """
    unscoped = [item for item in (quota_items or []) if not _is_scoped(item)]
    usage = tightest_quota_usage(unscoped)
    if not usage:
        return unavailable_report(namespace, QUOTA_NOT_FOUND)

    resources: Dict[str, Dict[str, Any]] = {}
    for resource in sorted(usage):
        entry = usage[resource]
        unit = resource_unit(resource)
        # tightest_quota_usage 只留 hard 解析得了的資源；used 看不懂時與它一樣當 0。
        hard_q = parse_quantity(entry["hard"]) or Decimal(0)
        used_q = parse_quantity(entry["used"]) or Decimal(0)
        resources[resource] = {
            "hard": entry["hard"],
            "used": entry["used"],
            "hard_value": _numeric(hard_q, unit),
            "used_value": _numeric(used_q, unit),
            "unit": unit,
            "ratio": float(round(used_q / hard_q, 4)) if hard_q else None,
            "quota": entry["quota"],
        }

    quota_names: List[str] = sorted({entry["quota"] for entry in usage.values()})
    return {
        "namespace": namespace,
        "available": True,
        "reason": None,
        "quotas": quota_names,
        "resources": resources,
    }
