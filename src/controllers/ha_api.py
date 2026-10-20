"""Home Assistant Integration API controller.

Provides OAuth 2.0 Bearer Token protected REST API endpoints
for Home Assistant Custom Component integration.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from odoo.http import Controller, Response, request, route

from .oauth2 import verify_oauth_token
from ..models.tunnel_binding_mixin import TunnelBindingConflict
from ..models.workspace_access import ROLE_ADMIN
from ..services.paas_capability import _PaaSCapability
from odoo import _

_logger = logging.getLogger(__name__)


def _json_response(data: dict, status: int = 200) -> Response:
    """Return a JSON response with proper content-type header.

    Args:
        data: Dictionary to serialize as JSON.
        status: HTTP status code.

    Returns:
        HTTP Response with JSON body.
    """
    return Response(
        json.dumps(data),
        status=status,
        content_type='application/json',
        headers={'Cache-Control': 'no-store', 'Pragma': 'no-cache'},
    )


def _release_binding(record, kwargs: dict) -> Response:
    """#1766：HA 端移除整合時呼叫，把綁定讓出來給下一台。

    只有持有者自己放得掉（`instance_uid` 要對得上），否則 409——不然 B 機器可以先
    放掉 A 的綁定再自己搶，整個閘就白做了。已經 unbound 回 200（冪等：HA 可能重試、
    平台可能先被後台解綁過）。

    ⚠ 這個端點**不會**讓對方的 cloudflared 斷線，平台收不回已交出的權杖；它只決定
    「下一台能不能接手」。
    """
    released, holder = record._tunnel_binding_release(
        (kwargs.get('instance_uid') or '').strip()[:64])
    if not released:
        return _json_response({
            'error': 'not_the_holder',
            'detail': _("This tunnel is held by another Home Assistant "
                        "installation (%(label)s); only that installation can "
                        "release it. A workspace owner or admin can release it "
                        "from the portal.", label=holder),
            'bound_to': holder,
        }, status=409)
    return _json_response({'binding_state': record.binding_state})


def _claim_binding(record, kwargs: dict) -> Response | None:
    """#1766：把這條 tunnel 綁給呼叫端那一台 HA，衝突時回 409。

    `instance_uid` 是呼叫端自報的 Home Assistant instance id（元件 0.4.12 起會
    帶）。第一台要到權杖的會被記成綁定對象；之後**其他** uid 來要同一筆資源就拿不
    到權杖。沒帶 uid 的舊元件照發（legacy），但 legacy → claimed 是單向的，見
    `models/tunnel_binding_mixin.py` 的模組 docstring。

    ⚠ 回 409 而不是 403：這不是權限問題（呼叫端確實是 owner／admin），而是資源
    狀態衝突，而且對方有明確修法（在後台解綁或改綁別的）。元件端靠這個狀態碼決定
    「不要退回用快取的權杖」——那正是會長出第二個 connector 的那條路。

    Returns a Response to send back, or None when the claim succeeded.
    """
    try:
        record._tunnel_binding_claim(
            (kwargs.get('instance_uid') or '').strip()[:64],
            (kwargs.get('instance_label') or '').strip()[:128],
        )
    except TunnelBindingConflict as conflict:
        _logger.warning(
            "[tunnel-binding] refused tunnel token: %s id=%s already bound to "
            "%s (caller uid=%r)",
            record._name, record.id, conflict.bound_to or '-',
            (kwargs.get('instance_uid') or '')[:64],
        )
        return _json_response({
            'error': 'already_bound',
            'detail': str(conflict),
            'bound_to': conflict.bound_to,
            'bound_at': conflict.bound_at,
        }, status=409)
    return None


def _json_error(error: str, detail: str, status: int = 400) -> Response:
    """Return a JSON error response.

    Args:
        error: Short error identifier (e.g. 'Unauthorized').
        detail: Human-readable error description.
        status: HTTP status code.

    Returns:
        HTTP Response with JSON error body.
    """
    return _json_response({'error': error, 'detail': detail}, status=status)


def _authenticate(
    required_scopes: list[str] | None = None,
) -> tuple[Any, Any] | Response:
    """Authenticate the request via OAuth Bearer token.

    Wraps ``verify_oauth_token`` and translates werkzeug exceptions
    into JSON error responses suitable for a REST API consumer.

    Args:
        required_scopes: Scopes that must all be present on the token.

    Returns:
        ``(user, token)`` tuple on success, or a ``Response`` on failure.
    """
    from werkzeug.exceptions import Unauthorized, Forbidden

    try:
        return verify_oauth_token(request, required_scopes=required_scopes)
    except Unauthorized as exc:
        return _json_error('Unauthorized', str(exc.description), 401)
    except Forbidden as exc:
        return _json_error('Forbidden', str(exc.description), 403)


def _capability(user: Any) -> _PaaSCapability:
    """Token 主體（``token.user_id``）的 /woow capability（#1528）。

    HA 端點是 ``auth='none'``：``request.env`` 沒有 uid，而 ``user`` 是
    ``verify_oauth_token`` 以 sudo 搜到的 token 擁有者。這裡用**該使用者、非 sudo**
    的 env 建 capability，成員／角色判準就與 /woow（``smart_home.py``／
    ``security_access.py``／``paas.py``）完全同一把尺：

    * internal：照舊走 ``workspace.check_user_access`` 與 ORM rules；
    * Portal（``share=True``）：capability 以固定 uid、sudo 查 ``workspace_access``，
      只認 active workspace、active 使用者的成員列。

    修正前這兩支 helper 對 ``user.share`` 直接回空／``None``（#1261 時代「Portal
    用不了 workspace」的判斷），但 /woow 自 #991 起已讓 Portal 成員使用產品——
    結果 Portal 客戶在 /woow 看得到自己的 SH／SA，HA 元件卻回「沒有任何工作區」。
    """
    return _PaaSCapability(request.env(user=user.id))


def _get_user_workspaces(user: Any) -> Any:
    """Return active workspaces the user is a member of.

    Internal 與 Portal 同一判準（``_PaaSCapability.memberships``，見
    ``_capability``）；封存的 workspace、停用的帳號都不列出。

    Args:
        user: ``res.users`` browse record（token 擁有者）。

    Returns:
        Recordset of ``woow_paas_platform.workspace``.
    """
    capability = _capability(user)
    if not capability.authenticated:
        # 停用／public 帳號：capability.memberships() 會丟 AccessError（→ 500）；
        # HA 端點的契約是「沒有成員資格 ⇒ 空清單」。
        return request.env['woow_paas_platform.workspace']
    return capability.memberships().mapped('workspace_id')


def _check_workspace_access(user: Any, workspace_id: int) -> Any | None:
    """Verify user is a member of an active workspace.

    與 /woow 的 ``_get_workspace_or_error`` 同形：capability 判定成員資格，
    再要求 ``state == 'active'``（internal 的 ``check_user_access`` 本身不看
    workspace 狀態，封存的 workspace 必須在這裡擋）。

    Args:
        user: ``res.users`` browse record（token 擁有者）。
        workspace_id: Database ID of the workspace.

    Returns:
        Workspace browse record if accessible, ``None`` otherwise. 呼叫端只拿
        ``.id`` 去做後續 sudo 查詢，不依賴回傳 record 的 env。
    """
    workspace = _capability(user).workspace(workspace_id)
    if not workspace or workspace.state != 'active':
        return None
    return workspace


def _require_manage_role(user: Any, workspace: Any) -> Response | None:
    """tunnel-token 端點的角色閘：只有該 workspace 的 owner／admin。

    其他 HA 端點（列表、詳情、狀態）只過 ``_check_workspace_access``——任何成員
    都能看。tunnel token 不同：拿到的人可以自己跑一個 connector 加入這條 tunnel、
    分走流量，屬於「設定」等級，role=user／guest 只能用瀏覽類功能。/woow 的
    SA token 出口（``security_access.api_security_access_tunnel_token``）本來就
    收斂到 owner/admin；2026-09-30 正式站實測 role=user 成員在 /woow 被擋、改走
    HA OAuth 卻拿得到完整 token（SH 與 SA 都是），這裡補上同一把尺。

    回 403 並帶獨立的 ``error='insufficient_role'``：HA component 靠它和「scope
    不足」「不是成員」（同樣 403）區分——重新授權修不好角色，component 要改提示
    「請 owner／admin 設定」而不是叫使用者 reauth。

    角色判定與 /woow 的 SA token 出口同一把尺（``_PaaSCapability.access``，
    owner ≥ admin 放行），Portal owner／admin 也適用（#1528）；``workspace`` 是
    sudo 取得的父層 record，capability 只用它的 id、不繼承 sudo。

    Returns:
        ``None`` 表示放行；否則為 403 JSON ``Response``。
    """
    if _capability(user).access(workspace, required_role=ROLE_ADMIN):
        return None
    return _json_error(
        'insufficient_role',
        _('Only workspace owners and admins can get the tunnel token.'),
        403,
    )


def _with_live_tunnel_status(record: Any) -> dict:
    """``record.to_dict()`` 疊上即時 tunnel 值（只讀，不寫 DB）。

    HA status 端點（SH／SA 各一支）原本呼叫 ``action_refresh_status()``——那是
    DB 寫入，而這兩支只過 OAuth scope 檢查（``_authenticate``）＋ workspace
    成員資格（``_check_workspace_access``），記錄又是 ``sudo()`` 取的 ⇒ 任何
    持 read scope 的成員（含 role=user）都能觸發一次寫入；/woow 的同一個動作
    （``refresh_status``）卻要 owner/admin（#524）。改成讀即時值後：每個有權限
    的成員都拿得到新鮮狀態，而且這條路徑上完全沒有寫入，兩邊的角色閘自然一致。

    回應形狀刻意與改動前**逐 key 相同**：只覆蓋 ``to_dict()`` 已經有的 key，
    ``tunnel_status_live`` 這種新 key 不外流給 HA component（它是外部安裝的
    client，舊版本會照舊的 key 集合解析）。拿不到即時值（operator 未設定／
    錯誤）時 ``_live_tunnel_status`` 回 ``{}``，退化成 DB 值。

    ⚠ 與舊行為的差別（兩格，不是一格）：
      1. operator 失敗時不再把 ``tunnel_status='error'`` 與 ``error_message``
         寫回 DB，而是回上一次寫入的值——與 /woow 詳情頁（#1256 / SH detail get）
         的退化方式一致。
      2. **成功時也不再寫回**。舊行為下，HA component 定期輪詢這兩支 status
         端點，是 DB 裡 ``tunnel_status``／``connector_*`` 唯一的**自動**更新來源
         （沒有任何 cron 呼叫 ``action_refresh_status``；其餘呼叫點只有 /woow 的
         owner/admin Refresh 與後台表單按鈕）。改只讀之後，stored 值＝「上一次
         有人手動 Refresh 的值」。所以凡是 HA 會讀到 tunnel 狀態的端點都要自己
         疊即時值：兩支 status 與兩支 detail 端點（``get_home``／
         ``sa_get_access``）走本函式，兩支 list 端點（``list_homes``／
         ``sa_list_accesses``）走 ``_live_tunnel_status_by_id``——否則 HA 會永遠
         停在上一次 Refresh（例如 tunnel 已 0 connector 數天仍回 ``connected``）。其他讀 stored 值的地方（/woow 清單卡片等）本來就不靠
         HA 輪詢刷新，不在這條回歸的範圍內。
    """
    data = record.to_dict()
    try:
        live = record._live_tunnel_status()
    except Exception:
        # `_live_tunnel_status` 自己只吞 PaaSOperatorError；這裡保留舊端點
        # 「refresh 失敗也回目前的值、不回 500」的保證（HA component 會定期輪詢，
        # 一次意外的例外不該讓整個 sensor 變 unavailable）。
        _logger.exception(
            "Live tunnel status failed for %s id=%s", record._name, record.id)
        live = {}
    data.update({k: v for k, v in live.items() if k in data})
    return data


def _live_tunnel_status_by_id(records: Any) -> dict:
    """HA list 端點用：``{record.id: tunnel_status}``，即時值優先、拿不到退回 DB 值。

    為什麼 list 也要疊：見 ``_with_live_tunnel_status`` docstring 第 2 格——status
    端點改只讀之後，stored ``tunnel_status`` 不再被 HA 輪詢自動刷新，list 若照舊
    讀 stored 值就會永遠停在上一次手動 Refresh。

    ⚠ /woow 的 SH／SA 清單刻意**不**疊（``controllers/smart_home.py`` detail
    ``get`` 的註解：「list 每一台各打一次 operator 太貴」）。HA 這兩支不同：
    範圍是單一 workspace、只回 ``tunnel_status`` 一格，而且改動前 HA 的輪詢本來
    就是「每台一次 operator 呼叫」——這裡只是把那筆成本搬回來，不是新增一類。

    有界：operator 第一次拿不到（未設定／逾時／錯誤，``_live_tunnel_status`` 回
    ``{}``）就停止對剩下的列發請求，一律退回 DB 值——operator 掛掉時一次 list
    請求最多只吃一次逾時，不是 N 次。沒有 ``tunnel_id`` 的列（尚未 provision）
    不打 operator，也不觸發熔斷。只讀、不寫 DB（理由同上）。
    """
    out = {}
    operator_down = False
    for rec in records:
        status = rec.tunnel_status or 'disconnected'
        if rec.tunnel_id and not operator_down:
            try:
                live = rec._live_tunnel_status()
            except Exception:
                # 同 `_with_live_tunnel_status`：一次意外例外不該讓整個清單 500。
                _logger.exception(
                    "Live tunnel status failed for %s id=%s", rec._name, rec.id)
                live = {}
            if live.get('tunnel_status'):
                status = live['tunnel_status']
            else:
                operator_down = True
        out[rec.id] = status
    return out


class HAIntegrationController(Controller):
    """REST API endpoints for Home Assistant Custom Component.

    All endpoints require OAuth 2.0 Bearer Token authentication.
    Responses use standard JSON (not Odoo JSON-RPC).
    """

    # ==================== Workspaces ====================

    @route(
        '/api/smarthome/workspaces',
        type='http', auth='none', methods=['GET'], csrf=False,
    )
    def list_workspaces(self, **kwargs: Any) -> Response:
        """List workspaces accessible to the authenticated user.

        Required scope: ``workspace:read``

        Returns:
            JSON with ``workspaces`` array containing id, name, slug.
        """
        result = _authenticate(required_scopes=['workspace:read'])
        if isinstance(result, Response):
            return result
        user, _token = result

        workspaces = _get_user_workspaces(user)
        data = [
            {
                'id': ws.id,
                'name': ws.name,
                'slug': ws.slug or '',
            }
            for ws in workspaces
        ]
        return _json_response({'workspaces': data})

    # ==================== Homes by Workspace ====================

    @route(
        '/api/smarthome/workspaces/<int:workspace_id>/homes',
        type='http', auth='none', methods=['GET'], csrf=False,
    )
    def list_homes(self, workspace_id: int, **kwargs: Any) -> Response:
        """List smart homes in a workspace.

        Required scope: ``smarthome:read``

        Args:
            workspace_id: Workspace database ID.

        Returns:
            JSON with ``homes`` array containing id, name, state,
            subdomain, tunnel_status.
        """
        result = _authenticate(required_scopes=['smarthome:read'])
        if isinstance(result, Response):
            return result
        user, _token = result

        workspace = _check_workspace_access(user, workspace_id)
        if not workspace:
            return _json_error('Forbidden', _('No access to this workspace'), 403)

        homes = request.env['woow_paas_platform.smart_home'].sudo().search([
            ('workspace_id', '=', workspace.id),
        ])
        # tunnel_status 疊即時值（只讀）：status 端點改只讀後 stored 值不再被
        # HA 輪詢自動刷新，見 `_live_tunnel_status_by_id`。
        tunnel_status = _live_tunnel_status_by_id(homes)
        data = [
            {
                'id': h.id,
                'name': h.name,
                'state': h.state,
                'subdomain': h.subdomain or '',
                'tunnel_status': tunnel_status[h.id],
            }
            for h in homes
        ]
        return _json_response({'homes': data})

    # ==================== Single Home Detail ====================

    @route(
        '/api/smarthome/homes/<int:smarthome_id>',
        type='http', auth='none', methods=['GET'], csrf=False,
    )
    def get_home(self, smarthome_id: int, **kwargs: Any) -> Response:
        """Get full details of a smart home.

        Required scope: ``smarthome:read``

        Args:
            smarthome_id: Smart home database ID.

        Returns:
            JSON with ``home`` object (full ``to_dict()`` output).
        """
        result = _authenticate(required_scopes=['smarthome:read'])
        if isinstance(result, Response):
            return result
        user, _token = result

        home = self._get_home_with_access(user, smarthome_id)
        if isinstance(home, Response):
            return home

        # 疊即時 tunnel 值（只讀）：理由見 `_with_live_tunnel_status` 第 2 格。
        return _json_response({'home': _with_live_tunnel_status(home)})

    # ==================== Tunnel Token ====================

    @route(
        '/api/smarthome/homes/<int:smarthome_id>/tunnel-token',
        type='http', auth='none', methods=['GET'], csrf=False,
    )
    def get_tunnel_token(self, smarthome_id: int, **kwargs: Any) -> Response:
        """Get tunnel credentials for a smart home.

        Required scope: ``smarthome:tunnel``；呼叫者須為該 workspace 的
        owner／admin（``_require_manage_role``）。

        Args:
            smarthome_id: Smart home database ID.

        Returns:
            JSON with tunnel_token, tunnel_id, subdomain.
        """
        result = _authenticate(required_scopes=['smarthome:tunnel'])
        if isinstance(result, Response):
            return result
        user, _token = result

        home = self._get_home_with_access(user, smarthome_id)
        if isinstance(home, Response):
            return home
        denied = _require_manage_role(user, home.workspace_id)
        if denied:
            return denied

        # `_get_home_with_access` returns a sudoed record only after the
        # workspace and owner/admin gates above have accepted the caller.
        sub = home.subscription_id
        if sub and sub._woow_trial_expired():
            return _json_error('trial_expired',
                               '七天試用已結束，請到 woowtech 選擇付費方案。', 409)

        # 綁定閘在**發權杖之前**（#1766）：claim 失敗就不該有權杖出門。claim 成功
        # 但 HTTP 回應在路上丟掉沒關係——同一個 uid 再問一次是冪等的。
        conflict = _claim_binding(home, kwargs)
        if conflict is not None:
            return conflict

        domain = home._get_paas_domain()
        subdomain = home.subdomain or ''
        fqdn = f"{subdomain}.{domain}" if subdomain else ''

        return _json_response({
            'tunnel_token': home.tunnel_token or '',
            'tunnel_id': home.tunnel_id or '',
            'subdomain': fqdn,
            'mcp': home._get_mcp_integration(),
        })

    @route(
        '/api/smarthome/homes/<int:smarthome_id>/tunnel-binding',
        type='http', auth='none', methods=['DELETE'], csrf=False,
    )
    def release_tunnel_binding(self, smarthome_id: int, **kwargs: Any) -> Response:
        """Release this smart home's Home Assistant binding (#1766).

        HA 的 `async_remove_entry` 會打這裡：使用者在 HA 移除整合 ⇒ 這個家要能馬上
        給下一台接。沒有這一支的話，綁定只能靠人去後台按「解除綁定」，換機器就卡住。

        Required scope: ``smarthome:tunnel``（跟拿權杖同一級——放掉綁定是它的反向
        動作）；呼叫者須為 owner／admin。只有持有者本人放得掉，見 `_release_binding`。
        """
        result = _authenticate(required_scopes=['smarthome:tunnel'])
        if isinstance(result, Response):
            return result
        user, _token = result

        home = self._get_home_with_access(user, smarthome_id)
        if isinstance(home, Response):
            return home
        denied = _require_manage_role(user, home.workspace_id)
        if denied:
            return denied
        return _release_binding(home, kwargs)

    # ==================== Live Status ====================

    @route(
        '/api/smarthome/homes/<int:smarthome_id>/status',
        type='http', auth='none', methods=['GET'], csrf=False,
    )
    def get_home_status(self, smarthome_id: int, **kwargs: Any) -> Response:
        """Return the live tunnel status of a smart home (read-only).

        Required scope: ``smarthome:read``

        不再呼叫 ``action_refresh_status()``（DB 寫入）——理由見
        ``_with_live_tunnel_status``。存取檢查不變：scope ＋ workspace 成員。

        Args:
            smarthome_id: Smart home database ID.

        Returns:
            JSON with ``home`` object (same keys as ``to_dict()``) whose
            tunnel fields carry the operator's live values when available.
        """
        result = _authenticate(required_scopes=['smarthome:read'])
        if isinstance(result, Response):
            return result
        user, _token = result

        home = self._get_home_with_access(user, smarthome_id)
        if isinstance(home, Response):
            return home

        return _json_response({'home': _with_live_tunnel_status(home)})

    # ==================== Security Access (mirror of smarthome) ========

    @route(
        '/api/security-access/workspaces',
        type='http', auth='none', methods=['GET'], csrf=False,
    )
    def sa_list_workspaces(self, **kwargs: Any) -> Response:
        """Mirror of /api/smarthome/workspaces. Scope: workspace:read."""
        result = _authenticate(required_scopes=['workspace:read'])
        if isinstance(result, Response):
            return result
        user, _token = result
        workspaces = _get_user_workspaces(user)
        return _json_response({'workspaces': [
            {'id': ws.id, 'name': ws.name, 'slug': ws.slug or ''}
            for ws in workspaces
        ]})

    @route(
        '/api/security-access/workspaces/<int:workspace_id>/accesses',
        type='http', auth='none', methods=['GET'], csrf=False,
    )
    def sa_list_accesses(self, workspace_id: int, **kwargs: Any) -> Response:
        """List security accesses in a workspace.

        Scope: security-access:read. Returns plan + next payment for the
        HA sensor (FR-9). tunnel_token 絕不在列表/detail 出現 —— 只走
        tunnel-token 專用端點（更高 scope）。
        """
        result = _authenticate(required_scopes=['security-access:read'])
        if isinstance(result, Response):
            return result
        user, _token = result
        workspace = _check_workspace_access(user, workspace_id)
        if not workspace:
            return _json_error('Forbidden', _('No access to this workspace'), 403)
        records = request.env['woow_paas_platform.security_access'].sudo().search([
            ('workspace_id', '=', workspace.id),
        ])
        # 同 list_homes：tunnel_status 疊即時值（只讀），見 `_live_tunnel_status_by_id`。
        tunnel_status = _live_tunnel_status_by_id(records)
        data = []
        for sa in records:
            sub_info = sa._get_subscription_info()
            data.append({
                'id': sa.id,
                'name': sa.name,
                'canonical_name': sa.canonical_name or '',
                'state': sa.state,
                'tunnel_status': tunnel_status[sa.id],
                'plan_name': sub_info['plan_name'] if sub_info else None,
                'next_payment': sub_info['next_payment'] if sub_info else None,
            })
        return _json_response({'accesses': data})

    @route(
        '/api/security-access/accesses/<int:sa_id>',
        type='http', auth='none', methods=['GET'], csrf=False,
    )
    def sa_get_access(self, sa_id: int, **kwargs: Any) -> Response:
        """Full detail. Scope: security-access:read."""
        result = _authenticate(required_scopes=['security-access:read'])
        if isinstance(result, Response):
            return result
        user, _token = result
        sa = self._get_sa_with_access(user, sa_id)
        if isinstance(sa, Response):
            return sa
        # 疊即時 tunnel 值（只讀）：理由見 `_with_live_tunnel_status` 第 2 格。
        return _json_response({'access': _with_live_tunnel_status(sa)})

    @route(
        '/api/security-access/accesses/<int:sa_id>/tunnel-token',
        type='http', auth='none', methods=['GET'], csrf=False,
    )
    def sa_get_tunnel_token(self, sa_id: int, **kwargs: Any) -> Response:
        """Tunnel credentials. Scope: security-access:tunnel (elevated).

        呼叫者須為該 workspace 的 owner／admin（``_require_manage_role``）。
        """
        result = _authenticate(required_scopes=['security-access:tunnel'])
        if isinstance(result, Response):
            return result
        user, _token = result
        sa = self._get_sa_with_access(user, sa_id)
        if isinstance(sa, Response):
            return sa
        denied = _require_manage_role(user, sa.workspace_id)
        if denied:
            return denied

        # 與 smart home 同步收緊（#1766）。⚠ 這兩個端點是鏡像：只收一側，攻擊面
        # 只是搬家到另一側（`two-layer-acl-lockstep` pattern）。
        conflict = _claim_binding(sa, kwargs)
        if conflict is not None:
            return conflict
        # Asymmetry with smart home's tunnel-token (which returns a single
        # `subdomain` FQDN) is intentional: a Security Access is a Cloudflare
        # Tunnel with N application routes and no single hostname, so there is
        # no one FQDN to return here. Consumers that need route hostnames read
        # them from the access detail (to_dict `routes`), not from this token
        # endpoint.
        return _json_response({
            'tunnel_token': sa.tunnel_token or '',
            'tunnel_id': sa.tunnel_id or '',
        })

    @route(
        '/api/security-access/accesses/<int:sa_id>/tunnel-binding',
        type='http', auth='none', methods=['DELETE'], csrf=False,
    )
    def sa_release_tunnel_binding(self, sa_id: int, **kwargs: Any) -> Response:
        """Mirror of ``release_tunnel_binding``. Scope: security-access:tunnel.

        ⚠ 兩個端點是鏡像：claim 收緊時兩邊一起收，release 也必須兩邊一起開——只開
        一側的話，SA 那邊換機器照樣卡死（`two-layer-acl-lockstep`）。
        """
        result = _authenticate(required_scopes=['security-access:tunnel'])
        if isinstance(result, Response):
            return result
        user, _token = result
        sa = self._get_sa_with_access(user, sa_id)
        if isinstance(sa, Response):
            return sa
        denied = _require_manage_role(user, sa.workspace_id)
        if denied:
            return denied
        return _release_binding(sa, kwargs)

    @route(
        '/api/security-access/accesses/<int:sa_id>/status',
        type='http', auth='none', methods=['GET'], csrf=False,
    )
    def sa_get_status(self, sa_id: int, **kwargs: Any) -> Response:
        """Live connector status (read-only). Scope: security-access:read.

        鏡像 ``get_home_status``：改讀 ``SecurityAccess._live_tunnel_status``
        （#1256），不再 ``action_refresh_status()`` 寫 DB。理由見
        ``_with_live_tunnel_status``。
        """
        result = _authenticate(required_scopes=['security-access:read'])
        if isinstance(result, Response):
            return result
        user, _token = result
        sa = self._get_sa_with_access(user, sa_id)
        if isinstance(sa, Response):
            return sa
        return _json_response({'access': _with_live_tunnel_status(sa)})

    def _get_sa_with_access(self, user: Any, sa_id: int) -> Any | Response:
        """Look up a security access and verify workspace access."""
        sa = request.env['woow_paas_platform.security_access'].sudo().browse(
            sa_id).exists()
        if not sa:
            return _json_error('Not Found', _('Security access not found'), 404)
        workspace = _check_workspace_access(user, sa.workspace_id.id)
        if not workspace:
            return _json_error(
                'Forbidden',
                _('No access to the workspace containing this security access'),
                403,
            )
        return sa

    # ==================== Private Helpers ====================

    def _get_home_with_access(
        self, user: Any, smarthome_id: int,
    ) -> Any | Response:
        """Look up a smart home and verify the user has workspace access.

        Args:
            user: Authenticated ``res.users`` browse record.
            smarthome_id: Smart home database ID.

        Returns:
            Smart home browse record on success, or JSON error ``Response``.
        """
        home = request.env['woow_paas_platform.smart_home'].sudo().browse(
            smarthome_id,
        ).exists()
        if not home:
            return _json_error('Not Found', _('Smart home not found'), 404)

        workspace = _check_workspace_access(user, home.workspace_id.id)
        if not workspace:
            return _json_error(
                'Forbidden',
                _('No access to the workspace containing this smart home'),
                403,
            )

        return home
