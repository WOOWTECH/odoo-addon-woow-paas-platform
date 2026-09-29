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
    )


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


def _get_user_workspaces(user: Any) -> Any:
    """Return active workspaces the user has access to.

    Args:
        user: ``res.users`` browse record.

    Returns:
        Recordset of ``woow_paas_platform.workspace``.
    """
    access_records = request.env['woow_paas_platform.workspace_access'].sudo().search([
        ('user_id', '=', user.id),
        ('workspace_id.state', '=', 'active'),
    ])
    return access_records.mapped('workspace_id')


def _check_workspace_access(user: Any, workspace_id: int) -> Any | None:
    """Verify user has access to a specific workspace.

    Args:
        user: ``res.users`` browse record.
        workspace_id: Database ID of the workspace.

    Returns:
        Workspace browse record if accessible, ``None`` otherwise.
    """
    access = request.env['woow_paas_platform.workspace_access'].sudo().search([
        ('user_id', '=', user.id),
        ('workspace_id', '=', workspace_id),
        ('workspace_id.state', '=', 'active'),
    ], limit=1)
    if not access:
        return None
    return access.workspace_id


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

        Required scope: ``smarthome:tunnel``

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

        domain = home._get_paas_domain()
        subdomain = home.subdomain or ''
        fqdn = f"{subdomain}.{domain}" if subdomain else ''

        return _json_response({
            'tunnel_token': home.tunnel_token or '',
            'tunnel_id': home.tunnel_id or '',
            'subdomain': fqdn,
            'mcp': home._get_mcp_integration(),
        })

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
        """Tunnel credentials. Scope: security-access:tunnel (elevated)."""
        result = _authenticate(required_scopes=['security-access:tunnel'])
        if isinstance(result, Response):
            return result
        user, _token = result
        sa = self._get_sa_with_access(user, sa_id)
        if isinstance(sa, Response):
            return sa
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
