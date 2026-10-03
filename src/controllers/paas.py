from __future__ import annotations

import ipaddress
import json
import logging
import threading
import traceback
import uuid
from typing import Any
from urllib.parse import urlparse

from odoo import _, fields
from odoo.exceptions import (
    AccessDenied, AccessError, MissingError, UserError, ValidationError,
)
from psycopg2 import OperationalError
from odoo.http import request, route, Controller
from odoo.tools import escape_psql

from .ha_paas import HiddenChainMixin
from .billing import _invoice_payability_fields
from ..models.workspace_access import (
    ROLE_OWNER, ROLE_ADMIN, ROLE_USER,
    ASSIGNABLE_ROLES,
)
from ..services.paas_capability import _PaaSCapability
from ..services.paas_operator import (
    get_paas_operator_client,
    PaaSOperatorError,
    PaaSOperatorConnectionError,
)
from ..models.cloud_app_template import SPECS_UNPARSEABLE  # #1052：哨兵隨 parser 搬到 template model
from ..models.cloud_service import WOOW_ODOO_ADMIN_PASSWORD_PATH, _internal_provision
# #536：sync-control 的兩個逃生門只認模組私有 sentinel（`call_kw` 把呼叫端
# context 整包換進 env，只判 truthy 的 key 租戶自己就打得開）。
from ..models.security_access import _rule_replace, _skip_route_sync
from ..models.workspace import WorkspaceHasServicesError

_logger = logging.getLogger(__name__)


class _SkipTrialInvoiceNotPayable(UserError):
    """`/woow` skip-trial 的穩定 capability 拒絕形狀。"""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(
            _('This invoice cannot be paid in /woow while automatic '
              'e-invoicing is enabled. Please contact support.'))


# Cloud metadata endpoints must never be reachable from a user-provided
# MCP URL. cluster.local resolves into pod-network IPs and must stay
# allowed (sidecar-backed URL form).
_SSRF_BLOCKED_IPV4 = frozenset({
    '169.254.169.254',  # AWS / GCP / Azure IMDSv1
})
_SSRF_BLOCKED_IPV6 = frozenset({
    'fd00:ec2::254',  # AWS IMDSv2 IPv6
})
_SSRF_ALLOWED_SCHEMES = frozenset({'http', 'https'})


# `ir.module.module.state` 值代表「模組不在租戶 DB 裡」，把對應 addon 從選擇裡拿掉是
# 安全的。其餘一律擋——Odoo 還會有 to install / to upgrade / to remove，那些同樣不能
# 把程式碼從底下抽掉。
#
# ⚠ 這份是**後端唯一權威**（#477）。前端 addon-removal-dialog/AddonRemovalDialog.js 的
# SAFE_TO_REMOVE_STATES 是同一判準的 JS 表達（對話框要拿原始 state 決定列哪幾列、
# 怎麼寫），兩邊必須 lock-step：改這裡就要改那裡。見
# docs/decisions/patterns/two-layer-acl-lockstep.md。
_ADDON_REMOVAL_SAFE_STATES = frozenset({
    'uninstalled', 'uninstallable', 'not_found'})


# #535：`_lookup_helm_value` 的「這個鍵不存在」哨兵。不能用 None——helm_values 裡
# 的 null 是合法值，拿 None 當「不存在」會把它與真的 null 混為一談。
_HELM_VALUE_MISSING = object()


def _lookup_helm_value(values: dict, key: str) -> Any:
    """依前端 ``getSpecValue`` 的順序查一個 spec key：先 flat、再 dot-path。

    #535：後端要判斷「使用者送回來的值 == 我們剛才顯示給他看的值」，就必須用
    **與前端相同的查法**取出「顯示值」。前端 ``ConfigurationTab.getSpecValue``
    ／``HelmValueForm.getValue`` 都是先試字面 key 再試 dot-path（舊資料真的可能
    以 ``'auth.anthropicApiKey'`` 當字面鍵落庫），只實作其中一種會對另一種形態
    誤判成「不存在」而放行遮罩值。
    """
    if not isinstance(values, dict):
        return _HELM_VALUE_MISSING
    if key in values:
        return values[key]
    parts = key.split('.')
    if len(parts) == 1:
        return _HELM_VALUE_MISSING
    cursor: Any = values
    for part in parts:
        if not isinstance(cursor, dict) or part not in cursor:
            return _HELM_VALUE_MISSING
        cursor = cursor[part]
    return cursor


PORTAL_SETTINGS_FLAG = 'woow_paas_platform.portal_project_settings_editable'


def _portal_project_settings_editable(env) -> bool:
    """Feature flag（2026-06-03 議題 H）：portal project settings 預設唯讀。
    Param 缺值 = off（fail-closed）。ai_assistant.py import 共用 —
    two-layer-acl-lockstep：三支寫入 endpoint 必須同一開關。"""
    return (env['ir.config_parameter'].sudo().get_param(
        PORTAL_SETTINGS_FLAG) or '').strip().lower() in ('true', '1')


SHOW_PROJECTS_FLAG = 'woow_paas_platform.show_projects'


def _show_projects(env) -> bool:
    """Cosmetic feature gate（#275）：/woow Projects/AI-assistant 可見性。
    缺值 = off（fail-closed）。**非安全邊界** — 只驅動前端 nav + router
    redirect；後端 project/task/chat API 不受此開關影響（對照
    _portal_project_settings_editable 才是 ACL 寫入守衛）。"""
    return (env['ir.config_parameter'].sudo().get_param(
        SHOW_PROJECTS_FLAG) or '').strip().lower() in ('true', '1')


def _account_denial(env) -> dict[str, Any] | None:
    """Product access is independent of Odoo's internal/Portal identity.

    #1261's structured refusal remains for public/inactive accounts only.
    The private capability seam grants no ORM ACL or ERP group membership.
    """
    if _PaaSCapability(env).authenticated:
        return None
    return {'success': False, 'error': _('Authentication required')}


def _validate_mcp_url(url: str, sidecar_backed: bool = False) -> str | None:
    """Minimal SSRF guard: reject non-http(s) schemes + cloud metadata IPs.

    #271: when *sidecar_backed* is True, additionally require an in-cluster
    sidecar DNS URL (http + '.svc.cluster.local'); a public hostname on a
    sidecar-backed row makes the in-cluster agent data-plane hit the CF
    Access login page and makes teardown's URL-prefix filter miss the row.
    """
    if not url:
        return 'URL is required'
    try:
        parsed = urlparse(url)
    except ValueError:
        return f'URL host not allowed: {url}'
    scheme = (parsed.scheme or '').lower()
    if scheme not in _SSRF_ALLOWED_SCHEMES:
        return (
            f'URL scheme not allowed: {parsed.scheme!r} '
            '(only http/https accepted)'
        )
    host = (parsed.hostname or '').strip()
    if not host:
        return f'URL host not allowed: {url}'
    if host in _SSRF_BLOCKED_IPV4:
        return f'URL host not allowed: {host}'
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.is_link_local:
            return f'URL host not allowed: [{host}]'
        if str(ip).lower() in _SSRF_BLOCKED_IPV6:
            return f'URL host not allowed: [{host}]'
    if sidecar_backed and not request.env[
        'woow_paas_platform.mcp_server'
    ]._is_in_cluster_url(url):
        return (
            'Sidecar-backed MCP URL must be in-cluster (http scheme + '
            'hostname ending in .svc.cluster.local, e.g. '
            'http://<release>-mcp.<namespace>.svc.cluster.local:3001/mcp); '
            f'got: {url}'
        )
    return None


class PaasController(HiddenChainMixin, Controller):
    @route("/woow", auth="user", website=False)
    def paas_app(self) -> Any:
        """
        Render the main PaaS application page.

        This endpoint serves the standalone OWL application at /woow.
        Requires authenticated user session.

        Returns:
            Response: Rendered QWeb template 'woow_paas_platform.paas_app'
                      with session_info context for frontend initialization.
        """
        # shallow copy：不 mutate 全域 ir.http.session_info()，只在 /woow 這頁附掛。
        # #275 cosmetic gate — 每次 render 即時讀一次 config_parameter（無快取，
        # 單次頁面載入成本可忽略）；router + nav 同步共用此注入值。
        session_info = dict(request.env['ir.http'].session_info())
        session_info['woow_show_projects'] = _show_projects(request.env)
        return request.render(
            'woow_paas_platform.paas_app',
            {'session_info': session_info}
        )

    # ==================== Config API ====================

    @route("/api/config", auth="user", methods=["POST"], type="json")
    def api_config(self) -> dict[str, Any]:
        """
        Get PaaS platform configuration for frontend.

        Returns:
            dict: Configuration including:
                - success (bool): True
                - data (dict): Configuration values
                    - domain: Base domain for deployed services (e.g., woowtech.io)
        """
        IrConfigParameter = request.env['ir.config_parameter'].with_context({}).sudo()
        domain = IrConfigParameter.get_param('woow_paas_platform.paas_domain', 'woowtech.io')

        return {
            'success': True,
            'data': {
                'domain': domain,
            }
        }

    # ==================== Workspace API ====================

    @route("/api/workspaces", auth="user", methods=["POST"], type="json")
    def api_workspace(self, action: str = 'list', name: str | None = None, description: str | None = None, **kwargs: Any) -> dict[str, Any]:
        """
        Handle workspace collection operations via JSON-RPC.

        Args:
            action (str): Operation type. One of:
                - 'list': Get all accessible workspaces
                - 'create': Create a new workspace
            name (str, optional): Workspace name (required for create)
            description (str, optional): Workspace description
            **kwargs: Additional parameters (ignored)

        Returns:
            dict: JSON response with structure:
                - success (bool): Whether the operation succeeded
                - data (dict|list): Result data (on success)
                - error (str): Error message (on failure)
                - count (int): Item count (for list operations)
        """
        # Portal gets product capabilities, never raw ORM permissions.
        _denial = _account_denial(request.env)
        if _denial:
            return _denial
        if action == 'list':
            return self._list_workspaces()
        elif action == 'create':
            return self._create_workspace(name, description)
        else:
            return {'success': False, 'error': _('Unknown action: %(action)s', action=action)}

    @route("/api/workspaces/<int:workspace_id>", auth="user", methods=["POST"], type="json")
    def api_workspace_detail(self, workspace_id: int, action: str = 'get', name: str | None = None, description: str | None = None, **kwargs: Any) -> dict[str, Any]:
        """
        Handle single workspace operations via JSON-RPC.

        Args:
            workspace_id (int): Target workspace ID (from URL path)
            action (str): Operation type. One of:
                - 'get': Get workspace details
                - 'update': Update workspace name/description
                - 'delete': Archive a workspace (soft delete)
            name (str, optional): Workspace name (optional for update)
            description (str, optional): Workspace description
            **kwargs: Additional parameters (ignored)

        Returns:
            dict: JSON response with structure:
                - success (bool): Whether the operation succeeded
                - data (dict): Result data (on success)
                - error (str): Error message (on failure)
        """
        # Public/inactive accounts are refused; Portal uses scoped capabilities.
        _denial = _account_denial(request.env)
        if _denial:
            return _denial
        if action == 'get':
            return self._get_workspace(workspace_id)
        elif action == 'update':
            return self._update_workspace(workspace_id, name, description)
        elif action == 'delete':
            return self._delete_workspace(workspace_id)
        else:
            return {'success': False, 'error': _('Unknown action: %(action)s', action=action)}

    def _list_workspaces(self) -> dict[str, Any]:
        """
        Get all workspaces accessible by the current user.

        Queries workspace_access records to find all workspaces where
        the current user has any access level and the workspace is active.

        Returns:
            dict: Response containing:
                - success (bool): True
                - data (list): List of workspace objects with:
                    - id, name, description, slug, state
                    - role: User's role in this workspace
                    - member_count: Total number of members
                    - is_owner: Whether current user owns this workspace
                    - created_date: ISO format creation timestamp
                - count (int): Number of workspaces
        """
        user = request.env.user
        access_records = _PaaSCapability(request.env).memberships()

        # Prefetch related workspace data to avoid N+1 queries
        access_records.mapped('workspace_id')

        workspaces = []
        for access in access_records:
            ws = access.workspace_id
            workspaces.append({
                'id': ws.id,
                'name': ws.name,
                'description': ws.description or '',
                'slug': ws.slug,
                'state': ws.state,
                'role': access.role,
                'member_count': ws.member_count,
                'is_owner': ws.owner_id.id == user.id,
                'created_date': ws.create_date.isoformat() if ws.create_date else None,
            })

        return {
            'success': True,
            'data': workspaces,
            'count': len(workspaces),
        }

    def _create_workspace(self, name: str | None, description: str | None) -> dict[str, Any]:
        """
        Create a new workspace with the current user as owner.

        The workspace model automatically creates an owner access record
        for the current user during creation.

        Args:
            name (str): Workspace name (required, will be stripped)
            description (str): Workspace description (optional)

        Returns:
            dict: Response containing:
                - success (bool): True on success, False on error
                - data (dict): Created workspace object (on success)
                - error (str): Error message (on failure)

        Raises:
            Validation error if name is empty after stripping.
        """
        name = (name or '').strip()
        description = (description or '').strip()

        if not name:
            return {'success': False, 'error': _('Workspace name is required')}

        # #525 A2：`Workspace.create()` 在 model 層被拆成**兩段非原子寫入**
        # （`workspace.py` 的 `create`：先 `super().create()` INSERT 主表列，再建
        # owner 的 `workspace_access`）。第二段拋例外時主表列已經進去了，而例外被
        # 這裡的 `except` 吃掉、以 HTTP 200 返回 → 不傳播到框架層 → request 正常
        # 結束 → transaction 照常 commit → 孤兒落地。
        #
        # 孤兒的兩個後果都不會讓使用者察覺，只會累積：
        #   1. `_list_workspaces` 是從 `workspace_access` 反查的，而 ir.rule
        #      `workspace_rule_member_read` 的 domain 同樣錨在 `access_ids` 的
        #      成員關係（`('access_ids', 'any', [('user_id', '=', user.id)])`）
        #      ⇒ 沒有 access 列的 workspace 對每個非 superuser 都不存在：看不到、
        #      進不去、刪不掉。（#658 之前這條規則叫 `workspace_rule_member_access`
        #      且讀寫合一；讀方向的判準沒變，只有寫方向被收窄到 owner/admin。）
        #   2. `unique_slug` 是 DB 層 constraint、不看 ir.rule，但 `_generate_slug`
        #      用的是**使用者身分**的 `search_count`、看不到孤兒 ⇒ 同名重試會產生
        #      同一個 slug → 撞 unique → IntegrityError → 交易毀損 → 框架層 500。
        #      使用者從此再也用不了那個名字，錯誤訊息也指不出原因。
        #
        # savepoint 而非整個 request 的 `cr.rollback()`：形態沿用 #525 A1／PR #527，
        # 把撤回界定在這一段，不依賴「try 之前永遠沒有別的寫入」這個會隨程式碼
        # 演進失效的前提；且 `_FlushingSavepoint` 失敗時會先 `cr.clear()` 再
        # `ROLLBACK TO SAVEPOINT`，ORM cache 一併清乾淨。
        try:
            with request.env.cr.savepoint():
                workspace = _PaaSCapability(request.env).create_workspace(
                    name, description)
        except Exception as e:
            _logger.error("Error creating workspace: %s\n%s", str(e), traceback.format_exc())
            return {'success': False, 'error': _('An error occurred while creating the workspace. Please try again.')}

        # 回應組裝**移出 try**（#525 A2 點名的第二個殘留來源）：這裡全是純讀取，
        # 屬於「已經建立成功之後」的工作。留在 try 內的話，連序列化／讀取錯誤都會
        # 被回報成「建立失敗」，而此時 workspace 其實已完整建成（含 owner access、
        # 使用者看得到）——又一種「畫面說失敗、東西卻在」。移出來之後這類例外會
        # 傳播到框架層，由框架 rollback 整筆並回報錯誤，回應與資料重新一致。
        return {
            'success': True,
            'data': {
                'id': workspace.id,
                'name': workspace.name,
                'description': workspace.description or '',
                'slug': workspace.slug,
                'state': workspace.state,
                'role': ROLE_OWNER,
                'member_count': 1,
                'is_owner': True,
                'created_date': workspace.create_date.isoformat() if workspace.create_date else None,
            }
        }

    def _get_workspace(self, workspace_id: int) -> dict[str, Any]:
        """
        Get detailed information for a specific workspace.

        Verifies that the current user has access to the workspace
        before returning the data.

        Args:
            workspace_id (int): Target workspace ID

        Returns:
            dict: Response containing:
                - success (bool): True on success
                - data (dict): Workspace details including:
                    - id, name, description, slug, state
                    - role: Current user's role
                    - member_count: Total members
                    - is_owner: Ownership flag
                    - owner: Owner details (id, name, email)
                    - created_date: ISO timestamp
                - error (str): Error message (on failure)

        Errors:
            - 'Workspace ID is required' if workspace_id is None
            - 'Workspace not found or access denied' if ID doesn't exist or no access
        """
        if not workspace_id:
            return {'success': False, 'error': _('Workspace ID is required')}

        # Validate workspace_id is an integer
        try:
            workspace_id = int(workspace_id)
        except (TypeError, ValueError):
            return {'success': False, 'error': _('Workspace not found or access denied')}

        user = request.env.user

        workspace = _PaaSCapability(request.env).workspace(workspace_id)

        # Combine existence and access check to prevent information leakage
        access = _PaaSCapability(request.env).access(workspace) if workspace.exists() else False
        if not access:
            return {'success': False, 'error': _('Workspace not found or access denied')}

        return {
            'success': True,
            'data': {
                'id': workspace.id,
                'name': workspace.name,
                'description': workspace.description or '',
                'slug': workspace.slug,
                'state': workspace.state,
                'role': access.role,
                'member_count': workspace.member_count,
                'is_owner': workspace.owner_id.id == user.id,
                'owner': {
                    'id': workspace.owner_id.id,
                    'name': workspace.owner_id.name,
                    'email': workspace.owner_id.email,
                },
                'created_date': workspace.create_date.isoformat() if workspace.create_date else None,
            }
        }

    def _update_workspace(self, workspace_id: int, name: str | None, description: str | None) -> dict[str, Any]:
        """
        Update workspace name and/or description.

        Requires admin or owner role to perform updates.
        Only provided fields (non-None) will be updated.

        Args:
            workspace_id (int): Target workspace ID
            name (str, optional): New workspace name (cannot be empty if provided)
            description (str, optional): New workspace description

        Returns:
            dict: Response containing:
                - success (bool): True on success
                - data (dict): Updated workspace (id, name, description, slug)
                - error (str): Error message (on failure)

        Errors:
            - 'Workspace ID is required' if workspace_id is None
            - 'Workspace not found or access denied' if ID doesn't exist or no access
            - 'Workspace name cannot be empty' if name is empty string
        """
        if not workspace_id:
            return {'success': False, 'error': _('Workspace ID is required')}

        # Validate workspace_id is an integer
        try:
            workspace_id = int(workspace_id)
        except (TypeError, ValueError):
            return {'success': False, 'error': _('Workspace not found or access denied')}

        workspace = _PaaSCapability(request.env).workspace(workspace_id)

        # Combine existence and access check to prevent information leakage
        access = _PaaSCapability(request.env).access(workspace, required_role=ROLE_ADMIN) if workspace.exists() else False
        if not access:
            return {'success': False, 'error': _('Workspace not found or access denied')}

        update_vals = {}
        if name is not None:
            name = name.strip()
            if not name:
                return {'success': False, 'error': _('Workspace name cannot be empty')}
            update_vals['name'] = name
        if description is not None:
            update_vals['description'] = description.strip()

        if update_vals:
            workspace.write(update_vals)

        return {
            'success': True,
            'data': {
                'id': workspace.id,
                'name': workspace.name,
                'description': workspace.description or '',
                'slug': workspace.slug,
            }
        }

    def _delete_workspace(self, workspace_id: int) -> dict[str, Any]:
        """
        Archive a workspace (soft delete).

        Only the workspace owner can delete/archive a workspace.
        This performs a soft delete by setting state to 'archived'
        rather than permanently removing the record.

        Args:
            workspace_id (int): Target workspace ID

        Returns:
            dict: Response containing:
                - success (bool): True on success
                - message (str): 'Workspace archived' (on success)
                - error (str): Error message (on failure)

        Errors:
            - 'Workspace ID is required' if workspace_id is None
            - 'Workspace not found or access denied' if ID doesn't exist or not owner
            - code 'workspace_has_services'（#1295）：工作區底下還有未刪除的服務，
              拒絕封存；`data.services` 為 `[{type, id, name, state}, ...]`，
              `type` ∈ security_access / smart_home / cloud_service。
        """
        if not workspace_id:
            return {'success': False, 'error': _('Workspace ID is required')}

        # Validate workspace_id is an integer
        try:
            workspace_id = int(workspace_id)
        except (TypeError, ValueError):
            return {'success': False, 'error': _('Workspace not found or access denied')}

        user = request.env.user

        workspace = _PaaSCapability(request.env).workspace(workspace_id)

        # Combine existence and ownership check to prevent information leakage
        if not workspace.exists() or workspace.owner_id.id != user.id:
            return {'success': False, 'error': _('Workspace not found or access denied')}

        # #1295：守衛在 model 層（`Workspace.write()` → `_check_archivable`），
        # 這裡只把它轉成結構化回應，前端據 `code` 列出要先刪除的服務。
        try:
            workspace.action_archive()
        except WorkspaceHasServicesError as e:
            return {
                'success': False,
                'code': e.code,
                'error': e.args[0],
                'data': {'services': e.services},
            }
        return {'success': True, 'message': _('Workspace archived')}

    # ==================== Workspace Members API ====================

    @route("/api/workspaces/<int:workspace_id>/members", auth="user", methods=["POST"], type="json")
    def api_workspace_members(self, workspace_id: int, action: str = 'list', email: str | None = None, role: str | None = None, **kwargs: Any) -> dict[str, Any]:
        """
        Handle workspace member collection operations via JSON-RPC.

        Args:
            workspace_id (int): Target workspace ID (from URL path)
            action (str): Operation type - 'list' or 'invite'
            email (str, optional): User email to invite (for invite)
            role (str, optional): Role to assign ('admin', 'user', 'guest')

        Returns:
            dict: JSON response with success/error structure
        """
        # Public/inactive accounts are refused; Portal uses scoped capabilities.
        _denial = _account_denial(request.env)
        if _denial:
            return _denial
        if action == 'list':
            return self._list_members(workspace_id)
        elif action == 'invite':
            return self._invite_member(workspace_id, email, role)
        else:
            return {'success': False, 'error': _('Unknown action: %(action)s', action=action)}

    @route("/api/workspaces/<int:workspace_id>/members/<int:access_id>", auth="user", methods=["POST"], type="json")
    def api_workspace_member(self, workspace_id: int, access_id: int, action: str = 'update_role', role: str | None = None, **kwargs: Any) -> dict[str, Any]:
        """
        Handle individual workspace member operations via JSON-RPC.

        Args:
            workspace_id (int): Target workspace ID (from URL path)
            access_id (int): Target access record ID (from URL path)
            action (str): Operation type - 'update_role' or 'remove'
            role (str, optional): New role to assign ('admin', 'user', 'guest')

        Returns:
            dict: JSON response with success/error structure
        """
        # Public/inactive accounts are refused; Portal uses scoped capabilities.
        _denial = _account_denial(request.env)
        if _denial:
            return _denial
        if action == 'update_role':
            return self._update_member_role(workspace_id, access_id, role)
        elif action == 'remove':
            return self._remove_member(workspace_id, access_id)
        else:
            return {'success': False, 'error': _('Unknown action: %(action)s', action=action)}

    def _list_members(self, workspace_id: int) -> dict[str, Any]:
        """
        Get all members of a workspace.

        Any user with access to the workspace can list its members.

        Args:
            workspace_id (int): Target workspace ID

        Returns:
            dict: Response containing:
                - success (bool): True on success
                - data (list): List of member objects with:
                    - id: Access record ID
                    - user_id: User's ID
                    - name: User's display name
                    - email: User's email
                    - role: Member's role (owner/admin/user/guest)
                    - invited_by: Name of user who invited this member
                    - invited_date: ISO timestamp of invitation
                - count (int): Number of members

        Errors:
            - 'Workspace not found or access denied' if ID doesn't exist or no access
        """
        workspace = _PaaSCapability(request.env).workspace(workspace_id)

        # Combine existence and access check to prevent information leakage
        access = _PaaSCapability(request.env).access(workspace) if workspace.exists() else False
        if not access:
            return {'success': False, 'error': _('Workspace not found or access denied')}

        members = []
        for member_access in workspace.access_ids:
            members.append({
                'id': member_access.id,
                'user_id': member_access.user_id.id,
                'name': member_access.user_name,
                'email': member_access.user_email,
                'role': member_access.role,
                'invited_by': member_access.invited_by_id.name if member_access.invited_by_id else None,
                'invited_date': member_access.invited_date.isoformat() if member_access.invited_date else None,
            })

        return {
            'success': True,
            'data': members,
            'count': len(members),
        }

    def _invite_member(self, workspace_id: int, email: str | None, role: str | None) -> dict[str, Any]:
        """
        Invite a new member to a workspace by email.

        Requires admin or owner role to invite. The target user must
        already exist as an active authenticated account (including Portal).

        Args:
            workspace_id (int): Target workspace ID
            email (str): Email address of user to invite
            role (str): Role to assign ('admin', 'user', 'guest')
                        Defaults to 'user' if not specified

        Returns:
            dict: Response containing:
                - success (bool): True on success
                - data (dict): New access record (id, user_id, name, email, role)
                - error (str): Error message (on failure)

        Errors:
            - 'Workspace not found or access denied' if not admin/owner or workspace doesn't exist
            - 'Email is required' if email is empty
            - 'Invalid role' if role not in allowed values
            - 'No user found with email: {email}' if user doesn't exist
            - 'User is already a member of this workspace' if duplicate
        """
        user = request.env.user

        workspace = _PaaSCapability(request.env).workspace(workspace_id)

        # Combine existence and access check to prevent information leakage
        access = _PaaSCapability(request.env).access(workspace, required_role=ROLE_ADMIN) if workspace.exists() else False
        if not access:
            return {'success': False, 'error': _('Workspace not found or access denied')}

        email = (email or '').strip().lower()
        role = role or ROLE_USER

        if not email:
            return {'success': False, 'error': _('Email is required')}

        if role not in ASSIGNABLE_ROLES:
            return {'success': False, 'error': _('Invalid role')}

        # Find user by email
        ResUsers = request.env['res.users'].with_context({}).sudo()
        # #1261：先取出**所有**同 email 的帳號，再挑第一個 internal（`share=False`）。
        # 舊寫法 `limit=1` 依 res.users 預設排序（name, login）撿一個——同一個 email
        # 同時有 internal 與 portal 帳號時，撿到哪個全看名字排序，可能把 internal
        # 使用者的邀請錯落到他的 portal 分身上。internal 之間的相對順序沿用預設排序，
        # 與舊行為一致。
        # =ilike still interprets SQL %/_ wildcards. Bind the literal email:
        # an underscore must never silently invite a different account.
        candidates = ResUsers.with_context({}).search([
            ('email', '=ilike', escape_psql(email)), ('active', '=', True),
        ]).filtered(lambda u: not u._is_public())

        if not candidates:
            # 完全沒有帳號：維持既有行為與文案（不自動建帳號、不發邀請信）。
            return {'success': False, 'error': _('No user found with email: %(email)s', email=email)}

        # Preserve internal-first selection for duplicate emails. Existing
        # active Portal members are valid invitees; never change their groups.
        target_user = candidates.filtered(lambda u: not u.share)[:1] or candidates[:1]

        WorkspaceAccess = workspace.env['woow_paas_platform.workspace_access']
        # Check if already a member
        existing = WorkspaceAccess.search([
            ('workspace_id', '=', workspace_id),
            ('user_id', '=', target_user.id),
        ], limit=1)

        if existing:
            return {'success': False, 'error': _('User is already a member of this workspace')}

        # #525 A3：唯一的寫入是這行 create，但例外被下面的 `except` 吃掉後不會傳播
        # 到框架層 → request 正常結束 → transaction 照常 commit → 那一列授權留下來：
        # **被邀請者實際已取得成員權限**，API 卻說失敗。加上
        # `unique_user_workspace` 與上面的「已是成員」前置檢查，使用者會被鎖在
        # 「第一次說邀請失敗、再邀說已是成員」的矛盾狀態，兩邊都不是真的。
        #
        # 殘留視窗在 `models.py` 的 `_create` 尾端：INSERT 已下 →
        # `records._validate_fields(...)` → `records.check_access('create')`。
        # #556 之後，這個呼叫者（admin/owner，已通過上面的 required_role 檢查）
        # 一律經 `workspace_access.py` 的 `create()` model guard
        # （`_check_assignable_role`，INSERT **之前**）與
        # `workspace_access_rule_owner_admin_manage`（`perm_create=True`）放行，
        # 不會撞到 `_check_owner_count`（role 不是 owner）或 create 方向的
        # ir.rule 拒絕——注入點仍然只能落在 `_validate_fields` 這個真實視窗，
        # 結構上照 A1／A2 同一形態修掉。
        try:
            with request.env.cr.savepoint():
                new_access = WorkspaceAccess.create({
                    'workspace_id': workspace_id,
                    'user_id': target_user.id,
                    'role': role,
                    'invited_by_id': user.id,
                })
        except Exception as e:
            _logger.error("Error inviting member: %s\n%s", str(e), traceback.format_exc())
            return {'success': False, 'error': _('An error occurred while inviting the member. Please try again.')}

        # 回應組裝移出 try，與 `_create_workspace` 對齊。**這一半今天沒有對應的
        # 測試，因為它造不出可達的失敗**：組裝只讀 `new_access.id`（純記憶體、
        # 不碰 DB）與上面已 sudo 取得的 `target_user`。移出來是結構防護——savepoint
        # 在離開 `with` 時就 release，日後若有人在組裝區加入一次 DB 讀取，留在 try
        # 內就會以「邀請失敗、對方卻已入群」的形式把本次修掉的缺陷原樣帶回來。
        return {
            'success': True,
            'data': {
                'id': new_access.id,
                'user_id': target_user.id,
                'name': target_user.name,
                'email': target_user.email,
                'role': role,
            }
        }

    def _update_member_role(self, workspace_id: int, access_id: int, role: str | None) -> dict[str, Any]:
        """
        Update a workspace member's role.

        Requires admin or owner role. Cannot modify the owner's role
        (use transfer ownership instead).

        Args:
            workspace_id (int): Target workspace ID
            access_id (int): Access record ID to update
            role (str): New role ('admin', 'user', 'guest')

        Returns:
            dict: Response containing:
                - success (bool): True on success
                - data (dict): Updated access record (id, role)
                - error (str): Error message (on failure)

        Errors:
            - 'Workspace not found or access denied' if workspace doesn't exist or lacks permission
            - 'Access ID is required' if access_id is None
            - 'Member not found' if access record doesn't exist
            - 'Invalid role' if role not in allowed values
            - 'Cannot change owner role...' if trying to modify owner
        """
        workspace = _PaaSCapability(request.env).workspace(workspace_id)

        # Combine existence and access check to prevent information leakage
        access = _PaaSCapability(request.env).access(workspace, required_role=ROLE_ADMIN) if workspace.exists() else False
        if not access:
            return {'success': False, 'error': _('Workspace not found or access denied')}

        if not access_id:
            return {'success': False, 'error': _('Access ID is required')}

        target_access = _PaaSCapability(request.env).member(workspace, access_id)
        if not target_access.exists() or target_access.workspace_id.id != workspace_id:
            return {'success': False, 'error': _('Member not found')}

        if role not in ASSIGNABLE_ROLES:
            return {'success': False, 'error': _('Invalid role')}

        # Cannot change owner's role directly
        if target_access.role == ROLE_OWNER:
            return {'success': False, 'error': _('Cannot change owner role. Use transfer ownership instead.')}

        target_access.write({'role': role})
        return {
            'success': True,
            'data': {
                'id': target_access.id,
                'role': role,
            }
        }

    def _remove_member(self, workspace_id: int, access_id: int) -> dict[str, Any]:
        """
        Remove a member from a workspace.

        Requires admin or owner role. Cannot remove the workspace owner.
        This permanently deletes the access record.

        Args:
            workspace_id (int): Target workspace ID
            access_id (int): Access record ID to remove

        Returns:
            dict: Response containing:
                - success (bool): True on success
                - message (str): 'Member removed' (on success)
                - error (str): Error message (on failure)

        Errors:
            - 'Access ID is required' if access_id is None
            - 'Workspace not found or access denied' if workspace doesn't exist or lacks permission
            - 'Member not found' if access record doesn't exist
            - 'Cannot remove the workspace owner' if target is owner
        """
        if not access_id:
            return {'success': False, 'error': _('Access ID is required')}

        workspace = _PaaSCapability(request.env).workspace(workspace_id)

        # Combine existence and access check to prevent information leakage
        access = _PaaSCapability(request.env).access(workspace, required_role=ROLE_ADMIN) if workspace.exists() else False
        if not access:
            return {'success': False, 'error': _('Workspace not found or access denied')}

        target_access = _PaaSCapability(request.env).member(workspace, access_id)
        if not target_access.exists() or target_access.workspace_id.id != workspace_id:
            return {'success': False, 'error': _('Member not found')}

        # Cannot remove owner
        if target_access.role == ROLE_OWNER:
            return {'success': False, 'error': _('Cannot remove the workspace owner')}

        target_access.unlink()
        return {'success': True, 'message': _('Member removed')}

    # ==================== Cloud Templates API ====================

    @route("/api/cloud/templates", auth="user", methods=["POST"], type="json")
    def api_cloud_templates(self, category: str | None = None, search: str | None = None, **kw: Any) -> dict[str, Any]:
        """
        List available cloud application templates.

        Args:
            category (str, optional): Filter by category
            search (str, optional): Search in name/description

        Returns:
            dict: Response containing:
                - success (bool): True on success
                - data (list): List of template objects
                - count (int): Number of templates
        """
        CloudAppTemplate = _PaaSCapability(request.env).cloud_templates()

        # #336：排除 hidden template（HA PaaS）不出現在 marketplace
        # #471 B 組：曝光層判準**留 is_hidden**（不經 HiddenChainMixin）。這裡改成
        # conceal_instances 會讓「定價未拍板／暫時下架」的商品重新出現在 marketplace。
        domain = [('is_active', '=', True), ('is_hidden', '=', False)]

        if category:
            domain.append(('category', '=', category))

        if search:
            search = search.strip()
            domain.append('|')
            domain.append(('name', 'ilike', search))
            domain.append(('description', 'ilike', search))

        templates = CloudAppTemplate.search(domain)

        data = []
        for tmpl in templates:
            data.append(self._format_template(tmpl))

        return {
            'success': True,
            'data': data,
            'count': len(data),
        }

    @route("/api/cloud/templates/<int:template_id>", auth="user", methods=["POST"], type="json")
    def api_cloud_template(self, template_id: int, **kw: Any) -> dict[str, Any]:
        """
        Get a single cloud application template by ID.

        Args:
            template_id (int): Template ID

        Returns:
            dict: Response containing:
                - success (bool): True on success
                - data (dict): Template details
                - error (str): Error message (on failure)
        """
        CloudAppTemplate = _PaaSCapability(request.env).cloud_templates()

        template = CloudAppTemplate.browse(template_id)
        # #471 B 組：曝光層 by-id 判準留 is_hidden（未上架商品的規格頁不可讀）。
        # 注意這擋的是 **template**，不是既有 service——後者判 conceal_instances。
        if not template.exists() or not template.is_active or template.is_hidden:
            return {'success': False, 'error': _('Template not found')}

        return {
            'success': True,
            'data': self._format_template(template, include_values=True),
        }

    def _format_template(self, template: Any, include_values: bool = False) -> dict[str, Any]:
        """Format a template record for API response."""
        data = {
            'id': template.id,
            'name': template.name,
            'slug': template.slug or '',
            'description': template.description or '',
            'category': template.category,
            'tags': self._safe_json_load(template.tags, [], 'tags', template.name),
            'documentation_url': template.documentation_url or '',
            'default_port': template.default_port,
            'ingress_enabled': template.ingress_enabled,
            'min_vcpu': template.min_vcpu,
            'min_ram_gb': template.min_ram_gb,
            'min_storage_gb': template.min_storage_gb,
            'pricing': self._template_pricing(template),
            # #501：可購買性。**頂層欄位，刻意不塞進 pricing**——「能不能買」與
            # 「多少錢」正交，混進同一個欄位正是 #478 的根因形態（一個欄位承載兩個
            # 語意）。前端 fail-closed 讀 `availability.purchasable`，`reason` 只
            # 決定文案。判準由 model 單點裁決，這裡直接搬、不做任何推導。
            'availability': template._availability(),
        }
        if include_values:
            data['helm_chart_name'] = template.helm_chart_name
            data['helm_chart_version'] = template.helm_chart_version
            defaults = self._safe_json_load(
                template.helm_default_values, {}, 'helm_default_values', template.name)
            # Portal sees tenant-configurable settings, not platform Helm defaults.
            if request.env.user.share:
                defaults = self._portal_config_values(defaults, template)
            data['helm_default_values'] = defaults
            data['helm_value_specs'] = template._parse_helm_value_specs()
            data['full_description'] = template.full_description or ''
        return data

    @classmethod
    def _portal_config_values(cls, values, template):
        """DTO of declared scalar settings only, never a raw Helm tree.

        The write filter has a legacy allow-all fallback for missing specs;
        that is not an acceptable disclosure policy for privileged reads.
        """
        specs = template._parse_helm_value_specs()
        if not isinstance(specs, dict):
            return {}
        selected = {}
        for spec in specs.get('required', []) + specs.get('optional', []):
            key = spec.get('key') if isinstance(spec, dict) else spec
            if not isinstance(key, str) or not key:
                continue
            value = _lookup_helm_value(values, key)
            if value is _HELM_VALUE_MISSING or not isinstance(value, (str, int, float, bool, type(None))):
                continue
            selected[key] = value
        return cls._sanitize_helm_values(cls._unflatten_dotpath_keys(selected), template)

    def _template_pricing(self, template: Any) -> dict[str, Any]:
        """marketplace 卡片／configure header 的價格摘要（#478）。

        #478 之前這裡什麼價格都不回，而前端 ``AppCard.formattedPrice`` 直接寫死
        ``"Free"`` ⇒ 每個付費商品都在卡片上謊報免費，點進去才看到 4500。修法是
        「A 為主、B 為底」：能算出價格就回真實起價，**算不出就回 None 讓前端不渲染**
        （而不是編一個 Free 出來）。

        ⚠ 可見性判準必須與 ``api_template_plans``（前台 plan 選卡區）**同源**：
        ``active`` ＋ 非 ``storefront_hidden``。兩邊不同源會讓卡片標一個使用者
        點進去看不到的價格——那只是把 #478 從「謊報免費」換成「謊報數字」。
        - ``active``：**顯式過濾**，不倚賴 x2many 讀取是否套 active_test。stg 的
          0 元測試 plan 268（2026-07-31 archived）就是現成反例：漏掉它會把起價
          拉回 0，付費商品又被標成 Free。
        - ``storefront_hidden``：席數變體（open-design +1access 5100）藏在這個
          旗標後面，計入會讓 tier_count 憑空多一個看不到的價位。

        回傳鍵：
          has_plans   有無「前台看得到的」plan
          min_price   起價（無則 None ⇒ 前端不渲染標籤）
          unit        起價的計價週期（month／year）
          tier_count  同週期的 distinct 價位數（>1 前端才加「起」）
          is_free     起價為 0（**唯一**還會顯示 Free 的情境）
        """
        empty = {'has_plans': False, 'min_price': None, 'unit': None,
                 'tier_count': 0, 'is_free': False}
        visible = template.sudo().subscription_plan_ids.filtered(
            lambda p: p.active and not p.storefront_hidden)
        if not visible:
            return empty

        # 週期優先序：月 → 年 → 其他。月／年並存時起價講的是月價（拿 36000 當
        # 「起價」是另一種誤導）。
        #
        # ⚠ 最後那個「其他」分支是**刻意**的：起初只認 month/year，於是 day/week
        # 計價的 template 會落到「沒有價格」而整個標籤不渲染——但 plan 選卡區
        # （api_template_plans 完全不濾 unit）照樣列得出價目，變成「卡片說不知道、
        # 點進去有價錢」。那是 #478 的鏡像。判準與年繳-only 的 fallback 同源：
        # **有價格就該顯示**，「不渲染」只保留給真的沒有可見 plan。
        # 跨端點一致性由 test_card_price_exists_in_the_plans_endpoint 實打
        # api_template_plans 比對（不是就地重寫過濾條件）。
        for unit in ('month', 'year'):
            scoped = visible.filtered(lambda p: p.sh_unit == unit)
            if scoped:
                break
        else:
            scoped = visible
            unit = min(scoped, key=lambda p: p.sh_plan_price).sh_unit or None

        prices = sorted(set(scoped.mapped('sh_plan_price')))
        return {
            'has_plans': True,
            'min_price': prices[0],
            'unit': unit,
            'tier_count': len(prices),
            'is_free': prices[0] == 0,
        }

    # ==================== Odoo Addons API ====================

    @route("/api/cloud/odoo-addons/sync", auth="user", methods=["POST"], type="json")
    def api_odoo_addons_sync(self, **kw: Any) -> dict[str, Any]:
        """Sync addon records from catalog.json (admin only)."""
        if not request.env.user.has_group('base.group_system'):
            return {'success': False, 'error': _('Admin access required')}
        result = request.env['woow_paas_platform.cloud_odoo_addon'].sync_from_catalog()
        return {'success': True, 'data': result}

    @route("/api/cloud/odoo-addons", auth="user", methods=["POST"], type="json")
    def api_odoo_addons(self, template_id: int | None = None, category: str | None = None, **kw: Any) -> dict[str, Any]:
        """List available Odoo addons, optionally filtered by template or category."""
        CloudOdooAddon = _PaaSCapability(request.env).odoo_addons()

        domain = [('active', '=', True)]
        if template_id:
            # Show addons linked to this template OR addons with no template restriction
            domain = [
                ('active', '=', True),
                '|',
                ('template_ids', 'in', [template_id]),
                ('template_ids', '=', False),
            ]
        if category:
            domain.append(('category', '=', category))

        addons = CloudOdooAddon.search(domain)
        data = []
        for addon in addons:
            data.append({
                'id': addon.id,
                'name': addon.name,
                'technical_name': addon.technical_name,
                'description': addon.description or '',
                'category': addon.category or 'other',
                'icon': addon.icon or '',
                'source_type': addon.source_type,
                'is_default': addon.is_default,
                'depends_on': [
                    {'id': d.id, 'technical_name': d.technical_name}
                    for d in addon.depends_on
                ],
            })

        return {'success': True, 'data': data, 'count': len(data)}

    # ==================== Cloud Services API ====================

    @route("/api/cloud-services/templates/<int:template_id>/plans", auth="user", methods=["POST"], type="json")
    def api_template_plans(self, template_id: int, **kw: Any) -> dict[str, Any]:
        """Get available subscription plans for a template."""
        CloudAppTemplate = _PaaSCapability(request.env).cloud_templates()
        template = CloudAppTemplate.browse(template_id)
        # #471 B 組：曝光層 by-id 判準留 is_hidden（未上架商品的規格頁不可讀）。
        # 注意這擋的是 **template**，不是既有 service——後者判 conceal_instances。
        if not template.exists() or not template.is_active or template.is_hidden:
            return {'success': False, 'error': _('Template not found')}

        plans = []
        # #330 Wave C：base plan 附掛隱藏席數變體（xmlid 反查、無變體＝[]）。
        # 單一 sudo recordset 複用（helper 與 filtered 共用，避免重複 sudo/browse）。
        plan_recs = template.sudo().subscription_plan_ids
        variants_by_base = plan_recs._attach_variants()
        # #368：group_key 配對月/年同 tier、specs 供前台卡片顯示
        group_keys = plan_recs._group_keys()
        # #336：前台 plans 一律排除 storefront_hidden（既存訂閱不受影響）
        for plan in plan_recs.filtered(
            lambda p: not p.storefront_hidden
        ):
            plans.append({
                'id': plan.id,
                'name': plan.name,
                'price': plan.sh_plan_price,
                'duration': plan.sh_duration,
                'unit': plan.sh_unit,
                'has_trial': plan.sh_trial,
                'trial_duration': plan.sh_trial_duration if plan.sh_trial else 0,
                'trial_unit': plan.sh_trial_unit if plan.sh_trial else None,
                'variants': variants_by_base.get(plan.id, []),
                'group_key': group_keys.get(plan.id),
                'specs': plan._get_display_specs(template),
            })

        return {'success': True, 'data': plans}

    @route("/api/cloud-services/services/<int:service_id>/change-plan", auth="user", methods=["POST"], type="json")
    def api_service_change_plan(self, service_id: int, plan_id: int | None = None, **kw: Any) -> dict[str, Any]:
        """Change subscription plan for a cloud service."""
        if not plan_id:
            return {'success': False, 'error': _('plan_id is required')}

        service = _PaaSCapability(request.env).cloud_service(service_id)
        if not service.exists() or service.template_id.conceal_instances:
            return {'success': False, 'error': _('Service not found')}

        workspace = service.workspace_id
        access = _PaaSCapability(request.env).access(workspace)
        if not access or access.role not in [ROLE_OWNER, ROLE_ADMIN]:
            return {'success': False, 'error': _('Admin access required')}

        Plan = request.env['sh.subscription.plan'].with_context({}).sudo()
        plan = Plan.browse(plan_id)
        if not plan.exists():
            return {'success': False, 'error': _('Plan not found')}
        # Plan must belong to THIS service's template — symmetric with the
        # create path's gate (`_create_service`, same file). The role check
        # above only gates who may change the plan, not whether the plan
        # belongs here; without this an owner could switch to another
        # template's (or an orphan) plan, producing a subscription/invoice
        # line inconsistent with the service's template.
        #
        # #949：⛔ 這裡**不**能寫成 `if svc_template.subscription_plan_ids and
        # plan not in …`。那個 truthiness 前綴讓「template 掛零個 plan」時整條
        # 判斷被跳過 ⇒ 任何 plan 都換得成（fail-open）。空集合時
        # `plan not in <空集合>` 本來就是 True ⇒ 擋，這才是要的語意。
        #
        # 與 SA 端（security_access.py::api_security_access_change_plan）的關係：
        # 對齊的是它「**template 存在**」那一格。SA 另有 template-less 的 else
        # 分支（改判 plan 家族），CS **沒有**那一格——`cloud_service.template_id`
        # 是 `required=True`（DB 側 NOT NULL），服務不可能沒有 template。
        #
        # ⇒ 剛建好、還沒掛方案的 template，其上的服務換不了方案。這是刻意選的
        #   方向（擋 > 放行）：建 template 的人要先把方案掛上去，服務才換得動。
        svc_template = service.template_id.sudo()
        if plan not in svc_template.subscription_plan_ids:
            return {'success': False,
                    'error': _('Plan not available for this template')}

        old_sub = service.sudo().subscription_id
        _logger.info(
            "[CLOUD_SVC] change_plan request service=%s new_plan=%s old_sub=%s",
            service_id, plan_id, old_sub.id if old_sub else None,
        )
        if not old_sub or old_sub.state != 'in_progress':
            return {'success': False, 'error': _('No active subscription')}

        # During trial, sh_end_date is legitimately NULL (billing period
        # hasn't started yet). Strict date completeness is only meaningful
        # for active subs — see the SH-side counterpart in smart_home.py
        # for the full rationale. `_compute_unused_refund` is already
        # trial-aware (returns 0), so the rest of the flow is safe.
        if not old_sub.sh_trial:
            if not old_sub.sh_end_date or not old_sub.sh_start_date:
                return {'success': False, 'error': _('Subscription dates are incomplete')}

        # Refund: defer to the model method instead of recomputing the
        # proration inline. The previous inline form
        #   refund = price * remaining_days / total_days
        # would have crashed on trial subs (sh_end_date is None) once the
        # date guard above was relaxed; the model method is the SH side's
        # canonical calculator and is already covered by
        # test_change_plan_refund.py for the trial / partial / end-of-cycle
        # cases. SH at smart_home.py:167 already routes through it; this
        # change brings CS to parity.
        # #773 降級前置合規 gate：目標 plan 的席數放不下現有用量就**擋住換方案**，
        # 並告訴租戶要刪到幾個。⛔ 系統不自動刪任何存取權（Eugene 裁決，#773 留言 10716）。
        #
        # 位置：所有既有閘之後、**第一個寫入之前**。這一格是硬需求不是風格——
        # 本端點的第一個 DB 副作用是退點（`refund_wallet_points`），接著才關舊訂閱、
        # 建新訂閱；gate 放到那之後只能「跑一半再整包回捲」，而 Eugene 裁的是「擋住」。
        #
        # ⚠ 刻意排在「無有效訂閱」與日期完整性兩閘**之後**（而不是白名單閘後面就插）：
        # 那兩閘的訊息更貼近租戶當下的問題，先跑它們可維持既有錯誤優先序；
        # 三腿的 gate 都落在同一個相對位置，同構好覆核。
        #
        # 尺與寫入守衛共用（`_seat_quota_violation` → `_seat_usage_for_anchor`）：
        # ⛔ 不得在這裡自己數席位，兩把尺漂開會產生「gate 放行但寫入被守衛擋」的死結。
        #
        # 升級天然通過（席數變大 ⇒ 用量 ≤ 席數），所以**不需要**先判斷這次是不是降級。
        #
        # #1261：同一支 gate 也擋「目標 0 席 ＋ 服務仍受 Access 保護」——CS 的 Access
        # 頁籤同樣只給有席位的方案（#746 拍板 6），換過去之後保護關不掉。
        # 判準收在 `_protection_gate_denial`，這裡不另寫。
        _denial = request.env['woow_paas_platform.security_access_access_policy']\
            ._seat_gate_denial(service, plan)
        if _denial:
            # #1261：`data` 原樣轉發 error 以外的欄位（`code`／current_seats／
            # target_seats，保護那種另帶 `protected_hostnames`）。不逐 key 挑：
            # gate 多一種拒絕時三個端點各挑一次就是漂移的起點。
            return {'success': False,
                    'error': _denial['error'],
                    'data': {k: v for k, v in _denial.items() if k != 'error'}}

        refund = old_sub._compute_unused_refund()

        partner = old_sub.sh_partner_id
        _logger.info(
            "[CLOUD_SVC] change_plan refund calc service=%s start=%s end=%s price=%s refund=%s partner=%s",
            service_id, old_sub.sh_start_date, old_sub.sh_end_date,
            old_sub.sh_plan_price, refund, partner.id,
        )

        # Refund unused points (#318: refund-typed so reconciliation can tell
        # it from a top-up; carries subscription_id for audit trail)
        if refund > 0:
            partner.sudo().refund_wallet_points(
                amount=refund,
                description="Plan change refund: %s" % old_sub.sh_subscription_plan_id.name,
                subscription=old_sub,
            )
            partner.invalidate_recordset(['wallet_balance'])
            _logger.info(
                "[CLOUD_SVC] change_plan refund credited service=%s partner=%s amount=%s",
                service_id, partner.id, refund,
            )

        # Close old subscription
        old_sub.sudo().write({
            'state': 'close',
            'sh_reason': 'Plan changed to: %s' % plan.name,
        })

        # Create new subscription
        service._create_and_confirm_subscription(partner, plan)
        _logger.info(
            "[CLOUD_SVC] change_plan done service=%s old_sub=%s new_sub=%s",
            service_id, old_sub.id,
            service.subscription_id.id if service.subscription_id else None,
        )

        return {
            'success': True,
            'data': {
                'refund_amount': refund,
                'new_plan': plan.name,
                'subscription': service._get_subscription_info(),
            },
        }

    def _skip_trial_invoice_payability(self, service, sub=None):
        """預演 skip-trial 將建立的 invoice，套用 billing 的 replay-safe SSOT。

        `_generate_invoice_vals()` 是實際 `_skip_trial()` 建票的共同 choke point；用
        `account.move.new()` 只解析 move_type／company／skip flag，不落 DB。若未來
        choke point 開始 stamp skip flag，estimate 與 mutation 會自動一起放行，
        不會留下第二套「skip-trial 永遠 flagless」的硬編判準。
        """
        sub = sub or service._sub()
        invoice_vals = sub._generate_invoice_vals()
        # 上游 vals 帶 journal_id 卻不帶 company_id；先釘回 subscription company，
        # 再用 sudo 建 transient move，避免租戶因 account.journal read ACL 在
        # `_compute_company_id` 爆 AccessError，也避免誤吃 request env company。
        invoice_vals['company_id'] = sub.sh_company_id.id
        invoice = request.env['account.move'].with_context({}).sudo().new(invoice_vals)
        return _invoice_payability_fields(invoice)

    def _ensure_skip_trial_invoice_payable(self, service, sub):
        """在 lifecycle business gates 之後、第一個 mutation 之前 fail-loud。"""
        payability = self._skip_trial_invoice_payability(service, sub=sub)
        if payability['payable_via_woow']:
            return
        _logger.info(
            "[CLOUD_SVC] skip_trial flagless_automatic_reject "
            "service=%s company=%s",
            service.id, sub.sh_company_id.id,
        )
        raise _SkipTrialInvoiceNotPayable(
            payability['pay_blocked_reason'])

    @route("/api/cloud-services/services/<int:service_id>/skip-trial-estimate", auth="user", methods=["POST"], type="json")
    def api_service_skip_trial_estimate(self, service_id: int, **kw: Any) -> dict[str, Any]:
        """Return the upcoming-invoice breakdown for skip-trial confirmation.

        Computes untaxed / tax / total + wallet sufficiency without
        persisting business mutations, then materializes an unsaved
        ``account.move`` preview to derive the payability fields. Used by the
        confirm modal so the displayed total and guard match what
        `api_service_skip_trial` will charge.
        """
        service = _PaaSCapability(request.env).cloud_service(service_id)
        if not service.exists() or service.template_id.conceal_instances:
            return {'success': False, 'error': _('Service not found')}

        workspace = service.workspace_id
        access = _PaaSCapability(request.env).access(workspace)
        if not access or access.role not in [ROLE_OWNER, ROLE_ADMIN]:
            return {'success': False, 'error': _('Admin access required')}

        # #553：原本是「一律 `str(e)`」——連 ORM 例外都照吐給租戶。分類比照
        # `_create_service`（#520／PR #523）與上方 `api_service_skip_trial`
        # （#525 A1／PR #527）。本端點只讀資料並以 `account.move.new()` 建立
        # 未落庫的 preview，沒有 DB mutation，所以**不需要** rollback／savepoint，
        # 只需要分類訊息。
        try:
            estimate = service._skip_trial_estimate()
            estimate.update(self._skip_trial_invoice_payability(service))

        except OperationalError:
            # #533：本站也是**枚舉補到的**（issue 未點名）。這支沒有 DB mutation
            # （只有讀取與未落庫的 `account.move.new()` preview，故刻意不設
            # savepoint），**沒有殘留面**——但「並發類例外被裸 `except Exception` 吞掉 ⇒
            # `retrying()` 看不到」這個結構缺口與另外兩站完全相同。
            #
            # 只讀不寫一樣撞得到 40001：Odoo 的 cursor 是 REPEATABLE READ，讀取在
            # 同一交易裡與別人的寫入衝突時照樣會拿到序列化失敗，而那正是重試一次
            # 就會成功的情況。吞掉它 ⇒ 租戶在下單前看到「估算失敗」，然後手動重按。
            raise

        except (AccessDenied, AccessError, MissingError) as e:
            # `UserError` 在 Odoo 18 有**四個**子類（`odoo/exceptions.py`）：
            # `AccessDenied`(:45)、`AccessError`(:64)、`MissingError`(:85)、
            # `ValidationError`(:94)。這裡攔前三個，只有 `ValidationError` 走原文。
            #
            # 🔴 `AccessDenied` 是 `UserError` 的**平行**子類，**不是**
            # `AccessError` 的子類 —— 只寫 `except AccessError` 攔不到它，它會落進
            # 下面的 `except (ValidationError, UserError)` 走原文。
            #
            # 三者都不是「講給租戶聽」的業務規則：`AccessError` 的訊息帶內部
            # model／欄位技術名；`MissingError` 的原文是 `Record does not exist or
            # has been deleted.\n(Record: woow_paas_platform.cloud_service(42,),
            # User: 2)`——model 技術名、記錄 id、內部 uid 全在裡面；`AccessDenied`
            # 是認證失敗（預設訊息 "Access Denied" 雖無害，但它可帶自訂訊息，且
            # 「登入憑證錯誤」本來就不該被包裝成本端點的業務回應）。
            #
            # #567 已將兩處參照實作（`_create_service` /
            # `api_service_skip_trial`）收斂為完整 technical triplet：
            # `AccessDenied` / `AccessError` / `MissingError` 統一轉泛用訊息；
            # `ValidationError` 與 business `UserError` 仍保留原文。
            _logger.error(
                "ORM error estimating skip-trial for service %s: %s\n%s",
                service_id, e, traceback.format_exc(),
            )
            return {'success': False,
                    'error': _('An error occurred while estimating the skip-trial charge.')}

        except (ValidationError, UserError) as e:
            # 業務規則例外（沒有訂閱、訂閱非 in_progress、不在試用期）：訊息本來
            # 就是寫給使用者看的，原文回前端。統一成泛用字串的話，真正的原因只留
            # 在 server log，使用者與第一線支援都無從排錯。
            message = str(e)
            _logger.warning(
                "Skip trial estimate rejected for service %s: %s",
                service_id, message,
            )
            return {'success': False, 'error': message}

        except Exception as e:
            _logger.error(
                "Error estimating skip-trial for service %s: %s\n%s",
                service_id, e, traceback.format_exc(),
            )
            return {'success': False,
                    'error': _('An error occurred while estimating the skip-trial charge.')}

        return {'success': True, 'data': estimate}

    @route("/api/cloud-services/services/<int:service_id>/skip-trial", auth="user", methods=["POST"], type="json")
    def api_service_skip_trial(self, service_id: int, **kw: Any) -> dict[str, Any]:
        """Skip trial period and start paid billing immediately."""
        service = _PaaSCapability(request.env).cloud_service(service_id)
        if not service.exists() or service.template_id.conceal_instances:
            return {'success': False, 'error': _('Service not found')}

        workspace = service.workspace_id
        access = _PaaSCapability(request.env).access(workspace)
        if not access or access.role not in [ROLE_OWNER, ROLE_ADMIN]:
            return {'success': False, 'error': _('Admin access required')}

        # #525 A1：`_skip_trial()` 的寫入是**分段**的，例外噴在哪一步，殘留就
        # 累積到哪——訂閱先脫離試用（`subscription_lifecycle_mixin._skip_trial`）
        # → 開出 out_invoice → `account_move._pay_from_wallet` post 發票、扣錢包
        # 點數 → 才建 `account.payment` 並 reconcile。例外被 except 吃掉後不會
        # 傳播到框架層，request 正常結束 → transaction 照常 commit → 上述殘留
        # 全部落地：**畫面說失敗，錢卻已經從錢包扣走**，發票停在 posted／未付款、
        # 沒有 payment、沒有 reconcile。
        #
        # 更糟的是殘留會**把重試路徑鎖死**：脫離試用的 write 排在 invoice create
        # 之前，而 `_skip_trial` 的前置檢查是 `if not sub.sh_trial: raise
        # UserError('Subscription is not in trial period.')`。使用者看到失敗再按
        # 一次會被自己剛剛的殘留擋掉——試用期燒掉、計費沒開始，且無法自我修復。
        #
        # 用 savepoint 而不是整個 request 的 `cr.rollback()`（#520／PR #523 在
        # `_create_service` 用的是後者）：那裡的殘留是單一 INSERT；這裡橫跨訂閱、
        # account.move（含明細、已 posted）、loyalty.card、wallet.transaction 四
        # 個 model，把撤回範圍界定在「這一段」比界定在「整個 request」穩健——不必
        # 依賴「這個 endpoint 在 try 之前永遠不會有別的寫入」這個會隨程式碼演進
        # 失效的前提。附帶好處：`cr.savepoint()` 失敗時會先 `cr.clear()` 再
        # `ROLLBACK TO SAVEPOINT`（`sql_db._FlushingSavepoint.rollback`），ORM
        # cache 與待計算欄位一併清乾淨；只補 `cr.rollback()` 得自己處理髒 cache。
        try:
            # #980：由 lifecycle seam 在原 `_skip_trial()` business precondition
            # 之後呼叫 guard；guard 通過後，model 才開 mutation savepoint。如此既保留
            # #567 的 gate／taxonomy contract，又仍早於 trial write、建票與付款鏈。
            invoice_info = service._skip_trial(
                before_mutation=lambda sub: (
                    self._ensure_skip_trial_invoice_payable(service, sub)))

        except _SkipTrialInvoiceNotPayable as e:
            return {
                'success': False,
                'error': str(e),
                'code': e.reason,
            }

        except OperationalError:
            # #533：並發類 DB 例外（`SerializationFailure` 40001 等）**必須**傳出去。
            # 它是 `Exception` 子類，被下面任何一個 except 收掉的話 Odoo 的
            # `retrying()`（`odoo/service/model.py`）永遠看不到——那一層在本 except
            # 鏈的**外層**，收到 `OperationalError` 才會做 `env.cr.rollback()`
            # （connection-level 的整條 ROLLBACK，不是 ROLLBACK TO SAVEPOINT）
            # → `env.reset()` → 重播整個 handler，下一輪才依 REPEATABLE READ 拿到
            # **新 snapshot**。吞掉它 ⇒ 一個本來重試就會成功的操作變成使用者看到
            # 的失敗，而 skip-trial 路徑上的衝突點（`loyalty.card` 點數扣除的並發
            # UPDATE、`account.move` 取號）正是最典型的寫寫衝突形態。
            #
            # 必須排在 #520／#525 那三段之前：`SerializationFailure` 不是
            # `UserError` 子類，但會被最後的 `except Exception` 吃掉。
            #
            # model 內 guard 之後的 mutation `cr.savepoint()`（#525 A1）**刻意保留、
            # 也不衝突**：它管的是「失敗時不留金流殘留」，本分支管的是「例外要讓
            # 框架看見」。新 snapshot
            # 由 `retrying()` 的整交易 rollback 提供，不是由 savepoint 提供
            # ——`ROLLBACK TO SAVEPOINT` 不換 transaction、不換 snapshot。
            #
            # 取捨（沿用 `_create_service` 同款分支的第 1 點，刻意接受）：攔截面比
            # 「會被重試的那三種」寬。`retrying()` 只重試
            # `PG_CONCURRENCY_EXCEPTIONS_TO_RETRY`（LockNotAvailable /
            # SerializationFailure / DeadlockDetected），其餘 `OperationalError`
            # 原樣拋出 ⇒ 前端拿到框架的錯誤而不是 `{'success': False}`。這是刻意
            # 的：連線斷、磁碟滿、statement timeout 本來就不該被包裝成「業務失敗」
            # 回 HTTP 200——那正是 #516／#520／#525 整個家族的根因形態。收窄成只認
            # 那三個 pgcode 等於把框架的白名單複製一份到這裡，框架改了我們不會知道。
            #
            # 重播安全不是 `_skip_trial` 的固有性質：#980 前置守衛以實際建票 vals
            # 套用 billing replay-safe predicate，已排除會從上游 `action_post()`
            # inline 呼叫 ECPay 的 flagless＋automatic 組合，才可安全交給框架重試。
            raise

        except (AccessDenied, AccessError, MissingError) as e:
            # 權限／ORM 技術例外不是講給租戶看的業務規則，細節只留 server log。
            _logger.error(
                "Access error skipping trial for service %s: %s\n%s",
                service_id, e, traceback.format_exc(),
            )
            return {'success': False,
                    'error': _('An error occurred while skipping the trial period.')}

        except (ValidationError, UserError) as e:
            # 業務規則例外（不在試用期、訂閱非 in_progress、錢包餘額不足、收款
            # journal 未設…）：訊息本來就是寫給使用者看的，原文回前端。統一成泛用
            # 字串的話，真正的原因只留在 server log，使用者與第一線支援都無從排錯。
            message = str(e)
            _logger.warning(
                "Skip trial rejected for service %s: %s", service_id, message,
            )
            return {'success': False, 'error': message}

        except Exception as e:
            _logger.error(
                "Error skipping trial for service %s: %s\n%s",
                service_id, e, traceback.format_exc(),
            )
            return {'success': False,
                    'error': _('An error occurred while skipping the trial period.')}

        return {
            'success': True,
            'data': {
                'invoice': invoice_info,
                'subscription': service._get_subscription_info(),
            },
        }

    @route("/api/workspaces/<int:workspace_id>/services", auth="user", methods=["POST"], type="json")
    def api_workspace_services(self, workspace_id: int, action: str = 'list', template_id: int | None = None, name: str | None = None, values: dict[str, Any] | None = None, odoo_addon_ids: list[int] | None = None, **kw: Any) -> dict[str, Any]:
        """
        Handle cloud service operations for a workspace.

        Args:
            workspace_id (int): Workspace ID
            action (str): 'list' or 'create'
            template_id (int, optional): Template ID (for create)
            name (str, optional): Service name (for create)
            values (dict, optional): Helm values override (for create)

        Returns:
            dict: Response containing:
                - success (bool): True on success
                - data (dict|list): Service(s) data
                - error (str): Error message (on failure)
        """
        # Validate workspace access
        workspace = _PaaSCapability(request.env).workspace(workspace_id)
        if not workspace.exists():
            return {'success': False, 'error': _('Workspace not found or access denied')}

        access = _PaaSCapability(request.env).access(workspace)
        if not access:
            return {'success': False, 'error': _('Workspace not found or access denied')}

        if action == 'list':
            return self._list_services(workspace)
        elif action == 'create':
            # Only admin/owner can create services
            if access.role not in [ROLE_OWNER, ROLE_ADMIN]:
                return {'success': False, 'error': _('Permission denied')}
            plan_id = kw.get('plan_id')
            return self._create_service(workspace, template_id, name, values, odoo_addon_ids, plan_id=plan_id)
        else:
            return {'success': False, 'error': _('Unknown action: %(action)s', action=action)}

    @route("/api/workspaces/<int:workspace_id>/services/<int:service_id>", auth="user", methods=["POST"], type="json")
    def api_workspace_service(self, workspace_id: int, service_id: int, action: str = 'get', values: dict[str, Any] | None = None, version: str | None = None, odoo_addon_ids: list[int] | None = None, **kw: Any) -> dict[str, Any]:
        """
        Handle operations on a specific cloud service.

        Args:
            workspace_id (int): Workspace ID
            service_id (int): Service ID
            action (str): 'get', 'update', or 'delete'
            values (dict, optional): Helm values (for update)
            version (str, optional): Chart version (for update)

        Returns:
            dict: Response containing:
                - success (bool): True on success
                - data (dict): Service data
                - error (str): Error message (on failure)
        """
        # Validate workspace and service access
        workspace = _PaaSCapability(request.env).workspace(workspace_id)
        if not workspace.exists():
            return {'success': False, 'error': _('Workspace not found or access denied')}

        access = _PaaSCapability(request.env).access(workspace)
        if not access:
            return {'success': False, 'error': _('Workspace not found or access denied')}

        service = _PaaSCapability(request.env).cloud_service(service_id, workspace_id)
        if (not service.exists() or service.workspace_id.id != workspace_id
                or service.template_id.conceal_instances):
            return {'success': False, 'error': _('Service not found')}

        if action == 'get':
            return self._get_service(service)
        elif action == 'update':
            if access.role not in [ROLE_OWNER, ROLE_ADMIN]:
                return {'success': False, 'error': _('Permission denied')}
            try:
                service._check_operation_allowed(operation='write')
            except OperationalError:
                raise
            except (AccessDenied, AccessError, MissingError) as e:
                _logger.exception("Access error checking service update: %s", e)
                return {'success': False,
                        'error': _('An error occurred while updating the service.')}
            except (ValidationError, UserError) as e:
                return {'success': False, 'error': str(e)}
            except Exception as e:
                _logger.exception("Error checking service update: %s", e)
                return {'success': False,
                        'error': _('An error occurred while updating the service.')}
            return self._update_service(service, values, version, odoo_addon_ids)
        elif action == 'delete':
            if access.role not in [ROLE_OWNER, ROLE_ADMIN]:
                return {'success': False, 'error': _('Permission denied')}
            try:
                service._check_operation_allowed(operation='write')
            except OperationalError:
                raise
            except (AccessDenied, AccessError, MissingError) as e:
                _logger.exception("Access error checking service deletion: %s", e)
                return {'success': False,
                        'error': _('An error occurred while deleting the service.')}
            except (ValidationError, UserError) as e:
                return {'success': False, 'error': str(e)}
            except Exception as e:
                _logger.exception("Error checking service deletion: %s", e)
                return {'success': False,
                        'error': _('An error occurred while deleting the service.')}
            return self._delete_service(service)
        else:
            return {'success': False, 'error': _('Unknown action: %(action)s', action=action)}

    @route("/api/workspaces/<int:workspace_id>/services/<int:service_id>/rollback", auth="user", methods=["POST"], type="json")
    def api_service_rollback(self, workspace_id: int, service_id: int, revision: int | None = None, **kw: Any) -> dict[str, Any]:
        """
        Rollback a service to a previous revision.

        Args:
            workspace_id (int): Workspace ID
            service_id (int): Service ID
            revision (int, optional): Target revision number. If not provided or 0,
                rolls back to the previous revision.

        Returns:
            dict: Response containing:
                - success (bool): True on success
                - message (str): Rollback confirmation
                - error (str): Error message (on failure)
        """
        # Validate revision parameter
        if revision is not None:
            try:
                revision = int(revision)
                if revision < 0:
                    return {'success': False, 'error': _('Revision must be a non-negative integer')}
            except (TypeError, ValueError):
                return {'success': False, 'error': _('Invalid revision number')}

        # Validate workspace and service access
        workspace = _PaaSCapability(request.env).workspace(workspace_id)
        if not workspace.exists():
            return {'success': False, 'error': _('Workspace not found or access denied')}

        access = _PaaSCapability(request.env).access(workspace, required_role=ROLE_ADMIN)
        if not access:
            return {'success': False, 'error': _('Workspace not found or access denied')}

        service = _PaaSCapability(request.env).cloud_service(service_id, workspace_id)
        if (not service.exists() or service.workspace_id.id != workspace_id
                or service.template_id.conceal_instances):
            return {'success': False, 'error': _('Service not found')}

        # #423 W5a MAJOR-1：補上訂閱閘。
        #
        # 同檔 api_workspace_service 的 update / delete 兩個分支都有這道檢查，
        # 只有 rollback 漏了（既有缺陷）。而 `suspended` 的成因正是訂閱
        # expire / cancel——沒有這道閘，欠費租戶自己就打得到這支 API。
        # 與上方 _rollback_service 的狀態閘是兩層獨立防護：一層擋權限，
        # 一層擋狀態。
        try:
            service._check_operation_allowed(operation='write')
        except OperationalError:
            raise
        except (AccessDenied, AccessError, MissingError) as e:
            _logger.exception("Access error checking service rollback: %s", e)
            return {'success': False,
                    'error': _('An error occurred while rolling back the service.')}
        except (ValidationError, UserError) as e:
            return {'success': False, 'error': str(e)}
        except Exception as e:
            _logger.exception("Error checking service rollback: %s", e)
            return {'success': False,
                    'error': _('An error occurred while rolling back the service.')}

        return self._rollback_service(service, revision)

    @route("/api/workspaces/<int:workspace_id>/services/<int:service_id>/reset-admin-password", auth="user", methods=["POST"], type="json")
    def api_service_reset_admin_password(self, workspace_id: int, service_id: int, **kw: Any) -> dict[str, Any]:
        """#357 重置 mcp admin password（一次性顯示新密碼）。

        限 ``template.has_admin_gui``（image 內建 admin GUI、由平台供給 admin_password）
        的 template；其餘回 400。流程：model._reset_admin_password 重生成強隨機密碼 → operator
        POST /api/releases/{ns}/{name}/reset-config → operator 內部 read-modify-write
        config.json + helm upgrade + rollout_restart。

        Returns:
            success=true 帶 data.admin_password 明文（一次性）
            success=false 帶 error
        """
        return self._service_admin_credentials(workspace_id, service_id)

    @route("/api/workspaces/<int:workspace_id>/services/<int:service_id>/admin-credentials", auth="user", methods=["POST"], type="json")
    def api_service_set_admin_credentials(
        self, workspace_id: int, service_id: int,
        username: str | None = None, password: str | None = None, **kw: Any,
    ) -> dict[str, Any]:
        """租戶自設 admin 帳號／密碼（open-design 的 OpenDesign 與 ttyd 共用這組）。

        只開放給 ``template.admin_credentials_settable`` 的模板；其餘回錯誤（它們
        只能 Reset）。只有密碼的模板（code-server）不收 ``username``。``password`` 空值＝平台鑄造新密碼並一次性回傳（同 Reset）；
        給了就用租戶的值、**不回傳明文**（租戶自己知道）。``username`` 空值＝不改。
        生效方式與 Reset 相同：helm upgrade ＋ rollout restart（服務重啟約一分鐘）。

        Returns:
            success=true 帶 data.admin_username（與鑄造時的 data.admin_password）
            success=false 帶 error
        """
        return self._service_admin_credentials(
            workspace_id, service_id,
            new_password=password if password not in (None, '') else None,
            new_username=username if username not in (None, '') else None,
            tenant_set=True,
        )

    def _service_admin_credentials(
        self, workspace_id: int, service_id: int,
        new_password: str | None = None, new_username: str | None = None,
        tenant_set: bool = False,
    ) -> dict[str, Any]:
        """Reset 與租戶自設憑證共用的授權、執行與回應組裝（兩個 route 的唯一實作）。

        授權與 gate 順序完全沿用 #357／#471／#533／#553 的判準（見下方各註解）；
        租戶自設只多一道能力 gate：``template.admin_username_helm_path``。
        """

        workspace = _PaaSCapability(request.env).workspace(workspace_id)
        if not workspace.exists():
            return {'success': False, 'error': _('Workspace not found or access denied')}

        access = _PaaSCapability(request.env).access(workspace, required_role=ROLE_ADMIN)
        if not access:
            return {'success': False, 'error': _('Workspace not found or access denied')}

        service = _PaaSCapability(request.env).cloud_service(service_id, workspace_id)
        # #471 C 組：補上 conceal_instances gate，與其他既有服務操作端點同判準。
        # 修前這裡**完全沒有**隱藏 gate，於是同一個服務「API 重設得了密碼，但唯一的
        # UI 入口（詳情頁）打不開」——正是 #460／#464 反覆撞到的場景。gate 刻意放在
        # exists() 之後、has_admin_gui 之前：能力判準（400）不該遮蔽存在性語意
        # （not found），否則 conceal 服務會回「不支援 admin GUI」洩漏它存在。
        if (not service.exists() or service.workspace_id.id != workspace_id
                or service.template_id.conceal_instances):
            return {'success': False, 'error': _('Service not found')}

        # #1370／#1186：app 自管密碼、由 operator exec 改 app DB 的模板（Odoo／n8n）。
        # 判別器讀 template.app_admin_reset_kind（與 admin_reset_supported 的 compute、
        # model 端 _reset_app_admin_password 同一欄位）。只做 Reset，不開放自設。
        app_reset_kind = service.template_id.app_admin_reset_kind or 'none'
        if app_reset_kind != 'none':
            if tenant_set:
                return {
                    'success': False,
                    'error': _("This service's admin credentials can only be reset, not set."),
                }
        # #357 C01：判別器讀 template.has_admin_gui，與 model 端（cloud_service
        # create/_build_mcp_sensitive_values）收斂到同一判準。原本這裡是 slug
        # allowlist、model 端卻判 mcp_enabled → 兩端不一致（controller 放行並生密碼、
        # model 過濾成空集合不建 secret）＝密碼永不生效的根因。
        elif not service.template_id.has_admin_gui:
            return {
                'success': False,
                'error': _('Reset admin password is only supported on templates '
                           'with a built-in admin GUI'),
            }
        # app_managed（omnigent）：平台只給初始密碼、之後由 app 自己保管，平台無從
        # 重設。讀 template.admin_reset_supported（與前端按鈕、model 防線同一欄位）。
        elif not service.template_id.admin_reset_supported:
            return {
                'success': False,
                'error': _("This service manages its own admin password. "
                           "Change it inside the app."),
            }
        # 租戶自設憑證的能力判準：與 has_admin_gui 同一紀律，讀 template 欄位、不自建
        # slug 名單；payload 的 template.admin_credentials_settable 鏡射同一個欄位。
        # 只有密碼的模板（admin_password_settable，code-server）不收帳號。
        elif tenant_set and not service.template_id.admin_credentials_settable:
            return {
                'success': False,
                'error': _("This service's admin credentials can only be reset, not set."),
            }
        elif (tenant_set and new_username is not None
                and not service.template_id.admin_username_helm_path):
            return {
                'success': False,
                'error': _("This service has no username; only its password can be set."),
            }

        try:
            # #357 C05：model 端刻意是 private（_reset_admin_password）——它回傳明文
            # 密碼，而授權只有上面那道 ROLE_ADMIN gate。public 命名會讓任何 workspace
            # 成員經 /web/dataset/call_kw 繞過本 gate 直接拿密碼，見 model 端 docstring。
            # 本 endpoint 是該 method 的唯一 entry（grep action_reset/_reset_admin_password
            # 全 repo 確認：無其他 controller、無 XML view button）。
            # Internal callers retain their ORM rules. Portal uses only the
            # resolved service capability, after membership + admin checks;
            # credential/lifecycle gates in the private model method remain.
            app_login = None
            if app_reset_kind != 'none':
                # #1370：同樣 private、同樣只有本 endpoint 一個入口（#357 C05）。
                applied_password, app_login = service._reset_app_admin_password()
            elif tenant_set:
                applied_password = service._reset_admin_password(
                    new_password=new_password, new_username=new_username,
                )
            else:
                applied_password = service._reset_admin_password()

        except OperationalError:
            # #533：本站是**枚舉補到的**（issue 未點名），而且它比 issue 本體更
            # 值得記——**model 層已經做對了，被這裡抵消。**
            #
            # `CloudService._reset_admin_password` 內有**兩處**
            # `except OperationalError: raise`（憑證落庫那段、記帳那段），註解逐字
            # 寫著「#518：並發／連線類 DB 例外（SerializationFailure 40001 等）必須
            # 傳出去讓 Odoo 的 `retrying()` 看見；此刻交易已 aborted，吞掉只會讓
            # 後續 flush 再炸」。那兩處刻意的 raise 一路傳到這裡，卻被下面的裸
            # `except Exception` 收掉 ⇒ **它們今天等於沒有作用**，傳播鏈斷在
            # controller 這一層。補上本分支，那兩處才真的通到框架。
            #
            # 重播的語意：`retrying()` 會重跑整個 handler ⇒ 再 reset 一次（另鑄一把
            # 新密碼重新下發）。model 端 :1222-1223 的註解已經替這個行為背書過
            # （「重播整個 handler 會重跑一次 reset……結果仍然是一致的」）——叢集與
            # DB 最後都是最後那一輪的密碼，租戶拿到的也是那一把。
            raise

        except (AccessDenied, AccessError, MissingError) as e:
            # #553 的同一個外洩面：`UserError` 的四個子類裡，只有 `ValidationError`
            # 是「講給租戶聽」的業務規則，其餘三個帶的是 ORM 內部識別字（model
            # 技術名／記錄 id／內部 uid）或認證失敗。原本只寫 `except UserError`，
            # 於是它們全被原文吐到租戶畫面。
            #
            # 🔴 `AccessDenied` 是 `UserError` 的**平行**子類、**不是**
            # `AccessError` 的子類，必須各自列名——漏掉它就會落進下面的原文分支
            # （分類清單見 `api_service_skip_trial_estimate` 該處註解）。
            _logger.error(
                "[#357] ORM error resetting admin password for service %s: %s\n%s",
                service.id, str(e), traceback.format_exc(),
            )
            return {
                'success': False,
                'error': _('Reset failed. Check operator logs for details.'),
            }

        except (ValidationError, UserError) as e:
            # model 端刻意寫給租戶看的業務訊息：服務未 running、正在升級請稍候
            # （#357 C11）、逾時後的「狀態未知，請稍後重按 Reset」（#461 C11′）。
            # 租戶要靠它決定下一步，統一成泛用字串會把「等一分鐘再試」變成「壞了」。
            return {'success': False, 'error': str(e)}

        except Exception as e:
            _logger.error(
                "[#357] reset-admin-password failed for service %s: %s\n%s",
                service.id, str(e), traceback.format_exc(),
            )
            return {
                'success': False,
                'error': _('Reset failed. Check operator logs for details.'),
            }

        # #530：Reset 同時換發 gateway API key，一併一次性回傳。訊息用複數形是
        # 因為租戶要回 MCP 那端重填 key——只說「密碼」會讓人以為 gateway 沒動。
        #
        # #525 B3：走到這一行代表密碼**已經生效**（model 端的提交點已經跨過）。
        # 以下純屬回應組裝，任何失敗都不得把它翻轉成失敗回應或 500——後者更糟，
        # 框架會回捲整筆交易，變成「叢集是新密碼、DB 是舊密碼、租戶什麼都沒拿到」。
        # gateway 憑證是附加品，取不到就少交付一把（租戶可再 Reset），但那把
        # **已生效的 admin 密碼**一定要送出去。
        try:
            gateway = self._gateway_credentials(service)
        except Exception:
            # 🔴 #533 **刻意不在這一段補 `except OperationalError: raise`**（上面那個
            # try 補了，這裡沒有——這個不一致是有意的，不是漏的）。
            #
            # 判準是「走到這裡時，密碼已經生效」這個前提，它比「讓 `retrying()`
            # 看到並發例外」優先：本段任何例外若逸出，框架會**回捲整筆交易** ⇒ DB
            # 退回舊密碼、叢集卻已是新的 ⇒ 憑證權威來源分岔，管理員登不進 admin
            # GUI，之後任何一次 redeploy 還會拿 DB 舊值去覆寫叢集。`retrying()` 會
            # 先重播 5 次，但第 5 次仍失敗時它照樣把原例外拋出去，那個結局就成立。
            # 拿「少交付一把 gateway key（可再 Reset 取得）」去換「不可回復的憑證
            # 分岔」，方向是反的。
            #
            # 而且本段是純讀取的附加品（`_gateway_credentials` 只讀 secret 與
            # template），它撞上 40001 的機率與代價都遠低於上面的主鏈。
            _logger.exception(
                "[#530] gateway credential lookup failed for service %s after a "
                "successful password reset — delivering the admin password only",
                service.id,
            )
            gateway = {}
        message = (
            'Admin password reset. Save the new password now — '
            'it will not be shown again.'
        )
        if tenant_set:
            message = (
                'Admin credentials updated. Save the new password now — '
                'it will not be shown again. The service restarts to apply them.'
                if new_password is None else
                'Admin credentials updated. The service restarts to apply them '
                '(about a minute).'
            )
        # #672：判準是 payload 裡**真的有 gateway key**，不是 payload 非空——
        # 同一個 dict 現在也可能只帶 dashboard_url（兩個閘門各判各的）。用非空當
        # 判準的話，那種服務會收到一句「Gateway API Key 已重設」的假話，而租戶會
        # 回 MCP 那端去換一把根本沒換過的 key。
        if gateway.get('gateway_api_key'):
            message = (
                'Admin password and Gateway API Key reset. Save both now — '
                'they will not be shown again. Update the Gateway API Key '
                'wherever you configured it (e.g. your MCP server).'
            )
        if app_reset_kind != 'none':
            # #1370：不重啟、立刻生效；舊 session 失效、兩步驟驗證已關。
            message = (
                'Admin password reset. Save the new password now — it will not be '
                'shown again. It works right away; devices signed in with the old '
                'password are signed out, and two-factor sign-in has been turned off.'
            )
        # 帳號不是敏感值；讀取失敗只是少一個顯示欄位，不得翻轉「已生效」的回應。
        try:
            # #1370：app 自管的帳號（Odoo login／n8n owner email）以 operator 實際
            # 重設的那一個為準；模板沒有 admin_username_helm_path。
            admin_username = app_login or service._admin_username()
        except Exception:
            _logger.exception(
                "admin username lookup failed for service %s after credentials "
                "took effect", service.id,
            )
            admin_username = None
        # 一次性憑證視窗（buildOneTimeCredentials）挑文案用：只有密碼的服務
        # （admin_credentials_settable 且無帳號）不能落到 hermes 的 MCP 說明。
        # 與 _format_service 的 template 子物件同名同值。同上：只是顯示用，讀取失敗
        # 不得翻轉「已生效」的回應（前端缺欄位時退回通用文案）。
        try:
            template_flags = {
                'admin_reset_supported': bool(service.template_id.admin_reset_supported),
                'admin_credentials_settable': bool(
                    service.template_id.admin_credentials_settable),
                'admin_username_editable': bool(
                    service.template_id.admin_username_helm_path),
                'app_admin_reset_kind': service.template_id.app_admin_reset_kind or 'none',
            }
        except Exception:
            _logger.exception(
                "template capability lookup failed for service %s after credentials "
                "took effect", service.id,
            )
            template_flags = {}
        data = dict(
            gateway,
            service_id=service.id,
            admin_username=admin_username,
            message=message,
            template=template_flags,
        )
        # 租戶自己填的密碼不回傳明文（他本來就知道，回傳只是多一處外洩面）；
        # 平台鑄造的（Reset、或自設流程選「隨機產生」）照舊一次性交付。
        if not (tenant_set and new_password is not None):
            data['admin_password'] = applied_password
        return {'success': True, 'data': data}

    @route("/api/workspaces/<int:workspace_id>/services/<int:service_id>/revisions", auth="user", methods=["POST"], type="json")
    def api_service_revisions(self, workspace_id: int, service_id: int, **kw: Any) -> dict[str, Any]:
        """
        Get revision history for a service.

        Args:
            workspace_id (int): Workspace ID
            service_id (int): Service ID

        Returns:
            dict: Response containing:
                - success (bool): True on success
                - data (list): List of revisions
                - error (str): Error message (on failure)
        """
        # Validate workspace and service access
        workspace = _PaaSCapability(request.env).workspace(workspace_id)
        if not workspace.exists():
            return {'success': False, 'error': _('Workspace not found or access denied')}

        access = _PaaSCapability(request.env).access(workspace)
        if not access:
            return {'success': False, 'error': _('Workspace not found or access denied')}

        service = _PaaSCapability(request.env).cloud_service(service_id, workspace_id)
        if (not service.exists() or service.workspace_id.id != workspace_id
                or service.template_id.conceal_instances):
            return {'success': False, 'error': _('Service not found')}

        return self._get_service_revisions(service)

    # ==================== Access Control (#745, per-service) ============
    #
    # 形狀比照 SA 的 access-config／access-policies 組
    # （`controllers/security_access.py`），差別只在**錨點**：SA 那邊 Access app
    # 是 per-route（一個 SA 底下 N 條 route，各自一個 app），所以有 SA-level
    # bridge ＋ per-route 兩支 config；CS 自己就是錨點（一台服務一個 subdomain
    # 一個 app，拍板 5 的整站粒度），兩者收斂成一支 `access-config`。
    #
    # 四步骨架與 SA 逐字相同，順序也刻意一致：workspace 可見性 → service 釘住
    # → role gate（owner/admin 才寫）→ 訂閱閘。`list` 是唯一跳過後兩步的分支
    # （成員讀得到自己服務的 policy），與 SA 的 access-policies 同款。

    def _get_workspace_service_or_error(self, workspace_id: int,
                                        service_id: int,
                                        require_manage: bool = True):
        """解析 (workspace, service) 並跑完授權四步；失敗回 (None, error_dict)。

        單一裁決處：兩支端點共用，免得其中一支日後少掉一步而另一支還在
        （SA 那邊 `refresh_status` 曾因為被分在 read 那側而漏掉 role gate）。

        `service` 釘在 `workspace_id` 上（`service.workspace_id.id !=
        workspace_id` ⇒ 找不到），所以 URL 上換一個別人的 service id 得到的是
        「Service not found」而不是別人的資料；`conceal_instances` 的模板一併
        當作不存在，與同檔其他 service 端點同款。
        """
        workspace = _PaaSCapability(request.env).workspace(workspace_id)
        if not workspace.exists():
            return None, {'success': False,
                          'error': _('Workspace not found or access denied')}
        access = _PaaSCapability(request.env).access(workspace)
        if not access:
            return None, {'success': False,
                          'error': _('Workspace not found or access denied')}
        service = _PaaSCapability(request.env).cloud_service(service_id, workspace_id)
        if (not service.exists() or service.workspace_id.id != workspace_id
                or service.template_id.conceal_instances):
            return None, {'success': False, 'error': _('Service not found')}
        if not require_manage:
            return service, None
        if access.role not in [ROLE_OWNER, ROLE_ADMIN]:
            return None, {'success': False,
                          'error': _('Insufficient permissions')}
        try:
            service._check_operation_allowed(operation='write')
        except OperationalError:
            raise
        except (AccessDenied, AccessError, MissingError) as e:
            _logger.exception("Access error checking service management: %s", e)
            return None, {
                'success': False,
                'error': _('An error occurred while accessing the service.'),
            }
        except (ValidationError, UserError) as e:
            return None, {'success': False, 'error': str(e)}
        except Exception as e:
            _logger.exception("Error checking service management: %s", e)
            return None, {
                'success': False,
                'error': _('An error occurred while accessing the service.'),
            }
        return service, None

    @route("/api/workspaces/<int:workspace_id>/services/<int:service_id>/access-config",
           auth="user", methods=["POST"], type="json")
    def api_service_access_config(self, workspace_id: int, service_id: int,
                                  **kwargs: Any) -> dict[str, Any]:
        """本服務的 Cloudflare Access 開關與登入方式（拍板 5：整站粒度）。"""
        service, error = self._get_workspace_service_or_error(
            workspace_id, service_id)
        if error:
            return error
        return self._update_service_access_config(service, kwargs)

    @route("/api/workspaces/<int:workspace_id>/services/<int:service_id>/access-policies",
           auth="user", methods=["POST"], type="json")
    def api_service_access_policies(self, workspace_id: int, service_id: int,
                                    action: str = 'list',
                                    **kwargs: Any) -> dict[str, Any]:
        """本服務的 Access policy CRUD ＋ reorder。"""
        if action == 'list':
            # Internal ORM rules or Portal capability, pinned to this service.
            # Non-members are denied before reaching the policy query.
            service, error = self._get_workspace_service_or_error(
                workspace_id, service_id, require_manage=False)
            if error:
                return error
            policies = service.env[
                'woow_paas_platform.security_access_access_policy'].search(
                    [('cloud_service_id', '=', service.id)])
            return {'success': True, 'data': [p.to_dict() for p in policies]}

        service, error = self._get_workspace_service_or_error(
            workspace_id, service_id)
        if error:
            return error
        if action == 'create':
            return self._create_service_access_policy(service, kwargs)
        elif action == 'update':
            return self._update_service_access_policy(
                service, kwargs.get('policy_id'), kwargs)
        elif action == 'delete':
            return self._delete_service_access_policy(
                service, kwargs.get('policy_id'))
        elif action == 'reorder':
            return self._reorder_service_access_policies(service, kwargs)
        return {'success': False, 'error': _('Unknown action: %(action)s', action=action)}

    # ---- Access Control helpers ----

    def _update_service_access_config(self, service, kwargs) -> dict[str, Any]:
        """白名單寫入。

        🔴 只有這四個欄位是 client 設定得了的。`access_app_id` /
        `access_unresolved_idp_types` 是伺服器在 sync 之後回存的值——前者另有
        model 層的身分錨點守衛（對所有人生效），但**白名單仍然是必要的第一層**：
        守衛丟的是 `UserError`，那會讓一個順手多帶欄位的合法請求整個失敗，而不
        是靜靜忽略它；後者則完全沒有 model 層守衛，白名單是它唯一的防線。

        `is_protected` 用 `in kwargs` 判而不是 `kwargs.get()`：關閉保護送的是
        `False`，truthy 判斷會讓「關閉」這個動作整個消失（而回應仍然是 success）。
        """
        vals = {}
        if 'is_protected' in kwargs:
            vals['is_protected'] = bool(kwargs['is_protected'])
            # 沒有對外 hostname ⇒ 沒有可以掛 Access app 的錨點。不擋的話這個
            # 寫入會**成功**、UI 顯示「已保護」，而 `_sync_access_to_operator`
            # 的第一道 gate 就 return，Cloudflare 上一個物件都不會有——一個對
            # 租戶而言完全無聲的謊。關閉方向（False）不擋：任何情況下都必須
            # 關得掉。
            if vals['is_protected'] and not service._access_hostname():
                return {'success': False,
                        'error': _('This service has no public address to '
                                   'protect yet.')}
        for f in ('login_google', 'login_email_otp'):
            if f in kwargs:
                vals[f] = bool(kwargs[f])
        if kwargs.get('session_duration'):
            vals['session_duration'] = kwargs['session_duration']
        try:
            if vals:
                service.write(vals)
            return {'success': True, 'data': service._access_to_dict()}
        except Exception as e:
            _logger.exception(
                "[CLOUD_SVC] Failed to update access config service=%s: %s",
                service.id, e)
            return {'success': False, 'error': str(e)}

    def _service_policy(self, service, policy_id):
        """釘在這台服務上的 policy——跨服務／未知 id 一律回空集。

        用 `search` 帶 `cloud_service_id` 條件而不是 filter
        `service.access_policy_ids`：兩者等價，但 search 走的是 ir.rule 過濾後
        的可見集合，語意上「找不到」與「看不到」收斂成同一個回應。
        """
        return _PaaSCapability(request.env).cloud_policy(service, policy_id)

    def _create_service_access_policy(self, service, kwargs) -> dict[str, Any]:
        if not kwargs.get('name'):
            return {'success': False, 'error': _('name is required')}
        # 兩層 quota：model 的 `_check_policy_quota` 是權威，這裡是為了回一句
        # 人看得懂的話。**0 = unlimited**（#743）——沒有這個豁免，model 那邊的
        # 豁免從 UI 不可達（`existing >= quota` 在 0 >= 0 時第一條就擋掉），而
        # CS access 變體的規格正是 `sa_max_access_policies = 0`（拍板 4）。
        # `sudo()` 只用在讀訂閱：租戶對 `sh.subscription.subscription` 沒有讀權。
        #
        # #1053：`existing` 的計數也走 `sudo()`，與 model 端 `_check_policy_quota`
        # 的池計數對齊（#896 已對 SA 錨點做同一件事，三個孿生體同進退）。額度是
        # **平台成本**，不是「這位呼叫者看得到多少」——`ir.rule` 只讓呼叫者看到
        # 池中一部分 policy 時，裸 `search_count` 會少算，而少算的症狀是「配額安
        # 靜地變寬」，與「方案的政策數上限很大」在輸出上完全不可區分。
        # ⛔ sudo 只包**這一次計數**：`service` 仍由 `_get_workspace_service_or_error`
        # 以呼叫者的 env 把關（非成員在到達這裡前就被擋掉），`Policy.create()`
        # 以下也一律留在呼叫者 env——本前置只是快速失敗，真正的守線是 model 那
        # 條 constraint。
        # （domain 以 `cloud_service_id` 收斂 ⇒ sudo 只放寬可見度，不會把別的
        # service／租戶的 policy 算進來。）
        Policy = service.env[
            'woow_paas_platform.security_access_access_policy']
        sub = service.sudo().subscription_id
        if sub and sub.sh_subscription_plan_id:
            quota = sub.sh_subscription_plan_id.sa_max_access_policies
            existing = Policy.sudo().search_count(
                [('cloud_service_id', '=', service.id)])
            if quota and existing >= quota:
                return {'success': False,
                        'error': _('Access policy quota exceeded: plan allows '
                                   '%d polic(ies)') % quota}
        # 白名單：`cloud_service_id` 由**已 gate 過的 service** 決定，不讀
        # kwargs——client 送什麼錨點都無效。`cf_policy_id` / `route_id` 同理。
        create_vals = {
            'cloud_service_id': service.id,
            'name': kwargs['name'],
            'decision': kwargs.get('decision', 'allow'),
        }
        # sequence 稀疏配號交給 model（max+10）；client 明給才照它的。
        if 'sequence' in kwargs:
            create_vals['sequence'] = kwargs['sequence']
        rule_commands = self._build_access_rule_commands(kwargs)
        if rule_commands is not None:
            create_vals['rule_ids'] = rule_commands
        try:
            pol = Policy.create(create_vals)
            return {'success': True, 'data': pol.to_dict()}
        except Exception as e:
            _logger.exception(
                "[CLOUD_SVC] Failed to create access policy service=%s: %s",
                service.id, e)
            return {'success': False, 'error': str(e)}

    _ACCESS_RULE_GROUPS = ('include', 'require', 'exclude')

    def _build_access_rule_commands(self, kwargs):
        """include/require/exclude → One2many create commands。

        逐條白名單到 group/rule_type/rule_value：client 不得在子規則上夾帶
        `cf_policy_id` / `policy_id` / 任何其他欄位（mass-assignment 守衛，與
        policy 本身同一套紀律）。三個 group key 一個都沒有 ⇒ 回 None，讓呼叫端
        自己決定（CS 這邊是「不帶 rule_ids」，交給 model 的
        `_check_at_least_one_include` fail-loud；刻意**不**接 SA 那條 legacy
        flat `rule_type`/`rule_value` 相容層——那是 P-rule 過渡期的遺物，新路徑
        沒有 in-flight client 需要它）。
        """
        if not any(g in kwargs for g in self._ACCESS_RULE_GROUPS):
            return None
        commands = []
        for grp in self._ACCESS_RULE_GROUPS:
            for r in kwargs.get(grp) or []:
                if not isinstance(r, dict):
                    continue
                commands.append((0, 0, {
                    'group': grp,
                    'rule_type': r.get('rule_type'),
                    'rule_value': r.get('rule_value'),
                }))
        return commands

    def _update_service_access_policy(self, service, policy_id,
                                      kwargs) -> dict[str, Any]:
        pol = self._service_policy(service, policy_id)
        if not pol:
            return {'success': False, 'error': _('Policy not found')}
        vals = {}
        for f in ('name', 'decision', 'sequence'):
            if f in kwargs:
                vals[f] = kwargs[f]
        write_pol = pol
        rule_commands = self._build_access_rule_commands(kwargs)
        if rule_commands is not None:
            # 整組取代。delete-before-create（`(5,0,0)` + creates）讓 email 上限
            # 只看新規則；中間那個空集會踩到「≥1 include」檢查，所以用
            # `sa_rule_replace` 抑制逐條檢查——policy 層的 @api.constrains 驗最終
            # 狀態。
            vals['rule_ids'] = [(5, 0, 0)] + rule_commands
            write_pol = _rule_replace(pol)
        try:
            if vals:
                write_pol.write(vals)
            return {'success': True, 'data': pol.to_dict()}
        except Exception as e:
            _logger.exception(
                "[CLOUD_SVC] Failed to update access policy %s: %s",
                policy_id, e)
            return {'success': False, 'error': str(e)}

    def _delete_service_access_policy(self, service,
                                      policy_id) -> dict[str, Any]:
        pol = self._service_policy(service, policy_id)
        if not pol:
            return {'success': False, 'error': _('Policy not found')}
        try:
            pol.unlink()
            return {'success': True, 'data': {'message': _('Policy deleted')}}
        except Exception as e:
            _logger.exception(
                "[CLOUD_SVC] Failed to delete access policy %s: %s",
                policy_id, e)
            return {'success': False, 'error': str(e)}

    def _reorder_service_access_policies(self, service,
                                         kwargs) -> dict[str, Any]:
        """重寫 precedence。逐筆寫入抑制 sync，結束後只推一次完整狀態。

        不抑制的話 N 條 policy 會打 N 次 operator，而且中間每一次送出去的都是
        **半排好**的順序——Cloudflare 會依序看到幾組不同的 precedence，最後才
        收斂。跨服務／未知的 policy_id 靜默略過（絕不寫到別人的列），而計數器
        走「被釘住的那些」而非原索引，略過一筆不會在號碼上留洞。
        """
        policy_ids = kwargs.get('policy_ids') or []
        if not isinstance(policy_ids, list):
            return {'success': False, 'error': _('policy_ids must be a list')}
        reordered = []
        try:
            seq = 0
            for pid in policy_ids:
                pol = self._service_policy(service, pid)
                if not pol:
                    continue
                seq += 10
                _skip_route_sync(pol).write({'sequence': seq})
                reordered.append(pol.id)
            if reordered:
                service._sync_access_to_operator()
            return {'success': True, 'data': {'reordered': reordered}}
        except Exception as e:
            _logger.exception(
                "[CLOUD_SVC] Failed to reorder access policies service=%s: %s",
                service.id, e)
            return {'success': False, 'error': str(e)}

    # ==================== Addon Module Operations ====================

    def _assess_addon_removal(
        self, service: Any, removed_addon_ids: list[int],
    ) -> dict[str, Any]:
        """「把這些 addon 從選擇裡拿掉安不安全」的**單一裁決點**。

        advisory 的預檢端點（`api_check_addon_removal`）與 authoritative 的寫入路徑
        （`_update_service`）共用這一份。回傳兩個**正交**維度，呼叫端各取所需：

        - ``can_assess`` / ``error`` — **能不能做出裁決**。服務非 running、operator 沒
          設定、operator 回 ``success: false``、查詢丟例外，全部落在這裡。兩條路徑都
          必須把它當「不安全」處理（fail-closed）：查不到模組狀態的時刻，正是租戶
          Odoo 不健康的時刻（rolling restart / OOMKilled / node drain 會讓 operator 的
          ``find_pod`` step 失敗），也正是這道保護最該生效的時刻。
        - ``modules`` / ``blocking`` — **裁決結果**。``modules`` 是逐模組的原始狀態
          （預檢端點交給前端對話框列「哪個模組、什麼狀態、被誰依賴」），``blocking``
          是其中不在 `_ADDON_REMOVAL_SAFE_STATES` 裡的子集。

        兩個維度**必須分開回**：預檢端點的 ``success`` 只反映第一個維度——它是
        advisory，查得到就回成功連同模組狀態，擋不擋由前端 ``blocksRemoval`` 依
        ``modules`` 判。把 ``blocking`` 併進 ``success`` 會讓 #476 鎖住的行為矩陣反轉
        （installed 模組本來就該回 ``success=True`` 讓對話框列出來）。

        #477 的根因是**同一個裁決有兩份實作、且失敗語意相反**：同一份 operator 回應，
        預檢端點 fail-closed，`_update_service` 卻讓它掉出 ``if`` 繼續跑 helm upgrade
        （operator 對 ``OdooModuleError`` 回的是 HTTP 200 ＋ ``success: false``，而平台
        client 只在 HTTP >= 400 才 raise，所以那條路無例外、也進不了註明
        ``# Fail-closed`` 的 ``except``）。safe-states 集合當時也散在兩處。抽成單一裁決
        點是為了讓「兩邊會漂開」在結構上不可能發生，不只是把當下的 diff 補齊——見
        docs/decisions/patterns/two-layer-acl-lockstep.md。
        """
        nothing_to_remove = {
            'can_assess': True, 'error': None, 'modules': {}, 'blocking': {}}
        if not removed_addon_ids:
            return nothing_to_remove

        def cannot(error: str) -> dict[str, Any]:
            return {
                'can_assess': False, 'error': error, 'modules': {}, 'blocking': {}}

        # 非 running 就讀不到租戶 Odoo 的模組狀態——那是**拒絕**的理由，不是「跳過檢查」
        # 的理由（#477 縫 (b)）。上游沒有別的閘擋得住：`_update_service` 的前置閘只擋
        # pending / deleting，以及非 init 額度出口的 error（#1159 Q30 放行額度出口的 error
        # 做 reconfigure，所以 error 也可能走到這裡），`_check_operation_allowed` 是訂閱閘（判 active /
        # grace / past-grace，且 base.group_system 直接 bypass），與 service state 正交。
        if service.state != 'running':
            return cannot(_(
                'Service must be running to check module states '
                '(current: %(state)s)', state=service.state))

        client = get_paas_operator_client(request.env)
        if not client:
            return cannot(_('PaaS Operator not configured'))

        CloudOdooAddon = _PaaSCapability(request.env).odoo_addons()
        addons = CloudOdooAddon.browse(removed_addon_ids).exists()
        if not addons:
            return nothing_to_remove

        name_to_id = {a.technical_name: a.id for a in addons}

        try:
            result = client.get_odoo_module_states(
                namespace=service.helm_namespace,
                release_name=service.helm_release_name,
                module_names=list(name_to_id),
            )
        except Exception as e:
            _logger.warning(
                "Cannot verify module states for addon removal on %s: %s",
                service.helm_release_name, e)
            return cannot(_('Cannot verify module states: %(reason)s', reason=e))

        if not result.get('success'):
            # operator 對 OdooModuleError 回 HTTP 200 ＋ success:false（step 為
            # find_pod / query_modules / validate 之一），client 不 raise。**必須與上面
            # 那個 except 同處置** —— #477 縫 (a) 就是這裡當初只有 if 沒有 else。
            error = result.get('error') or _('Unknown error')
            _logger.warning(
                "Operator could not report module states for %s: %s",
                service.helm_release_name, error)
            return cannot(_('Cannot verify module states: %(reason)s', reason=error))

        modules: dict[str, Any] = {}
        blocking: dict[str, Any] = {}
        for name, info in (result.get('modules') or {}).items():
            entry = {
                'addon_id': name_to_id.get(name),
                'state': info.get('state', 'unknown'),
                'dependents': info.get('dependents', []),
            }
            modules[name] = entry
            if entry['state'] not in _ADDON_REMOVAL_SAFE_STATES:
                blocking[name] = entry

        return {
            'can_assess': True, 'error': None,
            'modules': modules, 'blocking': blocking,
        }

    @staticmethod
    def _addon_removal_blocking_error(blocking: dict[str, Any]) -> str:
        """把 `_assess_addon_removal` 的 ``blocking`` 子集轉成給租戶看的拒絕理由。

        只有 authoritative 路徑會用到（advisory 預檢把 ``modules`` 原封不動交給前端
        對話框，由它渲染更完整的「被誰依賴 / 去哪裡停用」）。措辭放這裡是為了讓未來
        若有第三個呼叫端出現時拿到同一句話。
        """
        parts = ', '.join(
            f"'{name}' ({info.get('state')})"
            for name, info in sorted(blocking.items()))
        return _(
            'Cannot remove %(modules)s: still present in Odoo. '
            'Please deactivate the module in Odoo first.',
            modules=parts,
        )

    @route(
        '/api/cloud-services/services/<int:service_id>/check-addon-removal',
        type='json', auth='user', methods=['POST'],
    )
    def api_check_addon_removal(
        self,
        service_id: int,
        removed_addon_ids: list[int] | None = None,
        **kw: Any,
    ) -> dict[str, Any]:
        """Check if removed addons can be safely deselected.

        Odoo 對 ``type='json'`` route 會把 JSON-RPC 的 ``params`` 展開成 kwargs，
        所以參數收進簽名即可（同檔 `api_service_change_plan` /
        `api_workspace_services` 的形狀）。**不要**改回 `request.get_json_data()`
        ——那支回的是整個 request body，讀不到住在 ``params`` 裡的鍵，會讓下面每
        一道檢查都被 early return 靜默跳過（#474）。``**kw`` 必留，用來吸收前端
        多送的鍵，否則 dispatch 會 TypeError。
        """
        if removed_addon_ids is None:
            removed_addon_ids = []
        if not isinstance(removed_addon_ids, (list, tuple)):
            return {'success': False, 'error': _('Invalid removed_addon_ids: expected a list of addon IDs')}
        try:
            removed_addon_ids = [int(addon_id) for addon_id in removed_addon_ids]
        except (TypeError, ValueError):
            return {'success': False, 'error': _('Invalid removed_addon_ids: expected a list of addon IDs')}

        if not removed_addon_ids:
            return {'success': True, 'modules': {}}

        service = _PaaSCapability(request.env).cloud_service(service_id)
        if not service.exists() or service.template_id.conceal_instances:
            return {'success': False, 'error': _('Service not found')}

        workspace = service.workspace_id
        access = _PaaSCapability(request.env).access(workspace)
        if not access:
            return {'success': False, 'error': _('Access denied')}

        # 狀態閘 / operator 設定 / 查詢失敗 / safe-states 全部住在
        # `_assess_addon_removal`——與 `_update_service` 的 authoritative 拒絕同源
        # （#477）。這支是 **advisory**：``success`` 只反映「查得到模組狀態」，
        # 「安不安全」交給前端 ``blocksRemoval`` 依 ``modules`` 判，所以這裡刻意
        # **不**把 ``verdict['blocking']`` 併進 ``success``（#476 的行為矩陣鎖住這件事：
        # installed 模組要回 success=True 才有東西給對話框列）。
        verdict = self._assess_addon_removal(service, removed_addon_ids)
        if not verdict['can_assess']:
            return {'success': False, 'error': verdict['error']}

        return {'success': True, 'modules': verdict['modules']}

    # ==================== MCP Server API (per-service) ====================

    @route(
        "/api/workspaces/<int:workspace_id>/services/<int:service_id>/mcp-servers",
        auth="user", methods=["POST"], type="json",
    )
    def api_service_mcp_servers(
        self,
        workspace_id: int,
        service_id: int,
        action: str = 'list',
        **kw: Any,
    ) -> dict[str, Any]:
        """CRUD operations for project-scoped MCP servers on a cloud service."""
        # These records belong to support projects, not to the cloud service.
        # Product workspace access must not widen the existing project/AI
        # sharing boundary, including through this legacy service URL.
        if request.env.user.share:
            return {'success': False, 'error': _('Permission denied')}
        workspace = _PaaSCapability(request.env).workspace(workspace_id)
        if not workspace.exists():
            return {'success': False, 'error': _('Workspace not found or access denied')}

        access = _PaaSCapability(request.env).access(workspace)
        if not access:
            return {'success': False, 'error': _('Workspace not found or access denied')}

        service = _PaaSCapability(request.env).cloud_service(service_id, workspace_id)
        if (not service.exists() or service.workspace_id.id != workspace_id
                or service.template_id.conceal_instances):
            return {'success': False, 'error': _('Service not found')}

        if action == 'list':
            return self._list_mcp_servers(service)

        # Legacy service-centric write endpoint must honour the same portal
        # settings flag as the project-centric route (two-layer-acl-lockstep:
        # 三支寫入 endpoint 必須同一開關). Without this gate, flag-off lets a
        # workspace owner/admin RPC past read-only via the legacy route.
        write_actions = ('create', 'update', 'delete', 'sync', 'test')
        if action in write_actions:
            if not _portal_project_settings_editable(request.env):
                return {'success': False, 'error': _('Permission denied')}

        if action == 'create':
            if access.role not in [ROLE_OWNER, ROLE_ADMIN]:
                return {'success': False, 'error': _('Permission denied')}
            return self._create_mcp_server(service, kw)
        elif action == 'update':
            if access.role not in [ROLE_OWNER, ROLE_ADMIN]:
                return {'success': False, 'error': _('Permission denied')}
            return self._update_mcp_server(service, kw)
        elif action == 'delete':
            if access.role not in [ROLE_OWNER, ROLE_ADMIN]:
                return {'success': False, 'error': _('Permission denied')}
            return self._delete_mcp_server(service, kw)
        elif action == 'sync':
            if access.role not in [ROLE_OWNER, ROLE_ADMIN]:
                return {'success': False, 'error': _('Permission denied')}
            return self._sync_mcp_server(service, kw)
        elif action == 'test':
            return self._test_mcp_server(service, kw)
        else:
            return {'success': False, 'error': _('Unknown action: %(action)s', action=action)}

    def _list_mcp_servers(self, service) -> dict[str, Any]:
        # User-scope MCP now hangs off project, not service (D6 keeps the
        # frontend tab on the service page; query walks service.project_ids).
        project = service.sudo().project_ids[:1]
        if not project:
            return {'success': True, 'data': []}
        servers = project.mcp_server_ids.filtered(
            lambda s: s.scope == 'user' and s.active
        )
        return {
            'success': True,
            'data': [self._format_mcp_server(s) for s in servers],
        }

    def _create_mcp_server(self, service, params: dict) -> dict[str, Any]:
        name = params.get('name', '').strip()
        url = params.get('url', '').strip()
        if not name or not url:
            return {'success': False, 'error': _('Name and URL are required')}
        # legacy create never stores sidecar_backed → validate as external.
        url_error = _validate_mcp_url(url, sidecar_backed=False)
        if url_error:
            return {'success': False, 'error': url_error}

        project = service.sudo().project_ids[:1]
        if not project:
            return {
                'success': False,
                'error': _('No support project bound to this service.'),
            }

        McpServer = request.env['woow_paas_platform.mcp_server'].with_context({}).sudo()
        server = McpServer.create({
            'name': name,
            'url': url,
            'transport': params.get('transport', 'sse'),
            'api_key': params.get('api_key', ''),
            'headers_json': params.get('headers_json', ''),
            'description': params.get('description', ''),
            'scope': 'user',
            'project_id': project.id,
        })
        return {'success': True, 'data': self._format_mcp_server(server)}

    def _update_mcp_server(self, service, params: dict) -> dict[str, Any]:
        server_id = params.get('server_id')
        if not server_id:
            return {'success': False, 'error': _('server_id is required')}

        McpServer = request.env['woow_paas_platform.mcp_server'].with_context({}).sudo()
        server = McpServer.browse(int(server_id))
        if not server.exists() or server.project_id.cloud_service_id.id != service.id:
            return {'success': False, 'error': _('MCP Server not found')}

        if 'url' in params:
            url_error = _validate_mcp_url(
                (params.get('url') or '').strip(),
                sidecar_backed=server.sidecar_backed,
            )
            if url_error:
                return {'success': False, 'error': url_error}

        vals = {}
        for field in ('name', 'url', 'transport', 'api_key', 'headers_json', 'description'):
            if field in params:
                vals[field] = params[field]
        if vals:
            server.write(vals)
        return {'success': True, 'data': self._format_mcp_server(server)}

    def _delete_mcp_server(self, service, params: dict) -> dict[str, Any]:
        server_id = params.get('server_id')
        if not server_id:
            return {'success': False, 'error': _('server_id is required')}

        McpServer = request.env['woow_paas_platform.mcp_server'].with_context({}).sudo()
        server = McpServer.browse(int(server_id))
        if not server.exists() or server.project_id.cloud_service_id.id != service.id:
            return {'success': False, 'error': _('MCP Server not found')}

        server.unlink()
        return {'success': True}

    def _sync_mcp_server(self, service, params: dict) -> dict[str, Any]:
        server_id = params.get('server_id')
        if not server_id:
            return {'success': False, 'error': _('server_id is required')}

        McpServer = request.env['woow_paas_platform.mcp_server'].with_context({}).sudo()
        server = McpServer.browse(int(server_id))
        if not server.exists() or server.project_id.cloud_service_id.id != service.id:
            return {'success': False, 'error': _('MCP Server not found')}

        # #554：`action_sync_tools` 自帶 catch-all，失敗時**不會**拋到這裡——
        # 只能靠它的 bool 回傳值判讀，否則一律回 success: True（使用者看到
        # 「同步成功」，實際 tool 清單處於半同步狀態）。失敗原因讀 callee 剛寫
        # 進去的 `state_message`。外層 try 留著接非預期例外（例如格式化階段）。
        try:
            if not server.action_sync_tools():
                reason = server.state_message or 'Unknown error'
                _logger.warning(
                    'MCP sync failed for server %s: %s', server.name, reason)
                return {'success': False, 'error': _('Sync failed: %(reason)s', reason=reason)}
        except Exception as e:
            _logger.warning('MCP sync failed for server %s: %s', server.name, e)
            return {'success': False, 'error': _('Sync failed: %(reason)s', reason=e)}

        return {'success': True, 'data': self._format_mcp_server(server)}

    def _test_mcp_server(self, service, params: dict) -> dict[str, Any]:
        server_id = params.get('server_id')
        if not server_id:
            return {'success': False, 'error': _('server_id is required')}

        McpServer = request.env['woow_paas_platform.mcp_server'].with_context({}).sudo()
        server = McpServer.browse(int(server_id))
        if not server.exists() or server.project_id.cloud_service_id.id != service.id:
            return {'success': False, 'error': _('MCP Server not found')}

        # #554：同 `_sync_mcp_server`——callee 自帶 catch-all，只能判讀 bool。
        try:
            if not server.action_test_connection():
                reason = server.state_message or 'Unknown error'
                return {
                    'success': False,
                    'error': _('Connection test failed: %(reason)s', reason=reason),
                }
        except Exception as e:
            return {'success': False, 'error': _('Connection test failed: %(reason)s', reason=e)}

        return {
            'success': True,
            'data': {
                'state': server.state,
                'state_message': server.state_message or '',
            },
        }

    @staticmethod
    def _format_mcp_server(server) -> dict[str, Any]:
        tools = []
        for t in server.tool_ids.filtered(lambda x: x.active):
            tools.append({
                'id': t.id,
                'name': t.name,
                'description': t.description or '',
            })
        return {
            'id': server.id,
            'name': server.name,
            'url': server.url,
            'transport': server.transport,
            'description': server.description or '',
            'state': server.state,
            'state_message': server.state_message or '',
            'tool_count': server.tool_count,
            'tools': tools,
            'last_sync': server.last_sync.isoformat() if server.last_sync else None,
            'sidecar_backed': bool(server.sidecar_backed),
        }

    # ==================== Project-centric Support API (D4 / OR-6) ====================

    _PROJECT_NOT_FOUND_ERROR = 'Project not found or access denied'

    class _SyntheticAccess:
        """Stand-in access record for unbound projects.

        Issued only after the unbound carve-out in
        ``_resolve_project_access`` has already verified that the current
        user is a project follower — the role is granted to a
        scope-resolved user, not to anyone authenticated. Mirrors the
        follower carve-out in ``ai_assistant.py:_check_project_access``;
        both layers must stay in lock-step, otherwise the rule layer
        denies but the controller layer still serves the row.
        """
        def __init__(self, role):
            self.role = role

    def _resolve_project_access(self, project_id: int):
        """Resolve a project + workspace access record for the current user.

        Returns ``(project, access)`` when the user can reach the project,
        or ``(None, None)`` for every deny path — the unified ``Project
        not found or access denied`` error keeps id enumeration closed.

        Unbound projects (no cloud_service / workspace) require the
        current user to be in ``project.message_partner_ids`` (follower
        carve-out, in lock-step with ``ai_assistant.py``).
        """
        Project = request.env['project.project'].with_context({}).sudo()
        project = Project.browse(project_id)
        if not project.exists():
            return None, None
        service = project.cloud_service_id
        if service and service.template_id.conceal_instances:
            return None, None
        workspace = service.workspace_id
        if not workspace:
            user_partner = request.env.user.partner_id
            if user_partner in project.message_partner_ids:
                return project, self._SyntheticAccess(role=ROLE_OWNER)
            return None, None
        # Preserve the existing bound-project authorization contract. The
        # product capability grants PaaS resources, not private ERP projects.
        access = workspace.check_user_access(request.env.user)
        if not access:
            return None, None
        return project, access

    @route(
        "/api/support-projects/<int:project_id>",
        auth="user", methods=["POST"], type="json",
    )
    def api_support_project_detail(
        self,
        project_id: int,
        action: str = 'get',
        **kw: Any,
    ) -> dict[str, Any]:
        """Project detail operations (get / update).

        URL is hyphenated (``/api/support-projects/...``) to avoid the
        ``/api/support/projects/<wid>`` slash pattern in ``ai_assistant.py``
        which scopes to a workspace, not a project — see Plan D4 / OR-6.
        """
        project, access = self._resolve_project_access(project_id)
        if not project or not access:
            return {'success': False, 'error': self._PROJECT_NOT_FOUND_ERROR}

        if action == 'get':
            return {'success': True, 'data': self._format_project_detail(project)}
        if action == 'update':
            if not _portal_project_settings_editable(request.env):
                return {'success': False, 'error': _('Permission denied')}
            if access.role not in (ROLE_OWNER, ROLE_ADMIN):
                return {'success': False, 'error': _('Permission denied')}
            return self._update_support_project(project, kw)
        return {'success': False, 'error': _('Unknown action: %(action)s', action=action)}

    def _format_project_detail(self, project) -> dict[str, Any]:
        servers = project.mcp_server_ids.filtered(
            lambda s: s.scope == 'user' and s.active
        )
        service = project.cloud_service_id
        return {
            'id': project.id,
            'name': project.name,
            'description': project.description or '',
            'cloud_service_id': service.id if service else False,
            'cloud_service_name': service.name if service else '',
            'cloud_service_mcp_enabled': bool(
                service and service.template_id.mcp_enabled
            ),
            # #271: sidecar flow only valid once the service has an in-cluster
            # identity (helm release + namespace). Frontend gates the
            # 'Add Sidecar MCP' button on this so an undeployed service can't
            # prefill a public sidecar URL that violates the in-cluster
            # invariant (same gate as the two sidecar URL builders).
            'cloud_service_deployed': bool(
                service and service.helm_release_name and service.helm_namespace
            ),
            'workspace_id': project.workspace_id.id if project.workspace_id else False,
            'workspace_name': project.workspace_id.name if project.workspace_id else '',
            'mcp_server_ids': [self._format_mcp_server(s) for s in servers],
            'portal_editable': _portal_project_settings_editable(request.env),
            'task_count': project.task_count,
            'open_task_count': project.open_task_count,
            'created_date': project.create_date.isoformat() if project.create_date else None,
        }

    def _update_support_project(self, project, params: dict) -> dict[str, Any]:
        vals: dict[str, Any] = {}
        if 'name' in params:
            name = (params.get('name') or '').strip()
            if not name:
                return {'success': False, 'error': _('Name cannot be empty')}
            vals['name'] = name
        if 'description' in params:
            vals['description'] = params.get('description') or ''

        if 'cloud_service_id' in params:
            target_raw = params.get('cloud_service_id')
            if target_raw:
                CloudService = request.env['woow_paas_platform.cloud_service'].with_context({}).sudo()
                target = CloudService.browse(int(target_raw))
                if not target.exists():
                    return {'success': False, 'error': _('Cloud Service not found')}
                current_ws = project.workspace_id
                if not current_ws or target.workspace_id.id != current_ws.id:
                    return {
                        'success': False,
                        'error': _('Cloud Service must belong to the same workspace.'),
                    }
                vals['cloud_service_id'] = target.id
            else:
                vals['cloud_service_id'] = False

        if not vals:
            return {'success': True, 'data': self._format_project_detail(project)}

        try:
            project.sudo().write(vals)
        except ValidationError as e:
            return {'success': False, 'error': str(e)}
        return {'success': True, 'data': self._format_project_detail(project)}

    @route(
        "/api/support-projects/<int:project_id>/mcp-servers",
        auth="user", methods=["POST"], type="json",
    )
    def api_support_project_mcp_servers(
        self,
        project_id: int,
        action: str = 'list',
        **kw: Any,
    ) -> dict[str, Any]:
        """Project-centric MCP CRUD (Plan D4 / AC #4-7)."""
        project, access = self._resolve_project_access(project_id)
        if not project or not access:
            return {'success': False, 'error': self._PROJECT_NOT_FOUND_ERROR}

        if action == 'list':
            return self._list_project_mcp_servers(project)

        # sidecar-hint is read-only but mirrors create's owner/admin gate
        # (it is the prefill backing for the Add Sidecar MCP shortcut).
        write_actions = (
            'create', 'update', 'delete', 'sync', 'test', 'sidecar-hint',
        )
        if action in write_actions:
            if not _portal_project_settings_editable(request.env):
                return {'success': False, 'error': _('Permission denied')}
            if access.role not in (ROLE_OWNER, ROLE_ADMIN):
                return {'success': False, 'error': _('Permission denied')}

        if action == 'sidecar-hint':
            return self._compute_project_sidecar_hint(project)
        if action == 'create':
            return self._create_project_mcp_server(project, kw)
        if action == 'update':
            return self._update_project_mcp_server(project, kw)
        if action == 'delete':
            return self._delete_project_mcp_server(project, kw)
        if action == 'sync':
            return self._sync_project_mcp_server(project, kw)
        if action == 'test':
            return self._test_project_mcp_server(project, kw)
        return {'success': False, 'error': _('Unknown action: %(action)s', action=action)}

    def _compute_project_sidecar_hint(self, project) -> dict[str, Any]:
        """Return prefill payload for the Add Sidecar MCP UI shortcut, or
        an error when the project has no mcp-enabled backing service."""
        service = project.cloud_service_id
        if not service or not service.template_id.mcp_enabled:
            return {
                'success': False,
                'error': _('No sidecar available for this project'),
            }
        template = service.template_id
        return {
            'success': True,
            'data': {
                'url': self._compute_sidecar_url_hint(service),
                'transport': template.mcp_transport or 'streamable_http',
                'api_key': service.mcp_auth_token or '',
                'suggested_name': f'{service.name} MCP',
            },
        }

    def _list_project_mcp_servers(self, project) -> dict[str, Any]:
        servers = project.mcp_server_ids.filtered(
            lambda s: s.scope == 'user' and s.active
        )
        return {
            'success': True,
            'data': [self._format_mcp_server(s) for s in servers],
        }

    def _create_project_mcp_server(self, project, params: dict) -> dict[str, Any]:
        name = (params.get('name') or '').strip()
        url = (params.get('url') or '').strip()
        if not name or not url:
            return {'success': False, 'error': _('Name and URL are required')}
        url_error = _validate_mcp_url(
            url, sidecar_backed=bool(params.get('sidecar_backed', False)),
        )
        if url_error:
            return {'success': False, 'error': url_error}

        McpServer = request.env['woow_paas_platform.mcp_server'].with_context({}).sudo()
        # #525 B4：與 `_update_project_mcp_server` 同理**刻意不包 try**——對齊
        # service-centric 的 `_create_mcp_server`（那支的 `McpServer.create(...)`
        # 同樣裸露、直接 return）。只改 update 會把「update 包／不包不一致」
        # 換成「create 包、update 不包」的新不一致。
        server = McpServer.create({
            'name': name,
            'url': url,
            'transport': params.get('transport', 'sse'),
            'api_key': params.get('api_key', ''),
            'headers_json': params.get('headers_json', ''),
            'description': params.get('description', ''),
            'scope': 'user',
            'project_id': project.id,
            # #271: URL↔flag consistency IS now enforced — see
            # _validate_mcp_url(sidecar_backed=...) above + the model
            # @api.constrains _check_sidecar_url_in_cluster.
            'sidecar_backed': bool(params.get('sidecar_backed', False)),
        })
        return {'success': True, 'data': self._format_mcp_server(server)}

    def _resolve_project_mcp(self, project, params: dict):
        server_id = params.get('server_id')
        if not server_id:
            return None, {'success': False, 'error': _('server_id is required')}
        McpServer = request.env['woow_paas_platform.mcp_server'].with_context({}).sudo()
        server = McpServer.browse(int(server_id))
        if not server.exists() or server.project_id.id != project.id:
            return None, {'success': False, 'error': _('MCP Server not found')}
        return server, None

    def _update_project_mcp_server(self, project, params: dict) -> dict[str, Any]:
        server, error = self._resolve_project_mcp(project, params)
        if error:
            return error
        if 'url' in params:
            url_error = _validate_mcp_url(
                (params.get('url') or '').strip(),
                sidecar_backed=server.sidecar_backed,
            )
            if url_error:
                return {'success': False, 'error': url_error}
        vals: dict[str, Any] = {}
        for field_name in ('name', 'url', 'transport', 'api_key', 'headers_json', 'description'):
            if field_name in params:
                vals[field_name] = params[field_name]
        if vals:
            # #525 B4：這裡**刻意不包 try**——對齊 service-centric 的
            # `_update_mcp_server`。Odoo 18 的 `write()` 是先寫後驗
            # （`models.py` 的 `field.write(...)` 在 `_validate_fields(...)` 之前），
            # 所以把 ValidationError 接下來改回 `success: False` 會讓新值隨
            # request 正常 commit ⇒ 使用者看到「更新失敗」但記錄已經被改。
            # 讓它傳到 JSON-RPC dispatcher，框架不 commit、整筆回捲，才是一致的。
            # 順帶解掉第二層不一致：`mcp_server.write()` 在 `super().write()` 之後
            # 才推 mcp snapshot，中途拋例外時 DB 已變而 running pod 內的
            # `mcp.json` 仍是舊值——回捲之後兩邊都不變，不會漂移。
            server.write(vals)
        return {'success': True, 'data': self._format_mcp_server(server)}

    def _delete_project_mcp_server(self, project, params: dict) -> dict[str, Any]:
        server, error = self._resolve_project_mcp(project, params)
        if error:
            return error
        server.unlink()
        return {'success': True}

    def _sync_project_mcp_server(self, project, params: dict) -> dict[str, Any]:
        server, error = self._resolve_project_mcp(project, params)
        if error:
            return error
        # #554：見 `_sync_mcp_server` 的註記——callee 自帶 catch-all，判讀 bool。
        try:
            if not server.action_sync_tools():
                reason = server.state_message or 'Unknown error'
                _logger.warning(
                    'MCP sync failed for server %s: %s', server.name, reason)
                return {'success': False, 'error': _('Sync failed: %(reason)s', reason=reason)}
        except Exception as e:
            _logger.warning('MCP sync failed for server %s: %s', server.name, e)
            return {'success': False, 'error': _('Sync failed: %(reason)s', reason=e)}
        return {'success': True, 'data': self._format_mcp_server(server)}

    def _test_project_mcp_server(self, project, params: dict) -> dict[str, Any]:
        server, error = self._resolve_project_mcp(project, params)
        if error:
            return error
        # #554：同上。
        try:
            if not server.action_test_connection():
                reason = server.state_message or 'Unknown error'
                return {
                    'success': False,
                    'error': _('Connection test failed: %(reason)s', reason=reason),
                }
        except Exception as e:
            return {'success': False, 'error': _('Connection test failed: %(reason)s', reason=e)}
        return {
            'success': True,
            'data': {
                'state': server.state,
                'state_message': server.state_message or '',
            },
        }

    # ==================== Cloud Service Helpers ====================

    @staticmethod
    def _unflatten_dotpath_keys(flat_dict: dict[str, Any]) -> dict[str, Any]:
        """Convert flat dot-path keys to nested dict structure.

        Example: {"a.b.c": 1, "a.b.d": 2} → {"a": {"b": {"c": 1, "d": 2}}}
        Keys without dots are kept as-is.
        """
        result = {}
        for key, value in flat_dict.items():
            parts = key.split('.')
            if len(parts) == 1:
                result[key] = value
                continue
            d = result
            for part in parts[:-1]:
                if part not in d or not isinstance(d[part], dict):
                    d[part] = {}
                d = d[part]
            d[parts[-1]] = value
        return result

    @staticmethod
    def _deep_merge(base: dict, override: dict) -> dict:
        """Deep merge override into base. Override values win on conflict."""
        result = base.copy()
        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = PaasController._deep_merge(result[key], value)
            else:
                result[key] = value
        return result

    @staticmethod
    def _safe_json_load(raw: Any, default: Any, field_label: str, ctx: str = '') -> Any:
        """Parse a JSON-in-Text field, returning ``default`` instead of raising.

        Seed/admin-editable JSON fields (tags, helm_default_values) live in XML and
        are prone to the same line-wrap-inside-a-string defect that broke
        helm_value_specs. These are consumed on hot read paths (marketplace listing,
        template detail) with NO surrounding try/except, so one malformed row would
        500 the whole endpoint for every user. Degrade to ``default`` + a loud log on
        the display paths; deploy paths (which feed Helm) should fail loud instead.
        """
        if not raw:
            return default
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            _logger.error(
                "Invalid JSON in %s%s; falling back to default",
                field_label, f' ({ctx})' if ctx else '',
            )
            return default

    @staticmethod
    def _password_spec_paths(specs: Any) -> set[tuple[str, ...]]:
        """從 helm_value_specs 取出所有 ``type: password`` 的鍵路徑。

        回傳的每個元素是一條 path tuple。同一把 key 收兩種形態：

        * ``tuple(key.split('.'))`` —— 巢狀落庫形態（``_unflatten_dotpath_keys``
          把 ``auth.anthropicApiKey`` 攤成 ``{'auth': {'anthropicApiKey': ...}}``）。
        * ``(key,)`` —— 扁平落庫形態。前端 ``getSpecValue`` 就是「先試 flat key
          再試 dot-path」，代表舊資料真的可能以字面 ``'auth.anthropicApiKey'``
          當鍵存在，只認巢狀會漏掉那一種。

        ⚠ 任何結構異常（specs 不是 dict、group 不是 list、spec 不是 dict）都讓
        例外往外拋，由呼叫端 fail-closed 接住——這裡不做 best-effort 略過，因為
        「只解析出一部分憑證鍵」的結果與「全部解析成功」長得一模一樣，會靜默漏遮。
        """
        paths: set[tuple[str, ...]] = set()
        for group in ('required', 'optional'):
            for spec in specs.get(group) or []:
                if spec.get('type') != 'password':
                    continue
                key = spec.get('key')
                if not key:
                    continue
                paths.add(tuple(key.split('.')))
                paths.add((key,))
        return paths

    @classmethod
    def _sanitize_helm_values(cls, values: dict, template: Any) -> dict:
        """Remove secret values from helm_values before exposing in API responses.

        遮罩由**兩種來源**疊起來，缺一不可：

        1. **spec-driven（#535，主力）**：template 的 ``helm_value_specs`` 裡每一條
           ``type: password`` 都是租戶親手填的憑證，一律全遮。這條讓「新增一個
           password 欄位」自動被涵蓋——不必回來補程式碼。
        2. **三條 hardcoded path（既有，不可移除）**：top-level ``secret`` 子樹、
           ``config.sensitive`` 葉節點、``auth.apiServerKey``。這些鍵**不在**
           ``helm_value_specs`` 裡（它們由平台自己注入，租戶看不到也填不到），
           spec-driven 涵蓋不到，所以是**增加一層**而不是取代。

        ⚠ **不得連坐同層的非憑證欄位**：只遮 spec 點名的那一格。PostgreSQL 家族
        seed 的 ``auth.username`` / ``auth.database`` 與 ``auth.*ApiKey`` 同層，
        整個 ``auth`` 子樹全遮會讓那些 template 的設定分頁變成一排 ``***``（#530
        已經踩過一次）。

        🔴 **遮罩與前端回填是同一個契約**（#535 D1 實測）：只改這裡而不改
        ``ConfigurationTab``，租戶按一次存檔就會把星號當成自己的輸入送回來，
        ``_deep_merge`` 讓它覆蓋既有值 ⇒ **平台把租戶的管理員密碼改成 ``***``**。
        前端側的對應改動見 ``ConfigurationTab.startEditing/validateForm/saveChanges``，
        後端側的兜底見 ``_drop_unchanged_password_values``。

        Mask surface: prefix 8 chars + '...' when long enough, else '***'.
        Depth bounded at 5 to bound CPU on adversarial payloads.
        """
        if not values:
            return values

        def _mask_value(v):
            """Legacy top-level ``secret`` 子樹：保留 8 字元前綴。

            n8n 既有 UI 靠前綴辨識是哪把 key（N8N_API_KEY 等），故此處維持原狀。
            """
            if not v:
                return '***'
            sv = str(v)
            return f'{sv[:8]}...' if len(sv) > 8 else '***'

        def _mask_value_fully(v):
            """#357 C19：``config.sensitive.*`` 一律全遮、**不留前綴**。

            遮罩是「_strip_mcp_sensitive_values 失效時」的第二道防線，第二道防線
            自身不該洩漏任何明文：admin_password 走 mcp-admin-core 的明文比對
            （secrets.compare_digest），前 8 字元對攻擊者是實打實的熵折損。
            與 _mask_value 的差別是刻意的——見該函式 docstring。
            """
            return '***'

        def _mask_secret_tree(value, masker=_mask_value):
            if isinstance(value, dict):
                return {key: _mask_secret_tree(item, masker) for key, item in value.items()}
            if isinstance(value, list):
                return [_mask_secret_tree(item, masker) for item in value]
            return masker(value)

        # --- spec-driven 憑證鍵（#535）＋ fail-closed 判定 -------------------
        #
        # 「無從得知哪些鍵是憑證」時全遮，而不是全放。舊碼收了 template 參數卻
        # 整段沒用到，於是傳什麼 template 進來輸出都一樣——連「傳錯 template」都
        # 沒有任何測試會紅。
        parsed_specs = template._parse_helm_value_specs_or_fail() if template else {}
        if not template or parsed_specs is SPECS_UNPARSEABLE:
            _logger.error(
                "Cannot determine credential keys for helm_values (template=%s); "
                "masking every value fail-closed.", template,
            )
            return _mask_secret_tree(values, _mask_value_fully)
        try:
            password_paths = cls._password_spec_paths(parsed_specs)
        except Exception:
            # 結構壞掉（specs 不是 dict／group 不是 list／spec 不是 dict）＝同樣
            # 屬於「讀不懂」，與 JSON 解析失敗同一類，走同一條 fail-closed。
            _logger.exception(
                "Malformed helm_value_specs structure for template %s; "
                "masking every value fail-closed.", template,
            )
            return _mask_secret_tree(values, _mask_value_fully)

        def _is_password_path(child_path: tuple[str, ...]) -> bool:
            """spec 的鍵路徑比對——與同函式既有兩條 path 分支一樣採**尾綴**比對。

            ``config.sensitive`` 用 ``child_path[-2:]``、``auth.apiServerKey`` 亦然，
            也就是這個函式整體是深度無關的。spec-driven 若改用「從根錨定」就會與
            旁邊兩條的語意不一致（同一份 doc 素材點名的「兩層 path 語意不對稱」正是
            這種東西），且對「值被搬到別的父節點底下」這種形態失去覆蓋。
            尾綴比對只會多遮同名的那一格，不會連坐同層鄰居。
            """
            return any(
                child_path[-len(p):] == p for p in password_paths
            )

        def _mask_secrets(d: dict, path: tuple[str, ...] = (), depth: int = 0) -> dict:
            result = {}
            for key, val in d.items():
                child_path = path + (key,)
                if key == 'secret' and isinstance(val, dict):
                    # legacy N8N_API_KEY / etc under top-level 'secret'
                    result[key] = _mask_secret_tree(val)
                elif child_path[-2:] == ('config', 'sensitive') and isinstance(val, dict):
                    # mcp-server chart sensitive channel: mask every leaf under
                    # config.sensitive (admin_password, mcp_auth_token,
                    # connection.*, proxy.bearer_token).
                    # #357 C19：這條分支用 _mask_value_fully（無前綴）——不同於上面
                    # legacy 'secret' 分支刻意保留的 8 字元前綴。
                    result[key] = _mask_secret_tree(val, _mask_value_fully)
                elif child_path[-2:] == WOOW_ODOO_ADMIN_PASSWORD_PATH:
                    # #1130 woow-odoo 初始管理員密碼：平台鑄造、不在 specs 裡，
                    # spec-driven 涵蓋不到；正常路徑已被 _strip_mcp_sensitive_values
                    # 擋在落庫之前，這裡是第二道。只遮這一格（同層 conf.* 是設定）。
                    result[key] = _mask_value_fully(val)
                elif child_path[-2:] == ('auth', 'apiServerKey'):
                    # #530 hermes gateway key：葉節點（字串），不在 config.sensitive
                    # 底下。刻意只遮這一格而非整個 auth 子樹——同層還有非憑證欄位
                    # （PostgreSQL 家族 seed 的 auth.username／auth.database），全遮
                    # 會讓那些 template 的設定分頁變成一排 ***。
                    result[key] = _mask_value_fully(val)
                elif _is_password_path(child_path):
                    # #535：租戶經 helm_value_specs（type: password）填入的憑證。
                    # 排在遞迴分支**之前**——值萬一是 dict（未來的複合憑證），要整棵
                    # 遮掉而不是下鑽後逐格放行。
                    result[key] = _mask_secret_tree(val, _mask_value_fully)
                elif isinstance(val, dict) and depth < 5:
                    result[key] = _mask_secrets(val, child_path, depth + 1)
                else:
                    result[key] = val
            return result

        return _mask_secrets(values)

    # _build_mcp_sidecar_config moved to CloudService model

    @classmethod
    def _drop_masked_echo_values(
        cls, flat_values: dict[str, Any], existing_values: dict[str, Any] | None, template: Any,
    ) -> dict[str, Any]:
        """#535：把「使用者原封送回遮罩值」的欄位從 reconfigure payload 剔除。

        read 端遮罩與 write 端回填是**同一個契約**。前端 ``startEditing()`` 會把
        API 回來的值全量回填進表單，所以租戶只要按一次存檔、連碰都不用碰密碼欄，
        被遮成 ``'***'`` 的憑證就會原封送回來，經 ``_deep_merge`` 覆蓋掉 DB 裡的
        真值——**平台自己把租戶的 Odoo 管理員密碼改成 ``'***'``**（#535 D1 實測，
        `odooPassword` 從 ``PLAIN_PW_D1_PROBE`` 變成 ``'***'``）。

        主修法在前端（password 欄位空白起始、留白＝不變更）。這裡是**兜底**：
        前端 bundle 與後端不是原子部署，租戶在升級當下已經開著的分頁跑的還是舊
        JS，那個窗口內照樣會送回星號。

        判準是「送回來的值 **等於我們剛才顯示給他看的值**、且與實值不同」⇒ 這格
        沒有變更。對沒被遮罩的欄位，顯示值就是實值，條件恆假 ⇒ **對非憑證欄位是
        no-op**，不會連坐（這是刻意寫成兩個條件而不是只比對 ``'***'`` 字面值的
        原因：後者會擋掉某個租戶真的想把 smtpHost 設成 ``'***'`` 的自由）。
        """
        if not flat_values:
            return flat_values

        existing = existing_values or {}
        shown = cls._sanitize_helm_values(dict(existing), template)

        kept: dict[str, Any] = {}
        for key, value in flat_values.items():
            shown_value = _lookup_helm_value(shown, key)
            real_value = _lookup_helm_value(existing, key)
            if (shown_value is not _HELM_VALUE_MISSING
                    and value == shown_value
                    and shown_value != real_value):
                _logger.info(
                    "Dropping unchanged masked value for helm key %r on reconfigure "
                    "(client echoed the mask back)", key,
                )
                continue
            kept[key] = value
        return kept

    def _list_services(self, workspace: Any) -> dict[str, Any]:
        """List all services in a workspace.

        #1159 Q1：**純讀**——不呼叫 operator、不寫 cloud_service（含 DNS 旗標）。回的是
        背景 poll 的最近觀測（``state``＋``observed_at``）；由觀測推進的狀態轉移只由
        ``CloudService._cron_check_deploying_services`` 寫，DNS 旗標由背景低頻重探清除
        （Q16）。前端 5 秒輪詢照舊，只是讀 DB。
        """
        CloudService = workspace.env['woow_paas_platform.cloud_service']

        services = CloudService.search([
            ('workspace_id', '=', workspace.id),
            ('template_id', 'not in', self._hidden_template_ids(request.env)),
        ])

        data = [self._format_service(svc) for svc in services]

        return {
            'success': True,
            'data': data,
            'count': len(data),
        }

    @staticmethod
    def _generate_admin_password():
        """#357 C20：委派給 model 的單一生成點，不在 controller 另生一份。

        原本這裡自己 ``import secrets`` 再 ``token_urlsafe(24)``，與
        ``CloudService._generate_admin_password`` / create() 是同一條安全屬性
        寫三處——改強度時容易只改一邊，且唯一性測試沒有單一標的可鎖。
        保留本 wrapper 只為不動既有 controller 呼叫點的命名。
        """
        return request.env['woow_paas_platform.cloud_service']._generate_admin_password()

    @staticmethod
    def _gateway_credentials(service: Any) -> dict[str, Any]:
        """#530／#672：組出 MCP 連線的**一次性**交付 payload（明文 key ＋ 完整網址）。

        建立服務與 Reset 兩條既有回應共用這一處，形狀才不會分岔。**刻意沒有回讀
        端點**——本 repo 的憑證一律一次性，新增回讀等於開一條新的外洩面並要自行
        承擔授權；租戶弄丟就跑 Reset 換發。

        交付判準＝`service._gateway_public_url()` 非空（chart 有 gateway 對外路徑
        **且**這台服務有對外根位址）。憑證的**鑄造與供給**判準是同源但較寬的
        `CloudService._chart_has_gateway()`（只看 chart 有沒有 gateway）——沒有
        subdomain 的服務仍要有 key（gateway 在叢集內照跑），只是平台沒有網址可以
        交出去。兩者刻意分層：對沒有 gateway 的 chart 交付一把 key ＋ 一條不存在的
        網址，比不交付更糟——它看起來像產品保證，而租戶會拿它去 debug 一個根本不
        存在的端點。

        三個閘門（鑄造／供給／交付）都從 seed 的 `gateway.externalPath` 推導，
        **沒有任何一處是 slug allowlist**，也不再借用 `template.has_admin_gui`
        （那是 admin_password 的閘門；has_admin_gui 模板裡只有 hermes-agent 有 gateway）。

        放 controller 而非 model：本 method 讀出明文，而授權只存在 controller 層
        （#357 C05）。回傳明文的 model 方法一律 private 且只有單一 controller 入口，
        這裡不新增第二個那樣的 model 方法。

        ## #672：dashboard 這一格（`dashboard_url`）

        MCP admin GUI 的「Dashboard REST API」區要填**三格**（URL／帳號／密碼），
        平台原本只交付了兩格：帳號是 GUI 預設的 `admin`（正確），密碼是
        `admin_password`——**URL 從未交付**，而 GUI 內建預設
        `http://hermes-agent-svc:9119` 在本平台 host 與 port 都不存在。三格缺一格
        ＝整個管理面壞掉（MCP 的 10 個 tool 有 8 個走 Dashboard REST API；空字串
        會讓 httpx 直接丟 `UnsupportedProtocol`）。租戶拿不到、也猜不到這個值。

        值本身不是新算的：`service._dashboard_public_url()` 與注入給 agent 的
        `HERMES_DASHBOARD_PUBLIC_URL` 讀同一張 value path 表（見 model 端
        `_DASHBOARD_PUBLIC_URL_VALUE_PATHS`），所以交出去的就是平台已經對 chart
        宣告過的那一個 URL。

        ⚠ 上面「三個閘門都從 seed 推導」那句**只適用 gateway**：dashboard 沒有可讀
        的 seed 值（它的對外形態是 root URL，chart 0.1.3 正是把 `dashboard.externalPath`
        拿掉的那一版），判準因此落在注入表上——那張表以 slug 為鍵，但它是**既有的**
        chart schema 知識、不是為這個判斷另立的名單。完整理由見 model 端該常數的註解。

        ⚠ **兩個閘門刻意各判各的**，dashboard 不掛在 gateway 的條件下：兩者是不同
        的 chart 能力（gateway 有子路徑、dashboard 是 root），今天恰好只有
        hermes-agent 兩者兼具。綁在一起的話，哪天有 chart 只具其一，交付就會整組
        消失或整組多出來——而兩種錯法都不會有任何一端報錯。
        """
        creds: dict[str, Any] = {}
        secret = service._get_secret_record()
        gateway_url = service._gateway_public_url()
        if secret and secret.gateway_api_key and gateway_url:
            creds['gateway_api_key'] = secret.gateway_api_key
            creds['gateway_url'] = gateway_url
        dashboard_url = service._dashboard_public_url()
        if dashboard_url:
            creds['dashboard_url'] = dashboard_url
        return creds

    def _create_service(self, workspace: Any, template_id: int | None, name: str | None, values: dict[str, Any] | None, odoo_addon_ids: list[int] | None = None, plan_id: int | None = None) -> dict[str, Any]:
        """Create a new cloud service."""
        if not template_id:
            return {'success': False, 'error': _('Template ID is required')}
        if not name:
            return {'success': False, 'error': _('Service name is required')}
        if not plan_id:
            return {'success': False, 'error': _('Subscription plan is required')}

        name = name.strip()
        if not name:
            return {'success': False, 'error': _('Service name is required')}

        CloudAppTemplate = _PaaSCapability(request.env).cloud_templates()
        CloudService = workspace.env['woow_paas_platform.cloud_service']

        template = CloudAppTemplate.browse(template_id)
        # #336：generic create 拒絕 hidden template（hidden 只能經 provision 端點建）
        # #471 B 組：判準留 is_hidden——「未上架」的唯一實質效力就是擋新建，改判
        # conceal_instances 等於讓待定價／暫時下架的商品可被直接下單。
        # （原註解舉的 hermes-agent 已於 #494 定價上架；語意本身不變。）
        if not template.exists() or not template.is_active or template.is_hidden:
            return {'success': False,
                    'error': _('This template cannot be deployed directly.')}

        # Validate subscription plan
        Plan = request.env['sh.subscription.plan'].with_context({}).sudo()
        plan = Plan.browse(plan_id)
        if not plan.exists():
            return {'success': False, 'error': _('Subscription plan not found')}
        # Verify plan belongs to this template — 與 change-plan 那條
        # （`api_service_change_plan`）是同一把尺，兩條要一起看。
        # #949：同樣⛔ 不加 `template.sudo().subscription_plan_ids and` 前綴——
        # 零方案的 template 會讓整條判斷被跳過（fail-open）。完整理由與「新建
        # template 要先掛方案」那格寫在 change-plan 側，不在這裡重述。
        if plan not in template.sudo().subscription_plan_ids:
            return {'success': False, 'error': _('Plan not available for this template')}

        # Always generate reference_id server-side (never accept from frontend)
        reference_id = str(uuid.uuid4())
        # Generate hash-based names for cross-sandbox uniqueness
        from ..services.naming import make_namespace, make_service_subdomain
        ns_prefix = request.env['ir.config_parameter'].with_context({}).sudo().get_param(
            'woow_paas_platform.namespace_prefix', 'paas-ws-'
        )
        # #677：namespace／subdomain 吃 workspace 的 **stored** `ws_hash`，不是
        # `slug`——slug 由租戶提供的 name 逐字決定，而舊的 `md5(slug)[:8]` 是 32-bit
        # 截斷 ⇒ 租戶搜得出一個 name 讓自己的 release 落進別人的 namespace。
        subdomain = make_service_subdomain(workspace.ws_hash, reference_id, name)
        helm_release_name = f"svc-{reference_id[:8]}"
        helm_namespace = make_namespace(workspace.ws_hash, prefix=ns_prefix)

        # Filter user values to only allowed keys, then merge with defaults
        # Note: silently filter on create (frontend may send defaults alongside user values)
        # helm_default_values feeds Helm directly, so fail loud on malformed JSON here
        # (unlike the display paths which degrade) rather than deploy a broken service.
        try:
            default_values = json.loads(template.helm_default_values) if template.helm_default_values else {}
        except (json.JSONDecodeError, TypeError):
            _logger.error("Invalid helm_default_values for template %s; blocking create", template.name)
            return {'success': False, 'error': _('Template misconfigured (invalid default values). Please contact support.')}
        filtered_user_values, rejected = template._filter_allowed_helm_values(values)
        if rejected:
            # #1160：create 對 spec 以外的鍵靜默丟棄（前端可能夾帶舊表單的鍵，例如
            # #1130 移除的 odooPassword），只留 log；reconfigure 則 fail-loud。
            _logger.warning(
                "Dropped helm value keys not declared by template %s on create: %s",
                template.name, sorted(rejected),
            )

        # Extract _init.* keys (not Helm values, used for post-deploy init)
        init_params = {}
        helm_only_values = {}
        for key, val in filtered_user_values.items():
            if key.startswith('_init.'):
                init_params[key[len('_init.'):]] = val
            else:
                helm_only_values[key] = val

        # Validate n8n password if template requires init
        if template.post_deploy_init_type == 'n8n':
            pwd = init_params.get('owner_password', '')
            if len(pwd) < 8:
                return {'success': False, 'error': _('Password must be at least 8 characters')}
            if not any(c.isupper() for c in pwd):
                return {'success': False, 'error': _('Password must contain at least one uppercase letter')}
            if not any(c.islower() for c in pwd):
                return {'success': False, 'error': _('Password must contain at least one lowercase letter')}
            if not any(c.isdigit() for c in pwd):
                return {'success': False, 'error': _('Password must contain at least one number')}

        # helm_value_specs 帶 ``format`` 的文字欄位（例 code-server 的 git 設定）：
        # 不合法就在收錢前擋下，別讓一行字串到部署時才把 chart／gitconfig 弄壞。
        format_error = template._validate_spec_formats(helm_only_values)
        if format_error:
            return {'success': False, 'error': format_error}

        nested_user_values = self._unflatten_dotpath_keys(helm_only_values)
        merged_values = self._deep_merge(default_values, nested_user_values)

        # #1130：woow-odoo 的管理員密碼由平台鑄造（下方），租戶表單只剩 login email
        # ——舊的 odooPassword 長度檢查整段移除（#1160）。email 檢查與 reconfigure 共用。
        email_error = CloudService._woow_odoo_admin_email_error(
            template, merged_values, reject_reserved=True)
        if email_error:
            return {'success': False, 'error': email_error}
        _logger.info(
            "Merged helm values keys for service '%s' (template: %s): %s",
            name,
            template.name,
            sorted(merged_values.keys()),
        )

        # Generate per-service API key for MCP sidecar ↔ main container communication
        mcp_api_key = None
        if template.mcp_enabled and template.mcp_api_key_helm_path:
            mcp_api_key = str(uuid.uuid4())
            # Inject into Helm values at the configured dot-path
            api_key_nested = self._unflatten_dotpath_keys(
                {template.mcp_api_key_helm_path: mcp_api_key}
            )
            merged_values = self._deep_merge(merged_values, api_key_nested)

        # #357: 對 admin GUI 內建的 template 生成 admin_password。密碼只放進
        # cloud_service_secret + 一次性 response，不寫回 helm_values。
        # C01：判別器＝template.has_admin_gui（與 model 端 create() 供給閘門同源），
        # 不用 slug allowlist——否則新增 template 要兩處各補一次、且與 model 端會漂移。
        # #1130：woow-odoo 的初始 Odoo 管理員密碼同樣在這裡鑄造、一次性顯示（#1160）；
        # 閘門是 chart 不是 has_admin_gui（後者會接上 MCP 的 config.sensitive／reset 鏈）。
        # 送進 chart 的路徑是 conf.adminPassword（`_build_odoo_sensitive_values`）。
        admin_password = None
        if template.has_admin_gui or template._is_woow_odoo_chart():
            admin_password = self._generate_admin_password()
            _logger.info(
                "[#357] generated admin_password for service '%s' (template=%s)",
                name, template.name,
            )

        # #332：部署前把選定 plan 的 cs_* 規格 overlay 進 helm values
        # （runtime overlay，template seed 不動；cs_* 全 0＝與現狀 bit-for-bit 相同）
        merged_values = CloudService._apply_plan_spec_overlay(
            merged_values, template, plan)

        try:
            # Create service record in pending state (cron picks up 'pending')
            service_vals = {
                'workspace_id': workspace.id,
                'template_id': template.id,
                'name': name,
                'reference_id': reference_id,
                'state': 'pending',
                'subdomain': subdomain,
                'internal_port': template.default_port,
                'helm_release_name': helm_release_name,
                'helm_namespace': helm_namespace,
                # #559：helm_values 有 groups=，放在 create vals 裡會讓租戶身分的
                # create 直接 AccessError。改在 create 之後以提權入口寫入
                # （`_apply_helm_values` 套用與 create() 相同的敏感值剝除）。
                # Internal create still enforces ir.rule. Portal create uses
                # the verified workspace capability after the admin gate;
                # request context cannot supply any defaults/anchors.
                'helm_chart_version': template.helm_chart_version,
                # #332：allocated_* 改讀選定 plan 規格、0 值 fallback template.min_*
                **CloudService._allocated_vals_from(template, plan),
            }
            # Store init params for post-deploy initialization
            if init_params.get('owner_email'):
                service_vals['n8n_owner_email'] = init_params['owner_email']
            if init_params.get('owner_password'):
                service_vals['n8n_owner_password'] = init_params['owner_password']
            # Resolve Odoo addon selection (with dependency expansion)
            resolved_addon_ids = []
            if odoo_addon_ids and template.slug == 'odoo':
                CloudOdooAddon = _PaaSCapability(request.env).odoo_addons()
                selected = CloudOdooAddon.browse(odoo_addon_ids).filtered('active')
                # Expand dependencies recursively
                all_addons = selected
                queue = list(selected)
                while queue:
                    addon = queue.pop(0)
                    for dep in addon.depends_on:
                        if dep not in all_addons:
                            all_addons |= dep
                            queue.append(dep)
                resolved_addon_ids = all_addons.ids
                service_vals['odoo_addon_ids'] = [(6, 0, resolved_addon_ids)]

            service_model = (
                CloudService.with_context(mcp_admin_password=admin_password)
                if admin_password else CloudService
            )
            service = _internal_provision(service_model).create(service_vals)
            service._apply_helm_values(merged_values)  # #559

            # ⚠ 順序刻意：所有「還可能失敗」的落庫步驟都排在建訂閱**之前**。
            # `_create_and_confirm_subscription` 會同步寄出訂閱確認信
            # （sh_subscription 的 `_sh_send_subscription_email` 用
            # `send_mail(force_send=True)`），那是 **rollback 撤不回**的外部副作用
            # ——#520 的整段 `cr.rollback()` 也一樣撤不回它。它之後若還有步驟失敗，
            # DB 全撤但信已寄出 ⇒ 租戶收到「訂閱成功」卻沒有服務。把可失敗的步驟
            # 前移，讓寄信之後只剩 cron trigger 與純讀的 `_format_service`，窗口
            # 縮到近乎零。徹底解需把寄信移出交易＝動第三方 addon，不在本 PR 範圍。
            expose_config = None
            if template.ingress_enabled:
                expose_config = {
                    'enabled': True,
                    'subdomain': subdomain,
                }
            deploy_params = {}
            if expose_config:
                deploy_params['expose_config'] = expose_config
            if mcp_api_key:
                deploy_params['mcp_api_key'] = mcp_api_key
            if deploy_params:
                # #1317：deploy_params 限 group_system，租戶身分寫不進去；
                # 值全由伺服器產生，改走 model 的平台身分窄寫入。
                service._set_deploy_params(deploy_params)

            # Create subscription（此後不再有可失敗的落庫步驟）
            partner = workspace.owner_id.partner_id
            _logger.info(
                "[CLOUD_SVC] deploy_service created service=%s workspace=%s template=%s partner=%s plan=%s",
                service.id, workspace.id, template.id, partner.id, plan.id,
            )
            service._create_and_confirm_subscription(partner, plan)

            # Trigger the deploy cron to pick up this service ASAP
            cron = service.env.ref(
                'woow_paas_platform.ir_cron_deploy_cloud_services',
                raise_if_not_found=False,
            )
            if cron:
                cron._trigger()

            resp_data = self._format_service(service)
            # #357: 一次性顯示密碼。建立成功時唯一一次回明文；後續 GET
            # 不提供回取 API。
            if admin_password:
                resp_data['admin_password'] = admin_password
                if template._is_woow_odoo_chart():
                    # #1130：Odoo 的登入帳號就是租戶填的 email，一起交付；
                    # OneTimeCredentialsModal 依 template.app_admin_reset_kind 顯示
                    # 「忘記可在服務頁 Reset」的文案（#1370／#1186）。
                    resp_data['admin_username'] = (
                        merged_values.get('conf') or {}).get('adminEmail')
            # #530/#672: gateway API key ＋ 完整網址、dashboard REST API 的根網址，
            # 同樣是一次性。兩組各有自己的閘門，只有具備該能力的 chart 才交付
            # （見 _gateway_credentials）。
            resp_data.update(self._gateway_credentials(service))
            return {
                'success': True,
                'data': resp_data,
            }

        # #520：這三個分支的共同義務是 **rollback**。走到這裡代表建立被拒絕，
        # 但 `super().create()` 的 INSERT 已經送進 DB 了——例外被 except 吃掉後就
        # 不會傳播到框架層，request 正常結束 → transaction 照常 commit → 那一列
        # 留在 DB，隨後被 `ir.cron`（Cloud Service: Deploy Pending）撿去 helm
        # install。上限守衛（`CloudService.create()` 的 max_per_partner，#336/
        # #345）整個「raise 以撤回」的設計就是這樣從唯一的使用者入口失效的。
        #
        # 撤回整個 transaction（而非 savepoint）是刻意的：這正是例外真的傳播到
        # 框架時框架會做的事，我們只是在**保留 `{'success': False}` response 形狀**
        # （前端 AppConfigurationPage 靠 result.error 顯示訊息）的前提下自己做。
        # 本 request 走到 try 之前只有讀取與參數驗證，沒有任何要保留的寫入。
        except OperationalError:
            # #518：並發類 DB 例外（SerializationFailure 40001 等）**必須**傳出去。
            # 它是 Exception 子類，被下面任何一個 except 收掉的話 Odoo 的
            # `retrying()` 永遠看不到、不會以新 snapshot 重試——全域上限
            # （#501）在並發下就形同虛設。且當下 PG 交易已 aborted，硬回一個
            # result 只會讓後續 flush 再炸。
            #
            # 必須排在 #520 那三段之前：`SerializationFailure` 不是 `UserError`
            # 子類，但會被最後的 `except Exception` 吃掉。
            #
            # ⚠ 兩個已知的取捨（刻意接受，不是疏漏）：
            #
            # 1. **攔截面比「會被重試的那三種」寬**。`retrying()` 只重試
            #    `PG_CONCURRENCY_ERRORS_TO_RETRY`（LOCK_NOT_AVAILABLE /
            #    SERIALIZATION_FAILURE / DEADLOCK_DETECTED），其餘 OperationalError
            #    走 `if not isinstance(...): raise` 原樣拋出 ⇒ 前端拿到框架的錯誤而
            #    不是 `{'success': False}` 的形狀。這是**刻意的**：連線斷、磁碟滿、
            #    statement timeout 本來就不該被包裝成「業務失敗」回 HTTP 200——那正是
            #    #516／#520／#525 整個家族的根因形態。而且交易已 aborted，硬回一個
            #    result 只會讓後續 flush 再炸。反過來收窄成只認那三個 pgcode，等於把
            #    框架的白名單複製一份到這裡，框架改了我們不會知道。
            # 2. **重試會重播整個 handler**，其中含 `_create_and_confirm_subscription`
            #    的 `send_mail(force_send=True)`（交易撤不回的外部副作用）。若
            #    OperationalError 在建訂閱**之後**才拋出（下方只剩 cron trigger 與純讀
            #    的 `_format_service`，機率低但非零），重試會讓同一封訂閱確認信重送。
            #    徹底解要把寄信移出交易＝動第三方 addon（sh_subscription），不在本 PR
            #    範圍。窗口已由「把可失敗步驟前移到建訂閱之前」縮到最小。
            raise
        except (AccessDenied, AccessError, MissingError) as e:
            # 權限／ORM 技術例外不是講給租戶看的業務規則，細節只留 server log。
            request.env.cr.rollback()
            _logger.error("Access error creating service: %s\n%s", str(e), traceback.format_exc())
            return {'success': False, 'error': _('An error occurred while creating the service.')}

        except (ValidationError, UserError) as e:
            # 業務規則例外（每帳號實例上限、訂閱前置條件…）：訊息本來就是寫給
            # 使用者看的，原文回前端。統一成泛用字串的話，真正的原因只留在
            # server log，使用者與第一線支援都無從排錯（#520 症狀 4）。
            message = str(e)
            request.env.cr.rollback()
            _logger.warning(
                "Service create rejected (workspace=%s, template=%s): %s",
                workspace.id, template_id, message,
            )
            return {'success': False, 'error': message}

        except Exception as e:
            request.env.cr.rollback()
            _logger.error("Error creating service: %s\n%s", str(e), traceback.format_exc())
            return {'success': False, 'error': _('An error occurred while creating the service.')}

    # _deploy_service_background removed — deployment is now handled by
    # ir.cron (CloudService._cron_process_pending_deploys) to avoid
    # ConnectionPool lock contention seen when the platform ran Odoo 18 with workers=0
    # (prod is now prefork workers=6 + max_cron_threads=2, #1159 Q12).

    def _get_service(self, service: Any) -> dict[str, Any]:
        """Get service details.

        #1159 Q1：**純讀**，同 ``_list_services``。
        """
        return {
            'success': True,
            'data': self._format_service(service, include_details=True),
        }

    def _update_service(self, service: Any, values: dict[str, Any] | None, version: str | None, odoo_addon_ids: list[int] | None = None) -> dict[str, Any]:
        """Update/upgrade a service."""
        # #1159 Q30（後端守衛在 #1257）：error 按原因拆開。init 的**額度**用完
        # （確定失敗／結果未知）而轉 error 時放行 reconfigure，並一律開新的一代
        # ——v1 沒有 retry-init 按鈕，這是租戶唯一的出口。helm 失敗、等待逾時等
        # 其他 error 照擋（addon 讓 pod 起不來屬 upgrading 逾時，出口是 rollback）。
        # #1130：template 已換 woow-odoo、服務仍是 Bitnami 存量 ⇒ 在任何寫入前擋下（狀態不動）。
        legacy_error = service._woow_odoo_legacy_release_error()
        if legacy_error:
            return {'success': False, 'error': legacy_error}
        init_exhausted_error = service._init_reconfigure_exit()
        if service.state in ['pending', 'deleting'] or (
                service.state == 'error' and not init_exhausted_error):
            return {'success': False, 'error': _('Cannot update service in %(state)s state', state=service.state)}
        # #1257 Q9：lease 持有期間可能有一次 init 正在叢集內執行，reconfigure 的
        # helm upgrade 會與它互踩（也會改掉 CAS 要比對的 state）。
        if service._init_lease_active():
            return {'success': False, 'error': self._init_in_flight_error(service)}

        client = get_paas_operator_client(request.env)
        if not client:
            return {'success': False, 'error': _('PaaS Operator not configured')}

        try:
            # Update Odoo addon selection if provided
            addon_changed = False
            if odoo_addon_ids is not None and service.template_id.slug == 'odoo':
                old_addon_ids = set(service.odoo_addon_ids.ids)
                CloudOdooAddon = _PaaSCapability(request.env).odoo_addons()
                selected = CloudOdooAddon.browse(odoo_addon_ids).filtered('active')
                # Expand dependencies
                all_addons = selected
                queue = list(selected)
                while queue:
                    addon = queue.pop(0)
                    for dep in addon.depends_on:
                        if dep not in all_addons:
                            all_addons |= dep
                            queue.append(dep)
                service.write({'odoo_addon_ids': [(6, 0, all_addons.ids)]})
                addon_changed = set(all_addons.ids) != old_addon_ids

                # Server-side validation: block removal of modules still present in
                # the tenant's Odoo. **這是 authoritative 的那一道**——advisory 的
                # `api_check_addon_removal` 只是讓前端能先開對話框，任何不走預檢的呼叫
                # 端（直接 RPC、未來前端重構、預檢與此處之間的 TOCTOU 空窗）都只會撞到
                # 這裡，所以安全判準不能掛在預檢上。
                #
                # #477：兩個維度都是拒絕理由——「查不到狀態」（``can_assess``，含服務非
                # running 與 operator 回 ``success: false``）與「查到但不安全」
                # （``blocking``）。舊碼只處理後者，且對 ``success: false`` 沒有 else，
                # 那條路會掉出檢查區塊直接跑 helm upgrade；非 running 也整段跳過。
                removed_ids = old_addon_ids - set(all_addons.ids)
                if removed_ids:
                    verdict = self._assess_addon_removal(service, sorted(removed_ids))
                    if not verdict['can_assess'] or verdict['blocking']:
                        # Fail-closed: rollback the addon write and reject.
                        service.write({
                            'odoo_addon_ids': [(6, 0, sorted(old_addon_ids))]})
                        return {
                            'success': False,
                            'error': verdict['error'] or (
                                self._addon_removal_blocking_error(verdict['blocking'])),
                        }

            # Filter user values to only allowed keys, reject unauthorized
            existing_values = service._helm_values_dict()  # #559：唯一入口
            filtered_user_values, rejected_keys = service.template_id._filter_allowed_helm_values(values)
            if rejected_keys:
                return {'success': False, 'error': _('Unauthorized configuration keys: %(keys)s', keys=', '.join(rejected_keys))}
            # _init.* are one-time provisioning params consumed at creation (stored on
            # the service record, e.g. n8n_owner_password). They are NOT Helm values and
            # must never be merged into helm_values on reconfigure — doing so would
            # persist the admin password in plaintext and echo it back unmasked via the
            # service-detail API (_sanitize_helm_values masks 'secret' dicts AND
            # #357 config.sensitive.* — both secret channels).
            filtered_user_values = {k: v for k, v in filtered_user_values.items() if not k.startswith('_init.')}
            # #535：租戶原封送回的遮罩值 ＝「這格沒變更」，不是新密碼。見該 helper
            # 的 docstring（D1 實測：不擋的話存檔一次就把真憑證覆蓋成 '***'）。
            filtered_user_values = self._drop_masked_echo_values(
                filtered_user_values, existing_values, service.template_id,
            )
            # helm_value_specs 帶 ``format`` 的文字欄位（例 code-server 的 git 設定）。
            format_error = service.template_id._validate_spec_formats(filtered_user_values)
            if format_error:
                return {'success': False, 'error': format_error}
            nested_user_values = self._unflatten_dotpath_keys(filtered_user_values)
            merged_values = self._deep_merge(existing_values, nested_user_values)

            # #1130：woow-odoo 的 conf.adminEmail 與 create 同一個檢查——只在本次真的送了
            # 這一格時檢查。只改 addon／其他設定的 reconfigure 不得因為舊資料（例：#1128
            # 的 Bitnami 存量、或測試 fixture）沒有 conf.adminEmail 就被擋。
            if 'adminEmail' in (nested_user_values.get('conf') or {}):
                email_error = service._woow_odoo_admin_email_error(
                    service.template_id, merged_values)
                if email_error:
                    return {'success': False, 'error': email_error}

            # Rebuild or remove addon initContainer helm values when addon selection changed
            # #1130：woow-odoo 的 addons.selected 由 `_assemble_deploy_values` 每次從
            # odoo_addon_ids 重算，不落 helm_values（Bitnami 形的鍵在 tenant schema 會擋）。
            if addon_changed and not service.template_id._is_woow_odoo_chart():
                if service.odoo_addon_ids:
                    image_vals = merged_values.get('image', {})
                    registry = image_vals.get('registry', '')
                    repo = image_vals.get('repository', '')
                    tag = image_vals.get('tag', 'latest')
                    image_ref = f"{registry}/{repo}:{tag}" if registry else f"{repo}:{tag}"
                    addon_values = service._build_odoo_addon_helm_values(image_ref)
                    merged_values = self._deep_merge(merged_values, addon_values)
                else:
                    # All addons deselected — remove addon-specific helm values
                    service._remove_addon_helm_values(merged_values)

            # #552: reconfigure 的 chart version fallback。**主執行緒**解析，兩個
            # 消費端（下面的 write ＋ upgrade_kwargs）共用同一個值。
            #
            # `version` 這個參數**永遠是 None**：前端三個入口都不帶它
            # （services/cloud_service.js:451 的 `version = null` 預設，:458 只在
            # 有值時才塞進 params；ConfigurationTab「儲存設定」／EditDomainModal
            # 「改網域」／AddonsTab「改 addon」都只傳 3 個參數）。⇒ 送進
            # `upgrade_release` 的 `version` 恆為 None ⇒ operator 不帶 `--version`
            # ⇒ helm 解析成 registry **當下的 latest**。這條路與 template 有沒有
            # 填版號無關，**全部 15 個 template 的 reconfigure 都不釘版**：
            # mcp-* 釘 0.1.5、home-assistant 釘 0.2.1 的 pin 在這裡完全沒作用。
            #
            # fallback 順序＝「這台服務現在跑的版本」優先於「商品目前釘的版本」：
            # reconfigure 的語意是改設定、不是升版，所以先原地釘住 service 已知的
            # 版本；service 沒有記錄（legacy）才退到 template 的 pin。
            #
            # ⚠ 刻意**不**在 `PaasOperatorClient.upgrade_release` 加 fail-closed
            # guard：那條路走背景執行緒，失敗回不到前端，會讓全部 15 個商品的
            # 「儲存設定」靜默壞掉（#552 修法順序：fallback 先於 fail-closed）。
            resolved_version = (
                version
                or service.helm_chart_version
                or service.template_id.helm_chart_version
            )

            # #482: 停權中的服務 reconfigure 後不得停在 'upgrading' —— poll
            # (_cron_check_deploying_services 掃 deploying/upgrading/initializing)
            # 會把容器型一路收斂成 running＝帳面復權，而 _do_resume 的 guard 又
            # 只認停權訊號，於是租戶的服務「看起來活著」卻沒有任何訂閱在付費。
            # 本入口的守衛只擋 pending/deleting/error，suspended 是放行的，所以
            # 這條路真的可達（對照 action_redeploy 的同款收斂）。
            halted = service._is_halted()

            # Transition to upgrading BEFORE calling operator so the
            # frontend gets an immediate response with state='upgrading'.
            # #559：helm_values 這一格走提權入口，其餘欄位維持使用者身分寫入
            # （ir.rule 對它們照常生效）。兩次 write 在同一個 transaction 內。
            service._apply_helm_values(merged_values, extra_vals={
                'state': 'suspended' if halted else 'upgrading',
                # #552: 與 upgrade_kwargs 用同一個 `resolved_version`，兩處語意不
                # 分岔（原式 `version or service.helm_chart_version` 在 fallback
                # 之後恆等於 `resolved_version`）。這是升級**前**的樂觀值；成功後
                # 由背景執行緒以 operator 回報的實際版本覆蓋。
                # #559：`helm_values` 已移出這個 write，改由 `_apply_helm_values`
                # 以提權入口寫入（欄位加了 groups=，租戶身分寫不進來）。
                'helm_chart_version': resolved_version,
                # ⚠ `fields.Datetime.now()`（UTC）而**不是** `datetime.now()`
                # （本機時區）。#489 讓這個欄位從「前台顯示用」變成 age timeout
                # 的**判定用**錨：伺服器時區若不是 UTC，錨與 `fields.Datetime.now()`
                # 就不同基準——UTC+8 會讓 age 算成負的（保護靜默失效），UTC-x 會
                # 讓 age 虛增（誤殺）。repo-lint `transition-anchor-utc` 機械攔。
                'last_upgraded_at': fields.Datetime.now(),
                'error_message': False,
            })
            # #1267：**不再**把 `addon_update_pending` 覆寫成「這次有沒有改 addon」
            # ——那會把前一次 addon 變更留下的待辦、以及首次部署還欠的 init 一起蓋掉。
            # 要不要 init 由欠 init 標記決定：改 addon 或從額度出口的 error 出發才開
            # 新一代（Q24／#1159 Q30），否則沿用既有的欠 init（不開新一代）。
            if addon_changed or init_exhausted_error:
                service._open_init_generation()
            if addon_changed:
                service.write({'addon_update_pending': True})
            response_data = self._format_service(service)

            # Capture values for the background thread (no ORM access).
            db_name = request.env.cr.dbname
            service_id = service.id
            # #411: 送 operator 的 values 必須是「全量組裝」結果（含 4 injectors
            # ＋ odoo addon），因為改走 reset_values 語意後不再靠 --reuse-values
            # 保留首裝注入的 injector 值（storage_class / image_pull_secrets /
            # allowed_origins / apiserver_cidrs）——若只送 merged_values，reset 後
            # 這些會全部從 rendered manifest 消失。組裝必須在**主執行緒**先算好
            # （背景執行緒無 ORM，不能碰 service / env / ir.config_parameter）：
            # svc.helm_values 已於上方 write 更新，helper 讀到的即最新 user config。
            assembled_values = service._assemble_deploy_values()['values']
            # #1130：woow-odoo 的 conf.adminPassword 每次全量送（reset_values 下不送就
            # 變空值；chart 對既有 DB 是 no-op）。只補 odoo 這一支——MCP 家族在
            # reconfigure 缺 sensitive 的既有行為不在本票範圍。
            odoo_sensitive = service._build_odoo_sensitive_values()
            upgrade_kwargs = dict(
                namespace=service.helm_namespace,
                release_name=service.helm_release_name,
                chart=service.template_id.helm_chart_name,
                values=assembled_values,
                # #552: 見上方 `resolved_version` 的說明——這裡原本傳恆為 None 的
                # `version`，等於把版本決定權交給 registry 的 latest。
                version=resolved_version,
                reset_values=True,
            )
            if odoo_sensitive:
                upgrade_kwargs['sensitive_values'] = odoo_sensitive
            operator_url = client.base_url
            operator_api_key = client._session.headers.get('X-API-Key', '')

            def _bg_upgrade():
                """Run helm upgrade in a background thread with its own cursor."""
                import odoo
                from ..services.paas_operator import PaaSOperatorClient
                bg_client = PaaSOperatorClient(operator_url, operator_api_key)
                try:
                    release_info = bg_client.upgrade_release(**upgrade_kwargs)
                    # #482: upgrade 以 chart 預設重渲染 workload，會把停權中的
                    # 容器喚醒（實測 replicas 0→1、pod 真的 Running）。收回停機
                    # 必須在 upgrade **之後**，所以只能在這條背景執行緒裡做；
                    # halted 是主執行緒算好傳進來的（這裡沒有 ORM 可用）。
                    if halted:
                        try:
                            bg_client.scale_release(
                                upgrade_kwargs['release_name'],
                                upgrade_kwargs['namespace'],
                                replicas=0,
                            )
                        except Exception:
                            _logger.exception(
                                "Failed to scale suspended service %s back down "
                                "after upgrade — workload may still be running "
                                "(billing bypass)", service_id,
                            )
                    with odoo.registry(db_name).cursor() as cr:
                        env = odoo.api.Environment(cr, odoo.SUPERUSER_ID, {})
                        svc = env['woow_paas_platform.cloud_service'].browse(service_id)
                        # 'suspended' 一併放行：#482 之後停權服務走的是這個 state，
                        # 少了它 helm_revision 就永遠停在升級前的版本。
                        if svc.exists() and svc.state in ('upgrading', 'suspended'):
                            svc.write({
                                'helm_revision': release_info.get('revision', svc.helm_revision + 1),
                                # #552: 第四條回寫路徑。upgrade 成功後才拿得到
                                # helm 真正解析出來的 chart 版本（fallback 都空
                                # 時仍是 registry latest，此時這行是唯一知道裝了
                                # 哪一版的地方）。`or svc.helm_chart_version` ＝
                                # 對舊版 operator 向後相容：讀不到就保留原值，
                                # 不把帳本洗成空。回寫對象是 per-service 欄位，
                                # 不是 template（seed noupdate=0＋healer 會還原）。
                                'helm_chart_version': (
                                    release_info.get('chart_version')
                                    or svc.helm_chart_version
                                ),
                            })
                except Exception as exc:
                    _logger.error("Background upgrade failed for service %s: %s", service_id, exc)
                    try:
                        with odoo.registry(db_name).cursor() as cr:
                            env = odoo.api.Environment(cr, odoo.SUPERUSER_ID, {})
                            svc = env['woow_paas_platform.cloud_service'].browse(service_id)
                            if svc.exists() and svc.state in ('upgrading', 'suspended'):
                                detail = getattr(exc, 'detail', None) or str(exc)
                                vals = {'error_message': _('Upgrade failed: %(reason)s', reason=detail)}
                                # #482: 停權服務升級失敗仍該停著 —— 翻成 'error'
                                # 會讓它離開停權語意（且 lifecycle cron 會把
                                # error 當成可再 suspend 的狀態來回搬）。錯誤本身
                                # 仍寫進 error_message，不靜默。
                                if svc.state == 'upgrading':
                                    vals['state'] = 'error'
                                svc.write(vals)
                    except Exception:
                        _logger.exception("Failed to write error state for service %s", service_id)

            thread = threading.Thread(target=_bg_upgrade, daemon=True)
            thread.start()

            return {
                'success': True,
                'data': response_data,
            }

        except PaaSOperatorConnectionError:
            return {'success': False, 'error': _('Unable to connect to deployment service')}

    @staticmethod
    def _init_in_flight_error(service: Any) -> str:
        """#1257：lease 期間拒絕 reconfigure／rollback 的可讀錯誤（#1267 驗收 4）。

        措辭不說「初始化進行中」：結果未知的 attempt 轉 error 之後 lease 仍刻意留到
        到期（遠端 exec 可能還在跑，Q8），那時畫面上的服務已經是 error。
        """
        return _(
            'An initialization request for this service may still be running in the '
            'cluster. Please try again after %(until)s UTC.',
            until=fields.Datetime.to_string(service.init_lease_until),
        )

    def _delete_service(self, service: Any) -> dict[str, Any]:
        """Delete/uninstall a service."""
        if service.state == 'deleting':
            return {'success': False, 'error': _('Service is already being deleted')}

        client = get_paas_operator_client(request.env)
        if not client:
            # Block deletion - we cannot clean up K8s resources without operator
            _logger.error(
                "Cannot delete service %s: PaaS Operator not configured. "
                "Kubernetes resources will NOT be cleaned up.",
                service.name
            )
            return {
                'success': False,
                'error': _('PaaS Operator not configured. Cannot safely delete service without cleaning up Kubernetes resources. Contact administrator.'),
            }

        try:
            service.write({'state': 'deleting'})

            # Uninstall Helm release (pass subdomain for Cloudflare cleanup)
            client.uninstall_release(
                namespace=service.helm_namespace,
                release_name=service.helm_release_name,
                subdomain=service.subdomain,
            )

            # Delete the record
            service.unlink()
            return {'success': True, 'message': _('Service deleted')}

        except PaaSOperatorConnectionError:
            service.write({
                'state': 'error',
                'error_message': _('Unable to connect to deployment service during deletion'),
            })
            return {'success': False, 'error': _('Unable to connect to deployment service')}

        except PaaSOperatorError as e:
            # If release not found, still delete the record
            if e.status_code == 404:
                service.unlink()
                return {'success': True, 'message': _('Service deleted')}

            _logger.error("Operator error during uninstall: %s", str(e))
            service.write({
                'state': 'error',
                'error_message': _('Deletion failed: %(reason)s', reason=e.detail or e.message),
            })
            return {'success': False, 'error': _('Deletion failed: %(reason)s', reason=e.detail or e.message)}

    def _rollback_service(self, service: Any, revision: int | None) -> dict[str, Any]:
        """Rollback service to a previous revision."""
        # #423 W5a MAJOR-1：suspended 必須擋下。
        #
        # 語意上：停權中的服務本來就不該允許改版本。
        # 技術上更關鍵：rollback 直呼 client.rollback_release()，**全程不經
        # _assemble_deploy_values**，所以 W5a.4 的 runStrategy 同源守衛對這條
        # 路徑完全不生效。而 helm rollback 重放的是 **stored manifest**
        # （其中 runStrategy: Always），停權卻是 operator 外科式
        # `kubectl patch vm` 改 **live 物件**——兩者本就不同步（同
        # reference_helm_resource_policy_stored_manifest 的既有教訓）。
        # 結果就是欠費停權的 VM 被喚醒並繼續計費。
        if service.state in ['pending', 'deleting', 'suspended']:
            return {'success': False, 'error': _('Cannot rollback service in %(state)s state', state=service.state)}
        # #1257 Q23：lease 持有期間拒絕（同 reconfigure 的理由）。
        if service._init_lease_active():
            return {'success': False, 'error': self._init_in_flight_error(service)}

        client = get_paas_operator_client(request.env)
        if not client:
            return {'success': False, 'error': _('PaaS Operator not configured')}

        try:
            client.rollback_release(
                namespace=service.helm_namespace,
                release_name=service.helm_release_name,
                revision=revision,
            )

            # #489：rollback 也是一次 helm 寫入，`last_upgraded_at` 是它的時間
            # 錨。不記的話 age timeout 會退回首裝時間，rollback 一發出就被判逾時。
            # （`fields.Datetime.now()` 的 UTC 基準要求見 `_update_service`。）
            service.write({
                'state': 'upgrading',
                'error_message': False,
                'last_upgraded_at': fields.Datetime.now(),
            })
            # #1257 Q23／#1267：還欠 init 的服務（含 init 額度用完的 error）rollback
            # 開新的一代——保留欠 init、計數歸零，下一輪導回 initializing。之前只寫
            # upgrading、不設 `addon_update_pending`，首次部署 init 失敗、已轉 error 的
            # 服務 rollback 後會被 poll 直接寫成 running。
            # init 已完成的服務**不**開新一代（M2 裁定 (A)，修訂 Q24 字面，守住 #1267
            # 驗收 5）：否則 n8n 每次 rollback 都多建一把 API key 並重啟，租戶改過 owner
            # 密碼時還會 login 失敗到額度用完、沒有出口。代價：Odoo rollback 到 addon
            # 組合不同的 revision 不會自動 update_list——與 #1257 以前的行為一致。
            if service.init_pending:
                service._open_init_generation()

            return {'success': True, 'message': _('Rollback to revision %(revision)s initiated', revision=revision)}

        except PaaSOperatorConnectionError:
            return {'success': False, 'error': _('Unable to connect to deployment service')}

        except PaaSOperatorError as e:
            _logger.error("Operator error during rollback: %s", str(e))
            return {'success': False, 'error': _('Rollback failed: %(reason)s', reason=e.detail or e.message)}

    def _get_service_revisions(self, service: Any) -> dict[str, Any]:
        """Get revision history for a service."""
        client = get_paas_operator_client(request.env)
        if not client:
            return {'success': False, 'error': _('PaaS Operator not configured')}

        try:
            result = client.get_revisions(
                namespace=service.helm_namespace,
                release_name=service.helm_release_name,
            )

            return {
                'success': True,
                'data': result.get('revisions', []),
            }

        except PaaSOperatorConnectionError:
            return {'success': False, 'error': _('Unable to connect to deployment service')}

        except PaaSOperatorError as e:
            if e.status_code == 404:
                return {'success': True, 'data': []}
            return {'success': False, 'error': _('Failed to get revisions: %(reason)s', reason=e.detail or e.message)}

    def _compute_sidecar_url_hint(self, service: Any) -> str:
        """Compute the MCP endpoint URL for a service's sidecar.

        Prefers the cluster-internal K8s service address: it stays in-cluster,
        never traverses the CF edge, and so cannot hit CF Access (#271). Falls
        back to subdomain, then to a localhost placeholder the user must edit.

        NOTE (W0 #283, 2026-07-05): the operator DOES publish a public
        ``{subdomain}-mcp`` CF route for the sidecar (see #301) — the earlier
        "CF only routes the main app port, not the sidecar" rationale was wrong.
        That public route is dead weight (the agent data-plane uses this
        in-cluster hint); prefer in-cluster so the agent path never regresses.
        """
        template = service.template_id
        endpoint_path = template.mcp_endpoint_path or '/mcp'
        sidecar_port = template.mcp_sidecar_port or 3001

        # Prefer the Kubernetes internal service URL: in-cluster, off the CF
        # edge, so it never hits CF Access (#271) and carries no public attack
        # surface. (W0 #283, 2026-07-05: the operator DOES build a public
        # {subdomain}-mcp CF route — see #301 — so this is NOT "the sidecar
        # port isn't routed"; it's "in-cluster is the correct path".)
        # Pattern: http://{release}-mcp.{namespace}.svc.cluster.local:{port}{path}
        if service.helm_release_name and service.helm_namespace:
            return (
                f"http://{service.helm_release_name}-mcp"
                f".{service.helm_namespace}.svc.cluster.local"
                f":{sidecar_port}{endpoint_path}"
            )

        # Fallback: construct from subdomain (user can update later)
        if service.subdomain:
            IrConfigParameter = request.env['ir.config_parameter'].with_context({}).sudo()
            paas_domain = IrConfigParameter.get_param(
                'woow_paas_platform.paas_domain', 'woowtech.io',
            )
            return f"https://{service.subdomain}.{paas_domain}{endpoint_path}"

        # Last resort: placeholder that the user must update
        return f"http://localhost:{sidecar_port}{endpoint_path}"

    def _format_service(self, service: Any, include_details: bool = False) -> dict[str, Any]:
        """Format a service record for API response."""
        # Map 'pending' to 'deploying' for UI — pending is an internal
        # state used by the cron as a pick-up lock; users should see 'deploying'.
        ui_state = 'deploying' if service.state == 'pending' else service.state

        # #1159：新轉移等待真 poll；比較規則由 model 定義，formatter／monitor 不另猜時計。
        # 此呼叫只讀已存值，不探測、不寫入。
        observation_pending, observation_freshness_at = service._observation_freshness()
        observation_age_seconds = (
            max(0, int((fields.Datetime.now() - observation_freshness_at).total_seconds()))
            if observation_freshness_at else None)

        data = {
            'id': service.id,
            'name': service.name,
            'state': ui_state,
            'subdomain': service.subdomain or '',
            'custom_domain': service.custom_domain or '',
            'error_message': service.error_message or '',
            # #423 W5a：VM 首次開通的磁碟 import 進度（容器型恆為空）。
            # 純顯示用，不參與狀態機。
            'import_progress': service.import_progress or '',
            # #648 (b)：服務已就緒、但寬限期內都沒能解析到 Public URL 的 DNS
            # record，是**帶著保留**放行的。前端據此在 Public URL 旁掛一句軟提示。
            #
            # ⚠ 放在 base dict 而**不是** `include_details` 區塊：列表頁走的是不帶
            # details 的那條路（`_list_services`），放錯地方的話 ServiceCard 永遠拿
            # 不到這個值——而使用者最先看到服務狀態的地方就是列表卡片。
            #
            # ⚠ 刻意**不**併進 `error_message`：那個通道在前端是紅色故障 banner，
            # 而且 ServiceDetailPage 的 `t-if="service.error_message"` 不判 state
            # ⇒ 一台健康的服務會冒出紅底錯誤。
            'public_url_dns_unverified': service.public_url_dns_unverified,
            # observed_at 保留真 poll 證據；新 episode 的 age 從等待邊界起算，
            # 不把舊證據立刻報成新轉移的 stale。consumer 依 pending 旗標分別呈現
            # 「等待更新」與「最後更新」，list/get 仍只讀已存值。
            'observed_at': service.observed_at.isoformat() if service.observed_at else None,
            'observation_pending': observation_pending,
            'observed_age_seconds': observation_age_seconds,
            'init_phase': service._init_phase() or None,
            # Q30：init 的出口與 attempt 序號（租戶看固定文案＋序號，operator 原始錯誤只在
            # attempt 記錄）；`init_reconfigure_allowed` 與 `_update_service` 的守衛同一判準。
            'init_exit': service.init_exit or None,
            'init_attempt_seq': service.init_attempt_seq,
            'init_reconfigure_allowed': service._init_reconfigure_exit(),
            'has_project': bool(service.project_ids),
            # #530：前端「Reset 會一併換發 Gateway API Key」那句文案的唯一資料入口
            # （ServiceDetailPage 的 hasGateway getter）。值＝**交付判準本體**
            # （service._gateway_public_url() 非空），與 _gateway_credentials 讀同一個
            # method——不得改判 template.has_admin_gui：那些模板裡只有 hermes-agent
            # 真的有 gateway，用它當 gate 等於對其餘模板的租戶宣告一件永遠不會
            # 發生的事。放在 service 層而非 template 子物件，因為判準含「這台服務有沒有
            # 對外根位址」（subdomain），那是 service 的屬性不是 template 的。
            'has_gateway': bool(service._gateway_public_url()),
            'template': {
                'id': service.template_id.id,
                'name': service.template_id.name,
                'slug': service.template_id.slug or '',
                'category': service.template_id.category,
                # #460 S1：前端 Reset 密碼按鈕的顯示判準（ServiceDetailPage 的
                # hasAdminGui getter）唯一的資料入口。**直接鏡射模板欄位、不做任何
                # 推導**——與 reset endpoint 的授權判準（:1293 的
                # service.template_id.has_admin_gui，#357 C01）收斂到同一欄位。
                # 前端原本自建 slug 白名單，與這裡不一致 → 後端放行、按鈕卻不出現。
                'has_admin_gui': bool(service.template_id.has_admin_gui),
                # 「Admin Credentials」按鈕的顯示判準（ServiceDetailPage 的
                # adminResetSupported getter）：鏡射模板的 admin_reset_supported，
                # 與上方 reset endpoint 的能力 gate 讀同一欄位。app_managed 的模板
                # （omnigent）has_admin_gui=True（開通時照樣供給、一次性顯示初始密碼）
                # 但平台不能重設，按鈕不該出現。
                'admin_reset_supported': bool(service.template_id.admin_reset_supported),
                # 「租戶能不能自設帳密」的前端判準（ServiceDetailPage 的
                # canEditAdminCredentials）：直接鏡射模板欄位，與
                # _service_admin_credentials 的能力 gate 讀同一個欄位。
                'admin_username_editable': bool(service.template_id.admin_username_helm_path),
                # 「Admin Credentials」對話框（自設憑證）的顯示判準：鏡射模板的
                # admin_credentials_settable，與 admin-credentials endpoint 的能力 gate
                # 讀同一欄位。只有密碼的模板（code-server）為 True、但
                # admin_username_editable 為 False（對話框不出現帳號欄）。
                'admin_credentials_settable': bool(
                    service.template_id.admin_credentials_settable),
                # #1370：Reset 走 operator exec 改 app DB（Odoo／n8n）時的種類；前端用它
                # 挑文案（不重啟、立刻生效、2FA 已關），不自建 slug 判準。
                'app_admin_reset_kind': service.template_id.app_admin_reset_kind or 'none',
                'helm_value_specs': service.template_id._parse_helm_value_specs(),
            },
            # 目前生效的 admin 帳號（非敏感；不開放改帳號的模板為 None），供對話框預填。
            'admin_username': service._admin_username(),
            'helm_revision': service.helm_revision,
            'created_date': service.create_date.isoformat() if service.create_date else None,
            'deployed_at': service.deployed_at.isoformat() if service.deployed_at else None,
            'odoo_addons': [
                {'id': a.id, 'name': a.name, 'technical_name': a.technical_name}
                for a in service.odoo_addon_ids
            ] if service.odoo_addon_ids else [],
            'subscription': service._get_subscription_info(),
        }

        if include_details:
            data.update({
                'helm_namespace': service.helm_namespace,
                'helm_release_name': service.helm_release_name,
                'helm_chart_version': service.helm_chart_version,
                # #627 (i)/E：讓「這個版本號多新」對租戶可見。B 那條對帳 cron 每日
                # 一次 ⇒ 最多一個週期的偵測延遲，這個欄位就是那個延遲的誠實揭露，
                # 不是裝飾——沒有它，平台等於假裝自己一直是準的。
                # #917：這格原本漏了 `.isoformat()`，裸 datetime 進 JSON ⇒ 落到
                # Odoo 的 `json_default`，序列化成 SQL 空格格式（`2026-08-26 12:34:56`），
                # 與同一份 payload 其他 datetime 欄位的 ISO `T` 格式不一致，
                # 而前端那格又是裸 `t-esc` ⇒ 原始字串直接印給租戶。
                'last_release_sync_at': (
                    service.last_release_sync_at.isoformat()
                    if service.last_release_sync_at else None
                ),
                # #559：helm_values 有 groups='base.group_system'，屬性直讀會
                # AccessError。走 model 的唯一入口——租戶看得到的仍是遮罩後的值
                # （#535），只是 ORM 直讀那條側門被關上了。
                'helm_values': (
                    self._portal_config_values(service._helm_values_dict(), service.template_id)
                    if request.env.user.share else self._sanitize_helm_values(
                        service._helm_values_dict(), service.template_id)
                ),
                # #745：Access 設定（拍板 10 的欄位組）。只掛在 detail 那一側
                # ——Access 頁籤只存在於 detail 頁，而 list 端點會為一個
                # workspace 的**每一台**服務各 walk 一次 `access_policy_ids`。
                # 顯示條件本身（`ac_max_access_seats > 0`）走 `subscription`，
                # 那個 key 兩側都有，所以精簡版不會因此少掉判斷依據。
                'access': service._access_to_dict(),
                # 對外 MQTT 位址（charts/emqx）。能力旗標與值分開給：旗標決定前端
                # 要不要畫這一格（空位址時顯示「尚未取得」而不是整格消失——那會讓
                # 租戶以為自己沒開成功），值是背景 poll 回讀到的最後一筆。
                # ⚠ 前端的 gate 必須讀 `has_public_mqtt_address`，不得用 slug 判
                # （repo-lint 第 7 條；見 cloud_app_template 同名欄位註解）。
                'has_public_mqtt_address': service._chart_has_public_mqtt_address(),
                'mqtt_public_url': service.mqtt_public_url or None,
                'internal_port': service.internal_port,
                'allocated_vcpu': service.allocated_vcpu,
                'allocated_ram_gb': service.allocated_ram_gb,
                'allocated_storage_gb': service.allocated_storage_gb,
                'last_upgraded_at': service.last_upgraded_at.isoformat() if service.last_upgraded_at else None,
            })

            # Expose n8n login credentials if applicable
            template = service.template_id
            if template.post_deploy_init_type == 'n8n' and template.post_deploy_init_email:
                data['n8n_credentials'] = {
                    'email': template.post_deploy_init_email,
                    'info': 'Password was auto-generated during deployment. Check service logs or redeploy to reset.',
                }

        return data
