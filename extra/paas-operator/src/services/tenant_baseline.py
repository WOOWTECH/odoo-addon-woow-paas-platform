"""工作區＝自己的區域網路（#1441 / #1436 / #514）：租戶 ns 的 egress baseline 與 Pod Security。

這個模組只放**純函式**（不 import settings、不呼叫 kubectl），讓 operator
（``helm.KubernetesService``）與一次性回填腳本（``scripts/ws_lan_backfill.py``）用的是
同一份 manifest，不會一邊改了另一邊沒跟上。

政策（使用者 2026-10-07 拍板）：

* 客戶服務只能連 **同工作區（全部 pod、全部埠）＋DNS＋公網**。不能連其他工作區、
  平台內部（paas-system 等）、其他非 PaaS ns、叢集 Service 網段、私網、雲端 metadata。
* 同一個工作區內所有服務要能互連（像區網）——ingress 由既有的
  ``paas-baseline-allow-same-ns`` 保證，egress 由這裡的同 ns 規則保證。
* 客戶工作區不能建立能碰主機的 pod（privileged／hostNetwork／hostPath／hostPort …）
  ⇒ ``pod-security.kubernetes.io/enforce=baseline``。唯一例外是管理者用的 Omnigent
  （見 ``DEFAULT_PSA_EXEMPT_NAMESPACES``）。

NetworkPolicy 只有 allow、是聯集：這條 baseline 只能把「沒有其他 egress 規則的 pod」
收斂到上面三類。chart 自己帶的更寬規則（``- {}``、未排除私網的 0.0.0.0/0）會把它抵銷，
所以 woow-paas-charts 同步收斂並在 CI 用 np-audit 擋（charts ``scripts/np-audit.py``）。
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional

# ---------------------------------------------------------------------------
# Egress baseline
# ---------------------------------------------------------------------------

BASELINE_EGRESS_NP_NAME = "paas-baseline-egress"

# 公網規則要排除的目的地。每一段都有理由，不要「順手」刪：
#   10.0.0.0/8      — pod CIDR 10.42.0.0/16、Service CIDR 10.43.0.0/16（含 kube-apiserver
#                     ClusterIP 10.43.0.1 與所有平台／其他工作區的 ClusterIP），以及每台主機
#                     pod 子網的廣播位址 10.42.<n>.255（#1441 實測會在 cni0 bridge 上跨 ns 氾濫）。
#   172.16.0.0/12   — RFC 1918。
#   192.168.0.0/16  — RFC 1918，含主機 LAN 192.168.10.0/24（node IP、NodePort、kube-apiserver
#                     :6443、LAN 廣播 192.168.10.255）。
#   169.254.0.0/16  — link-local，含雲端 metadata 169.254.169.254。
#   100.64.0.0/10   — CGNAT（Tailscale／WARP 的 tailnet 位址）。
#   224.0.0.0/4     — 多播（mDNS 224.0.0.251、SSDP 239.255.255.250；#1441：多播在共用的
#                     bridge 上會被同主機所有 ns 收到，只有送出端的 egress 擋得住）。
#   240.0.0.0/4     — 保留位址，含 limited broadcast 255.255.255.255。
BASELINE_EGRESS_BLOCKED_CIDRS: List[str] = [
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "169.254.0.0/16",
    "100.64.0.0/10",
    "224.0.0.0/4",
    "240.0.0.0/4",
]

# k3s 的 CoreDNS（kube-system，label k8s-app=kube-dns）。pod 送到 Service 10.43.0.10:53，
# kube-router 在 DNAT 之後比對，所以要放的是 CoreDNS pod，不是 Service IP。只開這兩個
# 標籤選到的 pod、只開 53 埠——kube-system 裡其他服務（traefik、metrics-server）一律不通。
KUBE_DNS_NAMESPACE = "kube-system"
KUBE_DNS_POD_LABELS: Dict[str, str] = {"k8s-app": "kube-dns"}


def build_baseline_egress_netpol(namespace: str) -> dict:
    """單一一條 Egress NetworkPolicy，選到 ns 內**每一個** pod（``podSelector: {}``）。

    刻意做成一條（而不是 default-deny ＋ 三條 allow）：NetworkPolicy 一旦有一條 Egress
    政策選到 pod，pod 的 egress 就只剩被允許的部分；分成好幾條 apply 時，中間會有
    「已經 deny、allow 還沒到」的空窗（例如先擋了 DNS）。一條 apply 是原子的。

    三條規則：
      1. 同 ns 全部 pod、全部埠（同工作區＝區網）。``podSelector: {}`` 不帶
         namespaceSelector ＝只限本 ns。經由同 ns 的 ClusterIP 也通（DNAT 後是本 ns pod）。
      2. kube-dns pod 的 UDP/TCP 53。
      3. 公網任意埠：``0.0.0.0/0`` 排除 ``BASELINE_EGRESS_BLOCKED_CIDRS``。
    """
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {
            "name": BASELINE_EGRESS_NP_NAME,
            "namespace": namespace,
            "labels": {"paas-managed": "true"},
        },
        "spec": {
            "podSelector": {},
            "policyTypes": ["Egress"],
            "egress": [
                {"to": [{"podSelector": {}}]},
                {
                    "to": [{
                        "namespaceSelector": {"matchLabels": {
                            "kubernetes.io/metadata.name": KUBE_DNS_NAMESPACE,
                        }},
                        "podSelector": {"matchLabels": dict(KUBE_DNS_POD_LABELS)},
                    }],
                    "ports": [
                        {"protocol": "UDP", "port": 53},
                        {"protocol": "TCP", "port": 53},
                    ],
                },
                {"to": [{"ipBlock": {
                    "cidr": "0.0.0.0/0",
                    "except": list(BASELINE_EGRESS_BLOCKED_CIDRS),
                }}]},
            ],
        },
    }


# ---------------------------------------------------------------------------
# Pod Security Admission
# ---------------------------------------------------------------------------

PSA_LABEL_PREFIX = "pod-security.kubernetes.io/"
PSA_MODES = ("enforce", "warn", "audit")

# 管理者用的 Omnigent（https://omnigent.woowtech.io）＝ ns paas-ws-ab6564da（工作區 31，
# svc-65b75f59）。使用者 2026-10-07 指定為**唯一**例外：它是平台管理者自己的工具，
# 不是客戶工作區；它的 sandbox runner 可能需要比 baseline 更多的權限。例外 ns 不設
# enforce（也不設 warn／audit，避免每次建 runner 都噴警告），只在 ns 上留下理由註記。
# 新增例外一律要寫明理由並經使用者同意——清單由環境變數 PSA_EXEMPT_NAMESPACES 覆寫。
DEFAULT_PSA_EXEMPT_NAMESPACES = ("paas-ws-ab6564da",)
PSA_EXEMPT_ANNOTATION = "paas.woowtech.io/psa-exempt-reason"
PSA_EXEMPT_REASON = (
    "管理者用的 Omnigent（omnigent.woowtech.io）；使用者 2026-10-07 指定為唯一的 "
    "Pod Security 例外（woow_paas_platform #1441）"
)


def parse_namespace_list(raw: Optional[str]) -> List[str]:
    """逗號分隔字串 → 去空白、去重、保序的 ns 清單。None／空字串 → []。"""
    seen: List[str] = []
    for part in (raw or "").split(","):
        name = part.strip()
        if name and name not in seen:
            seen.append(name)
    return seen


def is_psa_exempt(namespace: str, exempt: Iterable[str]) -> bool:
    return namespace in set(exempt)


def build_pod_security_labels(
    enforce: str = "baseline",
    warn: str = "restricted",
    audit: str = "restricted",
    version: str = "latest",
) -> Dict[str, str]:
    """一般租戶 ns 的 PSA 標籤（enforce＋warn＋audit 與各自的 -version）。

    * enforce=baseline：擋 privileged、hostNetwork／hostPID／hostIPC、hostPath、
      hostPort、新增 baseline 以外的 capabilities（例如 NET_ADMIN、SYS_ADMIN、NET_RAW
      的 **add**）、不安全的 sysctl／procMount／AppArmor／SELinux 設定。
      2026-10-07 對正式站 37 個 paas-ws-* 做 ``--dry-run=server``：0 個 pod 不合規。
    * warn／audit=restricted：只回警告／寫稽核，不擋——用來觀察哪些 chart 離 restricted
      還有多遠（HA 以 root 執行，restricted 會擋，所以不 enforce）。
    """
    labels: Dict[str, str] = {}
    for mode, level in (("enforce", enforce), ("warn", warn), ("audit", audit)):
        if level:
            labels[f"{PSA_LABEL_PREFIX}{mode}"] = level
            labels[f"{PSA_LABEL_PREFIX}{mode}-version"] = version
    return labels


def psa_label_keys() -> List[str]:
    """所有本模組可能寫入的 PSA 標籤鍵（例外 ns 用來移除）。"""
    keys: List[str] = []
    for mode in PSA_MODES:
        keys.append(f"{PSA_LABEL_PREFIX}{mode}")
        keys.append(f"{PSA_LABEL_PREFIX}{mode}-version")
    return keys
