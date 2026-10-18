"""租戶 workload 的兩條 render 期政策（商用驗收 AI-FIX-NET-1：#1497／INFRA-XSVC-007）。

由 helm post-renderer（``pvc_keep_post_renderer.py``）在 install／upgrade 時對 chart render
出來的 manifest 套用，所以**所有** chart（現有與未來）一次涵蓋，不必逐 chart 改版：

1. **egress gate**（#1497 NetworkPolicy 啟動空窗）。k3s 內建的 kube-router 在 pod 出現後才
   補 iptables，新 pod 最初 0.1–2.6 秒不受 ``paas-baseline-egress`` 約束（實測 24 次中
   23 次在窗內連得到 k8s API、kubelet、他 ns 的 pod）。可以執行任意碼的 SKU（Code Server、
   pi-agent、n8n Code 節點）在 Redeploy 拿到新 pod 時可利用。政策沒有辦法在 CNI 端提早，所以
   在每個 pod template 最前面插一個 init container：反覆探測 k8s API ClusterIP，**連續兩次連
   不到**（＝ egress 規則已套用）才放行，應用程式容器因此從第 0 秒起就在規則之內。
2. **節點迴避**（INFRA-XSVC-007）。ubuntuserver-59mqj 是正式站與 Artifactory 所在、10-07 曾
   停擺的節點，租戶負載不應落在上面。在每個 pod template 加 ``requiredDuringScheduling``
   nodeAffinity ``kubernetes.io/hostname NotIn [...]``；既有 affinity 保留並與之 AND。

這個模組只放**純函式**，只 import 標準函式庫——post-renderer 以腳本方式執行（沒有 package
context），必須能被 ``from tenant_workload_policy import …`` 與
``from src.tenant_workload_policy import …`` 兩種方式載入。

設計原則：
* 預設全關（呼叫端不給 options＝什麼都不改）；任何一步例外都原樣放行（呼叫端包 try/except），
  政策缺陷不能讓租戶的 install／upgrade 失敗。
* 冪等：重跑不會重複插入；已存在的同名 init container／affinity 條件不動。
* 不替 pod 加它原本沒有的能力：gate 容器 non-root、drop ALL、唯讀根檔案系統、seccomp
  RuntimeDefault（通過 baseline 與 restricted），資源明寫（不吃 LimitRange 的預設）。
"""
from __future__ import annotations

import copy
import ipaddress
from typing import Any, Dict, Iterable, List, Optional, Tuple

GATE_CONTAINER_NAME = "woow-egress-gate"
GATE_SKIP_ANNOTATION = "paas.woowtech.io/egress-gate"   # 值 "skip"＝這個 pod template 不插 gate
GATE_DEFAULT_IMAGE = "busybox:1.36"                        # 與 woow-paas-charts 現有 init container 同一個，節點上已有快取
GATE_DEFAULT_TIMEOUT_SECONDS = 60
GATE_ON_TIMEOUT_CHOICES = ("proceed", "fail")
# 主探測目標（k8s API ClusterIP）被 chart 自己的 NetworkPolicy 放行時（例：open-design console 明確放
# 10.43.0.1/32），改探測所在節點的 kubelet：``status.hostIP:10250``。baseline 的 192.168.0.0/16
# 排除一定涵蓋它，而且空窗期間實測連得到（X-SEC-003）。
GATE_ALT_PORT = 10250
GATE_ALT_SAMPLE_IP = "192.168.10.21"      # 只用來靜態判斷 chart NP 是否放行「節點 IP」，不連線
HOSTNAME_LABEL = "kubernetes.io/hostname"

# 「政策已生效」的判準：連續幾次連不到探測目標。探測目標預設是 k8s API 的 ClusterIP
# （KUBERNETES_SERVICE_HOST:PORT，每個 pod 都有），baseline 的 10.0.0.0/8 排除一定包含它。
GATE_CONSECUTIVE_BLOCKED = 2

# busybox 1.36 的 ash＋nc。``nc -z -w 1``：連得上＝政策還沒生效；連不上（refused 或 1 秒逾時）
# ＝已被擋。連續 GATE_CONSECUTIVE_BLOCKED 次才算（避免 API 瞬斷誤判）。逾時處理由
# GATE_ON_TIMEOUT 決定：proceed（預設，失效時退回修補前的行為，不讓服務起不來）或 fail。
GATE_SCRIPT = r"""
T="${GATE_TARGET:-}"
if [ -z "$T" ]; then T="${KUBERNETES_SERVICE_HOST:-10.43.0.1}:${KUBERNETES_SERVICE_PORT:-443}"; fi
H="${T%:*}"; P="${T##*:}"
START=$(date +%s); BLOCKED=0; N=0
while :; do
  N=$((N+1))
  if nc -z -w 1 "$H" "$P" >/dev/null 2>&1; then BLOCKED=0; else BLOCKED=$((BLOCKED+1)); fi
  if [ "$BLOCKED" -ge "${GATE_CONSECUTIVE:-2}" ]; then
    echo "egress-gate: policy active (target $T blocked x$BLOCKED, probes=$N, $(( $(date +%s) - START ))s)"
    exit 0
  fi
  if [ $(( $(date +%s) - START )) -ge "${GATE_TIMEOUT:-60}" ]; then break; fi
  sleep 0.2
done
echo "egress-gate: policy NOT observed within ${GATE_TIMEOUT:-60}s (target $T, probes=$N)" >&2
if [ "${GATE_ON_TIMEOUT:-proceed}" = "fail" ]; then exit 1; fi
echo "egress-gate: proceeding (on_timeout=proceed)" >&2
exit 0
""".strip()


# ---------------------------------------------------------------------------
# pod template 定位
# ---------------------------------------------------------------------------

_SPEC_TEMPLATE_KINDS = ("Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job")


def pod_templates(doc: Any) -> List[Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]]:
    """回傳 ``[(pod_spec, pod_labels, pod_annotations), …]``（都是 doc 內的**同一份**物件）。

    * Deployment／StatefulSet／DaemonSet／ReplicaSet／Job：``spec.template``
    * CronJob：``spec.jobTemplate.spec.template``
    * Pod：``spec`` 本身
    * KubeVirt VirtualMachine：``spec.template.spec``（VMI spec；只有 ``affinity`` 有意義，
      呼叫端用 ``is_pod_workload`` 區分，gate 不插進 VM）
    """
    if not isinstance(doc, dict):
        return []
    kind = doc.get("kind")
    spec = doc.get("spec")
    if not isinstance(spec, dict):
        return []
    tmpl: Optional[Dict[str, Any]] = None
    if kind in _SPEC_TEMPLATE_KINDS or kind == "VirtualMachine":
        tmpl = spec.get("template")
    elif kind == "CronJob":
        tmpl = ((spec.get("jobTemplate") or {}).get("spec") or {}).get("template")
    elif kind == "Pod":
        meta = doc.get("metadata") or {}
        return [(spec, meta.get("labels") or {}, meta.get("annotations") or {})]
    if not isinstance(tmpl, dict):
        return []
    pod_spec = tmpl.get("spec")
    if not isinstance(pod_spec, dict):
        return []
    meta = tmpl.get("metadata")
    if not isinstance(meta, dict):
        meta = {}
    labels = meta.get("labels")
    if not isinstance(labels, dict):
        labels = {}
    annotations = meta.get("annotations")
    if not isinstance(annotations, dict):
        annotations = {}
    return [(pod_spec, labels, annotations)]


def is_pod_workload(doc: Any) -> bool:
    """會產生一般 pod 的 kind（VM 的 virt-launcher 由 KubeVirt 建，插不進 init container）。"""
    return isinstance(doc, dict) and doc.get("kind") in _SPEC_TEMPLATE_KINDS + ("CronJob", "Pod")


def template_is_mutable(doc: Any) -> bool:
    """upgrade 時改 pod template 不會被 API server 以 immutable 拒絕。

    Job.spec.template 與 Pod.spec 建立後不可變：非 helm hook 的 Job／Pod 一旦被改，``helm
    upgrade`` 會整個失敗（field is immutable）。helm hook 每次都會刪掉重建，不受影響。
    其餘 kind（Deployment／StatefulSet／DaemonSet／CronJob／VirtualMachine）的 template 可變。
    """
    if not isinstance(doc, dict):
        return False
    if doc.get("kind") in ("Job", "Pod"):
        annotations = (doc.get("metadata") or {}).get("annotations") or {}
        return "helm.sh/hook" in annotations
    return True


# ---------------------------------------------------------------------------
# 節點迴避
# ---------------------------------------------------------------------------

def _hostname_notin_values(expr: Dict[str, Any]) -> List[str]:
    if expr.get("key") == HOSTNAME_LABEL and expr.get("operator") == "NotIn":
        return [str(v) for v in (expr.get("values") or [])]
    return []


def has_node_avoidance(pod_spec: Dict[str, Any], nodes: Iterable[str]) -> bool:
    """每個 nodeSelectorTerm 都已經帶有涵蓋 ``nodes`` 的 hostname NotIn。"""
    need = set(nodes)
    req = (((pod_spec.get("affinity") or {}).get("nodeAffinity") or {})
           .get("requiredDuringSchedulingIgnoredDuringExecution") or {})
    terms = req.get("nodeSelectorTerms") or []
    if not terms:
        return False
    for term in terms:
        have: set = set()
        for expr in (term.get("matchExpressions") or []):
            have.update(_hostname_notin_values(expr))
        if not need <= have:
            return False
    return True


def add_node_avoidance(pod_spec: Dict[str, Any], nodes: List[str]) -> str:
    """把 ``hostname NotIn nodes`` 以 AND 加進 pod 的 required nodeAffinity。

    nodeSelectorTerms 之間是 OR，所以 chart 已有 term 時，要把條件加進**每一個** term（只加一
    個新 term 反而等於放寬）。回傳結果字串（供記錄）：``added``／``present``／
    ``skip-pinned``／``skip-selector-conflict``／``skip-empty``。
    """
    nodes = [n for n in nodes if n]
    if not nodes:
        return "skip-empty"
    if pod_spec.get("nodeName"):
        return "skip-pinned"        # 已經釘死節點（含釘在迴避節點的情形）：不動，交給 chart 作者
    selector = pod_spec.get("nodeSelector") or {}
    if isinstance(selector, dict) and selector.get(HOSTNAME_LABEL) in nodes:
        return "skip-selector-conflict"   # 與迴避清單矛盾，硬加只會讓 pod 永遠 Pending
    if has_node_avoidance(pod_spec, nodes):
        return "present"
    affinity = pod_spec.get("affinity")
    if not isinstance(affinity, dict):
        affinity = pod_spec["affinity"] = {}
    node_aff = affinity.get("nodeAffinity")
    if not isinstance(node_aff, dict):
        node_aff = affinity["nodeAffinity"] = {}
    required = node_aff.get("requiredDuringSchedulingIgnoredDuringExecution")
    if not isinstance(required, dict):
        required = node_aff["requiredDuringSchedulingIgnoredDuringExecution"] = {}
    terms = required.get("nodeSelectorTerms")
    if not isinstance(terms, list) or not terms:
        terms = required["nodeSelectorTerms"] = [{}]
    for term in terms:
        exprs = term.get("matchExpressions")
        if not isinstance(exprs, list):
            exprs = term["matchExpressions"] = []
        for expr in exprs:
            if _hostname_notin_values(expr):
                merged = list(dict.fromkeys(_hostname_notin_values(expr) + list(nodes)))
                expr["values"] = merged
                break
        else:
            exprs.append({"key": HOSTNAME_LABEL, "operator": "NotIn", "values": list(nodes)})
    return "added"


# ---------------------------------------------------------------------------
# egress gate
# ---------------------------------------------------------------------------

def build_egress_gate_container(
    image: str = GATE_DEFAULT_IMAGE,
    timeout_seconds: int = GATE_DEFAULT_TIMEOUT_SECONDS,
    on_timeout: str = "proceed",
    target: str = "",
    probe_host_ip: bool = False,
) -> Dict[str, Any]:
    """gate init container。資源與 securityContext 全部明寫（不依賴 LimitRange 預設）。

    ``probe_host_ip``：探測所在節點的 kubelet（``status.hostIP:10250``），用在主目標被 chart
    自己的 NetworkPolicy 放行的 pod。明確指定的 ``target`` 優先。
    """
    if on_timeout not in GATE_ON_TIMEOUT_CHOICES:
        on_timeout = "proceed"
    env: List[Dict[str, Any]] = [
        {"name": "GATE_TIMEOUT", "value": str(int(timeout_seconds))},
        {"name": "GATE_ON_TIMEOUT", "value": on_timeout},
        {"name": "GATE_CONSECUTIVE", "value": str(GATE_CONSECUTIVE_BLOCKED)},
    ]
    if target:
        env.append({"name": "GATE_TARGET", "value": target})
    elif probe_host_ip:
        # K8s 會展開 $(VAR)，而且只能引用「在它前面」定義的 env。
        env.append({"name": "GATE_HOST_IP", "valueFrom": {"fieldRef": {"fieldPath": "status.hostIP"}}})
        env.append({"name": "GATE_TARGET", "value": "$(GATE_HOST_IP):%d" % GATE_ALT_PORT})
    return {
        "name": GATE_CONTAINER_NAME,
        "image": image,
        "imagePullPolicy": "IfNotPresent",
        "command": ["sh", "-c", GATE_SCRIPT],
        "env": env,
        "resources": {
            "requests": {"cpu": "5m", "memory": "8Mi", "ephemeral-storage": "1Mi"},
            "limits": {"cpu": "50m", "memory": "32Mi", "ephemeral-storage": "16Mi"},
        },
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": 65534,
            "runAsGroup": 65534,
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
            "seccompProfile": {"type": "RuntimeDefault"},
        },
    }


def has_egress_gate(pod_spec: Dict[str, Any]) -> bool:
    return any(
        isinstance(c, dict) and c.get("name") == GATE_CONTAINER_NAME
        for c in (pod_spec.get("initContainers") or [])
    )


def inject_egress_gate(pod_spec: Dict[str, Any], gate: Dict[str, Any]) -> str:
    """把 gate 放在 initContainers 最前面。回傳 ``added``／``present``／``skip-host-network``。"""
    if pod_spec.get("hostNetwork"):
        return "skip-host-network"      # hostNetwork pod 不受 NetworkPolicy 約束，gate 永遠等不到
    if has_egress_gate(pod_spec):
        return "present"
    inits = pod_spec.get("initContainers")
    if not isinstance(inits, list):
        inits = []
    pod_spec["initContainers"] = [copy.deepcopy(gate)] + inits
    return "added"


# --- chart 自帶的 egress NetworkPolicy 若本來就放行探測目標，gate 永遠等不到 -----------------

def _selector_matches(labels: Dict[str, str], selector: Optional[Dict[str, Any]]) -> bool:
    """K8s LabelSelector 對 ``labels`` 的比對（matchLabels＋matchExpressions；空 selector＝全部）。"""
    if not selector:
        return True
    for k, v in (selector.get("matchLabels") or {}).items():
        if str(labels.get(k)) != str(v) or k not in labels:
            return False
    for expr in (selector.get("matchExpressions") or []):
        key, op, vals = expr.get("key"), expr.get("operator"), [str(v) for v in (expr.get("values") or [])]
        has = key in labels
        if op == "In":
            if not (has and str(labels[key]) in vals):
                return False
        elif op == "NotIn":
            if has and str(labels[key]) in vals:
                return False
        elif op == "Exists":
            if not has:
                return False
        elif op == "DoesNotExist":
            if has:
                return False
        else:
            return False            # 不認得的運算子：保守視為不符合
    return True


def _port_of(target: str) -> Tuple[Optional[str], Optional[int]]:
    if not target or ":" not in target:
        return None, None
    host, _, port = target.rpartition(":")
    try:
        return host, int(port)
    except ValueError:
        return host, None


def _ip_in_block(ip: str, block: Dict[str, Any]) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
        if addr not in ipaddress.ip_network(block.get("cidr", ""), strict=False):
            return False
        for ex in (block.get("except") or []):
            if addr in ipaddress.ip_network(ex, strict=False):
                return False
        return True
    except ValueError:
        return False


def egress_policy_allows_target(np_doc: Dict[str, Any], host: str, port: Optional[int]) -> bool:
    """這份 NetworkPolicy 的 egress 規則是否放行 ``host:port``（以 IP 位址判斷）。"""
    spec = np_doc.get("spec") or {}
    types = spec.get("policyTypes") or []
    if "Egress" not in types and "egress" not in spec:
        return False
    for rule in (spec.get("egress") or []):
        ports = rule.get("ports")
        if ports and port is not None:
            if not any((p.get("port") in (None, port) or str(p.get("port")) == str(port))
                       and p.get("protocol", "TCP") == "TCP" for p in ports):
                continue
        peers = rule.get("to")
        if not peers:                       # 沒有 to＝任何目的地
            return True
        for peer in peers:
            block = peer.get("ipBlock")
            if block and _ip_in_block(host, block):
                return True
    return False


def gate_unreachable_for(
    labels: Dict[str, str],
    network_policies: Iterable[Dict[str, Any]],
    target: str,
) -> bool:
    """True＝chart 自己的 NetworkPolicy 會放行探測目標給這個 pod，gate 永遠等不到 → 不插。

    ``target`` 是 ``host:port``；沒指定（用 pod 內的 KUBERNETES_SERVICE_HOST）時用叢集預設
    ``10.43.0.1:443`` 判斷。
    """
    host, port = _port_of(target or "10.43.0.1:443")
    if not host:
        return False
    for np_doc in network_policies:
        spec = np_doc.get("spec") or {}
        if not _selector_matches(labels, spec.get("podSelector")):
            continue
        if egress_policy_allows_target(np_doc, host, port):
            return True
    return False


def alt_probe_allowed_by_chart(labels: Dict[str, str], network_policies: Iterable[Dict[str, Any]]) -> bool:
    """chart 的 NetworkPolicy 是否也放行「節點 IP:10250」（備援探測目標）。"""
    for np_doc in network_policies:
        if not _selector_matches(labels, (np_doc.get("spec") or {}).get("podSelector")):
            continue
        if egress_policy_allows_target(np_doc, GATE_ALT_SAMPLE_IP, GATE_ALT_PORT):
            return True
    return False


# ---------------------------------------------------------------------------
# 對整份 render 結果套用
# ---------------------------------------------------------------------------

def apply_tenant_policy(
    docs: List[Any],
    options: Optional[Dict[str, Any]],
    live: Optional[Dict[str, Dict[str, bool]]] = None,
) -> Tuple[List[Any], List[str]]:
    """對 render 出來的 docs 套用 gate／節點迴避；回傳 ``(新 docs, 記錄行)``，不改傳入的物件。

    ``options``（post-renderer 的第二個 JSON 引數 ``tenant``）：
      ``avoid_nodes`` list[str]、``egress_gate`` dict|None（image／timeout_seconds／on_timeout／
      target）、``mirror_live`` bool、``immutable_live`` dict|None（``immutable_live_facts`` 的結果；
      給了才會處理非 hook 的 Job／bare Pod）。
    ``mirror_live`` 為真時（reset-config 等「不得改 pod template」的路徑）：已存在於叢集的
    workload 只在**叢集上已經有**該項時才套，沒有就保持現狀，避免一次密碼重設變成整個服務
    滾動重啟（INFRA-HS-002-1：RWO 重新 attach 撞 atomic 15 秒）；叢集上還沒有的新物件照常套。
    ``live`` 形如 ``{"Deployment/name": {"gate": bool, "avoid": bool}}``。
    """
    log: List[str] = []
    if not options:
        return docs, log
    avoid = [n for n in (options.get("avoid_nodes") or []) if n]
    gate_cfg = options.get("egress_gate") or None
    if not avoid and not gate_cfg:
        return docs, log
    mirror = bool(options.get("mirror_live"))
    live = live or {}
    # ``immutable_live``：叢集上非 hook Job／bare Pod 的現況（``immutable_live_facts``）；None＝未知。
    immutable_live = options.get("immutable_live")
    if immutable_live is not None and not isinstance(immutable_live, dict):
        immutable_live = None
    out = copy.deepcopy(docs)
    nps = [d for d in out if isinstance(d, dict) and d.get("kind") == "NetworkPolicy"]
    gate = None
    if gate_cfg:
        gate = build_egress_gate_container(
            image=gate_cfg.get("image") or GATE_DEFAULT_IMAGE,
            timeout_seconds=gate_cfg.get("timeout_seconds") or GATE_DEFAULT_TIMEOUT_SECONDS,
            on_timeout=gate_cfg.get("on_timeout") or "proceed",
            target=gate_cfg.get("target") or "",
        )
    for doc in out:
        templates = pod_templates(doc)
        if not templates:
            continue
        kind = doc.get("kind")
        name = (doc.get("metadata") or {}).get("name", "?")
        key = "%s/%s" % (kind, name)
        if not template_is_mutable(doc):
            # netpol-startup-window：非 hook Job／bare Pod 只有「叢集上已存在」時改 template 才會
            # immutable 失敗。叢集上沒有（新安裝、Job 被 TTL 清掉）就照常套；已存在就照叢集上那份
            # 渲染（有 gate 原樣帶回、沒有就不加）；叢集現況未知＝沿用舊行為不動。
            if immutable_live is None:
                log.append("%s: skip (pod template is immutable and not a helm hook; live state unknown)" % key)
                continue
            if key in immutable_live:
                for pod_spec, _labels, _annotations in templates:
                    log.extend("%s: %s" % (key, line)
                               for line in mirror_immutable(pod_spec, immutable_live[key] or {}))
                continue
            log.append("%s: immutable template not live yet; policy applied on create" % key)
        facts = live.get(key)
        for pod_spec, labels, annotations in templates:
            if avoid:
                if mirror and facts is not None and not facts.get("avoid"):
                    log.append("%s: node-avoid kept as live (mirror)" % key)
                else:
                    log.append("%s: node-avoid %s" % (key, add_node_avoidance(pod_spec, avoid)))
            if gate is not None and is_pod_workload(doc):
                if annotations.get(GATE_SKIP_ANNOTATION) == "skip":
                    log.append("%s: egress-gate skip (annotation)" % key)
                elif mirror and facts is not None and not facts.get("gate"):
                    log.append("%s: egress-gate kept as live (mirror)" % key)
                elif gate_unreachable_for(labels, nps, gate_cfg.get("target") or ""):
                    if gate_cfg.get("target") or alt_probe_allowed_by_chart(labels, nps):
                        log.append("%s: egress-gate skip (chart NetworkPolicy allows the probe target)" % key)
                    else:
                        alt = build_egress_gate_container(
                            image=gate_cfg.get("image") or GATE_DEFAULT_IMAGE,
                            timeout_seconds=gate_cfg.get("timeout_seconds") or GATE_DEFAULT_TIMEOUT_SECONDS,
                            on_timeout=gate_cfg.get("on_timeout") or "proceed",
                            probe_host_ip=True,
                        )
                        log.append("%s: egress-gate %s (probing node kubelet; chart allows the API)"
                                   % (key, inject_egress_gate(pod_spec, alt)))
                else:
                    log.append("%s: egress-gate %s" % (key, inject_egress_gate(pod_spec, gate)))
    return out, log


def mirror_immutable(pod_spec: Dict[str, Any], facts: Dict[str, Any]) -> List[str]:
    """已存在的非 hook Job／bare Pod：把叢集上那份 gate／affinity 原樣帶回 render 結果。

    template 不可變，render 結果必須跟叢集上一樣（helm 的三方 patch 才不會改到它）：叢集上有 gate
    就放回**同一份** container（設定之後改過也一樣），有節點迴避就放回叢集上的 affinity；
    沒有就什麼都不加。
    """
    log: List[str] = []
    gate = facts.get("gate_container")
    if isinstance(gate, dict) and not has_egress_gate(pod_spec):
        inits = pod_spec.get("initContainers")
        if not isinstance(inits, list):
            inits = []
        pod_spec["initContainers"] = [copy.deepcopy(gate)] + inits
        log.append("egress-gate kept as live (immutable)")
    elif not gate:
        log.append("egress-gate kept as live (immutable, none)")
    affinity = facts.get("affinity")
    if facts.get("avoid") and isinstance(affinity, dict):
        pod_spec["affinity"] = copy.deepcopy(affinity)
        log.append("node-avoid kept as live (immutable)")
    return log


def immutable_live_facts(items: Optional[List[Dict[str, Any]]], avoid_nodes: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    """kubectl ``get jobs,pods`` 的 ``items`` → ``{"Job/x": {"gate_container", "affinity", "avoid"}}``。

    只收 Job 與**沒有 ownerReferences** 的 Pod（helm 直接建立的 bare Pod；Deployment／Job 產生的
    pod 不是 render 出來的物件）。``gate_container`` 是叢集上那份 gate（沒有就 None），
    ``affinity`` 是叢集上的 pod affinity（沒有就 None）。
    """
    facts: Dict[str, Dict[str, Any]] = {}
    nodes = [n for n in avoid_nodes if n]
    for item in items or []:
        if not isinstance(item, dict) or item.get("kind") not in ("Job", "Pod"):
            continue
        meta = item.get("metadata") or {}
        name = meta.get("name")
        if not name or (item["kind"] == "Pod" and meta.get("ownerReferences")):
            continue
        templates = pod_templates(copy.deepcopy(item))
        if not templates:
            continue
        pod_spec = templates[0][0]
        gate = next((c for c in (pod_spec.get("initContainers") or [])
                     if isinstance(c, dict) and c.get("name") == GATE_CONTAINER_NAME), None)
        affinity = pod_spec.get("affinity") if isinstance(pod_spec.get("affinity"), dict) else None
        facts["%s/%s" % (item["kind"], name)] = {
            "gate_container": gate,
            "affinity": affinity,
            "avoid": bool(nodes) and has_node_avoidance(pod_spec, nodes),
        }
    return facts


def live_facts(items: Optional[List[Dict[str, Any]]], avoid_nodes: Iterable[str]) -> Dict[str, Dict[str, bool]]:
    """kubectl ``items``（各種 workload）→ ``{"Kind/name": {"gate": bool, "avoid": bool}}``。"""
    facts: Dict[str, Dict[str, bool]] = {}
    nodes = [n for n in avoid_nodes if n]
    for item in items or []:
        if not isinstance(item, dict):
            continue
        name = (item.get("metadata") or {}).get("name")
        if not name or not item.get("kind"):
            continue
        templates = pod_templates(copy.deepcopy(item))
        if not templates:
            continue
        pod_spec = templates[0][0]
        facts["%s/%s" % (item["kind"], name)] = {
            "gate": has_egress_gate(pod_spec),
            "avoid": bool(nodes) and has_node_avoidance(pod_spec, nodes),
        }
    return facts
