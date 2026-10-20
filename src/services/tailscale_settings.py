# -*- coding: utf-8 -*-
"""VPN 節點（charts/tailscale）設定的**存檔前**驗證——INFRA-TS-005-5、INFRA-TS-007-1。

純函式、不 import odoo（同 ``vpn_node_status.py``）。只在存檔（create／reconfigure）時跑，
**部署組裝不套用**：既有服務已存的值就算不合這裡的規則，重新部署時照舊交給 chart（chart 的
entrypoint 只略過壞的那一筆），不會因為這支驗證變嚴而整欄被丟掉。

## 為什麼要在平台擋

商用驗收 Tailscale 242（.243）：

* INFRA-TS-007-1：轉發清單輸入 ``9101=no-such-svc:8080,9102=svc-x.other-ns.svc.cluster.local:80,
  70000=a:1`` 沒有任何欄位驗證，儲存鈕照樣能按；不合格的項目只在容器 stderr 被略過。
* INFRA-TS-005-5：``controlPlane=custom``、``loginServer``＝另一台的內部網址、``authKey``＝已撤銷的
  key 儲存後 ``success=true``；控制平面與 key 種類錯配、自訂網址留空或格式錯，都要等到節點登入失敗
  才在容器日誌出現。

## 規則的來源

``check_serve`` 逐條對照映像 entrypoint 的 ``parse_serve``（paas-odoo-ci ``tailscale/entrypoint.sh``；
節點端仍以它為準、並在服務頁「VPN 節點狀態」回報略過原因）：格式 ``<tailnet 埠>=<服務>:<埠>``、
埠 1–65535、只接受**同一工作區** namespace 的 Service（裸名或 ``<名>.<ns>[.svc[.<cluster domain>]]``）、
Service 名是 DNS label、同一個 tailnet 埠不重複、最多 32 筆。兩邊規則一致，平台擋下的就是節點會略過的。

``check_login`` 只擋**一定不會成功**的組合（不猜 key 的真偽）：

* 控制平面「官方 Tailscale」卻貼了不是 ``tskey-`` 開頭的 key（Headscale 的 key）；
* 控制平面是 Headscale（同工作區或自訂）卻貼了 ``tskey-`` 開頭的 key；
* 「自訂網址」卻沒填控制伺服器、或不是 ``http(s)://主機`` 形式；
* key 中間有空白或換行（複製時夾帶）。

每個 check 回 ``None`` 或 ``(code, params)``；``code`` 由 ``cloud_app_template._spec_format_message``
翻成租戶語言。
"""
import re
from urllib.parse import urlsplit

SERVE_MAX = 32
CONTROL_PLANES = ('official', 'workspace', 'custom')

_ENTRY_RE = re.compile(r'^([0-9]+)=([A-Za-z0-9.-]+):([0-9]+)$')
_NAME_RE = re.compile(r'^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$')
_SAFE_RE = re.compile(r'[^A-Za-z0-9=:.,_/@-]')
_HOST_RE = re.compile(r'^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$')


def _show(entry):
    """錯誤訊息裡回顯租戶自己輸入的那一段：限長、限字元。"""
    return _SAFE_RE.sub('?', str(entry))[:80] or '?'


def _valid_port(text):
    return text.isdigit() and len(text) <= 5 and 1 <= int(text) <= 65535


def check_serve(raw, namespace, domain='cluster.local'):
    """``tailscale.serve`` → 第一個不合格項目的 ``(code, params)``；全部合格（或空）回 ``None``。

    Args:
        raw: 租戶輸入的轉發清單（逗號分隔）
        namespace: 這台服務的 namespace（``cloud_service.helm_namespace``）；空值＝不知道，此時只接受
            裸的 Service 名（chart 在 namespace 不明時會整單忽略）
        domain: 叢集網域（chart 預設 ``cluster.local``）
    """
    if raw is None or raw is False:
        return None
    if not isinstance(raw, str):
        return ('serve_bad_format', {'entry': _show(raw)})
    ns = (namespace or '').strip().lower()
    allowed_rest = {''}
    if ns:
        allowed_rest |= {ns, ns + '.svc', '%s.svc.%s' % (ns, domain)}
    seen = set()
    for piece in raw.split(','):
        entry = re.sub(r'\s+', '', piece)
        if not entry:
            continue
        match = _ENTRY_RE.fullmatch(entry)
        if not match:
            return ('serve_bad_format', {'entry': _show(entry)})
        lport, target, tport = match.groups()
        if not (_valid_port(lport) and _valid_port(tport)):
            return ('serve_port_range', {'entry': _show(entry)})
        target = target.lower().rstrip('.')
        name, _, rest = target.partition('.')
        if rest not in allowed_rest:
            return ('serve_other_namespace', {'entry': _show(entry)})
        if not _NAME_RE.fullmatch(name) or name == 'localhost':
            return ('serve_not_service_name', {'entry': _show(entry)})
        port = int(lport)
        if port in seen:
            return ('serve_duplicate_port', {'entry': _show(entry), 'port': port})
        if len(seen) >= SERVE_MAX:
            return ('serve_too_many', {'max': SERVE_MAX})
        seen.add(port)
    return None


def effective_control_plane(control_plane, login_server):
    """同 chart（與 ``vpn_node_status.control_plane_of``）的解讀：合法值優先；沒有時自訂網址非空＝custom。"""
    if isinstance(control_plane, str) and control_plane in CONTROL_PLANES:
        return control_plane
    if isinstance(login_server, str) and login_server.strip():
        return 'custom'
    return 'official'


def check_login(control_plane, login_server, auth_key):
    """控制平面／控制伺服器／auth key 的組合 → 第一個問題的 ``(code, params)`` 或 ``None``。

    ``auth_key`` 是 None／空字串時不檢查 key（沒填＝改用網頁登入，或沿用既有、看不到的值）。
    """
    plane = effective_control_plane(control_plane, login_server)
    if plane == 'custom':
        server = login_server.strip() if isinstance(login_server, str) else ''
        if not server:
            return ('login_server_required', {})
        try:
            parts = urlsplit(server)
            host = parts.hostname or ''
        except ValueError:
            return ('login_server_invalid', {'entry': _show(server)})
        if (parts.scheme not in ('http', 'https') or not _HOST_RE.fullmatch(host)
                or re.search(r'\s', server)):
            return ('login_server_invalid', {'entry': _show(server)})
    if not isinstance(auth_key, str) or not auth_key.strip():
        return None
    key = auth_key.strip()
    if re.search(r'\s', key):
        return ('auth_key_whitespace', {})
    is_tailscale_key = key.startswith('tskey-')
    if plane == 'official' and not is_tailscale_key:
        return ('auth_key_not_tailscale', {})
    if plane == 'workspace' and is_tailscale_key:
        return ('auth_key_is_tailscale', {})
    if plane == 'custom' and is_tailscale_key and not _is_tailscale_host(login_server):
        return ('auth_key_is_tailscale', {})
    return None


def _is_tailscale_host(login_server):
    """自訂網址其實指向官方 Tailscale 控制面（``*.tailscale.com``）時，``tskey-`` 是對的。"""
    try:
        host = (urlsplit(login_server.strip()).hostname or '').lower()
    except (AttributeError, ValueError):
        return False
    return host == 'tailscale.com' or host.endswith('.tailscale.com')
