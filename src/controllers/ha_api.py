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
            return _json_error('Forbidden', 'No access to this workspace', 403)

        homes = request.env['woow_paas_platform.smart_home'].sudo().search([
            ('workspace_id', '=', workspace.id),
        ])
        data = [
            {
                'id': h.id,
                'name': h.name,
                'state': h.state,
                'subdomain': h.subdomain or '',
                'tunnel_status': h.tunnel_status or 'disconnected',
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

        return _json_response({'home': home.to_dict()})

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
        })

    # ==================== Status Refresh ====================

    @route(
        '/api/smarthome/homes/<int:smarthome_id>/status',
        type='http', auth='none', methods=['GET'], csrf=False,
    )
    def get_home_status(self, smarthome_id: int, **kwargs: Any) -> Response:
        """Refresh and return the current tunnel status of a smart home.

        Required scope: ``smarthome:read``

        Args:
            smarthome_id: Smart home database ID.

        Returns:
            JSON with ``home`` object containing updated status fields.
        """
        result = _authenticate(required_scopes=['smarthome:read'])
        if isinstance(result, Response):
            return result
        user, _token = result

        home = self._get_home_with_access(user, smarthome_id)
        if isinstance(home, Response):
            return home

        try:
            home.action_refresh_status()
        except Exception:
            _logger.exception(
                "Failed to refresh status for Smart Home id=%s", smarthome_id,
            )
            # Return current state even if refresh fails; the model
            # already updates tunnel_status to 'error' on failure.

        return _json_response({'home': home.to_dict()})

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
            return _json_error('Forbidden', 'No access to this workspace', 403)
        records = request.env['woow_paas_platform.security_access'].sudo().search([
            ('workspace_id', '=', workspace.id),
        ])
        data = []
        for sa in records:
            sub_info = sa._get_subscription_info()
            data.append({
                'id': sa.id,
                'name': sa.name,
                'canonical_name': sa.canonical_name or '',
                'state': sa.state,
                'tunnel_status': sa.tunnel_status or 'disconnected',
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
        return _json_response({'access': sa.to_dict()})

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
        return _json_response({
            'tunnel_token': sa.tunnel_token or '',
            'tunnel_id': sa.tunnel_id or '',
        })

    @route(
        '/api/security-access/accesses/<int:sa_id>/status',
        type='http', auth='none', methods=['GET'], csrf=False,
    )
    def sa_get_status(self, sa_id: int, **kwargs: Any) -> Response:
        """Refresh + return connector status. Scope: security-access:read."""
        result = _authenticate(required_scopes=['security-access:read'])
        if isinstance(result, Response):
            return result
        user, _token = result
        sa = self._get_sa_with_access(user, sa_id)
        if isinstance(sa, Response):
            return sa
        try:
            sa.action_refresh_status()
        except Exception:
            _logger.exception(
                "Failed to refresh status for Security Access id=%s", sa_id)
        return _json_response({'access': sa.to_dict()})

    def _get_sa_with_access(self, user: Any, sa_id: int) -> Any | Response:
        """Look up a security access and verify workspace access."""
        sa = request.env['woow_paas_platform.security_access'].sudo().browse(
            sa_id).exists()
        if not sa:
            return _json_error('Not Found', 'Security access not found', 404)
        workspace = _check_workspace_access(user, sa.workspace_id.id)
        if not workspace:
            return _json_error(
                'Forbidden',
                'No access to the workspace containing this security access',
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
            return _json_error('Not Found', 'Smart home not found', 404)

        workspace = _check_workspace_access(user, home.workspace_id.id)
        if not workspace:
            return _json_error(
                'Forbidden',
                'No access to the workspace containing this smart home',
                403,
            )

        return home
