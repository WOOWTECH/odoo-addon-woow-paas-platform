"""#1407: refuse an upgrade that Kubernetes would reject — BEFORE helm applies anything.

``helm upgrade`` is not transactional: it patches resources one by one, so when the
API server rejects one of them (an immutable field), the release is marked
``failed`` while the resources patched before it already run the new chart. That is
how ``svc-800e44fe`` (home-assistant-full) ended up with a 0.1.5 Deployment under a
failed release, and the admin only saw "Helm upgrade failed. Check operator logs".

The post-renderer already reuses live values where that is always safe
(``pvc_keep_post_renderer.reuse_live_values``). This module checks the rendered
manifest (``helm upgrade --dry-run``, same post-renderer) against the live objects
for whatever is still immutable and different, and turns both those conflicts and
raw helm failures into a Traditional-Chinese message naming the object and field.

Pure functions only — the caller fetches the manifest and the live objects.
"""
import re
from typing import Any, Dict, Iterable, List, Optional

import yaml

from src.pvc_keep_post_renderer import claim_facts, parse_quantity

_KIND_LABEL = {
    "PersistentVolumeClaim": "磁碟（PVC）",
    "StatefulSet": "StatefulSet",
    "Deployment": "Deployment",
}


def _conflict(kind, name, field, live, rendered, hint=""):
    return {
        "kind": kind, "name": name, "field": field,
        "live": live, "rendered": rendered, "hint": hint,
    }


def _claim_conflicts(kind, name, live_spec, rendered_spec, prefix=""):
    live, new = claim_facts(live_spec), claim_facts(rendered_spec)
    out = []
    if new["storageClassName"] and live["storageClassName"] and new["storageClassName"] != live["storageClassName"]:
        out.append(_conflict(kind, name, prefix + "storageClassName",
                             live["storageClassName"], new["storageClassName"]))
    if new["accessModes"] and live["accessModes"] and sorted(new["accessModes"]) != sorted(live["accessModes"]):
        out.append(_conflict(kind, name, prefix + "accessModes",
                             live["accessModes"], new["accessModes"]))
    if new["volumeMode"] and live["volumeMode"] and new["volumeMode"] != live["volumeMode"]:
        out.append(_conflict(kind, name, prefix + "volumeMode",
                             live["volumeMode"], new["volumeMode"]))
    live_size, new_size = parse_quantity(live["storage"]), parse_quantity(new["storage"])
    if live_size is not None and new_size is not None and new_size < live_size:
        out.append(_conflict(kind, name, prefix + "resources.requests.storage",
                             live["storage"], new["storage"], "容量只能加大，不能縮小"))
    return out


def _match_labels(obj):
    return ((obj.get("spec") or {}).get("selector") or {}).get("matchLabels")


def find_immutable_conflicts(rendered_docs: Iterable[Any], live_items: Iterable[Dict]) -> List[Dict]:
    """Conflicts between rendered objects and the live objects of the same kind/name."""
    live = {}
    for item in live_items or []:
        meta = item.get("metadata") or {}
        if meta.get("name"):
            live[(item.get("kind"), meta["name"])] = item
    conflicts = []
    for doc in rendered_docs:
        if not isinstance(doc, dict):
            continue
        kind = doc.get("kind")
        name = (doc.get("metadata") or {}).get("name")
        current = live.get((kind, name))
        if current is None:
            continue
        if kind == "PersistentVolumeClaim":
            conflicts += _claim_conflicts(kind, name, current.get("spec"), doc.get("spec"))
        elif kind in ("Deployment", "StatefulSet"):
            new_sel, live_sel = _match_labels(doc), _match_labels(current)
            if new_sel is not None and live_sel is not None and new_sel != live_sel:
                conflicts.append(_conflict(kind, name, "spec.selector.matchLabels",
                                           live_sel, new_sel))
        if kind == "StatefulSet":
            new_spec, live_spec = doc.get("spec") or {}, current.get("spec") or {}
            for field in ("serviceName", "podManagementPolicy"):
                if new_spec.get(field) and live_spec.get(field) and new_spec[field] != live_spec[field]:
                    conflicts.append(_conflict(kind, name, "spec." + field,
                                               live_spec[field], new_spec[field]))
            new_t = {(t.get("metadata") or {}).get("name"): t
                     for t in new_spec.get("volumeClaimTemplates") or []}
            live_t = {(t.get("metadata") or {}).get("name"): t
                      for t in live_spec.get("volumeClaimTemplates") or []}
            if set(new_t) != set(live_t):
                conflicts.append(_conflict(
                    kind, name, "spec.volumeClaimTemplates",
                    sorted(n for n in live_t if n), sorted(n for n in new_t if n),
                    "新版 chart 增減了磁碟範本，StatefulSet 不能就地修改，需要人工遷移"))
            for tname in sorted(n for n in set(new_t) & set(live_t) if n):
                conflicts += _claim_conflicts(
                    kind, name, live_t[tname].get("spec"), new_t[tname].get("spec"),
                    prefix="volumeClaimTemplates[%s]." % tname)
    return conflicts


#: #1513：PVC 擴容被 Kubernetes／Longhorn 拒絕的 409 ``detail.error_code``。平台
#: （woow_paas_platform ``api_service_change_plan``）認這個字串，改名要兩邊一起改。
STORAGE_EXPANSION_ERROR_CODE = "storage_expansion_rejected"

#: Longhorn validator 拒絕擴容的特徵（longhorn-manager v1.10.2
#: ``ReplicaScheduler.CheckReplicasSizeExpansion``）。
_LONGHORN_CAPACITY_MARKERS = ("CheckReplicasSizeExpansion", "cannot schedule")
#: API server 明確拒絕的前綴（kubectl 把 HTTP 403／422 印成這兩種）。其他失敗（逾時、
#: 連不上、5xx）都是「不知道」。
_API_REJECTION_PREFIXES = ("Error from server (Forbidden)", "Error from server (Invalid)")


def find_pvc_expansions(rendered_docs: Iterable[Any], live_items: Iterable[Dict]) -> List[Dict]:
    """#1513：rendered 與 live 同名的 PVC 中，要求容量**變大**的那些。

    縮小不在這裡（那是 ``find_immutable_conflicts`` 的衝突）；新建的 PVC 不在這裡（那是
    #1415 開通預檢的範圍）；StatefulSet ``volumeClaimTemplates`` 不能就地擴容，也不在這裡。
    """
    live = {}
    for item in live_items or []:
        meta = item.get("metadata") or {}
        if item.get("kind") == "PersistentVolumeClaim" and meta.get("name"):
            live[meta["name"]] = item
    out = []
    for doc in rendered_docs:
        if not isinstance(doc, dict) or doc.get("kind") != "PersistentVolumeClaim":
            continue
        name = (doc.get("metadata") or {}).get("name")
        current = live.get(name)
        if current is None:
            continue
        old = claim_facts(current.get("spec"))["storage"]
        new = claim_facts(doc.get("spec"))["storage"]
        old_size, new_size = parse_quantity(old), parse_quantity(new)
        if old_size is not None and new_size is not None and new_size > old_size:
            exp = {"name": name, "live": old, "rendered": new}
            volume = (current.get("spec") or {}).get("volumeName")
            if volume:
                exp["volume"] = volume
            out.append(exp)
    return out


def longhorn_volume_expansion_blocker(volume: Any) -> Optional[str]:
    """#1513：Longhorn volume 物件 → 不能擴容的原因（中文），可以擴或看不懂回 None。

    longhorn-manager v1.10.2 ``VolumeManager.Expand``：volume 的 ``Scheduled`` 條件不是 True
    （replica 沒排滿，叢集額度不足時以 degraded 建出來的 volume 就是這樣）一律拒絕
    "cannot expand volume before replica scheduling success"。admission webhook 只檢查**已排上**
    的 replica 所在磁碟，看不到這件事——#1158 e2e 實測：helm upgrade 成功、PVC 永遠停在 Resizing。
    """
    if not isinstance(volume, dict):
        return None
    conditions = (volume.get("status") or {}).get("conditions")
    if isinstance(conditions, dict):  # 舊版 Longhorn 以 type 為 key
        conditions = [dict(c, type=t) for t, c in conditions.items() if isinstance(c, dict)]
    if not isinstance(conditions, list):
        return None
    for cond in conditions:
        if isinstance(cond, dict) and cond.get("type") == "Scheduled":
            if cond.get("status") == "True":
                return None
            return "Longhorn volume 的 replica 尚未全部排上（%s），Longhorn 不允許擴容" % (
                cond.get("message") or cond.get("reason") or "Scheduled=%s" % cond.get("status"))
    return None


def classify_expansion_rejection(stderr: Optional[str]) -> Optional[str]:
    """``kubectl patch --dry-run=server`` 的失敗 → ``capacity``／``rejected``／None（不知道）。

    ``capacity``＝Longhorn 排程額度放不下；``rejected``＝API server 明確拒絕（例如
    StorageClass 不支援擴容）——兩者真正的 helm upgrade 都一定會在同一步失敗。
    """
    text = (stderr or "").strip()
    if not text.startswith(_API_REJECTION_PREFIXES):
        return None
    if "validator.longhorn.io" in text and all(m in text for m in _LONGHORN_CAPACITY_MARKERS):
        return "capacity"
    return "rejected"


def build_expansion_error_detail(namespace: str, denials: List[Dict]) -> Dict[str, Any]:
    """#1513 的 409 ``detail``：機器可讀（error_code／reason／pvcs）＋給管理員的中文訊息。"""
    reason = "capacity" if all(d["reason"] == "capacity" for d in denials) else "rejected"
    lines = ["升級前檢查發現磁碟無法擴充，已停止升級（沒有套用任何變更）："]
    for d in denials:
        why = ("叢集儲存（Longhorn）排程額度不足" if d["reason"] == "capacity"
               else "Kubernetes 拒絕擴容")
        lines.append("・磁碟（PVC）「%s」%s → %s：%s（%s）" % (
            d["name"], d["live"], d["rendered"], why, (d.get("message") or "")[:300]))
    return {
        "error_code": STORAGE_EXPANSION_ERROR_CODE,
        "reason": reason,
        "namespace": namespace,
        "pvcs": [{k: d.get(k) for k in ("name", "live", "rendered", "reason")} for d in denials],
        "message": "\n".join(lines),
    }


def _fmt(value):
    if isinstance(value, (list, dict)):
        return yaml.safe_dump(value, default_flow_style=True, allow_unicode=True).strip()
    return str(value)


def describe_conflicts(conflicts: List[Dict]) -> str:
    """Admin-facing Traditional-Chinese summary (one line per conflict)."""
    lines = ["升級前檢查發現下列欄位建立後不能修改，已停止升級（沒有套用任何變更）："]
    for c in conflicts:
        line = "・%s「%s」的 %s：現在是 %s，新版要改成 %s" % (
            _KIND_LABEL.get(c["kind"], c["kind"]), c["name"], c["field"],
            _fmt(c["live"]), _fmt(c["rendered"]))
        if c.get("hint"):
            line += "（%s）" % c["hint"]
        lines.append(line)
    return "\n".join(lines)


_CANNOT_PATCH = re.compile(
    r'cannot patch "(?P<name>[^"]+)" with kind (?P<kind>\w+): .*?is invalid: (?P<why>[^\n]+)')
_FORBIDDEN_FIELD = re.compile(r'(?P<field>[\w.\[\]]+): Forbidden: (?P<msg>[^,\n]+)')
_EXISTS = re.compile(
    r'(?P<kind>\w+) "(?P<name>[^"]+)" in namespace "[^"]+" exists and cannot be imported')


def describe_helm_failure(stderr: Optional[str], action: str = "升級") -> str:
    """Raw helm stderr → a short Traditional-Chinese reason (never the whole log)."""
    text = (stderr or "").strip()
    m = _CANNOT_PATCH.search(text)
    if m:
        kind = _KIND_LABEL.get(m.group("kind"), m.group("kind"))
        f = _FORBIDDEN_FIELD.search(m.group("why"))
        if f:
            return "Helm %s失敗：%s「%s」的 %s 建立後不能修改（%s）。release 可能已部分套用，請先修正後再重新部署。" % (
                action, kind, m.group("name"), f.group("field"), f.group("msg").strip())
        return "Helm %s失敗：Kubernetes 拒絕修改%s「%s」：%s" % (
            action, kind, m.group("name"), m.group("why").strip()[:300])
    m = _EXISTS.search(text)
    if m:
        return "Helm %s失敗：%s「%s」已經存在，但不屬於這個服務的 release，不能接手。" % (
            action, _KIND_LABEL.get(m.group("kind"), m.group("kind")), m.group("name"))
    lowered = text.lower()
    if "another operation" in lowered and "in progress" in lowered:
        # #1552：上一次 helm 作業被中斷（operator OOMKilled／重啟）會讓 release 停在
        # pending-*，之後每一次 upgrade 都撞這句。沙盒實測：helm rollback 會把它收掉。
        return ("Helm %s失敗：這個服務上一次的 Helm 作業被中斷，release 停在「進行中」，"
                "新的設定無法套用。請先用「回復版本」回到上一個正常版本，再重新送出。" % action)
    if "timed out" in lowered or "deadline exceeded" in lowered:
        return "Helm %s失敗：等待資源就緒逾時。" % action
    if "not found" in lowered and ("chart" in lowered or "manifest unknown" in lowered):
        return "Helm %s失敗：找不到指定版本的 chart（%s）。" % (action, text.splitlines()[-1][:200])
    last = text.splitlines()[-1] if text else ""
    if last.startswith("Error: "):
        last = last[len("Error: "):]
    return "Helm %s失敗：%s" % (action, last[:300] or "原因不明，請查看 operator log。")
