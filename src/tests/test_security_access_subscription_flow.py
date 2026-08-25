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

# #522: server-owned identity anchors (tunnel_id/tunnel_token/dns_record_id/
# access_app_id/...) are immutable for everyone; provisioning code stores the
# ids Cloudflare hands back through this in-process sentinel hatch, and these
# fixtures simulate exactly that. See models/security_access.py.
from odoo.addons.woow_paas_platform.models.security_access import (
    _internal_identity as _internal,
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
        _internal(sa).write({'state': 'active', 'tunnel_id': 'tun-x'})
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

    def test_route_check_rejects_bad_format_before_db_cf(self):
        """Availability fail-fast: a bad-format prefix (dot / symbol / uppercase)
        returns available=False with the format reason WITHOUT touching the DB
        collision search or the Cloudflare check — so the UI never shows
        'Available' for a prefix the create constraint would reject (the
        availability↔create asymmetry this fixes)."""
        mock_client = MagicMock()
        # If the operator were reached, it would say available — prove we do NOT.
        mock_client.check_route_availability.return_value = {'available': True}
        with patch(MOCK_GET_CLIENT, return_value=mock_client), \
                patch(MOCK_GET_CLIENT_MODEL, return_value=mock_client):
            with _patch_request(self.env):
                for bad in ['has.dot', 'sub.domain', 'Bad', 'under_score', '-x']:
                    res = self.controller._check_route_prefix(bad)
                    self.assertTrue(res['success'], bad)
                    self.assertFalse(res['data']['available'], bad)
                    self.assertIn('hyphen', res['data']['reason'].lower(), bad)
        # never consulted the operator for a malformed prefix
        mock_client.check_route_availability.assert_not_called()

    def test_route_check_reserved_prefix_now_available(self):
        """Reserved-prefix block removed: 'paas-'/'svc-' prefixes are no longer
        rejected by format, so availability defers to DB/CF like any other."""
        mock_client = MagicMock()
        mock_client.check_route_availability.return_value = {'available': True}
        with patch(MOCK_GET_CLIENT, return_value=mock_client), \
                patch(MOCK_GET_CLIENT_MODEL, return_value=mock_client):
            with _patch_request(self.env):
                res = self.controller._check_route_prefix('paas-custom')
        self.assertTrue(res['success'])
        self.assertTrue(res['data']['available'])
        mock_client.check_route_availability.assert_called_once()

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
            _internal(sa).write({'state': 'active', 'tunnel_id': 'tun-q'})
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


class TestSecurityAccessAccessControl(TransactionCase):
    """access-config / policy CRUD controller helpers."""

    def setUp(self):
        super().setUp()
        self.controller = SecurityAccessController()
        self.SA = self.env['woow_paas_platform.security_access']
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'AC Ctrl WS', 'owner_id': self.env.user.id})
        Access = self.env['woow_paas_platform.workspace_access']
        if not Access.search([('workspace_id', '=', self.workspace.id),
                              ('user_id', '=', self.env.user.id)]):
            Access.create({'workspace_id': self.workspace.id,
                           'user_id': self.env.user.id, 'role': 'owner'})

    def _active_sa(self, mc):
        with patch(MOCK_GET_CLIENT, return_value=mc), \
                patch(MOCK_GET_CLIENT_MODEL, return_value=mc):
            sa = self.SA.create({'name': 'AC GW',
                                 'workspace_id': self.workspace.id})
            _internal(sa).write({'state': 'active', 'tunnel_id': 'tun-ac'})
            self.env['woow_paas_platform.security_access_route'].create({
                'security_access_id': sa.id,
                'subdomain_prefix': sa.canonical_name})
        return sa

    def _mock(self):
        mc = MagicMock()
        mc.sync_tunnel_routes.return_value = {'routes': []}
        mc.sync_access_app.return_value = {'app_id': 'app-1',
                                           'policy_ids': ['pol-1'],
                                           'allowed_idps': ['idp-g']}
        return mc

    def test_update_access_config(self):
        """P2.5 bridge: access-config writes the SA's PRIMARY route's
        is_protected / login / session (not an SA-level field)."""
        mc = self._mock()
        sa = self._active_sa(mc)
        with _patch_request(self.env), \
                patch(MOCK_GET_CLIENT, return_value=mc), \
                patch(MOCK_GET_CLIENT_MODEL, return_value=mc):
            res = self.controller._update_access_config(sa, {
                'access_enabled': True, 'login_google': True,
                'login_email_otp': True, 'session_duration': '8h'})
        self.assertTrue(res['success'], res.get('error'))
        route = sa.route_ids[:1]
        self.assertTrue(route.is_protected)
        self.assertEqual(route.session_duration, '8h')

    def test_update_access_config_whitelist_only(self):
        """P2.5: _update_access_config's whitelist is the SOLE defense for the
        server-owned per-route access_app_id / access_unresolved_idp_types: the
        route write() immutable guard covers only subdomain_prefix, NOT these
        (plain writable Chars), and the owner/admin role gate doesn't help (an
        owner may legitimately call access-config). Absent the whitelist, an
        owner could smuggle {'access_app_id': 'attacker-app'} onto the route and
        repoint it at an arbitrary CF Access app. Send forged server-owned keys
        and assert none reached the route write()."""
        mc = self._mock()
        sa = self._active_sa(mc)
        route = sa.route_ids[:1]
        # Seed the per-route fail-open warning so a forged blank can't clear it.
        route.access_unresolved_idp_types = 'google'
        orig_app_id = route.access_app_id
        orig_state = sa.state
        orig_tunnel = sa.tunnel_id
        orig_session = route.session_duration
        orig_unresolved = route.access_unresolved_idp_types
        # Send ONLY forged server-owned keys (no whitelisted field): the
        # whitelist must drop them ALL → vals ends empty → no route write() → no
        # operator sync. The positive path is covered by test_update_access_config.
        mc.sync_access_app.reset_mock()
        with _patch_request(self.env), \
                patch(MOCK_GET_CLIENT, return_value=mc), \
                patch(MOCK_GET_CLIENT_MODEL, return_value=mc):
            res = self.controller._update_access_config(sa, {
                'access_app_id': 'attacker-app',
                'state': 'deleting',
                'tunnel_id': 'attacker-tunnel',
                'access_unresolved_idp_types': ''})
        self.assertTrue(res['success'], res.get('error'))
        # every forged server-owned key was dropped (none reached write())
        self.assertEqual(route.access_app_id, orig_app_id)
        self.assertEqual(sa.state, orig_state)
        self.assertEqual(sa.tunnel_id, orig_tunnel)
        self.assertEqual(route.session_duration, orig_session)
        # the forged blank did NOT clear the fail-open widening warning
        self.assertEqual(route.access_unresolved_idp_types, orig_unresolved)
        self.assertEqual(route.access_unresolved_idp_types, 'google')
        # vals ended empty → no operator sync fired
        mc.sync_access_app.assert_not_called()

    def _sa_policies(self, sa):
        return self.env[
            'woow_paas_platform.security_access_access_policy'].search(
                [('security_access_id', '=', sa.id)])

    def test_create_policy(self):
        """P2.5 bridge: _create_policy pins the new policy to the SA's primary
        route (route_id), surfaced via the stored related security_access_id."""
        mc = self._mock()
        sa = self._active_sa(mc)
        route = sa.route_ids[:1]
        with _patch_request(self.env), \
                patch(MOCK_GET_CLIENT, return_value=mc), \
                patch(MOCK_GET_CLIENT_MODEL, return_value=mc):
            route.write({'is_protected': True})
            res = self.controller._create_policy(sa, {
                'name': 'corp', 'decision': 'allow',
                'rule_type': 'email_list', 'rule_value': 'user@acme.com'})
        self.assertTrue(res['success'], res.get('error'))
        self.assertEqual(len(self._sa_policies(sa)), 1)
        self.assertEqual(self._sa_policies(sa).route_id, route)

    def test_policy_quota_enforced_at_controller(self):
        """plan sa_max_access_policies=1：第二條 policy 在 controller 層
        (_create_policy) 被擋，回 structured {success:False} 且 error 含 'quota'。

        Policies 的 quota 與 routes 一樣有兩層：controller _create_policy
        (len(sa.access_policy_ids) >= quota → structured error) 與 model
        @api.constrains _check_policy_quota (count > quota → ValidationError)。
        Routes 兩層都有測（test_route_quota_enforced_at_controller +
        _at_orm_backstop），但 policies 修前只測了 ORM backstop
        (test_policy_quota_enforced)；controller 這條 structured-error code path
        無測，是 two-layer-quota 半套覆蓋的反例。鎖住它：移除/弱化 controller
        層 gate（error message 漂移、或 gate 被拿掉只剩 per-row ORM）會被抓到。"""
        mc = self._mock()
        sa = self._active_sa(mc)
        plan = self.env['sh.subscription.plan'].create({
            'name': 'AC Policy Quota Plan', 'sh_duration': 1, 'sh_unit': 'month',
            'sh_plan_price': 100, 'sh_no_of_billing_cycle': -1,
            'sa_max_access_policies': 1,
            'ac_max_access_seats': 99,
            # ↑ #521：席位 enforcement 上線後此欄預設 0 ＝「一席都沒有」，
            # 帶 email_list 的 fixture 會被 `_check_access_seat_quota` 擋住。
            # 本測試測的是 policy 數／email 數 quota、不是席位，給明顯夠用的
            # 額度讓紅燈原因只可能是被測的那一條。fixture forward coupling，
            # **不是**產品側豁免（既有 0 席方案的存量資料仍未拍板）。
            })
        sub = self.env['sh.subscription.subscription'].create({
            'sh_partner_id': self.env.user.partner_id.id,
            'state': 'in_progress'})
        sub.sh_subscription_plan_id = plan.id
        sa.subscription_id = sub.id
        with _patch_request(self.env), \
                patch(MOCK_GET_CLIENT, return_value=mc), \
                patch(MOCK_GET_CLIENT_MODEL, return_value=mc):
            r1 = self.controller._create_policy(sa, {
                'name': 'p1', 'decision': 'allow',
                'rule_type': 'email_list', 'rule_value': 'user@a.com'})
            self.assertTrue(r1['success'], r1.get('error'))
            r2 = self.controller._create_policy(sa, {
                'name': 'p2', 'decision': 'allow',
                'rule_type': 'email_list', 'rule_value': 'user@b.com'})
        self.assertFalse(r2['success'])
        self.assertIn('quota', r2['error'].lower())
        # the over-quota policy must not have landed
        self.assertEqual(len(self._sa_policies(sa)), 1)

    def test_policy_quota_zero_is_unlimited_at_controller(self):
        """#743 陽性對照 A（controller 層）：quota=0 ⇒ 不限。

        這一層必測，不能只測 model：controller 的預檢是 `existing >= quota`，
        quota=0 時 `0 >= 0` 成立，會在 Policy.create() 之前就 return
        structured error——model 的 0 豁免根本走不到。前端（AddPolicyModal）
        走的正是這條路，所以只修 model 的話使用者感知到的行為完全沒變，
        而 model-only 的測試會是綠的。"""
        mc = self._mock()
        sa = self._active_sa(mc)
        plan = self.env['sh.subscription.plan'].create({
            'name': 'AC Zero Quota Plan', 'sh_duration': 1, 'sh_unit': 'month',
            'sh_plan_price': 100, 'sh_no_of_billing_cycle': -1,
            'sa_max_access_policies': 0,
            'ac_max_access_seats': 99,
            # ↑ #521：席位 enforcement 上線後此欄預設 0 ＝「一席都沒有」，
            # 帶 email_list 的 fixture 會被 `_check_access_seat_quota` 擋住。
            # 本測試測的是 policy 數／email 數 quota、不是席位，給明顯夠用的
            # 額度讓紅燈原因只可能是被測的那一條。fixture forward coupling，
            # **不是**產品側豁免（既有 0 席方案的存量資料仍未拍板）。
            })
        sub = self.env['sh.subscription.subscription'].create({
            'sh_partner_id': self.env.user.partner_id.id,
            'state': 'in_progress'})
        sub.sh_subscription_plan_id = plan.id
        sa.subscription_id = sub.id
        # 釘死「預檢有跑到 quota 那一行」——沒 sub / 沒 plan 會整段跳過，
        # 那時 create 成功與「0 豁免生效」輸出相同。
        self.assertTrue(sa.sudo().subscription_id.sh_subscription_plan_id)
        with _patch_request(self.env), \
                patch(MOCK_GET_CLIENT, return_value=mc), \
                patch(MOCK_GET_CLIENT_MODEL, return_value=mc):
            for idx in (1, 2):
                res = self.controller._create_policy(sa, {
                    'name': 'z%d' % idx, 'decision': 'allow',
                    'rule_type': 'email_list',
                    'rule_value': 'u%d@z.com' % idx})
                self.assertTrue(res['success'], res.get('error'))
        self.assertEqual(len(self._sa_policies(sa)), 2)

    def test_policy_quota_positive_still_enforced_at_controller(self):
        """#743 陽性對照 B（controller 層）：正值 quota 仍要擋。

        與 test_policy_quota_enforced_at_controller 不同軸：那條 quota=1，
        第一筆就是邊界；這條 quota=2 驗「用滿 2 筆仍可、第 3 筆才擋」，
        確保 0 豁免沒有被寫成「任何 quota 都放行」。"""
        mc = self._mock()
        sa = self._active_sa(mc)
        plan = self.env['sh.subscription.plan'].create({
            'name': 'AC Two Quota Plan', 'sh_duration': 1, 'sh_unit': 'month',
            'sh_plan_price': 100, 'sh_no_of_billing_cycle': -1,
            'sa_max_access_policies': 2,
            'ac_max_access_seats': 99,
            # ↑ #521：席位 enforcement 上線後此欄預設 0 ＝「一席都沒有」，
            # 帶 email_list 的 fixture 會被 `_check_access_seat_quota` 擋住。
            # 本測試測的是 policy 數／email 數 quota、不是席位，給明顯夠用的
            # 額度讓紅燈原因只可能是被測的那一條。fixture forward coupling，
            # **不是**產品側豁免（既有 0 席方案的存量資料仍未拍板）。
            })
        sub = self.env['sh.subscription.subscription'].create({
            'sh_partner_id': self.env.user.partner_id.id,
            'state': 'in_progress'})
        sub.sh_subscription_plan_id = plan.id
        sa.subscription_id = sub.id
        with _patch_request(self.env), \
                patch(MOCK_GET_CLIENT, return_value=mc), \
                patch(MOCK_GET_CLIENT_MODEL, return_value=mc):
            for idx in (1, 2):
                ok = self.controller._create_policy(sa, {
                    'name': 'q%d' % idx, 'decision': 'allow',
                    'rule_type': 'email_list',
                    'rule_value': 'u%d@q.com' % idx})
                self.assertTrue(ok['success'], ok.get('error'))
            over = self.controller._create_policy(sa, {
                'name': 'q3', 'decision': 'allow',
                'rule_type': 'email_list', 'rule_value': 'u3@q.com'})
        self.assertFalse(over['success'])
        self.assertIn('quota', over['error'].lower())
        self.assertEqual(len(self._sa_policies(sa)), 2)

    def test_create_policy_invalid_rule_rejected(self):
        mc = self._mock()
        sa = self._active_sa(mc)
        with _patch_request(self.env), \
                patch(MOCK_GET_CLIENT, return_value=mc), \
                patch(MOCK_GET_CLIENT_MODEL, return_value=mc):
            res = self.controller._create_policy(sa, {
                'name': 'bad', 'decision': 'allow',
                'rule_type': 'ip_range', 'rule_value': 'nope'})
        self.assertFalse(res['success'])

    def test_update_policy_whitelist_only(self):
        """P2.5: _update_policy must not let client-controlled keys
        (cf_policy_id / route_id) reach write(); only the field whitelist
        applies. route_id is server-owned now (pins the CF Access app)."""
        mc = self._mock()
        sa = self._active_sa(mc)
        route = sa.route_ids[:1]
        with _patch_request(self.env), \
                patch(MOCK_GET_CLIENT, return_value=mc), \
                patch(MOCK_GET_CLIENT_MODEL, return_value=mc):
            pol = self.env[
                'woow_paas_platform.security_access_access_policy'].create({
                    'route_id': route.id, 'name': 'corp',
                    'decision': 'allow', 'rule_type': 'email_list',
                    'rule_value': 'user@acme.com'})
            res = self.controller._update_policy(sa, pol.id, {
                'name': 'renamed', 'cf_policy_id': 'attacker-set',
                'route_id': 999999})
        self.assertTrue(res['success'], res.get('error'))
        self.assertEqual(pol.name, 'renamed')
        # client-supplied non-whitelisted keys were ignored
        self.assertNotEqual(pol.cf_policy_id, 'attacker-set')
        self.assertEqual(pol.route_id, route)

    def test_create_policy_whitelist_only(self):
        """P2.5 Phase 3 white-list: _create_policy never trusts the client for
        server-owned fields. Two prongs:

        (1) a cross-SA route_id (pointing at ANOTHER SA's route) is REJECTED
            ('Route not found') — not silently re-pinned, and certainly never
            written to the other SA's route (cross-SA injection deny). This is
            the security-critical change vs. the P2 bridge, where route_id was
            ignored and the policy always landed on the primary route.
        (2) with a VALID same-SA route_id, a forged cf_policy_id is dropped
            (server-owned: the row only gets a cf id from the operator's
            returned policy_ids on sync)."""
        mc = self._mock()
        sa = self._active_sa(mc)
        route = sa.route_ids[:1]
        # A second SA + route whose id the attacker tries to smuggle in.
        other_sa = self.SA.create({
            'name': 'Other GW', 'workspace_id': self.workspace.id})
        other_route = self.env[
            'woow_paas_platform.security_access_route'].create({
                'security_access_id': other_sa.id,
                'subdomain_prefix': 'other-route'})
        with _patch_request(self.env), \
                patch(MOCK_GET_CLIENT, return_value=mc), \
                patch(MOCK_GET_CLIENT_MODEL, return_value=mc):
            # (1) cross-SA route_id → deny, nothing created on either SA.
            cross = self.controller._create_policy(sa, {
                'name': 'cross', 'decision': 'allow',
                'rule_type': 'email_list', 'rule_value': 'user@cross.com',
                'cf_policy_id': 'attacker-set',
                'route_id': other_route.id})
            # (2) valid same-SA route_id + forged cf_policy_id → cf dropped.
            ok = self.controller._create_policy(sa, {
                'name': 'corp', 'decision': 'allow',
                'rule_type': 'email_list', 'rule_value': 'user@acme.com',
                'cf_policy_id': 'attacker-set',
                'route_id': route.id})
        # (1) cross-SA route_id rejected; no policy on either SA.
        self.assertFalse(cross['success'])
        self.assertIn('route not found', (cross.get('error') or '').lower())
        self.assertFalse(self._sa_policies(sa).filtered(
            lambda p: p.name == 'cross'))
        self.assertFalse(other_route.access_policy_ids)
        # (2) created on the chosen (primary) route, cf_policy_id NOT the forged
        # value (server-owned).
        self.assertTrue(ok['success'], ok.get('error'))
        pol = self._sa_policies(sa).filtered(lambda p: p.name == 'corp')
        self.assertEqual(len(pol), 1)
        self.assertNotEqual(pol.cf_policy_id, 'attacker-set')
        self.assertEqual(pol.route_id, route)
        self.assertEqual(pol.security_access_id.id, sa.id)
        self.assertNotIn(pol, other_route.access_policy_ids)

    def test_delete_policy(self):
        mc = self._mock()
        sa = self._active_sa(mc)
        route = sa.route_ids[:1]
        with _patch_request(self.env), \
                patch(MOCK_GET_CLIENT, return_value=mc), \
                patch(MOCK_GET_CLIENT_MODEL, return_value=mc):
            pol = self.env[
                'woow_paas_platform.security_access_access_policy'].create({
                    'route_id': route.id, 'name': 'corp',
                    'decision': 'allow', 'rule_type': 'email_list',
                    'rule_value': 'user@acme.com'})
            res = self.controller._delete_policy(sa, pol.id)
        self.assertTrue(res['success'], res.get('error'))
        self.assertEqual(len(self._sa_policies(sa)), 0)

    # ==================== P2.5 Phase 3: per-route CRUD ====================

    def _second_route(self, sa, mc, prefix='ac-second'):
        """A non-primary protected route on the same SA, so per-route create
        can target a route other than the primary one."""
        with patch(MOCK_GET_CLIENT, return_value=mc), \
                patch(MOCK_GET_CLIENT_MODEL, return_value=mc):
            return self.env[
                'woow_paas_platform.security_access_route'].create({
                    'security_access_id': sa.id,
                    'subdomain_prefix': prefix, 'is_protected': True})

    def test_create_policy_on_explicit_route_id(self):
        """Phase 3: _create_policy 帶 route_id 可把 policy 綁到 SA 底下的任一
        route（非僅 primary），並真的落在指定的那條 route 上。"""
        mc = self._mock()
        sa = self._active_sa(mc)
        second = self._second_route(sa, mc)
        with _patch_request(self.env), \
                patch(MOCK_GET_CLIENT, return_value=mc), \
                patch(MOCK_GET_CLIENT_MODEL, return_value=mc):
            res = self.controller._create_policy(sa, {
                'name': 'on-second', 'decision': 'allow',
                'rule_type': 'email_list', 'rule_value': 'user@second.com',
                'route_id': second.id})
        self.assertTrue(res['success'], res.get('error'))
        pol = self._sa_policies(sa).filtered(lambda p: p.name == 'on-second')
        self.assertEqual(len(pol), 1)
        self.assertEqual(pol.route_id, second)
        self.assertNotEqual(pol.route_id, sa.route_ids[:1])

    def test_create_policy_cross_sa_route_id_denied(self):
        """Phase 3 white-list 守線：route_id 指向『另一個 SA』的 route → 必須被
        拒（Route not found），policy 不得建到別 SA 的 route。跨 SA 注入面。"""
        mc = self._mock()
        sa = self._active_sa(mc)
        # A second SA in the SAME workspace with its own route.
        other_sa = self.SA.create({
            'name': 'Other GW X', 'workspace_id': self.workspace.id})
        other_route = self.env[
            'woow_paas_platform.security_access_route'].create({
                'security_access_id': other_sa.id,
                'subdomain_prefix': 'cross-sa-route'})
        with _patch_request(self.env), \
                patch(MOCK_GET_CLIENT, return_value=mc), \
                patch(MOCK_GET_CLIENT_MODEL, return_value=mc):
            res = self.controller._create_policy(sa, {
                'name': 'cross', 'decision': 'allow',
                'rule_type': 'email_list', 'rule_value': 'user@cross.com',
                'route_id': other_route.id})
        self.assertFalse(res['success'])
        self.assertIn('route not found', (res.get('error') or '').lower())
        # nothing landed on the other SA's route
        self.assertFalse(other_route.access_policy_ids)
        # nor (under the gated sa's pin) was a policy created at all
        self.assertFalse(self._sa_policies(sa).filtered(
            lambda p: p.name == 'cross'))

    def test_update_route_access_writes_per_route(self):
        """Phase 3: _update_route_access 寫進指定 route 的 is_protected /
        login / session（非 SA 層、非 primary-only）。"""
        mc = self._mock()
        sa = self._active_sa(mc)
        second = self._second_route(sa, mc, prefix='ac-cfg-second')
        # start it unprotected so the flip is observable
        with patch(MOCK_GET_CLIENT, return_value=mc), \
                patch(MOCK_GET_CLIENT_MODEL, return_value=mc):
            second.write({'is_protected': False})
        with _patch_request(self.env), \
                patch(MOCK_GET_CLIENT, return_value=mc), \
                patch(MOCK_GET_CLIENT_MODEL, return_value=mc):
            res = self.controller._update_route_access(sa, second.id, {
                'is_protected': True, 'login_google': True,
                'session_duration': '8h'})
        self.assertTrue(res['success'], res.get('error'))
        second.invalidate_recordset(
            ['is_protected', 'login_google', 'session_duration'])
        self.assertTrue(second.is_protected)
        self.assertTrue(second.login_google)
        self.assertEqual(second.session_duration, '8h')
        # primary route untouched (per-route isolation)
        primary = sa.route_ids[:1]
        self.assertFalse(primary.is_protected)

    def test_update_route_access_whitelist_only(self):
        """Phase 3: _update_route_access 的 whitelist 是 server-owned per-route
        access_app_id / access_unresolved_idp_types 的唯一防線 —— forged 鍵全
        部被丟棄，不得寫進 route。"""
        mc = self._mock()
        sa = self._active_sa(mc)
        route = sa.route_ids[:1]
        route.access_unresolved_idp_types = 'google'
        orig_app_id = route.access_app_id
        orig_unresolved = route.access_unresolved_idp_types
        mc.sync_access_app.reset_mock()
        with _patch_request(self.env), \
                patch(MOCK_GET_CLIENT, return_value=mc), \
                patch(MOCK_GET_CLIENT_MODEL, return_value=mc):
            res = self.controller._update_route_access(sa, route.id, {
                'access_app_id': 'attacker-app',
                'access_unresolved_idp_types': '',
                'security_access_id': 999999})
        self.assertTrue(res['success'], res.get('error'))
        route.invalidate_recordset(
            ['access_app_id', 'access_unresolved_idp_types'])
        self.assertEqual(route.access_app_id, orig_app_id)
        self.assertEqual(route.access_unresolved_idp_types, orig_unresolved)
        self.assertEqual(route.access_unresolved_idp_types, 'google')
        # non-whitelisted fields (access_app_id / security_access_id) ignored
        # vals ended empty → no operator sync fired
        mc.sync_access_app.assert_not_called()

    def test_update_route_access_cross_sa_route_id_denied(self):
        """Phase 3 white-list：route_id 指向另一個 SA 的 route → 拒（Route not
        found），不得翻動別 SA route 的 access config。"""
        mc = self._mock()
        sa = self._active_sa(mc)
        other_sa = self.SA.create({
            'name': 'Other GW Cfg', 'workspace_id': self.workspace.id})
        other_route = self.env[
            'woow_paas_platform.security_access_route'].create({
                'security_access_id': other_sa.id,
                'subdomain_prefix': 'cross-sa-cfg', 'is_protected': False})
        with _patch_request(self.env), \
                patch(MOCK_GET_CLIENT, return_value=mc), \
                patch(MOCK_GET_CLIENT_MODEL, return_value=mc):
            res = self.controller._update_route_access(sa, other_route.id, {
                'is_protected': True})
        self.assertFalse(res['success'])
        self.assertIn('route not found', (res.get('error') or '').lower())
        other_route.invalidate_recordset(['is_protected'])
        self.assertFalse(other_route.is_protected)

    def test_policy_list_filters_by_route_id(self):
        """Phase 3: access-policies list 帶 route_id 只回該 route 的 policy；
        cross-SA route_id 回空（never 另一 SA 的 rows）。"""
        mc = self._mock()
        sa = self._active_sa(mc)
        primary = sa.route_ids[:1]
        second = self._second_route(sa, mc, prefix='ac-list-second')
        with patch(MOCK_GET_CLIENT, return_value=mc), \
                patch(MOCK_GET_CLIENT_MODEL, return_value=mc):
            self.env[
                'woow_paas_platform.security_access_access_policy'].create([
                    {'route_id': primary.id, 'name': 'on-primary',
                     'decision': 'allow', 'rule_type': 'email_list',
                     'rule_value': 'user@p.com'},
                    {'route_id': second.id, 'name': 'on-2nd',
                     'decision': 'allow', 'rule_type': 'email_list',
                     'rule_value': 'user@s.com'}])
        with _patch_request(self.env(user=self.env.user)), \
                patch(MOCK_GET_CLIENT, return_value=mc), \
                patch(MOCK_GET_CLIENT_MODEL, return_value=mc):
            scoped = self.controller.api_sa_access_policies(
                workspace_id=self.workspace.id, sa_id=sa.id,
                action='list', route_id=second.id)
            cross = self.controller.api_sa_access_policies(
                workspace_id=self.workspace.id, sa_id=sa.id,
                action='list', route_id=999999)
        self.assertTrue(scoped['success'])
        self.assertEqual([p['name'] for p in scoped['data']], ['on-2nd'])
        # unknown / cross-SA route_id → empty, never the SA's other policies
        self.assertTrue(cross['success'])
        self.assertEqual(cross['data'], [])
