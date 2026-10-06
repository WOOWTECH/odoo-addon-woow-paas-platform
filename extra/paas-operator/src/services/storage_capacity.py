"""#1415：開通前檢查叢集儲存（Longhorn）排程額度，與唯讀的額度回報。

背景（2026-10-04 正式站 .204）：刪除服務後保留的孤兒 PVC（#317）把 Longhorn 的排程額度
吃滿，Nextcloud 80Gi 資料磁碟的兩個 replica 排不進任何節點——PVC 建了、pod 卡 Init 30 分鐘、
平台逾時轉 error。#1348 的預檢只看工作區 ResourceQuota，看不到叢集層的額度。

本模組只做**計算**，全是純函式、不呼叫 kubectl（取資料在
``KubernetesService.get_longhorn_capacity_snapshot`` 等）：

* :class:`LonghornCluster` 把 ``nodes.longhorn.io``／``settings.longhorn.io`` 收斂成排程模型；
* :func:`collect_volume_requests` 從 helm 渲染出的 manifest 找出「這次會新建、落在 Longhorn
  的 volume」（PVC、StatefulSet ``volumeClaimTemplates``、CDI DataVolume）；
* :func:`simulate` 照 Longhorn 的排程規則把這些 volume 的 replica 逐一放進模型（多顆一起、
  扣掉已放的），回報哪些 volume 排不上；
* :func:`build_capacity_error_detail`／:func:`capacity_report` 產出 409 的 ``detail`` 與唯讀
  端點的回應。

排程規則逐條對照 longhorn-manager v1.10.2（正式站版本）：

* ``ReplicaScheduler.ListSchedulableNodes``：節點未刪除、``Ready``＝True、``Schedulable``＝True、
  ``spec.allowScheduling``。
* ``FindDiskCandidates``：磁碟 spec 存在、``allowScheduling``、未要求 eviction、磁碟條件
  ``Schedulable``＝True；v1 引擎只用 ``filesystem`` 磁碟、v2 只用 ``block``。
* ``IsSchedulableToDisk(size, requiredStorage=volume.Status.ActualSize)``——新 volume 的
  ActualSize 是 0：
    - ``StorageMaximum > 0`` 且 ``StorageAvailable > 0``；
    - ``StorageAvailable - 0 > StorageMaximum × minimal-available% / 100``；
    - ``size + StorageScheduled ≤ (StorageMaximum − StorageReserved) × over-provisioning% / 100``。
* ``getDiskCandidates``：先挑「這顆 volume 還沒用過的節點、而且在沒用過的 zone」；沒有的話
  看 ``replica-soft-anti-affinity``／``replica-zone-soft-anti-affinity``（StorageClass 參數可覆寫）
  決定能不能退而求其次；同一顆磁碟放第二個 replica 看 ``replica-disk-soft-anti-affinity``。
  zone 空字串視為同一個 zone（``getCurrentNodesAndZones``）。
* 節點／磁碟 tag 要符合 StorageClass 的 ``nodeSelector``／``diskSelector``
  （``types.IsSelectorsInTags`` ＋ ``allow-empty-*-selector-volume``）。
* ``volume_controller``：``allow-volume-creation-with-degraded-availability``＝true（預設）時，
  只要排上一個 replica，volume 就是 Scheduled、可以掛載（degraded）；false 時要全部排上。
* volume 大小：Longhorn CSI 以 2 MiB 對齊（``util.RoundUpSize``）；CDI DataVolume 的
  ``spec.storage``（非 Block）會加 filesystem overhead（CDI v1.64 ``util.GetRequiredSpace``：
  1 MiB 對齊後 ×(1+overhead) 取上整）。

**判定原則**：只有「確定放不下」才擋——任何一顆新 volume 照上面的規則連 Longhorn 自己都
不會把它標成 Scheduled 時才擋。讀不到 Longhorn（CRD 不存在、權限、逾時）、渲染失敗、看不懂
的欄位一律放行，行為與本檢查加入前相同（呼叫端負責）。這和 #515 文中的「fail-closed」是兩回事：
那是指平台 client 不可忽略 operator 回的 ``success: false``，不是指查不到就拒絕開通。

刻意**沒有**模擬的部分（都只會讓本檢查偏樂觀＝少擋，不會誤擋）：instance-manager／engine image
是否 ready、backing image 的 selector、``strict-local`` data locality、Longhorn 自己挑磁碟時以
「實際剩餘空間」排序（本模擬挑排程餘裕最大的磁碟，多顆 volume 一起放時對開通者較有利）、
同時有兩筆開通互相搶額度。
"""
from __future__ import annotations

import copy
import json
import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from src.services.storage_quota import ORPHANED_AT_LABEL, parse_quantity

#: 平台（woow_paas_platform ``cloud_service._execute_deploy``）認這個字串，改名要兩邊一起改。
CAPACITY_ERROR_CODE = "cluster_storage_capacity_exhausted"

LONGHORN_PROVISIONER = "driver.longhorn.io"

GIB = 1024 ** 3
MIB = 1024 ** 2
#: Longhorn ``util.SizeAlignment``（CSI 建 volume 前把大小對齊到 2 MiB）。
LONGHORN_SIZE_ALIGNMENT = 2 * MIB
#: CDI ``util.DefaultAlignBlockSize``。
CDI_ALIGN_BLOCK_SIZE = MIB
#: CDIConfig 讀不到時用 CDI 自己的預設 global overhead（v1.64 ``common.DefaultGlobalOverhead``）。
CDI_DEFAULT_FILESYSTEM_OVERHEAD = 0.06

DEFAULT_SC_ANNOTATIONS = (
    "storageclass.kubernetes.io/is-default-class",
    "storageclass.beta.kubernetes.io/is-default-class",
)

#: 會影響排程的 Longhorn 設定與其官方預設值（longhorn-manager v1.10.2 types/setting.go）。
#: 設定讀不到某一項時用預設值（舊版 Longhorn 沒有那項設定時，行為本來就是預設值）。
SETTING_DEFAULTS: Dict[str, str] = {
    "storage-over-provisioning-percentage": "100",
    "storage-minimal-available-percentage": "25",
    "replica-soft-anti-affinity": "false",
    "replica-zone-soft-anti-affinity": "true",
    "replica-disk-soft-anti-affinity": "true",
    "allow-empty-node-selector-volume": "true",
    "allow-empty-disk-selector-volume": "true",
    "allow-volume-creation-with-degraded-availability": "true",
    "default-replica-count": '{"v1":"3","v2":"3"}',
}
LONGHORN_SETTING_NAMES = tuple(SETTING_DEFAULTS)

#: 回報裡各節點預訂率（scheduled／limit）的小數位數。
_RATIO_DIGITS = 4


class CapacityDataError(ValueError):
    """Longhorn 資料看不懂（缺欄位、型別不對）——呼叫端當成「不知道」放行。"""


def format_gib(num_bytes: Optional[int]) -> Optional[str]:
    """位元組 → ``"80Gi"``／``"77.2Gi"``（取一位小數、去掉多餘的 0）；None 原樣回傳。"""
    if num_bytes is None:
        return None
    gib = Decimal(int(num_bytes)) / Decimal(GIB)
    text = f"{gib:.1f}".rstrip("0").rstrip(".")
    return f"{text}Gi"


def _round_up(value: int, multiple: int) -> int:
    return -(-int(value) // multiple) * multiple


def longhorn_volume_size(requested_bytes: int) -> int:
    """PVC 請求 → Longhorn volume 實際大小（``util.RoundUpSize``：2 MiB 對齊，≤0 視為 2 MiB）。"""
    if requested_bytes <= 0:
        return LONGHORN_SIZE_ALIGNMENT
    return _round_up(requested_bytes, LONGHORN_SIZE_ALIGNMENT)


def cdi_size_with_overhead(requested_bytes: int, overhead: float) -> int:
    """CDI 渲染後的 PVC 請求：``util.GetRequiredSpace``（1 MiB 對齊後 ×(1+overhead) 取上整），
    再對齊到 1 MiB。

    最後那次對齊對照 #423 W1 PoC 實測：宣告 64Gi、overhead 0.06 ⇒ quota 拒收訊息裡的
    ``requested: requests.storage=72843526144``（＝69469Mi；GetRequiredSpace 本身是
    72842645341）。
    """
    aligned = _round_up(requested_bytes, CDI_ALIGN_BLOCK_SIZE)
    return _round_up(int(math.ceil(aligned * (1 + overhead))), CDI_ALIGN_BLOCK_SIZE)


# ── Longhorn 設定 ────────────────────────────────────────────────────────


def _setting_text(raw: Any, data_engine: str = "v1") -> str:
    """設定值字串；v1.10 起部分設定是 data-engine 專屬的 JSON（``{"v1":"3","v2":"3"}``）。"""
    text = "" if raw is None else str(raw).strip()
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
        except ValueError:
            return text
        if isinstance(parsed, dict):
            return str(parsed.get(data_engine, parsed.get("v1", ""))).strip()
    return text


def _as_bool(text: str) -> bool:
    return text.strip().lower() == "true"


def _as_int(text: str, name: str) -> int:
    try:
        return int(text)
    except (TypeError, ValueError):
        raise CapacityDataError(f"Longhorn setting {name}={text!r} is not an integer")


@dataclass(frozen=True)
class LonghornSettings:
    over_provisioning_percentage: int = 100
    minimal_available_percentage: int = 25
    replica_soft_anti_affinity: bool = False
    replica_zone_soft_anti_affinity: bool = True
    replica_disk_soft_anti_affinity: bool = True
    allow_empty_node_selector_volume: bool = True
    allow_empty_disk_selector_volume: bool = True
    allow_degraded_creation: bool = True
    default_replica_count: int = 3

    @classmethod
    def from_items(cls, items: Iterable[Dict[str, Any]]) -> "LonghornSettings":
        """``settings.longhorn.io`` 的 items（只取 :data:`LONGHORN_SETTING_NAMES`）。"""
        values = dict(SETTING_DEFAULTS)
        for item in items or []:
            name = (item.get("metadata") or {}).get("name")
            if name in values and item.get("value") not in (None, ""):
                values[name] = str(item.get("value"))
        get = lambda name: _setting_text(values[name])  # noqa: E731
        return cls(
            over_provisioning_percentage=_as_int(
                get("storage-over-provisioning-percentage"), "storage-over-provisioning-percentage"),
            minimal_available_percentage=_as_int(
                get("storage-minimal-available-percentage"), "storage-minimal-available-percentage"),
            replica_soft_anti_affinity=_as_bool(get("replica-soft-anti-affinity")),
            replica_zone_soft_anti_affinity=_as_bool(get("replica-zone-soft-anti-affinity")),
            replica_disk_soft_anti_affinity=_as_bool(get("replica-disk-soft-anti-affinity")),
            allow_empty_node_selector_volume=_as_bool(get("allow-empty-node-selector-volume")),
            allow_empty_disk_selector_volume=_as_bool(get("allow-empty-disk-selector-volume")),
            allow_degraded_creation=_as_bool(get("allow-volume-creation-with-degraded-availability")),
            default_replica_count=_as_int(get("default-replica-count"), "default-replica-count"),
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "over_provisioning_percentage": self.over_provisioning_percentage,
            "minimal_available_percentage": self.minimal_available_percentage,
            "replica_soft_anti_affinity": self.replica_soft_anti_affinity,
            "replica_zone_soft_anti_affinity": self.replica_zone_soft_anti_affinity,
            "replica_disk_soft_anti_affinity": self.replica_disk_soft_anti_affinity,
            "allow_degraded_creation": self.allow_degraded_creation,
        }


# ── 節點／磁碟模型 ────────────────────────────────────────────────────────


def _condition_true(conditions: Any, ctype: str) -> bool:
    for cond in conditions or []:
        if isinstance(cond, dict) and cond.get("type") == ctype:
            return str(cond.get("status")) == "True"
    return False


def _int_field(obj: Dict[str, Any], key: str, where: str) -> int:
    value = obj.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CapacityDataError(f"{where}.{key}={value!r} is not a number")
    return int(value)


@dataclass
class Disk:
    node: str
    name: str
    uuid: str
    maximum: int
    available: int
    scheduled: int
    reserved: int
    tags: Tuple[str, ...] = ()
    disk_type: str = "filesystem"
    usable: bool = True  # allowScheduling、未 eviction、Schedulable 條件

    def limit(self, settings: LonghornSettings) -> int:
        """排程上限（``overProvisionLimit``；float64 → int64 截斷，與 Longhorn 同）。"""
        return int(float(self.maximum - self.reserved)
                   * float(settings.over_provisioning_percentage) / 100)

    def headroom(self, settings: LonghornSettings) -> int:
        return self.limit(settings) - self.scheduled

    def not_full(self, settings: LonghornSettings) -> bool:
        """``IsSchedulableToDisk`` 的實際空間條件（新 volume ActualSize＝0）。"""
        minimal = int(float(self.maximum) * float(settings.minimal_available_percentage) / 100)
        return self.maximum > 0 and self.available > 0 and self.available > minimal

    def fits(self, size: int, settings: LonghornSettings) -> bool:
        return self.not_full(settings) and size + self.scheduled <= self.limit(settings)


@dataclass
class Node:
    name: str
    zone: str
    tags: Tuple[str, ...]
    schedulable: bool
    disks: List[Disk] = field(default_factory=list)


def _selectors_in_tags(tags: Sequence[str], selectors: Sequence[str], allow_empty: bool) -> bool:
    """``types.IsSelectorsInTags``。"""
    if not selectors:
        return allow_empty or not tags
    tag_set = set(tags)
    return all(sel in tag_set for sel in selectors)


class LonghornCluster:
    """Longhorn 排程模型（節點、磁碟、設定）。``simulate`` 會在**複本**上扣額度。"""

    def __init__(self, nodes: List[Node], settings: LonghornSettings):
        self.nodes = nodes
        self.settings = settings

    @classmethod
    def from_snapshot(
        cls, node_items: Iterable[Dict[str, Any]], setting_items: Iterable[Dict[str, Any]]
    ) -> "LonghornCluster":
        settings = LonghornSettings.from_items(setting_items)
        nodes: List[Node] = []
        for item in node_items or []:
            meta = item.get("metadata") or {}
            name = meta.get("name")
            if not name:
                raise CapacityDataError("Longhorn node without a name")
            spec = item.get("spec") or {}
            status = item.get("status") or {}
            schedulable = (
                not meta.get("deletionTimestamp")
                and bool(spec.get("allowScheduling"))
                and _condition_true(status.get("conditions"), "Ready")
                and _condition_true(status.get("conditions"), "Schedulable")
            )
            node = Node(
                name=name, zone=str(status.get("zone") or ""),
                tags=tuple(spec.get("tags") or ()), schedulable=bool(schedulable),
            )
            disk_specs = spec.get("disks") or {}
            for disk_name, dstatus in (status.get("diskStatus") or {}).items():
                dspec = disk_specs.get(disk_name)
                if not isinstance(dspec, dict) or not isinstance(dstatus, dict):
                    continue  # FindDiskCandidates：spec 不存在的磁碟不列入
                where = f"node {name} disk {disk_name}"
                node.disks.append(Disk(
                    node=name, name=disk_name, uuid=str(dstatus.get("diskUUID") or disk_name),
                    maximum=_int_field(dstatus, "storageMaximum", where),
                    available=_int_field(dstatus, "storageAvailable", where),
                    scheduled=_int_field(dstatus, "storageScheduled", where),
                    reserved=_int_field(dspec, "storageReserved", where),
                    tags=tuple(dspec.get("tags") or ()),
                    disk_type=str(dspec.get("diskType") or dstatus.get("diskType") or "filesystem"),
                    usable=(
                        bool(dspec.get("allowScheduling"))
                        and not dspec.get("evictionRequested")
                        and _condition_true(dstatus.get("conditions"), "Schedulable")
                    ),
                ))
            nodes.append(node)
        return cls(nodes, settings)

    def copy(self) -> "LonghornCluster":
        return LonghornCluster(copy.deepcopy(self.nodes), self.settings)

    def node_summaries(self) -> List[Dict[str, Any]]:
        """各節點（只算可排程磁碟）的上限／已預訂／餘裕。不可排程的節點也列出、標 False。"""
        out = []
        for node in sorted(self.nodes, key=lambda n: n.name):
            disks = [d for d in node.disks if d.usable]
            limit = sum(d.limit(self.settings) for d in disks)
            scheduled = sum(d.scheduled for d in disks)
            headroom = max(
                [max(0, d.headroom(self.settings)) for d in disks
                 if node.schedulable and d.not_full(self.settings)] or [0])
            out.append({
                "name": node.name,
                "schedulable": node.schedulable and bool(disks),
                "limit_bytes": limit,
                "scheduled_bytes": scheduled,
                # 單一磁碟上能再放的最大 replica（一個 replica 不能跨磁碟）。
                "headroom_bytes": headroom,
                "scheduled_ratio": round(scheduled / limit, _RATIO_DIGITS) if limit > 0 else None,
                "limit": format_gib(limit),
                "scheduled": format_gib(scheduled),
                "headroom": format_gib(headroom),
            })
        return out


# ── StorageClass ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StorageClassInfo:
    name: str
    provisioner: str
    replicas: Optional[int]  # None＝沒寫 numberOfReplicas（Longhorn 用 default-replica-count）
    node_selector: Tuple[str, ...] = ()
    disk_selector: Tuple[str, ...] = ()
    data_engine: str = "v1"
    soft_anti_affinity: Optional[bool] = None  # None＝跟全域設定（ignored／沒寫）
    zone_soft_anti_affinity: Optional[bool] = None
    disk_soft_anti_affinity: Optional[bool] = None

    @property
    def is_longhorn(self) -> bool:
        return self.provisioner == LONGHORN_PROVISIONER

    def replica_count(self, settings: LonghornSettings) -> int:
        return self.replicas if self.replicas else settings.default_replica_count


def _tri_state(value: Optional[str]) -> Optional[bool]:
    if value == "enabled":
        return True
    if value == "disabled":
        return False
    return None


def _split(value: Optional[str]) -> Tuple[str, ...]:
    return tuple(s for s in (value or "").split(",") if s)


def parse_storage_classes(items: Iterable[Dict[str, Any]]) -> Tuple[Dict[str, StorageClassInfo], Optional[str]]:
    """StorageClass items → ({name: info}, 叢集預設 SC 名)。

    預設 SC：k8s ≥1.26 有多個 default 時取 creationTimestamp 最新者（同時間取名字小的；
    ``pkg/volume/util.GetDefaultClass``）。正式站同時有 local-path 與 longhorn 兩個 default。
    """
    classes: Dict[str, StorageClassInfo] = {}
    defaults: List[Tuple[str, str]] = []
    for item in items or []:
        meta = item.get("metadata") or {}
        name = meta.get("name")
        if not name:
            continue
        params = item.get("parameters") or {}
        replicas: Optional[int] = None
        raw = params.get("numberOfReplicas")
        if raw not in (None, ""):
            try:
                replicas = int(raw)
            except (TypeError, ValueError):
                replicas = None
        classes[name] = StorageClassInfo(
            name=name, provisioner=str(item.get("provisioner") or ""), replicas=replicas,
            node_selector=_split(params.get("nodeSelector")),
            disk_selector=_split(params.get("diskSelector")),
            data_engine=str(params.get("dataEngine") or "v1"),
            soft_anti_affinity=_tri_state(params.get("replicaSoftAntiAffinity")),
            zone_soft_anti_affinity=_tri_state(params.get("replicaZoneSoftAntiAffinity")),
            disk_soft_anti_affinity=_tri_state(params.get("replicaDiskSoftAntiAffinity")),
        )
        annotations = meta.get("annotations") or {}
        if any(str(annotations.get(key, "")).lower() == "true" for key in DEFAULT_SC_ANNOTATIONS):
            defaults.append((str(meta.get("creationTimestamp") or ""), name))
    default_name = None
    if defaults:
        # 最新的在前；同時間名字小的在前。
        defaults.sort(key=lambda pair: pair[1])
        defaults.sort(key=lambda pair: pair[0], reverse=True)
        default_name = defaults[0][1]
    return classes, default_name


# ── 這次開通會建哪些 volume ──────────────────────────────────────────────


@dataclass
class VolumeRequest:
    name: str
    kind: str  # PersistentVolumeClaim／StatefulSet／DataVolume
    requested_bytes: int  # PVC 上的請求（DataVolume 已含 CDI overhead）
    size_bytes: int  # Longhorn volume 大小（2 MiB 對齊）
    storage_class: StorageClassInfo
    replicas: int

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name, "kind": self.kind,
            "size": format_gib(self.size_bytes), "size_bytes": self.size_bytes,
            "storage_class": self.storage_class.name, "replicas": self.replicas,
        }


def _storage_request(spec: Dict[str, Any]) -> Optional[int]:
    requests = ((spec or {}).get("resources") or {}).get("requests") or {}
    quantity = parse_quantity(requests.get("storage"))
    if quantity is None:
        return None
    return int(quantity.to_integral_value(rounding="ROUND_CEILING"))


def _resolve_class(
    spec: Dict[str, Any], classes: Dict[str, StorageClassInfo], default_class: Optional[str]
) -> Optional[StorageClassInfo]:
    """PVC spec 的 storageClassName：沒寫＝叢集預設 SC；空字串＝不動態佈建（不算）。"""
    name = (spec or {}).get("storageClassName")
    if name is None:
        name = default_class
    if not name:
        return None
    return classes.get(str(name))


def cdi_overhead_for(overheads: Optional[Dict[str, Any]], storage_class: str) -> float:
    """CDIConfig ``status.filesystemOverhead`` → 這個 StorageClass 的 overhead（讀不到用 CDI 預設）。"""
    overheads = overheads or {}
    raw = (overheads.get("storageClass") or {}).get(storage_class)
    if raw is None:
        raw = overheads.get("global")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return CDI_DEFAULT_FILESYSTEM_OVERHEAD
    return value if 0 <= value < 1 else CDI_DEFAULT_FILESYSTEM_OVERHEAD


def _manifest_claims(
    docs: Iterable[Dict[str, Any]],
) -> List[Tuple[str, str, Dict[str, Any], bool]]:
    """(PVC 名, 來源 kind, 有 storageClassName／resources 的 spec, 要不要加 CDI overhead)。"""
    claims = []
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        kind = doc.get("kind")
        meta = doc.get("metadata") or {}
        spec = doc.get("spec") or {}
        if kind == "PersistentVolumeClaim":
            claims.append((meta.get("name"), kind, spec, False))
        elif kind == "StatefulSet":
            replicas = spec.get("replicas", 1)
            if not isinstance(replicas, int) or replicas < 0:
                replicas = 1
            for vct in spec.get("volumeClaimTemplates") or []:
                vct_name = (vct.get("metadata") or {}).get("name")
                for ordinal in range(replicas):
                    claims.append((f"{vct_name}-{meta.get('name')}-{ordinal}", kind,
                                   vct.get("spec") or {}, False))
        elif kind == "DataVolume" and str(doc.get("apiVersion", "")).startswith("cdi.kubevirt.io/"):
            # spec.storage（StorageProfile API）：CDI 依 volumeMode 加 filesystem overhead；
            # spec.pvc：原樣照請求建 PVC。DataVolume 建的 PVC 與 DataVolume 同名。
            if isinstance(spec.get("storage"), dict):
                storage = spec["storage"]
                claims.append((meta.get("name"), kind, storage,
                               storage.get("volumeMode") != "Block"))
            elif isinstance(spec.get("pvc"), dict):
                claims.append((meta.get("name"), kind, spec["pvc"], False))
    return claims


def has_storage_datavolume(docs: Iterable[Dict[str, Any]]) -> bool:
    """manifest 裡有沒有用 ``spec.storage`` 的 DataVolume（才需要讀 CDI 的 overhead）。"""
    return any(
        isinstance(doc, dict) and doc.get("kind") == "DataVolume"
        and isinstance((doc.get("spec") or {}).get("storage"), dict)
        for doc in docs
    )


def collect_volume_requests(
    docs: Iterable[Dict[str, Any]],
    classes: Dict[str, StorageClassInfo],
    default_class: Optional[str],
    settings: LonghornSettings,
    existing_pvc_names: Iterable[str] = (),
    cdi_overheads: Optional[Dict[str, Any]] = None,
) -> List[VolumeRequest]:
    """這次開通**新建**、而且落在 Longhorn 的 volume。

    已經存在的同名 PVC 不算（重試安裝時上次留下的 PVC：Longhorn 已經記帳，或本來就在等排程，
    這次不會再多建一顆）。非 Longhorn 的 StorageClass（local-path…）、``storageClassName: ""``、
    看不懂大小的請求都不算。``cdi_overheads`` 是 CDIConfig 的 ``status.filesystemOverhead``
    （None＝用 CDI 預設值）。

    CDI 匯入期間的 scratch PVC 不算：它落在 CDIConfig 的 scratchSpaceStorageClass（正式站是
    local-path），而且 HAOS 用 ``pullMethod: node`` 根本不需要 scratch。
    """
    existing = set(existing_pvc_names or ())
    out: List[VolumeRequest] = []
    for name, kind, spec, cdi_overhead in _manifest_claims(docs):
        if not name or name in existing:
            continue
        sc = _resolve_class(spec, classes, default_class)
        if sc is None or not sc.is_longhorn:
            continue
        requested = _storage_request(spec)
        if requested is None:
            continue
        if cdi_overhead:
            requested = cdi_size_with_overhead(requested, cdi_overhead_for(cdi_overheads, sc.name))
        out.append(VolumeRequest(
            name=name, kind=kind, requested_bytes=requested,
            size_bytes=longhorn_volume_size(requested),
            storage_class=sc, replicas=max(1, sc.replica_count(settings)),
        ))
    return out


# ── 模擬放置 ────────────────────────────────────────────────────────────


@dataclass
class Placement:
    volume: VolumeRequest
    nodes: List[str] = field(default_factory=list)

    @property
    def placed(self) -> int:
        return len(self.nodes)


def _effective(flag: Optional[bool], default: bool) -> bool:
    return default if flag is None else flag


def _place_replica(
    cluster: LonghornCluster, volume: VolumeRequest, used_disks: Dict[str, int],
    used_nodes: set, used_zones: set,
) -> Optional[Disk]:
    """照 ``getDiskCandidates`` 的層級挑一顆磁碟放下一個 replica；放不下回 None。"""
    s = cluster.settings
    sc = volume.storage_class
    node_soft = _effective(sc.soft_anti_affinity, s.replica_soft_anti_affinity)
    zone_soft = _effective(sc.zone_soft_anti_affinity, s.replica_zone_soft_anti_affinity)
    disk_soft = _effective(sc.disk_soft_anti_affinity, s.replica_disk_soft_anti_affinity)
    wanted_type = "block" if sc.data_engine == "v2" else "filesystem"

    def candidates(nodes: List[Node]) -> List[Disk]:
        disks = []
        for node in nodes:
            for disk in node.disks:
                if not disk.usable or disk.disk_type != wanted_type:
                    continue
                if not _selectors_in_tags(disk.tags, sc.disk_selector,
                                          s.allow_empty_disk_selector_volume):
                    continue
                if disk.fits(volume.size_bytes, s):
                    disks.append(disk)
        # filterDisksWithMatchingReplicas：沒放過這顆 volume 的磁碟優先；disk 反親和為硬性時只能用它們。
        fresh = [d for d in disks if not used_disks.get(d.uuid)]
        if fresh or not disk_soft:
            return fresh
        return disks

    eligible = [
        n for n in cluster.nodes
        if n.schedulable and _selectors_in_tags(n.tags, sc.node_selector,
                                                s.allow_empty_node_selector_volume)
    ]
    unused_nodes = [n for n in eligible if n.name not in used_nodes]
    tiers = [[n for n in unused_nodes if n.zone not in used_zones]]
    if zone_soft:
        tiers.append(unused_nodes)
        if node_soft:
            tiers.append([n for n in eligible if n.name in used_nodes])
    for tier in tiers:
        disks = candidates(tier)
        if disks:
            # 樂觀：挑排程餘裕最大的磁碟（Longhorn 自己挑實際剩餘最多的），見模組 docstring。
            return max(disks, key=lambda d: (d.headroom(s), d.node, d.name))
    return None


def simulate(cluster: LonghornCluster, volumes: Sequence[VolumeRequest]) -> List[Placement]:
    """在 ``cluster`` 的複本上依序放下所有 volume 的所有 replica（大的先放）。

    回傳每顆 volume 實際放得下的節點（順序同輸入）。原 ``cluster`` 不變。
    """
    work = cluster.copy()
    by_uuid = {d.uuid: d for n in work.nodes for d in n.disks}
    zone_of = {n.name: n.zone for n in work.nodes}
    placements = {id(v): Placement(volume=v) for v in volumes}
    for volume in sorted(volumes, key=lambda v: v.size_bytes, reverse=True):
        used_disks: Dict[str, int] = {}
        used_nodes: set = set()
        used_zones: set = set()
        for _ in range(volume.replicas):
            disk = _place_replica(work, volume, used_disks, used_nodes, used_zones)
            if disk is None:
                break
            target = by_uuid[disk.uuid]
            target.scheduled += volume.size_bytes
            used_disks[disk.uuid] = used_disks.get(disk.uuid, 0) + 1
            used_nodes.add(disk.node)
            used_zones.add(zone_of.get(disk.node, ""))
            placements[id(volume)].nodes.append(disk.node)
    return [placements[id(v)] for v in volumes]


def is_schedulable(placement: Placement, settings: LonghornSettings) -> bool:
    """Longhorn 會不會把這顆 volume 標成 Scheduled（可掛載）。"""
    if placement.placed >= placement.volume.replicas:
        return True
    return settings.allow_degraded_creation and placement.placed >= 1


def largest_placeable_volume(
    cluster: LonghornCluster, storage_class: StorageClassInfo, full_replicas: bool = True
) -> int:
    """這個 StorageClass 現在還能新建的最大單顆 volume（位元組，2 MiB 對齊；放不下任何東西回 0）。

    ``full_replicas=True``＝所有 replica 都排得上（有備援）；False＝至少一個（degraded 也算）。
    單顆 volume 的可放與否對大小單調，用二分搜尋，判準就是 :func:`simulate`。
    """
    replicas = max(1, storage_class.replica_count(cluster.settings))
    need = replicas if full_replicas else 1
    upper = max([d.limit(cluster.settings) for n in cluster.nodes for d in n.disks] or [0])

    def ok(size: int) -> bool:
        probe = VolumeRequest(name="probe", kind="probe", requested_bytes=size, size_bytes=size,
                              storage_class=storage_class, replicas=replicas)
        return simulate(cluster, [probe])[0].placed >= need

    lo, hi = 0, upper // LONGHORN_SIZE_ALIGNMENT  # 以 2 MiB 為單位
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if ok(mid * LONGHORN_SIZE_ALIGNMENT):
            lo = mid
        else:
            hi = mid - 1
    return lo * LONGHORN_SIZE_ALIGNMENT


# ── 孤兒 PVC（#317 保留中） ──────────────────────────────────────────────


def summarize_orphans(
    pvc_items: Iterable[Dict[str, Any]], classes: Dict[str, StorageClassInfo],
    settings: LonghornSettings,
) -> Dict[str, Any]:
    """跨 namespace 的孤兒 PVC 佔用（只讀 label，不碰 PVC）。

    ``longhorn_scheduled_bytes``＝落在 Longhorn 的孤兒 PVC 大小 × 其 SC 的 replica 數，
    即它們佔掉的排程額度（未計 PVC 後來被擴容的部分）。
    """
    count = 0
    requested = 0
    longhorn_scheduled = 0
    namespaces = set()
    epochs: List[int] = []
    for pvc in pvc_items or []:
        meta = pvc.get("metadata") or {}
        labels = meta.get("labels") or {}
        if ORPHANED_AT_LABEL not in labels:
            continue
        spec = pvc.get("spec") or {}
        size = _storage_request(spec) or 0
        count += 1
        requested += size
        namespaces.add(meta.get("namespace"))
        sc = classes.get(spec.get("storageClassName") or "")
        if sc is not None and sc.is_longhorn:
            longhorn_scheduled += longhorn_volume_size(size) * max(1, sc.replica_count(settings))
        try:
            epochs.append(int(labels.get(ORPHANED_AT_LABEL, "")))
        except ValueError:
            pass
    return {
        "count": count,
        "namespaces": len(namespaces),
        "requested_bytes": requested,
        "requested": format_gib(requested),
        "longhorn_scheduled_bytes": longhorn_scheduled,
        "longhorn_scheduled": format_gib(longhorn_scheduled),
        "oldest_orphaned_at_epoch": min(epochs) if epochs else None,
    }


# ── 對外的兩種輸出 ──────────────────────────────────────────────────────


def build_capacity_error_detail(
    namespace: str,
    cluster: LonghornCluster,
    placements: Sequence[Placement],
    orphans: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """409 的 ``detail``（經 ``ErrorResponse.detail`` 的 dict 形，同 #1348）。"""
    settings = cluster.settings
    unschedulable = [p for p in placements if not is_schedulable(p, settings)]
    largest_by_class: Dict[str, int] = {}
    for placement in placements:
        sc = placement.volume.storage_class
        if sc.name not in largest_by_class:
            largest_by_class[sc.name] = largest_placeable_volume(cluster, sc, full_replicas=False)
    worst = min(largest_by_class.values()) if largest_by_class else 0
    detail: Dict[str, Any] = {
        "error_code": CAPACITY_ERROR_CODE,
        "message": (
            "Cluster storage (Longhorn) cannot schedule "
            + ", ".join(f"{p.volume.name} ({format_gib(p.volume.size_bytes)} x{p.volume.replicas})"
                        for p in unschedulable)
            + f"; largest new volume that still fits: {format_gib(worst)}"
        ),
        "namespace": namespace,
        "unschedulable": [
            dict(p.volume.as_dict(), replicas_placeable=p.placed) for p in unschedulable
        ],
        "requested": [dict(p.volume.as_dict(), replicas_placeable=p.placed) for p in placements],
        "largest_placeable_volume_bytes": worst,
        "largest_placeable_volume": format_gib(worst),
        "nodes": cluster.node_summaries(),
        "settings": settings.as_dict(),
        "suggestion": (
            "Free Longhorn scheduling capacity (orphaned PVCs kept by #317 count in full, "
            "times their replica count), or raise storage-over-provisioning-percentage / add "
            "disks, then retry the deployment."
        ),
    }
    if orphans is not None:
        detail["orphaned_pvcs"] = orphans
    return detail


def capacity_report(
    cluster: LonghornCluster,
    classes: Dict[str, StorageClassInfo],
    default_class: Optional[str],
    storage_class: Optional[str] = None,
    orphans: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """唯讀端點的回應：各節點額度、最大可放單顆 volume、孤兒 PVC 佔用。"""
    nodes = cluster.node_summaries()
    ratios = [n["scheduled_ratio"] for n in nodes if n["schedulable"] and n["scheduled_ratio"] is not None]
    report: Dict[str, Any] = {
        "available": True,
        "settings": cluster.settings.as_dict(),
        "nodes": nodes,
        "max_scheduled_ratio": max(ratios) if ratios else None,
        "default_storage_class": default_class,
        "orphaned_pvcs": orphans,
    }
    name = storage_class or default_class
    sc = classes.get(name or "")
    report["storage_class"] = name
    if sc is None or not sc.is_longhorn:
        report.update({
            "storage_class_error": "not_found" if sc is None else "not_longhorn",
            "replicas": None,
            "largest_placeable_volume_bytes": None,
            "largest_placeable_volume": None,
            "largest_placeable_volume_degraded_bytes": None,
            "largest_placeable_volume_degraded": None,
        })
        return report
    full = largest_placeable_volume(cluster, sc, full_replicas=True)
    degraded = largest_placeable_volume(cluster, sc, full_replicas=False)
    report.update({
        "storage_class_error": None,
        "replicas": sc.replica_count(cluster.settings),
        "largest_placeable_volume_bytes": full,
        "largest_placeable_volume": format_gib(full),
        "largest_placeable_volume_degraded_bytes": degraded,
        "largest_placeable_volume_degraded": format_gib(degraded),
    })
    return report
