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

# 兩條呼叫路徑、兩個 patch target（namespace 不同，缺一不可）：
# 1. controller._check_route_prefix 用 function-local import
#    `from ..services.paas_operator import get_paas_operator_client`，每次
#    呼叫從 services.paas_operator 模組現抓 attribute → patch 來源模組。
# 2. model 的 route create/write/unlink → _sync_routes_to_operator 用的是
#    models/security_access.py 頂部 import 進自己 namespace 的那份 copy →
#    patch 來源模組不影響它，必須另 patch model namespace。
# 只 patch 其中一個會讓另一條路徑 fall through 到真實 client（在 sandbox 有
# 設定 operator URL 時直接打 HTTP，被 Odoo BlockedRequest 擋下而紅）。
MOCK_GET_CLIENT = (
    'odoo.addons.woow_paas_platform.services.paas_operator.'
    'get_paas_operator_client'
)
MOCK_GET_CLIENT_MODEL = (
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
        mock_client = MagicMock()
        mock_client.sync_tunnel_routes.return_value = {'routes': []}
        mock_client.check_route_availability.return_value = {
            'available': True}
        with patch(MOCK_GET_CLIENT, return_value=mock_client), \
                patch(MOCK_GET_CLIENT_MODEL, return_value=mock_client):
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
        mock_client = MagicMock()
        mock_client.sync_tunnel_routes.return_value = {'routes': []}
        mock_client.check_route_availability.return_value = {
            'available': True}
        with _patch_request(self.env), \
                patch(MOCK_GET_CLIENT, return_value=mock_client), \
                patch(MOCK_GET_CLIENT_MODEL, return_value=mock_client):
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


class TestSecurityAccessPlanEndpoints(TransactionCase):
    """templates / plans / skip-trial-estimate 端點."""

    def setUp(self):
        super().setUp()
        self.controller = SecurityAccessController()
        self.plan = self.env['sh.subscription.plan'].create({
            'name': 'SA Plans EP', 'sh_duration': 1, 'sh_unit': 'month',
            'sh_plan_price': 100, 'sh_no_of_billing_cycle': -1,
            'sh_trial': True, 'sh_trial_duration': 7, 'sh_trial_unit': 'day',
            'sa_max_routes': 3,
        })
        self.template = self.env[
            'woow_paas_platform.security_access_template'].create({
                'name': 'SA Plans Tmpl',
                'subscription_plan_ids': [(6, 0, [self.plan.id])],
            })

    def test_list_templates(self):
        with _patch_request(self.env):
            res = self.controller.api_sa_templates()
        self.assertTrue(res['success'])
        names = [t['name'] for t in res['data']]
        self.assertIn('SA Plans Tmpl', names)

    def test_template_plans_include_quota(self):
        with _patch_request(self.env):
            res = self.controller.api_sa_template_plans(self.template.id)
        self.assertTrue(res['success'])
        plan_data = res['data'][0]
        self.assertEqual(plan_data['max_routes'], 3)
        self.assertTrue(plan_data['has_trial'])

    def test_template_without_plans_hard_fails(self):
        empty = self.env[
            'woow_paas_platform.security_access_template'].create({
                'name': 'Empty Tmpl'})
        with _patch_request(self.env):
            res = self.controller.api_sa_template_plans(empty.id)
        self.assertFalse(res['success'])
