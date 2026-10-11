"""#1647：升級前檢查工作區的 compute 配額（CPU／記憶體／ephemeral-storage 的 ResourceQuota）。

背景（2026-10-10 正式站 omnigent.woowtech.io）：namespace 的 ``limits.cpu`` 上限是 12，
server 與同 namespace 的 PG 被調成各 8。Deployment 是 ``Recreate``——舊 pod 先被砍，新 pod
在 admission 被 quota 拒絕（ReplicaSet ``FailedCreate: exceeded quota``），剩 0 個 pod、
中斷近 6 小時，而平台紀錄一直是 running。#1407／#1513 的升級預檢已經 dry-run 出 manifest、
讀了 live 的 Deployment／StatefulSet；本模組用同一份資料算「這個 release 升級後的 pod 放不放
得進 ResourceQuota」，確定放不下就在 helm 套用任何東西之前擋下。

**判定原則：只擋「確定放不下」，所有「不知道」都放行。**

* 沒有 quota、讀不到資料、數量看不懂、selector 看不懂（``matchExpressions``）、pod 與 workload
  對不起來、pod-level resources、``OnDelete`` 的 StatefulSet、容器缺 limits 又沒有 LimitRange
  可以補——一律不判（該資源略過，或整次 :class:`Unknown`），行為與本檢查加入前相同。
* 兩層判斷。Tier 1（所有更新策略）問的是「升級完成後，本 release 的 pod 全部存在時放不放
  得下」：其他 workload 的佔用＋本 release 的新需求 ＞ 上限就是放不下，不論 pod 怎麼換。
  Tier 2 只看 ``RollingUpdate`` 且 ``maxUnavailable`` 解析為 0 的 Deployment——新 pod 要先
  建出來、舊 pod 才會被砍，所以要多放得下一顆。**這一層只在 pod 的 resources 真的有變時才
  擋**：pod template 沒變的 upgrade 根本不會換 pod，也就不需要那一顆的空間；而 rendered 與
  live 的 template 之間有太多 API server 補的預設值，能可靠比對的只有 resources（解析成數字後）。
  證明不了 pod 會被換時只回一筆 advisory。Tier 2 沒擋到的情況是「新 pod 建不出來、舊 pod
  照跑」，不會中斷服務；``Recreate`` 與 StatefulSet 先刪後建，由 Tier 1 涵蓋。
  同一次升級把 replicas 調小時，Deployment controller 先把舊 ReplicaSet 縮到新的 replicas
  才開始換 pod（scaling event 先於 rollout），所以「現在的用量」要先扣掉會被縮掉的那幾顆
  舊 pod（算不準是哪幾顆，扣最大的＝偏放行）。
* 其他 workload 的佔用取兩種估法的較小者（quota 的 ``status.used`` 扣掉本 release 與正在
  終止的 pod／直接加總其他 pod），因為 ``status.used`` 由 quota controller 非同步更新，
  落後時會偏高。
* Job 擁有的 pod（含 CronJob 產生的 Job）是**暫時性**的佔用：跑完就釋出。只因為它們才放不下
  時不擋，回一筆 ``transient_job_usage`` 的 advisory；扣掉它們仍然放不下才擋。代價是 Job 還在
  跑的那段時間 ``Recreate`` 的新 pod 會先被 admission 擋下、等 Job 結束後由 ReplicaSet
  controller 重試建出來——短暫中斷，換來不把「幾分鐘後就會過」的變更判成確定的失敗。
  這一條是放寬，只放寬到**算得出來**的部分：用量算不出來的 Job pod 不扣（當成 0）。若因為有
  一顆算不出來就整段放行，namespace 裡只要有這樣一顆 Job pod，其他常駐 workload 就已經佔滿
  的升級（事故的形狀）也會被放過去。
* quota 的上限：admission 看 ``status.hard``，它由 quota controller 從 ``spec.hard`` 同步過去。
  平台剛推送新 quota 的那一小段時間兩者不一致——同一個資源取**較大**者（見
  :func:`live_hard`）。
* 換方案**降級**（平台將推送的上限比現在小）而只有 Tier 2 放不下時照樣擋（那個升級在推送
  之後確實會卡住），但 shortfall 多帶 ``reason: "rolling_on_downgrade"``——這不是「其他服務
  佔太多」，平台據此換成降級專用的說明（一般的說明請租戶聯絡客服調整配額，與降級相反）。

刻意**沒有**算進需求的（都只會少擋，不會誤擋）：DaemonSet、Job、CronJob（只回 advisory）、
bare Pod、KubeVirt VM、helm hook、RuntimeClass 在 admission 才加的 overhead、帶 scope 的
ResourceQuota。

與 ``storage_quota.py`` 的分工：那支管**首次開通**的 PVC 數量與 ``requests.storage``（#1348）；
本模組管**升級**時 pod 的 ``limits.*``／``requests.*``。數量解析共用那裡的
:func:`~src.services.storage_quota.parse_quantity`。

全部是純函式，不呼叫 kubectl／helm——取資料在
``KubernetesService.get_compute_quota_snapshot`` 與升級預檢既有的 dry-run。

⛔ rendered manifest 可能含 Secret：本模組不得把 ``rendered_docs`` 寫進 log、例外訊息或
回傳值。對外只有物件的 kind／名稱與算出來的數字。
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, Iterable, List, Optional, Tuple

from src.services.storage_quota import parse_quantity

#: 平台 ``src/services/paas_operator.py`` 有同名常數，改名兩邊一起改。
COMPUTE_QUOTA_ERROR_CODE = "workspace_compute_quota_exceeded"

#: 本檢查關心的 quota 資源（順序＝回報順序）。
TRACKED_RESOURCES = (
    "limits.cpu", "limits.memory", "requests.cpu", "requests.memory",
    "limits.ephemeral-storage", "requests.ephemeral-storage",
)

#: 計入需求的 workload 種類（其餘見檔頭「沒有算進需求的」）。
WORKLOAD_KINDS = ("Deployment", "StatefulSet")

#: quota 的 ``cpu``／``memory`` 是 ``requests.cpu``／``requests.memory`` 的別名。
_HARD_ALIASES = {"cpu": "requests.cpu", "memory": "requests.memory"}
_RELEASE_ANNOTATION = "meta.helm.sh/release-name"
_TERMINAL_PHASES = ("Succeeded", "Failed")
#: controller owner 是這些 kind 的 pod 跑完就釋出配額（CronJob 產生的也是 Job 擁有）。
_TRANSIENT_OWNER_KINDS = ("Job",)
#: shortfall 的 ``reason``：換方案降級（將推送的上限比現在小）而新舊 pod 並存時放不下。
#: 平台 ``cloud_service._compute_quota_error_message`` 認這個字串，改名兩邊一起改。
REASON_ROLLING_ON_DOWNGRADE = "rolling_on_downgrade"
_ZERO = Decimal(0)
_GI = Decimal(1024 ** 3)
_MI = Decimal(1024 ** 2)
_CONSUMER_LIMIT = 5

_RESOURCE_LABELS = {
    "limits.cpu": "CPU 上限",
    "limits.memory": "記憶體上限",
    "requests.cpu": "CPU 需求",
    "requests.memory": "記憶體需求",
    "limits.ephemeral-storage": "暫存磁碟上限",
    "requests.ephemeral-storage": "暫存磁碟需求",
}


class Unknown(Exception):
    """資料不足以下判斷——呼叫端一律放行。

    ``reason`` 是固定的機器碼（會出現在 upgrade-preflight 的回應），``subject`` 只放
    物件的 kind／名稱，不放 manifest 內容。
    """

    def __init__(self, reason: str, subject: str = ""):
        self.reason = reason
        self.subject = subject
        super().__init__(f"{reason} ({subject})" if subject else reason)


def _split(resource: str) -> Tuple[str, str]:
    """``limits.ephemeral-storage`` → ``("limits", "ephemeral-storage")``。"""
    kind, dimension = resource.split(".", 1)
    return kind, dimension


def _mapping(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def limitrange_defaults(items: Optional[Iterable[Any]]) -> Dict[str, Dict[str, Optional[Decimal]]]:
    """namespace 的 LimitRange → 容器沒宣告時會被補上的值。

    依 ``metadata.name`` 排序走過所有 ``type: Container`` 的項目，每個資源取第一個出現的
    ``default``（limits）與 ``defaultRequest``（requests）；只有 ``default`` 沒有
    ``defaultRequest`` 時 ``defaultRequest``＝``default``（API server 對 LimitRange 物件的預設）。
    值讀叢集上的實物，不在這裡另寫一份平台預設（``ns-quota-hard-limits-limitrange-lockstep``）。

    Returns:
        ``{"limits": {dimension: Decimal | None}, "requests": {...}}``；``None``＝寫了但看不懂。
    """
    defaults: Dict[str, Dict[str, Optional[Decimal]]] = {"limits": {}, "requests": {}}
    ordered = sorted(
        (item for item in items or [] if isinstance(item, dict)),
        key=lambda item: str(_mapping(item.get("metadata")).get("name") or ""),
    )
    for item in ordered:
        for entry in _mapping(item.get("spec")).get("limits") or []:
            if not isinstance(entry, dict) or entry.get("type") != "Container":
                continue
            default = _mapping(entry.get("default"))
            default_request = _mapping(entry.get("defaultRequest"))
            for dimension, value in default.items():
                defaults["limits"].setdefault(dimension, parse_quantity(value))
            for dimension, value in default_request.items():
                defaults["requests"].setdefault(dimension, parse_quantity(value))
            for dimension, value in default.items():
                if dimension not in default_request:
                    defaults["requests"].setdefault(dimension, parse_quantity(value))
    return defaults


def container_resources(
    container: Any, defaults: Optional[Dict[str, Dict[str, Optional[Decimal]]]],
) -> Dict[str, Decimal]:
    """一個容器在 quota 上計的量（``TRACKED_RESOURCES`` 的子集；算不出的鍵不在回傳值裡）。

    limits[x]＝容器自己寫的，否則 LimitRange ``default``；requests[x]＝容器自己寫的，否則
    「容器自己有寫 limits[x]」時等於該 limits（API server 對 Pod 的預設，早於 LimitRange），
    否則 LimitRange ``defaultRequest``。

    ``defaults=None`` 表示物件已經被 API server 補過預設（live pod），不補任何值。
    """
    resources = _mapping(_mapping(container).get("resources"))
    own = {"limits": _mapping(resources.get("limits")),
           "requests": _mapping(resources.get("requests"))}
    out: Dict[str, Decimal] = {}
    for resource in TRACKED_RESOURCES:
        kind, dimension = _split(resource)
        if dimension in own[kind]:
            value = parse_quantity(own[kind][dimension])
        elif defaults is None:
            continue
        elif kind == "requests" and dimension in own["limits"]:
            value = parse_quantity(own["limits"][dimension])
        elif dimension in defaults[kind]:
            value = defaults[kind][dimension]
        else:
            continue
        if value is None or value < 0:
            continue
        out[resource] = value
    return out


def pod_resources(
    pod_spec: Any, defaults: Optional[Dict[str, Dict[str, Optional[Decimal]]]],
) -> Dict[str, Decimal]:
    """一顆 pod 在 quota 上計的量（k8s 的 pod 有效資源規則，``resourcehelper.PodRequests``）。

    一般容器加總；``initContainers`` 依序走——``restartPolicy: Always``（sidecar）加進總和並
    累計，其餘取 max(該容器＋已累計的 sidecar)；結果＝max(加總, init 峰值)，再加
    ``spec.overhead``（limits 只在本來非 0 時加，同 ``PodLimits``）。

    某個資源有任何容器算不出值 ⇒ 回傳值不含該鍵（呼叫端不判那個資源）。

    Raises:
        Unknown: ``spec.resources``（pod-level resources）存在——計量規則不同，不猜。
    """
    spec = _mapping(pod_spec)
    if spec.get("resources"):
        raise Unknown("pod_level_resources")
    known = set(TRACKED_RESOURCES)
    total = dict.fromkeys(TRACKED_RESOURCES, _ZERO)
    for container in spec.get("containers") or []:
        values = container_resources(container, defaults)
        known &= set(values)
        for resource, value in values.items():
            total[resource] += value
    sidecars = dict.fromkeys(TRACKED_RESOURCES, _ZERO)
    init_peak = dict.fromkeys(TRACKED_RESOURCES, _ZERO)
    for container in spec.get("initContainers") or []:
        values = container_resources(container, defaults)
        known &= set(values)
        restartable = _mapping(container).get("restartPolicy") == "Always"
        for resource in TRACKED_RESOURCES:
            value = values.get(resource, _ZERO)
            if restartable:
                total[resource] += value
                sidecars[resource] += value
                in_use = sidecars[resource]
            else:
                in_use = value + sidecars[resource]
            init_peak[resource] = max(init_peak[resource], in_use)
    overhead = _mapping(spec.get("overhead"))
    out: Dict[str, Decimal] = {}
    for resource in TRACKED_RESOURCES:
        if resource not in known:
            continue
        value = max(total[resource], init_peak[resource])
        kind, dimension = _split(resource)
        if dimension in overhead:
            extra = parse_quantity(overhead[dimension])
            if extra is None:
                continue
            if kind == "requests" or value > 0:
                value += extra
        out[resource] = value
    return out


def live_hard(quota_item: Any) -> Dict[str, Any]:
    """一個 live ResourceQuota 現在的上限（``{hard 的鍵: 原始值}``）。

    admission 看的是 ``status.hard``，它由 quota controller 從 ``spec.hard`` 同步過去。平台剛
    推送新 quota 的那一小段時間兩者不一致（換方案：扣款 → 推送 quota → 背景 upgrade）；這時
    拿落後的那一邊去擋，會把已經扣款的升級誤判成放不下。所以：

    * ``status.hard`` 還沒有（quota 剛建立）⇒ 用 ``spec.hard``；反過來也一樣。
    * 兩邊都有、同一個鍵的值不同 ⇒ 取**較大**者（放行方向）。
    * 只在其中一邊出現的鍵、或任一邊看不懂 ⇒ 回傳值不含該鍵（那個資源不判）。
    """
    item = _mapping(quota_item)
    spec_hard = _mapping(_mapping(item.get("spec")).get("hard"))
    status_hard = _mapping(_mapping(item.get("status")).get("hard"))
    if not spec_hard or not status_hard:
        return dict(status_hard or spec_hard)
    merged: Dict[str, Any] = {}
    for key in spec_hard:
        if key not in status_hard:
            continue
        declared = parse_quantity(spec_hard[key])
        enforced = parse_quantity(status_hard[key])
        if declared is None or enforced is None:
            continue
        merged[key] = spec_hard[key] if declared >= enforced else status_hard[key]
    return merged


def _is_scoped(quota_item: Dict[str, Any]) -> bool:
    spec = _mapping(quota_item.get("spec"))
    return bool(spec.get("scopes") or spec.get("scopeSelector"))


def pending_reductions(
    quota_items: Optional[Iterable[Any]],
    pending_hard: Optional[Dict[str, Any]],
    platform_quota_name: Optional[str],
) -> set:
    """``pending_hard`` 比平台那個 quota **現在**的上限小的資源（換方案降級）。

    只認兩邊都看得懂、而且確實變小的鍵；live 沒有那個 quota 物件時回空集合。
    """
    reduced: set = set()
    if pending_hard is None or not platform_quota_name:
        return reduced
    for item in quota_items or []:
        if not isinstance(item, dict) or _is_scoped(item):
            continue
        if str(_mapping(item.get("metadata")).get("name") or "") != platform_quota_name:
            continue
        current = live_hard(item)
        for key, raw in _mapping(pending_hard).items():
            new, old = parse_quantity(raw), parse_quantity(current.get(key))
            if new is not None and old is not None and new < old:
                reduced.add(_HARD_ALIASES.get(key, key))
    return reduced


def quota_limits(
    quota_items: Optional[Iterable[Any]],
    pending_hard: Optional[Dict[str, Any]] = None,
    platform_quota_name: Optional[str] = None,
) -> Dict[str, Dict[str, Any]]:
    """namespace 的 ResourceQuota → 每個資源最緊的那一筆。

    * 帶 ``spec.scopes``／``spec.scopeSelector`` 的 quota 只管一部分 pod，跳過。
    * ``pending_hard`` 有值時（換方案：平台會在檢查後立刻推送新的 quota），名稱等於
      ``platform_quota_name`` 的那個 quota 的 hard 以它取代；live 沒有該物件就合成一筆
      used 全為 0 的。
    * admission 是每個 quota 都要過，所以每個資源取 hard−used 最小的一筆。hard 的取法見
      :func:`live_hard`（``status.hard`` 與 ``spec.hard`` 不一致時取較大者）。

    Returns:
        ``{resource: {"hard": Decimal, "used": Decimal, "quota": name}}``
    """
    entries: List[Tuple[str, Dict[str, Any], Dict[str, Any]]] = []
    replaced = False
    for item in quota_items or []:
        if not isinstance(item, dict) or _is_scoped(item):
            continue
        status = _mapping(item.get("status"))
        name = str(_mapping(item.get("metadata")).get("name") or "")
        hard = live_hard(item)
        if pending_hard is not None and platform_quota_name and name == platform_quota_name:
            hard = pending_hard
            replaced = True
        entries.append((name, hard, _mapping(status.get("used"))))
    if pending_hard is not None and platform_quota_name and not replaced:
        entries.append((platform_quota_name, pending_hard, {}))

    tightest: Dict[str, Dict[str, Any]] = {}
    for name, hard, used in entries:
        for key, raw_hard in _mapping(hard).items():
            hard_q = parse_quantity(raw_hard)
            used_q = parse_quantity(used.get(key, "0"))
            if hard_q is None or used_q is None:
                continue
            resource = _HARD_ALIASES.get(key, key)
            current = tightest.get(resource)
            if current is None or hard_q - used_q < current["hard"] - current["used"]:
                tightest[resource] = {"hard": hard_q, "used": used_q, "quota": name}
    return tightest


def _controller_owner(pod: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for ref in _mapping(pod.get("metadata")).get("ownerReferences") or []:
        if isinstance(ref, dict) and ref.get("controller") is True:
            return ref
    return None


def _replicaset_deployment(replicaset_name: Any) -> Optional[str]:
    """ReplicaSet 名稱 ``<deployment>-<pod-template-hash>`` → Deployment 名稱。

    hash 不含 ``-``，所以去掉最後一段就是 Deployment 名稱——``foo-bar-7d9c`` 屬於
    ``foo-bar``，不會被 ``foo`` 以前綴誤認。
    """
    if not isinstance(replicaset_name, str) or "-" not in replicaset_name:
        return None
    return replicaset_name.rsplit("-", 1)[0]


def own_pods(live_workload: Dict[str, Any], pods: Optional[Iterable[Any]]) -> List[Dict[str, Any]]:
    """``pods`` 中屬於這個 live Deployment／StatefulSet 的那些。

    兩個條件都要成立：``spec.selector.matchLabels`` ⊆ pod labels，而且 pod 的 controller
    owner 相符（Deployment → ReplicaSet ``<deploy>-<hash>``；StatefulSet → 同名 StatefulSet）。
    只看 selector 會把 selector 互為子集的兩個 workload 混在一起。

    Raises:
        Unknown: ``matchExpressions`` 非空或沒有 ``matchLabels``——不重寫 selector 的求值。
    """
    kind = live_workload.get("kind")
    name = str(_mapping(live_workload.get("metadata")).get("name") or "")
    selector = _mapping(_mapping(live_workload.get("spec")).get("selector"))
    match_labels = selector.get("matchLabels")
    if selector.get("matchExpressions") or not isinstance(match_labels, dict) or not match_labels:
        raise Unknown("selector_unreadable", f"{kind}/{name}")
    matched = []
    for pod in pods or []:
        if not isinstance(pod, dict):
            continue
        labels = _mapping(_mapping(pod.get("metadata")).get("labels"))
        if any(labels.get(key) != value for key, value in match_labels.items()):
            continue
        owner = _controller_owner(pod)
        if owner is None:
            continue
        if kind == "Deployment":
            if (owner.get("kind") != "ReplicaSet"
                    or _replicaset_deployment(owner.get("name")) != name):
                continue
        elif kind == "StatefulSet":
            if owner.get("kind") != "StatefulSet" or owner.get("name") != name:
                continue
        else:
            continue
        matched.append(pod)
    return matched


def _replicas(spec: Dict[str, Any], live: Optional[Dict[str, Any]], subject: str) -> int:
    raw = spec.get("replicas")
    if raw is None and live is not None:
        raw = _mapping(live.get("spec")).get("replicas")
    if raw is None:
        return 1
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise Unknown("replicas_unreadable", subject)
    return raw


def _scaled(raw: Any, replicas: int, round_up: bool, subject: str) -> int:
    """``maxSurge``／``maxUnavailable`` 的整數或百分比 → 顆數（k8s ``GetScaledValueFromIntOrPercent``）。"""
    if isinstance(raw, bool):
        raise Unknown("rolling_update_unreadable", subject)
    if isinstance(raw, int):
        return max(raw, 0)
    if isinstance(raw, str) and raw.endswith("%"):
        try:
            percent = int(raw[:-1])
        except ValueError:
            raise Unknown("rolling_update_unreadable", subject) from None
        scaled = replicas * max(percent, 0)
        return -(-scaled // 100) if round_up else scaled // 100
    raise Unknown("rolling_update_unreadable", subject)


def _max_unavailable_abs(spec: Dict[str, Any], replicas: int, subject: str) -> int:
    """Deployment RollingUpdate 的 ``maxUnavailable``（顆數；整數或百分比向下取整，預設 25%）。

    k8s ``ResolveFenceposts``：``maxSurge`` 與 ``maxUnavailable`` 都解析成 0 時
    ``maxUnavailable`` 當 1——不會多建 pod，所以不是「新舊並存」的情況。
    """
    rolling = _mapping(_mapping(spec.get("strategy")).get("rollingUpdate"))
    unavailable = rolling.get("maxUnavailable")
    surge = rolling.get("maxSurge")
    unavailable_abs = _scaled("25%" if unavailable is None else unavailable, replicas, False, subject)
    surge_abs = _scaled("25%" if surge is None else surge, replicas, True, subject)
    if unavailable_abs == 0 and surge_abs == 0:
        return 1
    return unavailable_abs


def _written_resources(pod_spec: Any) -> Dict[str, Dict[str, Optional[Decimal]]]:
    """pod template 裡每個容器**自己寫**的 resources（解析成數字；不補任何預設）。"""
    spec = _mapping(pod_spec)
    out: Dict[str, Dict[str, Optional[Decimal]]] = {}
    for container in list(spec.get("containers") or []) + list(spec.get("initContainers") or []):
        if not isinstance(container, dict) or not container.get("name"):
            continue
        resources = _mapping(container.get("resources"))
        written: Dict[str, Optional[Decimal]] = {}
        for kind in ("limits", "requests"):
            for dimension, value in _mapping(resources.get(kind)).items():
                written[f"{kind}.{dimension}"] = parse_quantity(value)
        out[str(container["name"])] = written
    return out


def _resources_changed(rendered: Dict[str, Any], live: Optional[Dict[str, Any]]) -> bool:
    """rendered 與 live 的同名容器，resources（解析成數字後）有沒有任何不同。

    比的是 template 上寫的值：API server 對 Pod 補的 requests 預設與 LimitRange 都不會寫回
    workload 的 template，兩邊是同一個基準。只在一邊出現的容器不比（不當成有變）。
    """
    if live is None:
        return False
    new = _written_resources(_template_spec(rendered))
    old = _written_resources(_template_spec(live))
    return any(new[name] != old[name] for name in new if name in old)


def _template_spec(workload: Dict[str, Any]) -> Dict[str, Any]:
    return _mapping(_mapping(_mapping(workload.get("spec")).get("template")).get("spec"))


def _sum(pods: List[Dict[str, Any]], usage: Dict[int, Dict[str, Decimal]], resource: str) -> Optional[Decimal]:
    """這些 pod 在 ``resource`` 上的加總；任何一顆算不出來回 ``None``（未知）。"""
    total = _ZERO
    for pod in pods:
        value = usage[id(pod)].get(resource)
        if value is None:
            return None
        total += value
    return total


def _known_sum(
    pods: List[Dict[str, Any]], usage: Dict[int, Dict[str, Decimal]], resource: str,
) -> Decimal:
    """這些 pod 在 ``resource`` 上**算得出來**的部分加總；算不出來的那幾顆當成 0。"""
    return sum((usage[id(pod)].get(resource, _ZERO) for pod in pods), _ZERO)


def _owner_label(pod: Dict[str, Any]) -> str:
    owner = _controller_owner(pod)
    if owner is None:
        return "Pod/%s" % (_mapping(pod.get("metadata")).get("name") or "?")
    if owner.get("kind") == "ReplicaSet":
        deployment = _replicaset_deployment(owner.get("name"))
        if deployment:
            return f"Deployment/{deployment}"
    return "%s/%s" % (owner.get("kind") or "?", owner.get("name") or "?")


def _consumers(
    others: List[Dict[str, Any]], usage: Dict[int, Dict[str, Decimal]], resource: str,
) -> List[Dict[str, Any]]:
    """其他 workload 依 ``resource`` 佔用由大到小的前幾名（給管理員找出誰佔了配額）。"""
    grouped: Dict[str, Dict[str, Any]] = {}
    for pod in others:
        entry = grouped.setdefault(_owner_label(pod), {"pods": 0, "value": _ZERO})
        entry["pods"] += 1
        entry["value"] += usage[id(pod)].get(resource, _ZERO)
    ranked = sorted(grouped.items(), key=lambda pair: (-pair[1]["value"], pair[0]))
    return [
        {"owner": owner, "pods": entry["pods"],
         "value": format_quantity(resource, entry["value"])}
        for owner, entry in ranked[:_CONSUMER_LIMIT]
    ]


def format_quantity(resource: str, value: Decimal) -> str:
    """數量 → 給人看的字串。cpu 以核為單位（最多 3 位小數）；其餘 ≥ 1Gi 用 Gi、否則 Mi（最多 2 位）。"""
    if resource.rsplit(".", 1)[-1] == "cpu":
        return _trim(value, 3)
    if value >= _GI:
        return _trim(value / _GI, 2) + "Gi"
    return _trim(value / _MI, 2) + "Mi"


def _trim(value: Decimal, places: int) -> str:
    text = format(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _shortfall(resource: str, phase: str, workload: Optional[str], **numbers: Decimal) -> Dict[str, Any]:
    kind, dimension = _split(resource)
    entry: Dict[str, Any] = {
        "resource": resource, "dimension": dimension, "kind": kind,
        "phase": phase, "workload": workload,
    }
    for key in ("hard", "used", "release_current", "other", "required", "available", "short_by"):
        entry[key] = format_quantity(resource, numbers[key])
    return entry


def _fits_without(
    transient_sum: Decimal, in_use: Decimal, wanted: Decimal, hard: Decimal,
) -> bool:
    """``in_use``（含 Job pod）＋``wanted`` 已經放不下時：扣掉 Job pod 之後放不放得下。

    ``transient_sum`` 只含用量算得出來的 Job pod（:func:`_known_sum`）——算不出來的不扣。
    """
    return max(_ZERO, in_use - transient_sum) + wanted <= hard


def _job_advisory(
    resource: str, phase: str, workload: Optional[str], in_use: Decimal, wanted: Decimal,
    transient_sum: Decimal, hard: Decimal,
) -> Dict[str, Any]:
    """只因為正在跑的 Job pod 才放不下時的提醒（不擋）。``job_pods``＝實際扣掉的量。"""
    return {
        "type": "transient_job_usage", "resource": resource, "phase": phase,
        "workload": workload,
        "required": format_quantity(resource, wanted),
        "used": format_quantity(resource, in_use),
        "job_pods": format_quantity(resource, transient_sum),
        "hard": format_quantity(resource, hard),
    }


def evaluate(
    release: str,
    rendered_docs: Optional[Iterable[Any]],
    live_items: Optional[Iterable[Any]],
    pods: Optional[Iterable[Any]],
    quota_items: Optional[Iterable[Any]],
    limitrange_items: Optional[Iterable[Any]],
    pending_hard: Optional[Dict[str, Any]] = None,
    platform_quota_name: Optional[str] = None,
) -> Dict[str, Any]:
    """這個 release 升級後的 pod 放不放得進 namespace 的 ResourceQuota。

    Args:
        release: helm release 名稱（認 live workload 上的 ``meta.helm.sh/release-name``）。
        rendered_docs: ``helm upgrade --dry-run`` 的 manifest（已 parse）。⛔ 不外流。
        live_items: namespace 內 live 的 Deployment／StatefulSet（可夾帶其他 kind）。
        pods／quota_items／limitrange_items: ``get_compute_quota_snapshot`` 的三組 items。
        pending_hard／platform_quota_name: 見 :func:`quota_limits`。

    Returns:
        ``{"status": "ok" | "exceeded", "quota_source": "live" | "pending", "quota": 第一個
        shortfall 的 quota 名或 None, "resources": {...}, "shortfalls": [...],
        "advisories": [...], "workloads": [...], "consumers": [...]}``——數量都是字串。
        ``rolling`` 的 shortfall 在換方案降級時多一鍵 ``reason``
        （:data:`REASON_ROLLING_ON_DOWNGRADE`；其餘情況沒有這一鍵）。advisory 的 ``type``：
        ``rolling_update_headroom``（證明不了 pod 會被換）、``transient_job_usage``（只因為
        正在跑的 Job pod 才放不下）、``cronjob_headroom``。

    Raises:
        Unknown: 資料不足以下判斷（呼叫端放行）。
    """
    result: Dict[str, Any] = {
        "status": "ok",
        "quota_source": "pending" if pending_hard is not None else "live",
        "quota": None, "resources": {}, "shortfalls": [], "advisories": [],
        "workloads": [], "consumers": [],
    }
    limits = {
        resource: entry
        for resource, entry in quota_limits(quota_items, pending_hard, platform_quota_name).items()
        if resource in TRACKED_RESOURCES
    }
    if not limits:
        return result

    live_workloads = [
        item for item in live_items or []
        if isinstance(item, dict) and item.get("kind") in WORKLOAD_KINDS
        and _mapping(item.get("metadata")).get("name")
    ]
    live_index = {(item["kind"], item["metadata"]["name"]): item for item in live_workloads}
    defaults = limitrange_defaults(limitrange_items)

    workloads: List[Dict[str, Any]] = []
    cron_jobs: List[Dict[str, Any]] = []
    for doc in rendered_docs or []:
        if not isinstance(doc, dict):
            continue
        kind = doc.get("kind")
        name = _mapping(doc.get("metadata")).get("name")
        if kind == "CronJob" and name:
            cron_jobs.append(doc)
        if kind not in WORKLOAD_KINDS or not name:
            continue
        subject = f"{kind}/{name}"
        spec = _mapping(doc.get("spec"))
        live = live_index.get((kind, name))
        replicas = _replicas(spec, live, subject)
        if kind == "StatefulSet":
            # StatefulSet 逐顆先刪後建，不會新舊並存；OnDelete 則根本不會自己換 pod。
            strategy = _mapping(spec.get("updateStrategy")).get("type") or "RollingUpdate"
            if strategy == "OnDelete":
                raise Unknown("statefulset_on_delete", subject)
            max_unavailable = None
        else:
            strategy = _mapping(spec.get("strategy")).get("type") or "RollingUpdate"
            max_unavailable = (
                _max_unavailable_abs(spec, replicas, subject)
                if strategy == "RollingUpdate" else None)
        workloads.append({
            "kind": kind, "name": name, "strategy": strategy, "replicas": replicas,
            "per_pod": pod_resources(_template_spec(doc), defaults),
            "max_unavailable_abs": max_unavailable, "rendered": doc, "live": live,
        })
    if not workloads:
        return result
    result["workloads"] = [
        {"kind": w["kind"], "name": w["name"], "strategy": w["strategy"],
         "replicas": w["replicas"],
         "per_pod": {r: format_quantity(r, v) for r, v in w["per_pod"].items()}}
        for w in workloads
    ]

    live_pods = [
        pod for pod in pods or []
        if isinstance(pod, dict)
        and _mapping(pod.get("status")).get("phase") not in _TERMINAL_PHASES
    ]
    rendered_keys = {(w["kind"], w["name"]) for w in workloads}
    own_ids = set()
    own_by_workload: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for item in live_workloads:
        key = (item["kind"], item["metadata"]["name"])
        annotations = _mapping(item["metadata"].get("annotations"))
        if key not in rendered_keys and annotations.get(_RELEASE_ANNOTATION) != release:
            continue
        matched = own_pods(item, live_pods)
        status_replicas = _mapping(item.get("status")).get("replicas")
        if isinstance(status_replicas, int) and status_replicas >= 1 and not matched:
            # workload 說自己有 pod、我們卻一顆都對不到：歸屬規則在這裡不成立，不猜。
            raise Unknown("ownership_mismatch", f"{key[0]}/{key[1]}")
        own_by_workload[key] = matched
        own_ids.update(id(pod) for pod in matched)
    own = [pod for pod in live_pods if id(pod) in own_ids]
    rest = [pod for pod in live_pods if id(pod) not in own_ids]
    leaving = [pod for pod in rest if _mapping(pod.get("metadata")).get("deletionTimestamp")]
    others = [pod for pod in rest if not _mapping(pod.get("metadata")).get("deletionTimestamp")]
    # 其他 workload 當中跑完就釋出的那些（Job 的 pod）：只因為它們才放不下時不擋。
    transient = [
        pod for pod in others
        if (_controller_owner(pod) or {}).get("kind") in _TRANSIENT_OWNER_KINDS]
    usage = {id(pod): pod_resources(pod.get("spec"), None) for pod in live_pods}
    downgraded = pending_reductions(quota_items, pending_hard, platform_quota_name)

    judged: Dict[str, Dict[str, Decimal]] = {}
    steady = set()
    for resource in TRACKED_RESOURCES:
        if resource not in limits:
            continue
        if any(resource not in w["per_pod"] for w in workloads):
            continue
        own_sum = _sum(own, usage, resource)
        if own_sum is None:
            continue
        hard, used = limits[resource]["hard"], limits[resource]["used"]
        leaving_sum = _sum(leaving, usage, resource)
        others_sum = _sum(others, usage, resource)
        # Job pod 佔了多少：只加總算得出來的那些，算不出來的當成 0（＝不扣）。⛔ 不要改回「有
        # 一顆算不出來就當成扣掉它們放得下」——那會讓確定放不下的升級整段被放行（見檔頭）。
        transient_sum = _known_sum(transient, usage, resource)
        # 其他 workload 的佔用：兩種估法取小（status.used 落後時偏高，見檔頭）。
        estimates = []
        if leaving_sum is not None:
            estimates.append(max(_ZERO, used - own_sum - leaving_sum))
        if others_sum is not None:
            estimates.append(others_sum)
        if not estimates:
            continue
        base = min(estimates)
        need = sum((w["replicas"] * w["per_pod"][resource] for w in workloads), _ZERO)
        numbers = {
            "hard": hard, "used": used, "release_current": own_sum, "other": base,
            "required": need, "available": max(hard - base, _ZERO),
        }
        judged[resource] = numbers
        result["resources"][resource] = {
            key: format_quantity(resource, value) for key, value in numbers.items()}

        # Tier 1：升級完成後本 release 的 pod 全部存在時放不放得下。需求為 0 的資源
        # admission 不檢查（quota 只驗有要求的資源），不擋。
        if need > 0 and base + need > hard:
            if transient and _fits_without(transient_sum, base, need, hard):
                # 只因為正在跑的 Job pod 才放不下：它們跑完就釋出，不是確定放不下。
                # 不 continue——換 pod 的過程（Tier 2）照樣要看。
                result["advisories"].append(_job_advisory(
                    resource, "steady", None, base, need, transient_sum, hard))
            else:
                steady.add(resource)
                result["shortfalls"].append(_shortfall(
                    resource, "steady", None, short_by=base + need - hard, **numbers))
                continue

        # Tier 2：新 pod 要先建出來、舊 pod 才會被砍的 Deployment，要多放得下一顆。
        now_estimates = []
        if leaving_sum is not None:
            now_estimates.append(max(_ZERO, used - leaving_sum))
        if others_sum is not None:
            now_estimates.append(own_sum + others_sum)
        used_now = min(now_estimates)
        for w in workloads:
            own_here = own_by_workload.get(("Deployment", w["name"])) or []
            if (w["kind"] != "Deployment" or w["strategy"] != "RollingUpdate"
                    or w["max_unavailable_abs"] != 0 or w["replicas"] < 1 or not own_here):
                continue
            per_pod = w["per_pod"][resource]
            if per_pod <= 0:
                continue
            # 同一次升級把 replicas 調小：controller 先把舊 ReplicaSet 縮到新的 replicas
            # （scaling event 先於 rollout），之後才多建新 pod。會被縮掉的是哪幾顆算不準，
            # 扣最大的那幾顆（偏放行）。
            surplus = len(own_here) - w["replicas"]
            freed = sum(sorted(
                (usage[id(pod)].get(resource, _ZERO) for pod in own_here),
                reverse=True)[:max(surplus, 0)], _ZERO)
            before_surge = max(_ZERO, used_now - freed)
            if before_surge + per_pod <= hard:
                continue
            subject = "Deployment/%s" % w["name"]
            if transient and _fits_without(transient_sum, before_surge, per_pod, hard):
                result["advisories"].append(_job_advisory(
                    resource, "rolling", subject, before_surge, per_pod, transient_sum, hard))
            elif _resources_changed(w["rendered"], w["live"]):
                entry = _shortfall(
                    resource, "rolling", subject,
                    hard=hard, used=before_surge, release_current=own_sum, other=base,
                    required=per_pod, available=max(hard - before_surge, _ZERO),
                    short_by=before_surge + per_pod - hard)
                if (resource in downgraded
                        and limits[resource]["quota"] == platform_quota_name):
                    # 換方案降級：將推送的上限比現在小，新舊 pod 並存時才放不下。照樣擋
                    # （推送之後那個升級確實會卡住），但原因不是「其他服務佔太多」。只在
                    # 卡住的就是那個變小的上限時才這樣標（最緊的是別的 quota 就不是）。
                    entry["reason"] = REASON_ROLLING_ON_DOWNGRADE
                result["shortfalls"].append(entry)
            else:
                # 證明不了這次 upgrade 會換 pod（template 沒變就不會），只提醒不擋。
                result["advisories"].append({
                    "type": "rolling_update_headroom", "resource": resource,
                    "workload": subject,
                    "required": format_quantity(resource, per_pod),
                    "used": format_quantity(resource, before_surge),
                    "hard": format_quantity(resource, hard),
                })

    # CronJob 不計入需求（不是常駐的 pod）；跑的時候放不下只提醒。
    for doc in cron_jobs:
        job_spec = _mapping(_mapping(_mapping(doc.get("spec")).get("jobTemplate")).get("spec"))
        try:
            per_run = pod_resources(_mapping(_mapping(job_spec.get("template")).get("spec")), defaults)
        except Unknown:
            continue
        parallelism = job_spec.get("parallelism")
        runs = parallelism if isinstance(parallelism, int) and not isinstance(parallelism, bool) \
            and parallelism > 0 else 1
        for resource, numbers in judged.items():
            if resource in steady or resource not in per_run:
                continue
            required = runs * per_run[resource]
            headroom = numbers["hard"] - numbers["other"] - numbers["required"]
            if required > 0 and headroom < required:
                result["advisories"].append({
                    "type": "cronjob_headroom", "resource": resource,
                    "workload": "CronJob/%s" % doc["metadata"]["name"],
                    "required": format_quantity(resource, required),
                    "available": format_quantity(resource, max(headroom, _ZERO)),
                })

    if result["shortfalls"]:
        first = result["shortfalls"][0]["resource"]
        result["status"] = "exceeded"
        result["quota"] = limits[first]["quota"]
        result["consumers"] = _consumers(others, usage, first)
    return result


def build_error_detail(namespace: str, release: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """409 的 ``detail``：機器可讀（error_code／shortfalls／consumers）＋給管理員的中文摘要。

    只取 :func:`evaluate` 算出來的欄位——不含 rendered manifest 的任何內容。
    """
    lines = ["升級前檢查發現工作區的資源配額放不下這次變更，已停止升級（沒有套用任何變更）："]
    for item in result.get("shortfalls") or []:
        label = _RESOURCE_LABELS.get(item["resource"], item["resource"])
        if item["phase"] == "rolling":
            # 降級：上限是平台將推送的那個（比現在小），不是「其他 workload 佔太多」。
            limit = ("換方案後的配額上限（比現在小）"
                     if item.get("reason") == REASON_ROLLING_ON_DOWNGRADE else "配額上限")
            lines.append(
                "・%s（%s）：%s 更新時新舊 pod 會同時存在，還需要 %s，目前已佔用 %s，"
                "%s %s，還差 %s" % (
                    label, item["resource"], item["workload"], item["required"],
                    item["used"], limit, item["hard"], item["short_by"]))
        else:
            lines.append(
                "・%s（%s）：這個服務升級後需要 %s，配額上限 %s，其他 workload 已佔用 %s，"
                "還差 %s" % (
                    label, item["resource"], item["required"], item["hard"],
                    item["other"], item["short_by"]))
    return {
        "error_code": COMPUTE_QUOTA_ERROR_CODE,
        "namespace": namespace,
        "release": release,
        "quota": result.get("quota"),
        "quota_source": result.get("quota_source"),
        "shortfalls": list(result.get("shortfalls") or []),
        "workloads": list(result.get("workloads") or []),
        "consumers": list(result.get("consumers") or []),
        "message": "\n".join(lines),
    }
