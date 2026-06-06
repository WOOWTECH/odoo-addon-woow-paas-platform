"""Tests for the Security Access subscription/route controller flow.

Mirrors test_smart_home_subscription_flow.py's `_patch_request` style: we
exercise the controller helper methods directly (rather than via HttpCase),
patching the controller's module-level `request` global with a MagicMock that
exposes `env`. Cross-user endpoint-level ACL is covered separately by the
HttpCase suite (orm-vs-endpoint-acl-test-coverage pattern).
"""
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase

from odoo.addons.woow_paas_platform.controllers.security_access import (
    SecurityAccessController,
)

MOCK_GET_CLIENT = (
    'odoo.addons.woow_paas_platform.models.security_access.'
    'get_paas_operator_client'
)


def _patch_request(env):
    return patch(
        'odoo.addons.woow_paas_platform.controllers.security_access.request',
        new=MagicMock(env=env),
    )


class TestSecurityAccessControllerFlow(TransactionCase):

    def setUp(self):
        super().setUp()
        self.controller = SecurityAccessController()
        self.SecurityAccess = self.env['woow_paas_platform.security_access']
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'SA Ctrl WS', 'owner_id': self.env.user.id,
        })
        Access = self.env['woow_paas_platform.workspace_access']
        if not Access.search([('workspace_id', '=', self.workspace.id),
                              ('user_id', '=', self.env.user.id)]):
            Access.create({
                'workspace_id': self.workspace.id,
                'user_id': self.env.user.id, 'role': 'owner',
            })
        self.plan = self.env['sh.subscription.plan'].create({
            'name': 'SA Ctrl Plan', 'sh_duration': 1, 'sh_unit': 'month',
            'sh_plan_price': 100, 'sh_no_of_billing_cycle': -1,
            'sa_max_routes': 1,
        })
        self.product = self.env['product.product'].create({
            'name': 'SA Ctrl Product', 'type': 'service',
            'sh_product_subscribe': True,
            'sh_subscription_plan_id': self.plan.id,
        })
        self.template = self.env[
            'woow_paas_platform.security_access_template'].create({
                'name': 'SA Ctrl Tmpl',
                'subscription_plan_ids': [(6, 0, [self.plan.id])],
            })
        # journal 設定（_create_and_confirm_subscription 前置；鏡像
        # test_smart_home_subscription_flow.py setUp 的 journal 處理）
        company = self.env.company.sudo()
        if not company.sh_journal_id:
            journal = self.env['account.journal'].sudo().search(
                [('type', '=', 'sale')], limit=1)
            if journal:
                company.sh_journal_id = journal.id

    def test_create_happy_path_creates_subscription(self):
        with _patch_request(self.env):
            res = self.controller._create_security_access(
                workspace=self.workspace,
                name='Happy Gateway',
                plan_id=self.plan.id,
                template_id=self.template.id,
            )
        self.assertTrue(res['success'], res.get('error'))
        sa = self.SecurityAccess.search([('name', '=', 'Happy Gateway')],
                                        limit=1)
        self.assertTrue(sa.subscription_id)
        self.assertEqual(sa.subscription_id.sh_subscription_plan_id, self.plan)

    def test_create_rejects_plan_not_in_template(self):
        other_plan = self.env['sh.subscription.plan'].create({
            'name': 'Foreign Plan', 'sh_duration': 1, 'sh_unit': 'month',
            'sh_plan_price': 50, 'sh_no_of_billing_cycle': -1,
        })
        with _patch_request(self.env):
            res = self.controller._create_security_access(
                workspace=self.workspace,
                name='Bad Gateway',
                plan_id=other_plan.id,
                template_id=self.template.id,
            )
        self.assertFalse(res['success'])

    def test_route_check_reports_db_conflict(self):
        sa = self.SecurityAccess.create({
            'name': 'Check Gateway', 'workspace_id': self.workspace.id,
        })
        sa.write({'state': 'active', 'tunnel_id': 'tun-x'})
        with patch(MOCK_GET_CLIENT) as mock_get_client:
            mock_client = MagicMock()
            mock_client.sync_tunnel_routes.return_value = {'routes': []}
            mock_client.check_route_availability.return_value = {
                'available': True}
            mock_get_client.return_value = mock_client
            self.env['woow_paas_platform.security_access_route'].create({
                'security_access_id': sa.id,
                'subdomain_prefix': 'check-taken',
            })
            with _patch_request(self.env):
                res = self.controller._check_route_prefix('check-taken')
        self.assertTrue(res['success'])
        self.assertFalse(res['data']['available'])

    def test_route_quota_enforced_at_controller(self):
        """plan sa_max_routes=1：第二條自訂 route 在 controller 層被擋。"""
        with _patch_request(self.env), patch(MOCK_GET_CLIENT) as mock_gc:
            mock_client = MagicMock()
            mock_client.sync_tunnel_routes.return_value = {'routes': []}
            mock_client.check_route_availability.return_value = {
                'available': True}
            mock_gc.return_value = mock_client
            res = self.controller._create_security_access(
                workspace=self.workspace, name='Quota GW',
                plan_id=self.plan.id, template_id=self.template.id,
            )
            sa = self.SecurityAccess.browse(res['data']['id'])
            sa.write({'state': 'active', 'tunnel_id': 'tun-q'})
            r1 = self.controller._create_route(sa, 'quota-r1',
                                               'http://localhost:80', True)
            self.assertTrue(r1['success'], r1.get('error'))
            r2 = self.controller._create_route(sa, 'quota-r2',
                                               'http://localhost:80', True)
        self.assertFalse(r2['success'])
        self.assertIn('quota', r2['error'].lower())
