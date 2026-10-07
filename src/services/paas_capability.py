"""Private /woow capability boundary; deliberately NOT an ORM/RPC API.

Portal has no PaaS ACL. Only the three product controllers and the HA REST
API (``controllers/ha_api.py``, membership/role checks for the OAuth token's
user, #1528) use this seam; the OAuth endpoints themselves and
model.check_user_access retain their historical contracts.
Internal callers keep their actual ORM environment/rules. Portal lookups use
fixed models and server-owned anchors, never a client model/domain/context.
Return recordsets only to controller code, which must serialize explicit DTOs
and retain its operation-specific role, subscription and lifecycle gates.
"""
from odoo.exceptions import AccessError

from ..models.workspace_access import ROLE_ADMIN, ROLE_HIERARCHY


class _PaaSCapability:
    def __init__(self, env):
        # Capture the principal BEFORE any sudo. Never replace request.env.
        self.env = env
        self.uid = env.uid
        self.user = env.user
        self.authenticated = bool(self.uid and self.user.active
                                  and not self.user._is_public())
        self.portal = self.authenticated and self.user.share
        # Preserve locale from the authenticated profile, never client context
        # (default_* and identity/company overrides are not capabilities).
        self._context = {'lang': self.user.lang or 'en_US', 'tz': self.user.tz}

    def _require_authenticated(self):
        if not self.authenticated:
            raise AccessError('Authentication required')

    def access(self, workspace, required_role=None):
        if not self.authenticated or not workspace:
            return False
        if not self.portal:
            # Do not inherit a sudo environment from a caller's recordset.
            return workspace.with_env(self.env).check_user_access(
                self.user, required_role=required_role)
        access = self.env['woow_paas_platform.workspace_access'].with_context(self._context).sudo().search([
            ('workspace_id', '=', workspace.id),
            ('workspace_id.state', '=', 'active'),
            ('user_id', '=', self.uid),
            ('user_id.active', '=', True),
        ], limit=1)
        if not access or access.role not in ROLE_HIERARCHY:
            return False
        if required_role and ROLE_HIERARCHY.index(access.role) < ROLE_HIERARCHY.index(required_role):
            return False
        return access

    def memberships(self):
        self._require_authenticated()
        Access = self.env['woow_paas_platform.workspace_access']
        if self.portal:
            Access = Access.with_context(self._context).sudo()
        return Access.search([
            ('user_id', '=', self.uid), ('user_id.active', '=', True),
            ('workspace_id.state', '=', 'active'),
        ])

    def workspace(self, workspace_id, required_role=None):
        Workspace = self.env['woow_paas_platform.workspace']
        if not self.authenticated:
            return Workspace.browse()
        candidate = Workspace.browse(workspace_id)
        if not candidate.exists() or not self.access(candidate, required_role):
            return Workspace.browse()
        if self.portal:
            candidate = candidate.with_context(self._context).sudo()
        return candidate

    def create_workspace(self, name, description):
        self._require_authenticated()
        Workspace = self.env['woow_paas_platform.workspace']
        if self.portal:
            Workspace = Workspace.with_context(self._context).sudo()
        return Workspace.create({
            'name': name, 'description': description, 'owner_id': self.uid,
        })

    def member(self, workspace, member_id):
        workspace = self.workspace(workspace.id, ROLE_ADMIN)
        if not workspace:
            return self.env['woow_paas_platform.workspace_access'].browse()
        return workspace.env['woow_paas_platform.workspace_access'].search([
            ('id', '=', member_id), ('workspace_id', '=', workspace.id),
        ], limit=1)

    def cloud_service(self, service_id, workspace_id=None):
        Service = self.env['woow_paas_platform.cloud_service']
        self._require_authenticated()
        if self.portal:
            # Anchor discovery only; no fields escape before membership proof.
            Service = Service.with_context(self._context).sudo()
        domain = [('id', '=', service_id)]
        if workspace_id is not None:
            domain.append(('workspace_id', '=', workspace_id))
        service = Service.search(domain, limit=1)
        if not service:
            return Service.browse()
        if not self.workspace(service.workspace_id.id):
            return Service.browse()
        if service.template_id.conceal_instances:
            return Service.browse()
        return service

    def smart_home(self, workspace, home_id):
        workspace = self.workspace(workspace.id)
        if not workspace:
            return self.env['woow_paas_platform.smart_home'].browse()
        return workspace.env['woow_paas_platform.smart_home'].search([
            ('id', '=', home_id), ('workspace_id', '=', workspace.id),
        ], limit=1)

    def security_access(self, workspace, sa_id):
        workspace = self.workspace(workspace.id)
        if not workspace:
            return self.env['woow_paas_platform.security_access'].browse()
        return workspace.env['woow_paas_platform.security_access'].search([
            ('id', '=', sa_id), ('workspace_id', '=', workspace.id),
        ], limit=1)

    def smart_home_route(self, home, route_id):
        home = self.smart_home(home.workspace_id, home.id)
        if not home:
            return self.env['woow_paas_platform.smart_home_route'].browse()
        return home.env['woow_paas_platform.smart_home_route'].search([
            ('smart_home_id', '=', home.id), ('id', '=', route_id),
        ], limit=1)

    def security_access_route(self, sa, route_id):
        sa = self.security_access(sa.workspace_id, sa.id)
        if not sa:
            return self.env['woow_paas_platform.security_access_route'].browse()
        return sa.env['woow_paas_platform.security_access_route'].search([
            ('security_access_id', '=', sa.id), ('id', '=', route_id),
        ], limit=1)

    def cloud_policy(self, service, policy_id):
        service = self.cloud_service(service.id, service.workspace_id.id)
        Policy = self.env['woow_paas_platform.security_access_access_policy']
        if not service:
            return Policy.browse()
        return service.env[Policy._name].search([
            ('cloud_service_id', '=', service.id), ('id', '=', policy_id),
        ], limit=1)

    def smart_home_policy(self, home, policy_id):
        home = self.smart_home(home.workspace_id, home.id)
        Policy = self.env['woow_paas_platform.security_access_access_policy']
        if not home:
            return Policy.browse()
        return home.env[Policy._name].search([
            ('smart_home_id', '=', home.id), ('id', '=', policy_id),
        ], limit=1)

    def security_access_policy(self, sa, policy_id):
        sa = self.security_access(sa.workspace_id, sa.id)
        Policy = self.env['woow_paas_platform.security_access_access_policy']
        if not sa:
            return Policy.browse()
        return sa.env[Policy._name].search([
            ('security_access_id', '=', sa.id), ('id', '=', policy_id),
        ], limit=1)

    # Rules have no by-id BFF API: controllers build only (0, 0, whitelist)
    # commands under a resolved policy. No caller-supplied ORM commands/IDs.

    def cloud_templates(self):
        self._require_authenticated()
        Template = self.env['woow_paas_platform.cloud_app_template']
        return Template.with_context(self._context).sudo() if self.portal else Template

    def odoo_addons(self):
        self._require_authenticated()
        Addon = self.env['woow_paas_platform.cloud_odoo_addon']
        return Addon.with_context(self._context).sudo() if self.portal else Addon
