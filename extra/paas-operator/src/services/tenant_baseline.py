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


# ---------------------------------------------------------------------------
# 公網埠收斂（商用驗收 egress-all-ports-open；AI-HER-008-2、AI-OD-007-9）
# ---------------------------------------------------------------------------
#
# 原始設計（2026-10-07）是公網**任意埠**，所以 chart 自己寫的 80/443 限制（hermes-agent-allow、
# open-design-allow …）因為 NP 聯集而無效。要收到多緊是產品／資安決策，這裡只提供兩個機制，
# 由 operator 設定打開（預設關＝manifest 與修補前逐字相同）：
#
# 1. 封埠（TENANT_BASELINE_EGRESS_BLOCKED_PORTS，例 "22,25"）：公網規則改成 1–65535 扣掉
#    這些埠的 TCP／UDP 範圍（``endPort``，k8s 1.25 GA；woow-k3s v1.34 的 kube-router 支援）。
#    其他合法用途（SMTP submission 587／465、IMAPS 993、DoT 853、MQTT bridge …）照通。
#    SCTP 到公網在封埠模式下不放（目前沒有任何 chart 用到）。
# 2. web profile（TENANT_BASELINE_EGRESS_WEB_PROFILE）：帶 ``EGRESS_PROFILE_LABEL=web`` 的
#    pod 不再被一般 baseline 選到，改由 ``paas-baseline-egress-web`` 管——同 ns＋DNS＋公網
#    TCP 80/443。chart 在 agent 類 pod（pi／OD／Hermes／code-server …）加上這個 label，
#    chart 自己的 80/443 限制才真的生效。哪些 SKU 要加是產品決策，由 woow-paas-charts 決定。

BASELINE_EGRESS_WEB_NP_NAME = "paas-baseline-egress-web"
EGRESS_PROFILE_LABEL = "paas.woowtech.io/egress-profile"
EGRESS_PROFILE_WEB = "web"
WEB_PROFILE_TCP_PORTS = (80, 443)

_MIN_PORT, _MAX_PORT = 1, 65535


def parse_port_list(raw: Optional[str]) -> List[int]:
    """逗號分隔的埠號 → 排序、去重的 int 清單。None／空字串 → []。

    只接受 1–65535 的十進位整數；範圍寫法（"22-25"）、服務名（"smtp"）一律 ValueError，
    避免設定打錯被默默忽略（那等於沒封）。
    """
    ports = set()
    for part in (raw or "").split(","):
        token = part.strip()
        if not token:
            continue
        if not token.isdigit():
            raise ValueError(f"port must be an integer 1-65535; got {token!r}")
        port = int(token)
        if not _MIN_PORT <= port <= _MAX_PORT:
            raise ValueError(f"port must be within 1-65535; got {port}")
        ports.add(port)
    return sorted(ports)


def _allowed_port_entries(blocked: Iterable[int], protocols=("TCP", "UDP")) -> List[dict]:
    """1–65535 扣掉 ``blocked`` 的連續區段 → NetworkPolicy ``ports`` 條目（每個 protocol 一組）。"""
    blocked_set = sorted(set(blocked))
    ranges = []
    start = _MIN_PORT
    for port in blocked_set:
        if port > start:
            ranges.append((start, port - 1))
        start = port + 1
    if start <= _MAX_PORT:
        ranges.append((start, _MAX_PORT))
    entries = []
    for protocol in protocols:
        for lo, hi in ranges:
            entry = {"protocol": protocol, "port": lo}
            if hi != lo:
                entry["endPort"] = hi
            entries.append(entry)
    return entries


def _internet_peer() -> dict:
    return {"ipBlock": {
        "cidr": "0.0.0.0/0",
        "except": list(BASELINE_EGRESS_BLOCKED_CIDRS),
    }}


def _same_ns_and_dns_rules() -> List[dict]:
    return [
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
    ]


def _egress_np(namespace: str, name: str, pod_selector: dict, internet_rule: dict) -> dict:
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {"paas-managed": "true"},
        },
        "spec": {
            "podSelector": pod_selector,
            "policyTypes": ["Egress"],
            "egress": _same_ns_and_dns_rules() + [internet_rule],
        },
    }


def build_baseline_egress_netpol(namespace: str, blocked_ports: Iterable[int] = (),
                                 web_profile: bool = False) -> dict:
    """單一一條 Egress NetworkPolicy，選到 ns 內**每一個** pod（``podSelector: {}``）。

    刻意做成一條（而不是 default-deny ＋ 三條 allow）：NetworkPolicy 一旦有一條 Egress
    政策選到 pod，pod 的 egress 就只剩被允許的部分；分成好幾條 apply 時，中間會有
    「已經 deny、allow 還沒到」的空窗（例如先擋了 DNS）。一條 apply 是原子的。

    三條規則：
      1. 同 ns 全部 pod、全部埠（同工作區＝區網）。``podSelector: {}`` 不帶
         namespaceSelector ＝只限本 ns。經由同 ns 的 ClusterIP 也通（DNAT 後是本 ns pod）。
      2. kube-dns pod 的 UDP/TCP 53。
      3. 公網：``0.0.0.0/0`` 排除 ``BASELINE_EGRESS_BLOCKED_CIDRS``；``blocked_ports`` 空＝任意埠，
         否則 TCP／UDP 1–65535 扣掉這些埠。

    ``web_profile=True``：``podSelector`` 改成排除 ``EGRESS_PROFILE_LABEL=web`` 的 pod（它們由
    ``build_baseline_egress_web_netpol`` 管）。沒有這個 label 的 pod 照舊被選到（NotIn 語意）。
    """
    blocked = list(blocked_ports)
    internet_rule: dict = {"to": [_internet_peer()]}
    if blocked:
        internet_rule["ports"] = _allowed_port_entries(blocked)
    pod_selector: dict = {}
    if web_profile:
        pod_selector = {"matchExpressions": [{
            "key": EGRESS_PROFILE_LABEL, "operator": "NotIn", "values": [EGRESS_PROFILE_WEB],
        }]}
    return _egress_np(namespace, BASELINE_EGRESS_NP_NAME, pod_selector, internet_rule)


def build_baseline_egress_web_netpol(namespace: str) -> dict:
    """agent 類 pod（``EGRESS_PROFILE_LABEL=web``）的 egress baseline：同 ns＋DNS＋公網 TCP 80/443。"""
    internet_rule = {
        "to": [_internet_peer()],
        "ports": [{"protocol": "TCP", "port": p} for p in WEB_PROFILE_TCP_PORTS],
    }
    return _egress_np(namespace, BASELINE_EGRESS_WEB_NP_NAME,
                      {"matchLabels": {EGRESS_PROFILE_LABEL: EGRESS_PROFILE_WEB}},
                      internet_rule)


def build_baseline_egress_netpols(namespace: str, blocked_ports: Iterable[int] = (),
                                  web_profile: bool = False) -> List[dict]:
    """要 apply 的 egress baseline（依序）。

    web profile 開時**先** web 政策、**後**收窄一般 baseline：任何時刻帶 web label 的 pod 都
    至少被一條 egress 政策選到——反過來的順序會有「一般 baseline 已排除它、web 政策還沒到」
    的空窗，那段時間它完全沒有 egress 政策＝全開。
    """
    general = build_baseline_egress_netpol(namespace, blocked_ports, web_profile)
    if not web_profile:
        return [general]
    return [build_baseline_egress_web_netpol(namespace), general]


# ---------------------------------------------------------------------------
# Ingress baseline：跨 ns 入口（cloudflared／agent）不放行資料庫 pod（M4 R13）
# ---------------------------------------------------------------------------
#
# ``paas-baseline-allow-cloudflared`` 與 ``paas-baseline-allow-mcp-reach`` 原本是
# ``podSelector: {}``＋不限 port ⇒ paas-cloudflare-system（egress 不受限）與被授權的 agent
# ns 都能直連租戶 DB:5432（2026-10-14 沙盒前後對照：pg_isready 由無回應變 accepting）。
# 設計是「工作區＝區網、DB 只給同 ns」，所以這兩條跨 ns 入口改成**排除資料庫類 pod**。
#
# 用排除（NotIn）而不是列舉 app pod（In）：NotIn 也會選到「沒有這個標籤」的 pod，所以
# n8n（沒有 component 標籤）、HAOS virt-launcher、各 chart 的 app／proxy pod 全部照舊
# 可達，不會因為漏列某個 chart 的 app component 而讓公開網址斷線。代價是新加的資料庫
# 類 component 要補進下面清單（woow-paas-charts 新 chart 的 DB pod 請沿用既有名稱）。
#
# 清單來源（woow-paas-charts main ffcf2cd 與正式站 53 個 paas-ws-* 盤點，2026-10-15）：
#   postgresql — litellm、nextcloud、woow-odoo 的 PostgreSQL
#   redis      — nextcloud 的 Redis
#   database／cache — hermes-agent 的 PostgreSQL／Redis
#   remote-db  — vibe-kanban 的 PostgreSQL
#   db-backup／db-restore — woow-odoo 的備份／還原 Job（持有 DB 帳密，不需要任何 ingress）
#   app.kubernetes.io/name=postgresql／redis／mysql／mariadb／mongodb／valkey — Bitnami 式
#     子 chart 的慣例（目前沒有 chart 用到，先擋）
#   app=omnigent-pg／omnigent-pg-backup — 管理者 Omnigent（paas-ws-ab6564da）手動搬遷時
#     建的 PostgreSQL 與備份 CronJob（woow.tw/manual=omnigent-cutover，不屬任何 chart）
#
# 刻意**不**限 port：同一個 ns 住多個服務，對外埠各不相同而且常有第二個埠（emqx 18083／
# 8083／1883、headscale 3000／8080、console 18790／7681、odoo 8080 同時服務 80／8072），
# baseline 建在 ns 建立當下、看不到之後裝的 release。逐 release 收斂到 route 的
# targetPort 是後續項（見 PR 待確認）。

DATA_STORE_COMPONENTS: List[str] = [
    "postgresql",
    "redis",
    "database",
    "cache",
    "remote-db",
    "db-backup",
    "db-restore",
]
DATA_STORE_NAMES: List[str] = [
    "postgresql",
    "redis",
    "mysql",
    "mariadb",
    "mongodb",
    "valkey",
]
DATA_STORE_LEGACY_APPS: List[str] = [
    "omnigent-pg",
    "omnigent-pg-backup",
]


def build_cross_ns_ingress_pod_selector() -> dict:
    """跨 ns 入口（cloudflared、agent reach）可以進到的 pod：資料庫類以外的全部 pod。

    三個 ``NotIn`` 是 AND：只要任一個標籤落在資料庫清單就不選。沒有該標籤的 pod 視為
    不在清單內（NotIn 的標準語意），所以沒有標籤的 app pod 照舊被選到。
    """
    return {
        "matchExpressions": [
            {
                "key": "app.kubernetes.io/component",
                "operator": "NotIn",
                "values": list(DATA_STORE_COMPONENTS),
            },
            {
                "key": "app.kubernetes.io/name",
                "operator": "NotIn",
                "values": list(DATA_STORE_NAMES),
            },
            {
                "key": "app",
                "operator": "NotIn",
                "values": list(DATA_STORE_LEGACY_APPS),
            },
        ],
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
