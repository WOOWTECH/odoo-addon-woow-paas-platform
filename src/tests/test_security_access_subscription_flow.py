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

    def test_route_quota_enforced_at_orm_backstop(self):
        """plan sa_max_routes=1：直接以 ORM create 第二條 route 撞 quota，
        必須由 model @api.constrains _check_route_quota（two-layer 第二層）
        擋下並 raise ValidationError。

        controller 層測試（test_route_quota_enforced_at_controller）會在第二
        條 route 進到 ORM 前就先擋，所以那條測試從不觸發 backstop。本測試繞過
        controller，直接打 ORM，鎖住 backstop 不被誤改（>/>= off-by-one、漏算
        primary、整條被移除）而靜默放行超額 route。
        """
        from odoo.exceptions import ValidationError
        sa = self.SecurityAccess.create({
            'name': 'ORM Quota GW', 'workspace_id': self.workspace.id,
            'template_id': self.template.id,
        })
        partner = self.workspace.owner_id.partner_id
        sa._create_and_confirm_subscription(partner, self.plan)
        self.assertEqual(sa.subscription_id.sh_subscription_plan_id.sa_max_routes,
                         1)
        Route = self.env['woow_paas_platform.security_access_route']
        # SA not active / no tunnel → create override skips operator sync, so
        # we exercise the ORM constraint without touching the operator client.
        Route.create({
            'security_access_id': sa.id,
            'subdomain_prefix': 'orm-quota-r1',
        })
        with self.assertRaises(ValidationError) as cm:
            Route.create({
                'security_access_id': sa.id,
                'subdomain_prefix': 'orm-quota-r2',
            })
        self.assertIn('quota', str(cm.exception).lower())

    def test_route_check_unknown_fails_closed(self):
        """operator 回 status=='unknown'（CF API 出錯）→ controller 必須回
        available:False + reason 含 'try again'（fail-closed）。

        operator 端的 unknown 語義有自己的測試，但使用者實際撞到的 gate 是
        Odoo controller 把 unknown 翻成 available:False 這段轉譯，回退到舊的
        silently-available 行為會讓 attacker 在 CF 短暫 5xx/429 時搶註已被佔
        用的 prefix。鎖住此轉譯。
        """
        mock_client = MagicMock()
        mock_client.check_route_availability.return_value = {
            'subdomain': 'unk-prefix', 'available': None, 'status': 'unknown'}
        with patch(MOCK_GET_CLIENT, return_value=mock_client), \
                _patch_request(self.env):
            res = self.controller._check_route_prefix('unk-prefix')
        self.assertTrue(res['success'])
        self.assertFalse(res['data']['available'])
        self.assertIn('try again', res['data']['reason'].lower())

    def test_route_check_operator_unreachable_fails_closed(self):
        """operator 整個連不上（client 拋例外）→ controller 必須 fail-closed
        回 available:False + reason 含 'try again'。

        operator-unreachable 比 CF 暫時報錯更嚴重，不可反而更寬鬆。此測試鎖住
        except 路徑與 unknown 路徑方向一致（皆 fail-closed），避免維護者誤把
        這裡當成可放行的 best-effort。
        """
        mock_client = MagicMock()
        mock_client.check_route_availability.side_effect = RuntimeError(
            'operator unreachable')
        with patch(MOCK_GET_CLIENT, return_value=mock_client), \
                _patch_request(self.env):
            res = self.controller._check_route_prefix('unreach-prefix')
        self.assertTrue(res['success'])
        self.assertFalse(res['data']['available'])
        self.assertIn('try again', res['data']['reason'].lower())

    def test_route_check_operator_not_configured_fails_closed(self):
        """operator 完全未設定（get_paas_operator_client 回 None）→ controller
        必須 fail-closed 回 available:False，而非靜默掉到 available:True。

        未設定是『無法確認』，不是『free』；它比 configured-but-unreachable
        嚴重度不低，故不可反而更寬鬆地放行。鎖住 `if not client` 分支與
        unknown / unreachable 兩條 fail-closed 路徑方向一致，防回歸成 silently
        available（attacker 趁 operator 未設好的視窗搶 squat 一個其實已被佔用
        的 prefix）。
        """
        with patch(MOCK_GET_CLIENT, return_value=None), \
                _patch_request(self.env):
            res = self.controller._check_route_prefix('noop-prefix')
        self.assertTrue(res['success'])
        self.assertFalse(res['data']['available'])
        self.assertIn('try again', res['data']['reason'].lower())


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


class TestSecurityAccessSubscriptionTrioGate(TransactionCase):
    """Controller-layer role/ACL guards for the subscription trio + routes.

    orm-vs-endpoint-acl-test-coverage (variant A/C): the sudo-cascade write
    endpoints (skip-trial / skip-trial-estimate / change-plan) and the route
    create/delete mutations must each be paired with a controller-level
    negative so a future role-gate drop or half-sudo can't widen access
    silently. Mirrors test_smart_home_subscription_flow's role-gate suite;
    uses the _patch_request style (no HttpCase needed).
    """

    def setUp(self):
        super().setUp()
        self.controller = SecurityAccessController()
        self.SecurityAccess = self.env['woow_paas_platform.security_access']
        self.WorkspaceAccess = self.env[
            'woow_paas_platform.workspace_access']
        self.owner = self.env['res.users'].create({
            'name': 'SA Trio Owner', 'login': 'sa_trio_owner',
            'email': 'sa_trio_owner@test.example',
        })
        base_user = self.env.ref('base.group_user')
        self.owner.groups_id = [(6, 0, [base_user.id])]
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'SA Trio WS', 'owner_id': self.owner.id,
        })
        self.plan = self.env['sh.subscription.plan'].create({
            'name': 'SA Trio Plan', 'sh_duration': 1, 'sh_unit': 'month',
            'sh_plan_price': 100, 'sh_no_of_billing_cycle': -1,
            'sa_max_routes': 3,
        })
        self.plan_b = self.env['sh.subscription.plan'].create({
            'name': 'SA Trio Plan B', 'sh_duration': 1, 'sh_unit': 'month',
            'sh_plan_price': 200, 'sh_no_of_billing_cycle': -1,
            'sa_max_routes': 5,
        })
        # _create_and_confirm_subscription resolves the subscription product
        # from product.template(sh_product_subscribe, sh_subscription_plan_id);
        # without one it raises "No subscription product found". Both plans need
        # a product: change-plan opens a NEW subscription on the *target* plan
        # (plan_b), so the sibling/owner gate tests provision on plan then
        # switch to plan_b — both legs hit the mixin. Mirrors the controller
        # class setUp above.
        self.product = self.env['product.product'].create({
            'name': 'SA Trio Product', 'type': 'service',
            'sh_product_subscribe': True,
            'sh_subscription_plan_id': self.plan.id,
        })
        self.product_b = self.env['product.product'].create({
            'name': 'SA Trio Product B', 'type': 'service',
            'sh_product_subscribe': True,
            'sh_subscription_plan_id': self.plan_b.id,
        })
        self.template = self.env[
            'woow_paas_platform.security_access_template'].create({
                'name': 'SA Trio Tmpl',
                'subscription_plan_ids': [(6, 0, [self.plan.id,
                                                  self.plan_b.id])],
            })
        self.sa = self.SecurityAccess.create({
            'name': 'Trio Gateway', 'workspace_id': self.workspace.id,
        })
        # journal 設定（_create_and_confirm_subscription 前置）
        company = self.env.company.sudo()
        if not company.sh_journal_id:
            journal = self.env['account.journal'].sudo().search(
                [('type', '=', 'sale')], limit=1)
            if journal:
                company.sh_journal_id = journal.id

    def _add_member(self, login, role):
        user = self.env['res.users'].create({
            'name': login, 'login': login,
            'email': '%s@test.example' % login,
        })
        user.groups_id = [(6, 0, [self.env.ref('base.group_user').id])]
        self.WorkspaceAccess.create({
            'workspace_id': self.workspace.id,
            'user_id': user.id, 'role': role,
        })
        return user

    def _outsider(self, login):
        user = self.env['res.users'].create({
            'name': login, 'login': login,
            'email': '%s@test.example' % login,
        })
        user.groups_id = [(6, 0, [self.env.ref('base.group_user').id])]
        return user

    # ---- skip-trial-estimate ----

    def test_skip_trial_estimate_non_member_denied(self):
        outsider = self._outsider('sa_est_outsider')
        with _patch_request(self.env(user=outsider)):
            res = self.controller.api_sa_skip_trial_estimate(
                workspace_id=self.workspace.id, sa_id=self.sa.id)
        self.assertFalse(res['success'])

    def test_skip_trial_estimate_member_user_role_denied(self):
        member = self._add_member('sa_est_member', 'user')
        with _patch_request(self.env(user=member)):
            res = self.controller.api_sa_skip_trial_estimate(
                workspace_id=self.workspace.id, sa_id=self.sa.id)
        self.assertFalse(res['success'])
        self.assertIn('permission', res['error'].lower())

    def test_skip_trial_estimate_admin_invokes_mixin(self):
        admin_member = self._add_member('sa_est_admin', 'admin')
        sentinel = {'total': 100.0, 'sufficient': True}
        with patch.object(
            type(self.sa), '_skip_trial_estimate', return_value=sentinel,
        ) as mocked, _patch_request(self.env(user=admin_member)):
            res = self.controller.api_sa_skip_trial_estimate(
                workspace_id=self.workspace.id, sa_id=self.sa.id)
        self.assertTrue(res['success'], res.get('error'))
        self.assertEqual(res['data'], sentinel)
        mocked.assert_called_once()

    # ---- skip-trial ----

    def test_skip_trial_non_member_denied(self):
        outsider = self._outsider('sa_skip_outsider')
        with _patch_request(self.env(user=outsider)):
            res = self.controller.api_sa_skip_trial(
                workspace_id=self.workspace.id, sa_id=self.sa.id)
        self.assertFalse(res['success'])

    def test_skip_trial_member_user_role_denied(self):
        member = self._add_member('sa_skip_member', 'user')
        with _patch_request(self.env(user=member)):
            res = self.controller.api_sa_skip_trial(
                workspace_id=self.workspace.id, sa_id=self.sa.id)
        self.assertFalse(res['success'])
        self.assertIn('permission', res['error'].lower())

    def test_skip_trial_admin_invokes_mixin(self):
        admin_member = self._add_member('sa_skip_admin', 'admin')
        invoice_sentinel = {'invoice_id': 7, 'amount': 100.0}
        with patch.object(
            type(self.sa), '_skip_trial', return_value=invoice_sentinel,
        ) as mocked, patch.object(
            type(self.sa), '_get_subscription_info', return_value={'id': 1},
        ), _patch_request(self.env(user=admin_member)):
            res = self.controller.api_sa_skip_trial(
                workspace_id=self.workspace.id, sa_id=self.sa.id)
        self.assertTrue(res['success'], res.get('error'))
        self.assertEqual(res['data']['invoice'], invoice_sentinel)
        mocked.assert_called_once()

    # ---- change-plan ----

    def test_change_plan_non_member_denied(self):
        outsider = self._outsider('sa_cp_outsider')
        with _patch_request(self.env(user=outsider)):
            res = self.controller.api_sa_change_plan(
                workspace_id=self.workspace.id, sa_id=self.sa.id,
                plan_id=self.plan_b.id)
        self.assertFalse(res['success'])

    def test_change_plan_member_user_role_denied(self):
        member = self._add_member('sa_cp_member', 'user')
        with _patch_request(self.env(user=member)):
            res = self.controller.api_sa_change_plan(
                workspace_id=self.workspace.id, sa_id=self.sa.id,
                plan_id=self.plan_b.id)
        self.assertFalse(res['success'])
        self.assertIn('permission', res['error'].lower())

    def test_change_plan_rejects_plan_not_in_template(self):
        """change-plan 帶入不屬於本 SA template 的 plan 須被拒（與 _create
        路徑對稱）。role gate 只擋誰能改，不擋 plan 歸屬。"""
        # SA bound to its template; give it an active subscription on plan.
        sa = self.SecurityAccess.create({
            'name': 'Bound Trio GW', 'workspace_id': self.workspace.id,
            'template_id': self.template.id,
        })
        partner = self.workspace.owner_id.partner_id
        sa._create_and_confirm_subscription(partner, self.plan)
        self.assertEqual(sa.subscription_id.state, 'in_progress')
        # A plan that exists but is NOT in this SA's template.
        foreign_plan = self.env['sh.subscription.plan'].create({
            'name': 'Foreign Trio Plan', 'sh_duration': 1, 'sh_unit': 'month',
            'sh_plan_price': 75, 'sh_no_of_billing_cycle': -1,
        })
        self.env.invalidate_all()
        with _patch_request(self.env(user=self.owner)):
            res = self.controller.api_sa_change_plan(
                workspace_id=self.workspace.id, sa_id=sa.id,
                plan_id=foreign_plan.id)
        self.assertFalse(res['success'])
        self.assertIn('not available for this template',
                      (res.get('error') or '').lower())
        # subscription unchanged (still on the original plan).
        self.assertEqual(sa.subscription_id.sh_subscription_plan_id, self.plan)

    def test_change_plan_accepts_sibling_plan_in_template(self):
        """同 template 內的另一 plan（plan_b）change-plan 不得被新檢查誤擋。"""
        sa = self.SecurityAccess.create({
            'name': 'Sibling Trio GW', 'workspace_id': self.workspace.id,
            'template_id': self.template.id,
        })
        partner = self.workspace.owner_id.partner_id
        sa._create_and_confirm_subscription(partner, self.plan)
        self.env.invalidate_all()
        with _patch_request(self.env(user=self.owner)):
            res = self.controller.api_sa_change_plan(
                workspace_id=self.workspace.id, sa_id=sa.id,
                plan_id=self.plan_b.id)
        # Must NOT be blocked by the template-ownership gate (plan_b is in
        # the template); any failure must be unrelated business logic.
        self.assertNotIn('not available for this template',
                         (res.get('error') or '').lower())

    def test_change_plan_owner_passes_role_and_acl_gate(self):
        """Workspace owner (base.group_user only) clears both the role gate
        and the sh.subscription.subscription ACL via the endpoint's sudo
        cascade; the failure mode (if any) is downstream business logic,
        never 'Insufficient permissions' / 'not allowed to access'."""
        manager_group = self.env.ref(
            'sh_subscription.group_manager_sh_subscription',
            raise_if_not_found=False)
        if manager_group:
            self.assertNotIn(manager_group, self.owner.groups_id)
        partner = self.workspace.owner_id.partner_id
        self.sa._create_and_confirm_subscription(partner, self.plan)
        self.assertEqual(self.sa.subscription_id.state, 'in_progress')
        self.env.invalidate_all()
        with _patch_request(self.env(user=self.owner)):
            res = self.controller.api_sa_change_plan(
                workspace_id=self.workspace.id, sa_id=self.sa.id,
                plan_id=self.plan_b.id)
        error_text = (res.get('error') or '').lower()
        self.assertNotIn('insufficient permissions', error_text, res)
        self.assertNotIn('not allowed to access', error_text, res)
        self.assertNotIn('sh.subscription.subscription', error_text, res)

    # ---- routes create/delete cross-user negatives ----

    def test_routes_create_non_member_denied_and_no_route(self):
        outsider = self._outsider('sa_rt_outsider')
        with _patch_request(self.env(user=outsider)):
            res = self.controller.api_security_access_routes(
                workspace_id=self.workspace.id, sa_id=self.sa.id,
                action='create', subdomain_prefix='intruder-route',
                service_url='http://localhost:80')
        self.assertFalse(res['success'])
        # No route leaked into the DB.
        self.assertFalse(self.env[
            'woow_paas_platform.security_access_route'].sudo().search(
                [('subdomain_prefix', '=', 'intruder-route')]))

    def test_routes_create_member_user_role_denied(self):
        member = self._add_member('sa_rt_member', 'user')
        with _patch_request(self.env(user=member)):
            res = self.controller.api_security_access_routes(
                workspace_id=self.workspace.id, sa_id=self.sa.id,
                action='create', subdomain_prefix='member-route',
                service_url='http://localhost:80')
        self.assertFalse(res['success'])
        self.assertIn('permission', res['error'].lower())

    def test_routes_delete_non_member_denied(self):
        outsider = self._outsider('sa_rtdel_outsider')
        with _patch_request(self.env(user=outsider)):
            res = self.controller.api_security_access_routes(
                workspace_id=self.workspace.id, sa_id=self.sa.id,
                action='delete', route_id=999999)
        self.assertFalse(res['success'])
