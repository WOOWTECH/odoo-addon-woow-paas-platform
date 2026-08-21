"""TDD red phase tests — cloud_service ir.rule workspace isolation (AC #7-8).

Plan: docs/refactor/plan-cloud-service-project-decouple.md (D8=A)

Current state (red expectation):
- ``src/security/ir.model.access.csv:7`` grants base.group_user RWCU on
  ``woow_paas_platform.cloud_service``.
- ``src/security/ir_rules.xml`` has *no* record rule for cloud_service.
- AC #7 violates: any internal user can read every workspace's services
  via ORM (controller filter is the only barrier; ORM is wide-open).
- AC #8 must remain green after D8=A is applied — the controller endpoint
  is the regression line.
"""
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase
from odoo.exceptions import AccessError
from ..models.cloud_service import _internal_provision


class TestCloudServiceWorkspaceRule(TransactionCase):
    """AC #7 — cross-workspace user must not read/write/unlink other ws services."""

    def setUp(self):
        super().setUp()
        self.Service = self.env['woow_paas_platform.cloud_service']
        self.Template = self.env['woow_paas_platform.cloud_app_template']
        self.Workspace = self.env['woow_paas_platform.workspace']
        self.Access = self.env['woow_paas_platform.workspace_access']
        User = self.env['res.users']

        group_user = self.env.ref('base.group_user')

        self.alice = User.create({
            'login': 'cs_acl_alice',
            'name': 'Alice CS-ACL',
            'email': 'alice-cs-acl@test.example',
            'groups_id': [(6, 0, [group_user.id])],
        })
        self.bob = User.create({
            'login': 'cs_acl_bob',
            'name': 'Bob CS-ACL',
            'email': 'bob-cs-acl@test.example',
            'groups_id': [(6, 0, [group_user.id])],
        })

        # Workspace W1 owned by Alice; W2 owned by Bob.
        self.w1 = self.Workspace.create({
            'name': 'W1 CS-ACL', 'owner_id': self.alice.id,
        })
        self.w2 = self.Workspace.create({
            'name': 'W2 CS-ACL', 'owner_id': self.bob.id,
        })

        self.template = self.Template.sudo().create({
            'name': 'Tmpl CS-ACL',
            'slug': 'tmpl-cs-acl',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'tmpl-cs-acl',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
        })

        self.svc_w1 = _internal_provision(self.Service.sudo()).create({
            'name': 'Svc in W1',
            'workspace_id': self.w1.id,
            'template_id': self.template.id,
        })
        self.svc_w2 = _internal_provision(self.Service.sudo()).create({
            'name': 'Svc in W2',
            'workspace_id': self.w2.id,
            'template_id': self.template.id,
        })

    # ---------------- AC #7 read ---------------- #
    def test_ac7_bob_cannot_read_w1_service_via_orm(self):
        """AC #7 read: search must not return cross-workspace services."""
        visible = self.Service.with_user(self.bob).search([
            ('id', '=', self.svc_w1.id),
        ])
        self.assertFalse(
            visible,
            "AC #7 violated: Bob (W2 only) saw a W1 cloud_service. "
            "Need ir.rule `cloud_service_rule_workspace_member`.",
        )

    def test_ac7_bob_browse_w1_read_raises_access_error(self):
        """AC #7 read: explicit browse().read() must raise AccessError."""
        svc_as_bob = self.Service.with_user(self.bob).browse(self.svc_w1.id)
        with self.assertRaises(AccessError):
            svc_as_bob.read(['name'])

    # ---------------- AC #7 write ---------------- #
    def test_ac7_bob_cannot_write_w1_service_via_orm(self):
        """AC #7 write: write on cross-workspace service must raise."""
        svc_as_bob = self.Service.with_user(self.bob).browse(self.svc_w1.id)
        with self.assertRaises(AccessError):
            svc_as_bob.write({'name': 'Bob hijacked W1'})

    # ---------------- AC #7 unlink ---------------- #
    def test_ac7_bob_cannot_unlink_w1_service_via_orm(self):
        """AC #7 unlink: unlink on cross-workspace service must raise."""
        svc_as_bob = self.Service.with_user(self.bob).browse(self.svc_w1.id)
        with self.assertRaises(AccessError):
            svc_as_bob.unlink()

    # ---------------- AC #7 own-workspace positive ---------------- #
    def test_ac7_bob_can_read_own_w2_service(self):
        """AC #7 positive: Bob must still read W2 services (sanity)."""
        visible = self.Service.with_user(self.bob).search([
            ('id', '=', self.svc_w2.id),
        ])
        self.assertEqual(
            visible, self.svc_w2,
            "Sanity: workspace member must keep read access to own ws",
        )


class TestCloudServiceControllerRegression(TransactionCase):
    """AC #8 — /api/workspaces/<id>/services list/get unchanged for members.

    Calls the controller method directly (not via HTTP) so the test is
    deterministic and exercises the same path as
    ``test_cloud_api.TestCloudAPI``.  After D8=A adds an ir.rule, the
    controller currently uses ``check_user_access`` + ORM read; the rule
    must let members keep reading their own ws services.
    """

    def setUp(self):
        from odoo.addons.woow_paas_platform.controllers.paas import PaasController
        super().setUp()
        self.controller = PaasController()

        self.Service = self.env['woow_paas_platform.cloud_service']
        self.Template = self.env['woow_paas_platform.cloud_app_template']
        self.Workspace = self.env['woow_paas_platform.workspace']
        User = self.env['res.users']

        group_user = self.env.ref('base.group_user')

        self.member = User.create({
            'login': 'cs_ctrl_member',
            'name': 'Ctrl Member',
            'email': 'cs-ctrl-member@test.example',
            'groups_id': [(6, 0, [group_user.id])],
        })
        self.outsider = User.create({
            'login': 'cs_ctrl_outsider',
            'name': 'Ctrl Outsider',
            'email': 'cs-ctrl-outsider@test.example',
            'groups_id': [(6, 0, [group_user.id])],
        })

        self.ws = self.Workspace.create({
            'name': 'WS CS-Ctrl Regression',
            'owner_id': self.member.id,
        })

        self.template = self.Template.sudo().create({
            'name': 'Tmpl CS-Ctrl',
            'slug': 'tmpl-cs-ctrl',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'tmpl-cs-ctrl',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
        })
        self.svc = _internal_provision(self.Service.sudo()).create({
            'name': 'Svc CS-Ctrl',
            'workspace_id': self.ws.id,
            'template_id': self.template.id,
        })

    def _patch_paas_request(self, env):
        return patch(
            'odoo.addons.woow_paas_platform.controllers.paas.request',
            new=MagicMock(env=env),
        )

    def test_ac8_list_services_endpoint_unchanged_for_member(self):
        """AC #8: controller list still returns own ws services for member."""
        with self._patch_paas_request(self.env(user=self.member)):
            result = self.controller.api_workspace_services(
                workspace_id=self.ws.id, action='list',
            )
        self.assertTrue(result.get('success'),
            f"List endpoint regression: {result.get('error')}")
        service_ids = [s['id'] for s in result.get('data', [])]
        self.assertIn(
            self.svc.id, service_ids,
            "AC #8 regression: workspace member must keep seeing own ws "
            "services via the controller after D8=A",
        )

    def test_ac8_list_services_denied_for_outsider(self):
        """AC #8 regression: outsider still denied by controller."""
        with self._patch_paas_request(self.env(user=self.outsider)):
            result = self.controller.api_workspace_services(
                workspace_id=self.ws.id, action='list',
            )
        self.assertFalse(
            result.get('success'),
            "Regression: outsider must still be rejected by controller "
            "(workspace.check_user_access barrier predates D8=A)",
        )
