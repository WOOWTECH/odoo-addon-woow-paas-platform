"""INFRA-FIX-005：VPN 節點（charts/tailscale）的即時狀態回讀。

## 為什麼需要

INFRA-TS-005／TS-007 在 .243 的實測（Tailscale 242，工作區自己的 Headscale 為控制面）：

1. 節點被控制面到期／刪除後，``tailscaled`` 的 BackendState 變成 ``NeedsLogin``，pod 仍是
   Ready（readiness 把 NeedsLogin 當成可用），平台 ``state`` 一直是 ``running``，/woow 顯示
   「執行中／ONLINE」——使用者看到綠燈，節點其實已離線。
2. 原生網頁的「Reauthenticate」給的登入網址是叢集內部位址
   （``http://<release>-headscale.<ns>.svc.cluster.local:8080/register/<id>``），瀏覽器打不開，
   /woow 也沒有任何說明。
3. ``tailscale.serve`` 轉發清單裡不合格的項目（他 namespace、IP、埠超界、重複埠…）會被 chart
   的 entrypoint 略過，原因**只印在容器 stderr**，租戶看不到；Service 不存在的項目更不會被略過
   （會被建成指向 NXDOMAIN 的轉發），也沒有任何提示。

本模組讓 operator 能「現場問 pod」並把這三件事用**封閉集合的代碼**回報給平台；平台據此在服務頁
顯示（見 ``woow_paas_platform`` ``/node-status`` 端點）。

## 做法（唯讀）

對 release 的 pod 的 ``tailscale`` 容器 exec **一支固定的 sh 腳本**（不帶任何租戶輸入），一次
取得：

* ``tailscale status --json --peers=false``：BackendState、AuthURL、Self（Online、IP、DNS 名）；
* ``tailscale serve status --json``：實際生效的 TCP 轉發；
* **entrypoint 自己的** ``parse_serve``（以 ``WOOW_TS_SOURCE_ONLY=1`` source ``/entrypoint.sh``，
  與 chart 的單元測試同一招；⚠ 先 ``grep`` 確認這份 entrypoint 真的認得該旗標——舊映像的
  entrypoint 沒有它，source 下去會整支跑起來、多開一個 tailscaled）：哪些項目有效、哪些被略過與原因。直接問 entrypoint 而不是在這裡再寫
  一份驗證——兩份驗證邏輯不會同時錯，只會無聲漂移。

再用 ``kubectl get services`` 對帳「轉發目標 Service 是否存在」。

## 回應的信任邊界

* **永不回原始 log／原始 stderr**。略過原因由 stderr 行的固定句型對應到封閉代碼（``SKIP_*``），
  不認得的歸 ``other``；``entry`` 只回租戶自己填的那一段（限長、限字元）。
* ``AuthURL`` 只在是「外部可達」位址時才原樣回；叢集內部位址（``*.svc``、``*.cluster.local``、
  單一標籤主機名、私有 IP）只回 ``cluster_internal`` 與註冊碼（``/register/<id>`` 的 id），
  由平台決定怎麼說明。註冊碼不是憑證——它只是 Headscale 待核可節點的識別，核可仍需要控制面的
  管理權限。
* 任何一步失敗都降級成 ``probe`` 代碼，不外洩 stderr；回 200（呼叫端不得把它當 5xx）。
"""
from __future__ import annotations

import ipaddress
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

#: chart 的容器名（charts/tailscale statefulset.yaml）。
CONTAINER_NAME = "tailscale"

#: exec／get services 的上限（秒）。平台每 30 秒最多問一次，不能卡住。
EXEC_TIMEOUT_SECONDS = 15
SERVICES_TIMEOUT_SECONDS = 8

SENTINEL_STATUS = "@@woow-status"
SENTINEL_SERVE = "@@woow-serve"
SENTINEL_WANTED = "@@woow-wanted"
NO_PARSER_MARK = "@@woow-noparser"

#: 固定腳本——**沒有任何插值**。socket 與 namespace 都從 pod 自己的 env 讀。
PROBE_SCRIPT = f"""\
S="${{TS_SOCKET:-/var/run/tailscale/tailscaled.sock}}"
echo '{SENTINEL_STATUS}'
tailscale --socket="$S" status --json --peers=false 2>/dev/null
echo
echo '{SENTINEL_SERVE}'
tailscale --socket="$S" serve status --json 2>/dev/null
echo
echo '{SENTINEL_WANTED}'
if [ -r /entrypoint.sh ] && command -v bash >/dev/null 2>&1 && grep -q WOOW_TS_SOURCE_ONLY /entrypoint.sh 2>/dev/null; then
  TS_SERVE_NS="${{TS_SERVE_NAMESPACE:-${{POD_NAMESPACE:-}}}}" bash -c \
'WOOW_TS_SOURCE_ONLY=1; . /entrypoint.sh; parse_serve "${{TS_SERVE:-}}" "$TS_SERVE_NS" "${{TS_CLUSTER_DOMAIN:-cluster.local}}"' 2>&1
else
  echo '{NO_PARSER_MARK}'
fi
"""

# ---- 探測結果（probe）-------------------------------------------------------------------
PROBE_OK = "ok"
PROBE_NO_POD = "no_pod"                    # 找不到可探測的 pod（還沒起來、正在被替換）
PROBE_POD_NOT_RUNNING = "pod_not_running"  # pod 不是 Running（Pending、CrashLoop 的空檔…）
PROBE_EXEC_FAILED = "exec_failed"          # kubectl exec 失敗／逾時（容器正在重啟）
PROBE_NO_OUTPUT = "no_output"              # 腳本沒印出可解析的狀態（tailscaled 還沒起來）

# ---- BackendState（tailscaled 的封閉集合；其餘一律 Unknown）---------------------------------
BACKEND_STATES = frozenset({
    "NoState", "NeedsLogin", "NeedsMachineAuth", "Stopped", "Starting", "Running",
})
BACKEND_UNKNOWN = "Unknown"

# ---- 登入網址種類 -------------------------------------------------------------------------
LOGIN_NONE = "none"
LOGIN_EXTERNAL = "external_url"
LOGIN_CLUSTER_INTERNAL = "cluster_internal"

# ---- 轉發項目的狀態 -----------------------------------------------------------------------
FWD_APPLIED = "applied"                  # 已生效，目標 Service 存在
FWD_NOT_APPLIED = "not_applied"          # 有效但還沒生效（節點沒登入、剛啟動、套用失敗）
FWD_SERVICE_MISSING = "service_missing"  # 已生效，但目標 Service 不存在（NXDOMAIN 轉發）
FWD_MISMATCH = "mismatch"                # 同一個埠生效的目標與期望不同
FWD_UNVERIFIED = "unverified"            # 已生效，Service 清單讀不到所以無法確認目標存在

# ---- 略過原因（entrypoint parse_serve 的固定句型 → 封閉代碼）----------------------------------
SKIP_BAD_FORMAT = "bad_format"
SKIP_PORT_RANGE = "port_range"
SKIP_OTHER_NAMESPACE = "other_namespace"
SKIP_NOT_SERVICE_NAME = "not_service_name"
SKIP_DUPLICATE_PORT = "duplicate_port"
SKIP_TOO_MANY = "too_many"
SKIP_NAMESPACE_UNKNOWN = "namespace_unknown"
SKIP_OTHER = "other"

_SKIP_PATTERNS: Tuple[Tuple[str, str], ...] = (
    ("expected <tailnet port>=", SKIP_BAD_FORMAT),
    ("ports must be 1-65535", SKIP_PORT_RANGE),
    ("only Services of this namespace", SKIP_OTHER_NAMESPACE),
    ("is not a Service name", SKIP_NOT_SERVICE_NAME),
    ("is already forwarded", SKIP_DUPLICATE_PORT),
    ("at most ", SKIP_TOO_MANY),
)

_SKIP_LINE_RE = re.compile(r"^\[woow-tailscale\] serve: skipping '(?P<entry>[^']*)': (?P<why>.*)$")
_NS_UNKNOWN_PREFIX = "[woow-tailscale] serve: own namespace unknown"
_VALID_LINE_RE = re.compile(
    r"^(?P<port>[1-9][0-9]{0,4}) (?P<name>[a-z][a-z0-9-]{0,61}[a-z0-9]|[a-z])"
    r"\.(?P<ns>[a-z0-9][a-z0-9.-]{0,251})\.svc\.(?P<domain>[a-z0-9.-]{1,253}):(?P<tport>[1-9][0-9]{0,4})$"
)
_ENTRY_SAFE_RE = re.compile(r"^[A-Za-z0-9=:.,_-]{1,100}$")
_REGISTER_PATH_RE = re.compile(r"^/register/(?P<key>[A-Za-z0-9_-]{4,128})/?$")

MAX_ITEMS = 40  # 與 entrypoint 的 max=32 對齊並留餘裕；避免畸形輸出撐大回應


def _safe_entry(raw: str) -> str:
    """租戶自己填的那一段：限長限字元，不合就以 ``?`` 取代（不回任何其他內容）。"""
    raw = raw.strip()
    if _ENTRY_SAFE_RE.fullmatch(raw):
        return raw
    cleaned = re.sub(r"[^A-Za-z0-9=:.,_-]", "?", raw)[:100]
    return cleaned or "?"


def _skip_code(why: str) -> str:
    for needle, code in _SKIP_PATTERNS:
        if needle in why:
            return code
    return SKIP_OTHER


def split_probe_output(text: str) -> Dict[str, str]:
    """把腳本輸出切成 ``status`` ／ ``serve`` ／ ``wanted`` 三段（缺哪段就沒有那個 key）。"""
    sections: Dict[str, List[str]] = {}
    current: Optional[str] = None
    names = {SENTINEL_STATUS: "status", SENTINEL_SERVE: "serve", SENTINEL_WANTED: "wanted"}
    for line in (text or "").split("\n"):
        stripped = line.rstrip("\r")
        if stripped in names:
            current = names[stripped]
            sections[current] = []
            continue
        if current is not None:
            sections[current].append(stripped)
    return {key: "\n".join(lines).strip() for key, lines in sections.items()}


def _loads(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


# ------------------------------------------------------------------------------------------
#  登入網址
# ------------------------------------------------------------------------------------------

def _is_cluster_internal_host(host: str) -> bool:
    """瀏覽器打不開的主機：叢集內部名稱、單一標籤主機名、私有／loopback／CGNAT／link-local IP。"""
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return True
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    else:
        cgnat = ipaddress.ip_network("100.64.0.0/10")
        return bool(
            ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified
            or ip.is_reserved or (ip.version == 4 and ip in cgnat)
        )
    if "." not in host:
        return True  # 單一標籤＝叢集內的 Service 短名
    return host.endswith((".svc", ".cluster.local", ".local", ".internal", ".localhost"))


def classify_auth_url(raw: Any) -> Dict[str, Any]:
    """``tailscale status`` 的 AuthURL → ``{kind, url, register_key}``。

    * 空／不是 http(s) → ``none``；
    * 外部可達 → ``external_url`` ＋ 原網址（含 query 照舊：官方 Tailscale 的登入網址就是
      ``https://login.tailscale.com/a/<token>``，那正是使用者要點開的東西）；
    * 叢集內部 → ``cluster_internal``，網址**不回**，只回 ``/register/<id>`` 的註冊碼（可能為 None）。
    """
    empty = {"kind": LOGIN_NONE, "url": None, "register_key": None}
    if not isinstance(raw, str) or not raw.strip():
        return empty
    text = raw.strip()
    if len(text) > 2048 or any(ch in text for ch in " \t\r\n\"'<>\\"):
        return empty
    try:
        parts = urlsplit(text)
        host = parts.hostname or ""
    except ValueError:
        return empty
    if parts.scheme not in ("http", "https") or not host:
        return empty
    if _is_cluster_internal_host(host):
        match = _REGISTER_PATH_RE.fullmatch(parts.path or "")
        return {
            "kind": LOGIN_CLUSTER_INTERNAL, "url": None,
            "register_key": match.group("key") if match else None,
        }
    return {"kind": LOGIN_EXTERNAL, "url": text, "register_key": None}


# ------------------------------------------------------------------------------------------
#  status / serve 解析
# ------------------------------------------------------------------------------------------

def parse_node(status: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """``tailscale status --json`` → 節點欄位（封閉集合／限長）。"""
    if not status:
        return {
            "backend_state": BACKEND_UNKNOWN, "online": None, "tailnet_ips": [],
            "dns_name": None, "login": classify_auth_url(None), "health_warnings": 0,
        }
    state = status.get("BackendState")
    state = state if isinstance(state, str) and state in BACKEND_STATES else BACKEND_UNKNOWN
    me = status.get("Self") if isinstance(status.get("Self"), dict) else {}
    online = me.get("Online")
    ips_raw = status.get("TailscaleIPs")
    if not isinstance(ips_raw, list):
        ips_raw = me.get("TailscaleIPs") if isinstance(me.get("TailscaleIPs"), list) else []
    ips: List[str] = []
    for item in ips_raw[:8]:
        if not isinstance(item, str):  # ip_address(5) 會「成功」變成 0.0.0.5
            continue
        try:
            ips.append(str(ipaddress.ip_address(item)))
        except ValueError:
            continue
        if len(ips) == 4:
            break
    dns_name = me.get("DNSName")
    if not (isinstance(dns_name, str) and re.fullmatch(r"[A-Za-z0-9.-]{1,253}", dns_name)):
        dns_name = None
    health = status.get("Health")
    return {
        "backend_state": state,
        "online": online if isinstance(online, bool) else None,
        "tailnet_ips": ips,
        "dns_name": dns_name.rstrip(".") if dns_name else None,
        "login": classify_auth_url(status.get("AuthURL")),
        "health_warnings": len(health) if isinstance(health, list) else 0,
    }


def parse_serve_status(serve: Optional[Dict[str, Any]]) -> Tuple[Dict[int, str], bool]:
    """``tailscale serve status --json`` → ``({tailnet 埠: 轉發目標}, 有沒有 TCP 以外的設定)``。"""
    applied: Dict[int, str] = {}
    if not serve:
        return applied, False
    tcp = serve.get("TCP")
    if isinstance(tcp, dict):
        for key, value in list(tcp.items())[:MAX_ITEMS]:
            if not (isinstance(key, str) and key.isdigit() and isinstance(value, dict)):
                continue
            target = value.get("TCPForward")
            if isinstance(target, str) and target:
                applied[int(key)] = target.lower()
    extra = any(k != "TCP" and v for k, v in serve.items())
    return applied, extra


def parse_wanted(text: str) -> Tuple[Optional[List[Dict[str, Any]]], List[Dict[str, str]], Optional[str]]:
    """entrypoint ``parse_serve`` 的輸出（stdout＋stderr 合併）→ ``(有效, 略過, 整單忽略原因)``。

    有效項目是 ``{port, service, namespace, target_port, target}``；略過項目是
    ``{entry, reason}``。``parser`` 缺席（舊映像沒有 ``/entrypoint.sh`` 或沒有 bash）時第一個值是
    None＝「不知道」，呼叫端只顯示已生效的轉發，不假裝知道期望清單。
    """
    if text == NO_PARSER_MARK:
        return None, [], None
    valid: List[Dict[str, Any]] = []
    skipped: List[Dict[str, str]] = []
    ignored_all: Optional[str] = None
    for line in (text or "").split("\n"):
        line = line.rstrip("\r")
        if line.startswith(_NS_UNKNOWN_PREFIX):
            ignored_all = SKIP_NAMESPACE_UNKNOWN
            continue
        m = _VALID_LINE_RE.fullmatch(line)
        if m:
            if len(valid) < MAX_ITEMS:
                valid.append({
                    "port": int(m.group("port")), "service": m.group("name"),
                    "namespace": m.group("ns"), "target_port": int(m.group("tport")),
                    "target": f"{m.group('name')}.{m.group('ns')}.svc.{m.group('domain')}:{m.group('tport')}",
                })
            continue
        s = _SKIP_LINE_RE.match(line)
        if s and len(skipped) < MAX_ITEMS:
            skipped.append({"entry": _safe_entry(s.group("entry")), "reason": _skip_code(s.group("why"))})
    return valid, skipped, ignored_all


def build_forwards(
    valid: Optional[List[Dict[str, Any]]],
    applied: Dict[int, str],
    service_names: Optional[frozenset],
) -> List[Dict[str, Any]]:
    """期望清單 × 實際生效 × Service 是否存在 → 每個轉發一列。

    ``valid`` 為 None（拿不到期望清單）時，只列實際生效的（來源是 serve status）。
    """
    rows: List[Dict[str, Any]] = []
    if valid is None:
        for port, target in sorted(applied.items())[:MAX_ITEMS]:
            host, _, tport = target.rpartition(":")
            name = host.split(".")[0] if host else ""
            exists = None if service_names is None else (name in service_names)
            rows.append({
                "port": port, "service": name or None,
                "target_port": int(tport) if tport.isdigit() else None,
                "status": (FWD_UNVERIFIED if exists is None
                           else FWD_APPLIED if exists else FWD_SERVICE_MISSING),
            })
        return rows
    for item in valid:
        actual = applied.get(item["port"])
        exists = None if service_names is None else (item["service"] in service_names)
        if actual is None:
            status = FWD_NOT_APPLIED
        elif actual != item["target"].lower():
            status = FWD_MISMATCH
        elif exists is None:
            status = FWD_UNVERIFIED
        else:
            status = FWD_APPLIED if exists else FWD_SERVICE_MISSING
        rows.append({
            "port": item["port"], "service": item["service"],
            "target_port": item["target_port"], "status": status,
        })
    return rows


def build_report(
    *,
    pod: Optional[str],
    probe: str,
    output: str = "",
    service_names: Optional[frozenset] = None,
) -> Dict[str, Any]:
    """組出回給平台的整份報告（純函式；``output`` 是 :data:`PROBE_SCRIPT` 的 stdout）。"""
    sections = split_probe_output(output)
    status_json = _loads(sections.get("status", ""))
    if probe == PROBE_OK and status_json is None:
        probe = PROBE_NO_OUTPUT
    node = parse_node(status_json)
    applied, extra = parse_serve_status(_loads(sections.get("serve", "")))
    valid, skipped, ignored_all = parse_wanted(sections.get("wanted", ""))
    forwards = build_forwards(valid, applied, service_names) if probe == PROBE_OK else []
    return {
        "probe": probe,
        "pod": pod,
        **node,
        "serve": {
            "wanted_known": valid is not None,
            "forwards": forwards,
            "skipped": skipped,
            "ignored_all": ignored_all,
            "extra_config": extra,
        },
    }


def collect(k8s_service: Any, namespace: str, release: str) -> Dict[str, Any]:
    """現場探測一個 release（唯讀）。呼叫端已驗過 namespace；kubectl 失敗一律降級成 probe 代碼。"""
    from src.models.schemas import PodPhase  # 延後 import：本模組的純函式不依賴 pydantic

    selector = f"app.kubernetes.io/instance={release}"
    try:
        pods = k8s_service.get_pods(namespace=namespace, label_selector=selector)
    except Exception as exc:  # noqa: BLE001 — 見 docstring：降級而不是 5xx
        logger.warning("vpn-node-status: could not list pods for %s/%s: %s", namespace, release, exc)
        return build_report(pod=None, probe=PROBE_NO_POD)
    live = [p for p in pods if not getattr(p, "deletion_timestamp", None)]
    if not live:
        return build_report(pod=None, probe=PROBE_NO_POD)
    pod = next((p for p in live if getattr(p, "phase", None) == PodPhase.RUNNING), None)
    if pod is None:
        return build_report(pod=live[0].name, probe=PROBE_POD_NOT_RUNNING)

    service_names: Optional[frozenset] = None
    try:
        result = k8s_service._run_command(
            ["get", "services", "--namespace", namespace,
             "--output", "jsonpath={.items[*].metadata.name}"],
            timeout=SERVICES_TIMEOUT_SECONDS,
        )
        service_names = frozenset((result.stdout or "").split())
    except Exception as exc:  # noqa: BLE001
        logger.warning("vpn-node-status: could not list services in %s: %s", namespace, exc)

    try:
        proc = k8s_service.exec_in_pod(
            namespace, pod.name, CONTAINER_NAME, ["sh", "-c", PROBE_SCRIPT],
            timeout=EXEC_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("vpn-node-status: exec failed in %s/%s: %s", namespace, pod.name, exc)
        return build_report(pod=pod.name, probe=PROBE_EXEC_FAILED, service_names=service_names)
    return build_report(
        pod=pod.name, probe=PROBE_OK, output=proc.stdout or "", service_names=service_names)
