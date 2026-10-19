"""Service-first onboarding for the HA integration (Bearer, explicit write scope)."""
import json

from psycopg2 import IntegrityError, OperationalError

from odoo.http import Controller, Response, request, route
from odoo.exceptions import UserError

from .ha_api import _authenticate, _capability, _json_error, _json_response
from .smart_home import SmartHomeController
from ..models.workspace_access import ROLE_ADMIN

_KINDS = {
    'remote_access': ('plan_smarthome_monthly', '遠端存取'),
    'remote_access_mcp': ('plan_smarthome_mcp_monthly', '遠端存取＋MCP'),
}


def _kind(home):
    # Classification survives trial expiry. Entitlement is checked separately.
    plan = home.sudo().subscription_id.sh_subscription_plan_id
    return 'remote_access_mcp' if plan.mcp_integration else 'remote_access'


def _home_dto(home):
    return {
        'id': home.id, 'name': home.name, 'state': home.state,
        'service_kind': _kind(home),
        'binding_state': home.binding_state,
        'bound_instance_label': home.bound_instance_label or '',
        'subscription': home._get_subscription_info(),
    }


class HAOnboardingController(Controller):
    @route('/api/smarthome/onboarding/options', type='http', auth='none',
           methods=['GET'], csrf=False)
    def options(self, service_kind=None, workspace_id=None, **kwargs):
        result = _authenticate(['workspace:read', 'smarthome:read'])
        if isinstance(result, Response):
            return result
        user, _token = result
        if not isinstance(service_kind, str) or service_kind not in _KINDS:
            return _json_error('invalid_service_kind', '請選擇要使用的服務。')
        capability = _capability(user)
        if not capability.authenticated:
            return _json_error('Forbidden', '請重新登入。', 403)
        memberships = capability.memberships()
        # Every returned workspace is a membership, then individually role-gated.
        workspaces = memberships.mapped('workspace_id').filtered(
            lambda ws: capability.access(ws, ROLE_ADMIN))
        body = {
            'workspaces': [{'id': ws.id, 'name': ws.name} for ws in workspaces],
            'has_memberships': bool(memberships),
            'service_kind': service_kind,
        }
        if workspace_id is not None:
            try:
                selected_id = int(workspace_id)
            except (TypeError, ValueError):
                return _json_error('invalid_workspace', '工作區無效。')
            if not 0 < selected_id <= 2147483647:
                return _json_error('invalid_workspace', '工作區無效。')
            workspace = capability.workspace(selected_id, ROLE_ADMIN)
            if not workspace or workspace.state != 'active':
                return _json_error('insufficient_role', '需要工作區擁有者或管理員權限。', 403)
            homes = workspace.env['woow_paas_platform.smart_home'].search([
                ('workspace_id', '=', workspace.id), ('state', '!=', 'deleting'),
            ]).filtered(lambda home: _kind(home) == service_kind)
            body['homes'] = [_home_dto(home) for home in homes]
        return _json_response(body)

    @route('/api/smarthome/onboarding', type='http', auth='none',
           methods=['POST'], csrf=False, readonly=False)
    def provision(self, **kwargs):
        result = _authenticate(['workspace:read', 'smarthome:read', 'smarthome:provision'])
        if isinstance(result, Response):
            return result
        user, _token = result
        try:
            payload = json.loads(request.httprequest.get_data(as_text=True))
        except (ValueError, TypeError):
            return _json_error('invalid_request', '無法讀取開通資料。')
        if not isinstance(payload, dict):
            return _json_error('invalid_request', '開通資料必須是物件。')
        kind = payload.get('service_kind')
        if not isinstance(kind, str) or kind not in _KINDS:
            return _json_error('invalid_service_kind', '請選擇要使用的服務。')
        workspace_id = payload.get('workspace_id')
        if workspace_id is not None and (
                type(workspace_id) is not int or not 0 < workspace_id <= 2147483647):
            return _json_error('invalid_workspace', '工作區無效。')
        # auth='none' must acquire the token principal before creating records.
        request.update_env(user=user.id, context={'lang': 'zh_TW'})
        capability = _capability(user)
        if not capability.authenticated:
            return _json_error('Forbidden', '請重新登入。', 403)
        try:
            # This section only writes DB intent. The setup cron invokes the
            # existing provision path AFTER this request commits.
            with request.env.cr.savepoint():
                # Reduce contention; the unique onboarding key is the final
                # concurrency guard even when an older snapshot waits here.
                request.env.cr.execute('SELECT pg_advisory_xact_lock(%s, %s)',
                                       (174901, int(workspace_id or -user.id)))
                memberships = capability.memberships()
                workspace = None
                created_workspace = False
                if workspace_id is not None:
                    workspace = capability.workspace(workspace_id, ROLE_ADMIN)
                    if not workspace or workspace.state != 'active':
                        return _json_error('insufficient_role', '需要工作區擁有者或管理員權限。', 403)
                elif memberships:
                    # Never pick another person's workspace or bypass a member's
                    # role by silently creating a new workspace.
                    return _json_error('select_workspace', '請先選擇工作區。', 409)

                plan_id, label = _KINDS[kind]
                template = request.env.ref('woow_paas_platform.smart_home_home_assistant').sudo()
                plan = request.env.ref('woow_paas_platform.' + plan_id).sudo()
                if (not template.is_active or plan not in template.subscription_plan_ids
                        or plan.storefront_hidden or not plan.sh_trial
                        or plan.sh_trial_unit != 'day' or plan.sh_trial_duration != 7):
                    return _json_error('trial_unavailable', '目前無法開通七天試用，請稍後再試。', 503)
                if workspace is None:
                    workspace = capability.create_workspace('我的智慧家庭', '從 Home Assistant 建立')
                    created_workspace = True
                homes = workspace.env['woow_paas_platform.smart_home'].search([
                    ('workspace_id', '=', workspace.id), ('state', '!=', 'deleting'),
                ]).filtered(lambda home: _kind(home) == kind)
                if homes:
                    return _json_response({
                        'created': False, 'workspace': {'id': workspace.id, 'name': workspace.name},
                        'homes': [_home_dto(home) for home in homes],
                    })
                key = (f'new-workspace:{user.id}' if created_workspace
                       else f'workspace:{workspace.id}:{kind}')
                # Server-authoritative create only after the membership/role
                # gate; factory keeps the existing template, plan, order and
                # subscription guards. No caller context/plan/name is forwarded.
                made = SmartHomeController()._create_smarthome(
                    workspace.sudo(), f'我的智慧家庭・{label}', 8123,
                    plan.id, template.id, trial_only=True, onboarding_key=key)
                if not made.get('success'):
                    raise UserError(made.get('error') or '無法建立試用服務。')
                home = capability.smart_home(workspace, made['data']['id'])
                home.sudo()._request_ha_setup()
                return _json_response({
                    'created': True, 'workspace': {'id': workspace.id, 'name': workspace.name},
                    'homes': [_home_dto(home)],
                }, status=202)
        except IntegrityError as exc:
            if exc.diag.constraint_name == 'woow_paas_platform_smart_home_ha_onboarding_key_unique':
                return _json_error('onboarding_retry', '服務已在另一個請求建立，請重試以繼續連結。', 409)
            raise
        except OperationalError:
            raise  # Let Odoo retry using a fresh snapshot.
        except UserError as exc:
            return _json_error('onboarding_failed', str(exc))

    @route('/api/smarthome/onboarding/homes/<int:home_id>/prepare',
           type='http', auth='none', methods=['POST'], csrf=False, readonly=False)
    def prepare(self, home_id, **kwargs):
        """Request background setup without creating another service."""
        result = _authenticate(['smarthome:read', 'smarthome:provision'])
        if isinstance(result, Response):
            return result
        user, _token = result
        capability = _capability(user)
        # Anchor discovery is internal and is not serialized before role proof.
        home = request.env['woow_paas_platform.smart_home'].sudo().browse(home_id).exists()
        if not home or not capability.workspace(home.workspace_id.id, ROLE_ADMIN):
            return _json_error('insufficient_role', '無法管理此服務。', 403)
        request.update_env(user=user.id, context={'lang': 'zh_TW'})
        home = home.with_env(request.env).sudo()
        try:
            home._check_operation_allowed()
            if home.state in ('pending', 'error'):
                home._request_ha_setup()
        except UserError as exc:
            return _json_error('service_unavailable', str(exc), 409)
        return _json_response({'home': _home_dto(home)}, status=202 if home.state != 'active' else 200)
