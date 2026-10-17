# -*- coding: utf-8 -*-
"""INFRA-FIX-005：VPN 節點（charts/tailscale）即時狀態——operator 回應 → 給 /woow 的公開形狀。

純函式、不 import odoo（同 ``workload_health.py``）。輸入是 operator
``GET /api/releases/{ns}/{release}/vpn-node-status`` 的回應 dict，輸出是前端直接渲染的 dict。

## 為什麼不寫進帳本

pod Ready ≠ 節點在線：chart 的 readiness 把 NeedsLogin 也當成可用（為了讓「用 auth key 登入中」
的第二次滾動等得到登入完成），所以節點被控制面到期／刪除後，平台 ``state`` 仍是 ``running``、
/woow 仍顯示 ONLINE（INFRA-TS-005／TS-007）。這個狀態變化很快（登入、核可、到期都是分鐘級），
而且只有打開服務頁的人需要看，所以**不進帳本、不加 cron、不碰 state**：租戶打開服務頁時才即時問
operator（與 ``service-resource-observations`` 同款），沒有寫入就沒有「值沒變就別寫」的長交易互撞問題。

## 再驗一次

operator 的回應是封閉集合的代碼，但這裡**不信任形狀**：每個欄位都重新對封閉集合／正規式檢查，
不合就當沒有。原因同 ``workload_health``：舊 operator（沒有這個端點）、半個版本的 operator、
或 operator 以外的東西回了一份長得像的 JSON，都不得變成租戶畫面上的字。

## 角色

登入入口（外部登入網址、註冊碼）只給能操作服務的人（owner／admin，``can_manage``）；其他成員只看到
狀態與轉發結果。理由：外部登入網址（例如官方 Tailscale 的 ``/a/<token>``）誰先點誰就能把這台節點登入到
自己的帳號。
"""
import ipaddress
import re

#: ``state`` 的七個值（前端 helper ``utils/vpn_node_status.js`` 的 STATES 與此一一對應）。
STATE_ONLINE = 'online'
STATE_OFFLINE = 'offline'            # tailscaled 在跑，但控制面看不到它在線（Self.Online=false）
STATE_NEEDS_LOGIN = 'needs_login'    # 到期／被刪除／從未登入：要重新登入
STATE_NEEDS_APPROVAL = 'needs_approval'  # 控制面要求管理者核可這台機器
STATE_STARTING = 'starting'
STATE_STOPPED = 'stopped'
STATE_UNKNOWN = 'unknown'            # 問不到（pod 不在、容器重啟中、operator 太舊）

_PROBES = frozenset({'ok', 'no_pod', 'pod_not_running', 'exec_failed', 'no_output'})
_BACKEND_STATES = frozenset({
    'NoState', 'NeedsLogin', 'NeedsMachineAuth', 'Stopped', 'Starting', 'Running', 'Unknown',
})
_LOGIN_KINDS = frozenset({'none', 'external_url', 'cluster_internal'})
_FORWARD_STATUSES = frozenset({
    'applied', 'not_applied', 'service_missing', 'mismatch', 'unverified',
})
_SKIP_REASONS = frozenset({
    'bad_format', 'port_range', 'other_namespace', 'not_service_name',
    'duplicate_port', 'too_many', 'namespace_unknown', 'other',
})
_CONTROL_PLANES = frozenset({'official', 'workspace', 'custom'})

_ENTRY_RE = re.compile(r'^[A-Za-z0-9=:.,_-]{1,100}$')
_SERVICE_RE = re.compile(r'^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$')
_DNS_RE = re.compile(r'^[A-Za-z0-9.-]{1,253}$')
_KEY_RE = re.compile(r'^[A-Za-z0-9_-]{4,128}$')
_URL_RE = re.compile(r'^https?://[^\s"\'<>\\]{1,2000}$')
MAX_ITEMS = 40


def control_plane_of(helm_values):
    """服務的控制平面：``official`` | ``workspace`` | ``custom``（同 chart 的解讀）。

    ``tailscale.controlPlane`` 有值且合法就用它；沒有（0.1.2 之前開通的服務）時，
    ``tailscale.loginServer`` 非空 ＝ ``custom``，否則 ``official``。
    """
    node = helm_values.get('tailscale') if isinstance(helm_values, dict) else None
    node = node if isinstance(node, dict) else {}
    plane = node.get('controlPlane')
    if isinstance(plane, str) and plane in _CONTROL_PLANES:
        return plane
    login_server = node.get('loginServer')
    if isinstance(login_server, str) and login_server.strip():
        return 'custom'
    return 'official'


def _port(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 65535 else None


def _state_of(probe, backend_state, online):
    if probe != 'ok':
        return STATE_UNKNOWN
    if backend_state == 'Running':
        return STATE_OFFLINE if online is False else STATE_ONLINE
    if backend_state == 'NeedsLogin':
        return STATE_NEEDS_LOGIN
    if backend_state == 'NeedsMachineAuth':
        return STATE_NEEDS_APPROVAL
    if backend_state in ('NoState', 'Starting'):
        return STATE_STARTING
    if backend_state == 'Stopped':
        return STATE_STOPPED
    return STATE_UNKNOWN


def _clean_forwards(raw):
    rows = []
    for item in (raw if isinstance(raw, list) else [])[:MAX_ITEMS]:
        if not isinstance(item, dict):
            continue
        port = _port(item.get('port'))
        status = item.get('status')
        if port is None or status not in _FORWARD_STATUSES:
            continue
        service = item.get('service')
        rows.append({
            'port': port,
            'service': service if isinstance(service, str) and _SERVICE_RE.fullmatch(service) else None,
            'target_port': _port(item.get('target_port')),
            'status': status,
        })
    return rows


def _clean_skipped(raw):
    rows = []
    for item in (raw if isinstance(raw, list) else [])[:MAX_ITEMS]:
        if not isinstance(item, dict):
            continue
        entry, reason = item.get('entry'), item.get('reason')
        if not (isinstance(entry, str) and _ENTRY_RE.fullmatch(entry)):
            continue
        rows.append({'entry': entry, 'reason': reason if reason in _SKIP_REASONS else 'other'})
    return rows


def _clean_login(raw, can_manage):
    raw = raw if isinstance(raw, dict) else {}
    kind = raw.get('kind') if raw.get('kind') in _LOGIN_KINDS else 'none'
    url = raw.get('url')
    key = raw.get('register_key')
    return {
        'kind': kind,
        # 角色閘：非 owner／admin 一律看不到能用來登入的東西。
        'url': (url if can_manage and kind == 'external_url' and isinstance(url, str)
                and _URL_RE.fullmatch(url) else None),
        'register_key': (key if can_manage and kind == 'cluster_internal' and isinstance(key, str)
                         and _KEY_RE.fullmatch(key) else None),
    }


def public_report(payload, *, can_manage, control_plane='official'):
    """operator 回應 → 前端渲染用的 dict。

    ``payload`` 不是 dict（舊 operator 回了別的東西）→ ``None``，呼叫端回「讀不到」。
    """
    if not isinstance(payload, dict):
        return None
    probe = payload.get('probe') if payload.get('probe') in _PROBES else 'no_output'
    backend = payload.get('backend_state')
    backend = backend if backend in _BACKEND_STATES else 'Unknown'
    online = payload.get('online') if isinstance(payload.get('online'), bool) else None

    ips = []
    for item in (payload.get('tailnet_ips') if isinstance(payload.get('tailnet_ips'), list) else [])[:8]:
        if not isinstance(item, str):
            continue
        try:
            ips.append(str(ipaddress.ip_address(item)))
        except ValueError:
            continue
    dns = payload.get('dns_name')

    serve = payload.get('serve') if isinstance(payload.get('serve'), dict) else {}
    return {
        'state': _state_of(probe, backend, online),
        'probe': probe,
        'control_plane': control_plane if control_plane in _CONTROL_PLANES else 'official',
        'tailnet_ips': ips[:4],
        'dns_name': dns if isinstance(dns, str) and _DNS_RE.fullmatch(dns) else None,
        'login': _clean_login(payload.get('login'), can_manage),
        'serve': {
            'wanted_known': serve.get('wanted_known') is True,
            'forwards': _clean_forwards(serve.get('forwards')),
            'skipped': _clean_skipped(serve.get('skipped')),
            'ignored_all': 'namespace_unknown' if serve.get('ignored_all') == 'namespace_unknown' else None,
            'extra_config': serve.get('extra_config') is True,
        },
    }
