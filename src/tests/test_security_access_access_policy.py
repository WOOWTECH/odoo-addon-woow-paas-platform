from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import TransactionCase

MOCK_GET_CLIENT = (
    'odoo.addons.woow_paas_platform.models.security_access.'
    'get_paas_operator_client'
)


class TestAccessPolicyModel(TransactionCase):
    """P2.5 access_policy 子模型綁 route_id + per-route access 欄位."""

    def setUp(self):
        super().setUp()
        self.SA = self.env['woow_paas_platform.security_access']
        self.Route = self.env['woow_paas_platform.security_access_route']
        self.Policy = self.env['woow_paas_platform.security_access_access_policy']
        self.workspace = self.env['woow_paas_platform.workspace'].create(
            {'name': 'AP WS'})

    def _make_sa(self):
        return self.SA.create({'name': 'AP GW', 'workspace_id': self.workspace.id})

    def _make_route(self, sa, prefix='ap-route', **kw):
        return self.Route.create(dict(
            {'security_access_id': sa.id, 'subdomain_prefix': prefix}, **kw))

    def test_route_access_fields_default(self):
        """P2.5: access 三件組下放 route。route 預設 not protected / 24h."""
        sa = self._make_sa()
        route = self._make_route(sa)
        self.assertFalse(route.is_protected)
        self.assertFalse(route.login_google)
        self.assertFalse(route.login_email_otp)
        self.assertEqual(route.session_duration, '24h')

    def test_policy_requires_route(self):
        """policy 改綁 route_id（required）；security_access_id 由 route 推導
        (store=True related)。"""
        sa = self._make_sa()
        route = self._make_route(sa)
        pol = self.Policy.create({
            'route_id': route.id, 'name': 'corp',
            'decision': 'allow', 'rule_type': 'email_domain',
            'rule_value': 'acme.com', 'sequence': 1})
        self.assertIn(pol, route.access_policy_ids)
        # related store: 從 route 推導出 SA
        self.assertEqual(pol.security_access_id, sa)
        # to_dict 含 route_id
        self.assertEqual(pol.to_dict()['route_id'], route.id)

    def test_policy_without_route_rejected(self):
        """route_id required — 無 route 不可建 policy。"""
        with self.assertRaises(Exception):
            self.Policy.create({
                'name': 'no-route', 'decision': 'allow',
                'rule_type': 'email_domain', 'rule_value': 'x.com'})

    def test_policy_rule_value_validation(self):
        sa = self._make_sa()
        route = self._make_route(sa)
        # email_domain 不可含 @
        with self.assertRaises(ValidationError):
            self.Policy.create({
                'route_id': route.id, 'name': 'bad',
                'decision': 'allow', 'rule_type': 'email_domain',
                'rule_value': '@acme.com'})
        # ip_range 必須是 CIDR
        with self.assertRaises(ValidationError):
            self.Policy.create({
                'route_id': route.id, 'name': 'bad2',
                'decision': 'allow', 'rule_type': 'ip_range',
                'rule_value': 'not-an-ip'})

    def test_policy_quota_enforced_sa_level_total(self):
        """quota 仍是 SA 級總量（plan.sa_max_access_policies 跨該 SA 全部 route
        加總，via stored related security_access_id），不是 per-route。"""
        sa = self._make_sa()
        r1 = self._make_route(sa, prefix='ap-r1')
        r2 = self._make_route(sa, prefix='ap-r2')
        plan = self.env['sh.subscription.plan'].create({
            'name': 'AP Plan', 'sh_duration': 1, 'sh_unit': 'month',
            'sh_plan_price': 100, 'sh_no_of_billing_cycle': -1,
            'sa_max_access_policies': 1})
        sub = self.env['sh.subscription.subscription'].create({
            'sh_partner_id': self.env.user.partner_id.id, 'state': 'in_progress'})
        sub.sh_subscription_plan_id = plan.id
        sa.subscription_id = sub.id
        # 第一筆（route 1）OK
        self.Policy.create({
            'route_id': r1.id, 'name': 'p1', 'decision': 'allow',
            'rule_type': 'email_domain', 'rule_value': 'a.com'})
        # 第二筆放到另一條 route 也應撞 SA 級總量配額
        with self.assertRaises(ValidationError):
            self.Policy.create({
                'route_id': r2.id, 'name': 'p2', 'decision': 'allow',
                'rule_type': 'email_domain', 'rule_value': 'b.com'})


class TestRouteAccessSync(TransactionCase):
    """P2.5 per-route _build_access_payload / _sync_access_to_operator."""

    def setUp(self):
        super().setUp()
        self.SA = self.env['woow_paas_platform.security_access']
        self.Route = self.env['woow_paas_platform.security_access_route']
        self.Policy = self.env[
            'woow_paas_platform.security_access_access_policy']
        self.workspace = self.env['woow_paas_platform.workspace'].create(
            {'name': 'AS WS'})

    def _active_sa(self, mock_client, protected=True):
        with patch(MOCK_GET_CLIENT, return_value=mock_client):
            sa = self.SA.create({'name': 'AS GW',
                                 'workspace_id': self.workspace.id})
            sa.write({'state': 'active', 'tunnel_id': 'tun-as'})
            route = self.Route.create({
                'security_access_id': sa.id,
                'subdomain_prefix': sa.canonical_name,
                'is_primary': True, 'is_protected': protected})
        return sa, route

    def _mock(self):
        m = MagicMock()
        m.sync_tunnel_routes.return_value = {'routes': []}
        m.sync_access_app.return_value = {
            'app_id': 'app-1', 'policy_ids': ['pol-1'],
            'allowed_idps': ['idp-g', 'idp-otp']}
        return m

    def test_build_payload_single_route_scope(self):
        """route._build_access_payload → domain = destinations = 該 route
        hostname only；policies = route.access_policy_ids；login/session 來自
        route 自己。"""
        mc = self._mock()
        sa, route = self._active_sa(mc, protected=True)
        with patch(MOCK_GET_CLIENT, return_value=mc):
            route.write({'login_google': True, 'login_email_otp': True,
                         'session_duration': '8h'})
            self.Policy.create({
                'route_id': route.id, 'name': 'corp', 'decision': 'allow',
                'rule_type': 'email_domain', 'rule_value': 'acme.com'})
        payload = route._build_access_payload()
        host = f"{sa.canonical_name}.{sa._get_paas_domain()}"
        self.assertTrue(payload['enabled'])
        self.assertEqual(payload['primary_hostname'], host)
        self.assertEqual(payload['protected_hostnames'], [host])
        self.assertEqual(sorted(payload['login_idp_types']),
                         ['google', 'onetimepin'])
        self.assertEqual(payload['session_duration'], '8h')
        self.assertEqual(payload['policies'][0]['rule_value'], 'acme.com')

    def test_protected_route_syncs_app_and_writes_back(self):
        """is_protected route create → sync app → 回填 route.access_app_id +
        policy cf id。"""
        mc = self._mock()
        sa, route = self._active_sa(mc, protected=True)
        with patch(MOCK_GET_CLIENT, return_value=mc):
            pol = self.Policy.create({
                'route_id': route.id, 'name': 'corp', 'decision': 'allow',
                'rule_type': 'email_domain', 'rule_value': 'acme.com'})
        self.assertTrue(mc.sync_access_app.called)
        self.assertEqual(route.access_app_id, 'app-1')
        self.assertEqual(pol.cf_policy_id, 'pol-1')

    def test_toggle_protected_true_syncs(self):
        mc = self._mock()
        sa, route = self._active_sa(mc, protected=False)
        with patch(MOCK_GET_CLIENT, return_value=mc):
            r2 = self.Route.create({'security_access_id': sa.id,
                                    'subdomain_prefix': 'as-extra',
                                    'is_protected': False})
            mc.sync_access_app.reset_mock()
            r2.write({'is_protected': True})
        self.assertTrue(mc.sync_access_app.called)
        payload = mc.sync_access_app.call_args.args[0]
        self.assertEqual(payload['protected_hostnames'],
                         [f"as-extra.{sa._get_paas_domain()}"])

    def test_toggle_protected_false_tears_down_and_clears_app_id(self):
        """is_protected=False → enabled=False teardown 並清空 route.access_app_id。"""
        mc = self._mock()
        sa, route = self._active_sa(mc, protected=True)
        with patch(MOCK_GET_CLIENT, return_value=mc):
            # route 已有 app_id
            self.assertEqual(route.access_app_id, 'app-1')
            mc.sync_access_app.return_value = {
                'app_id': None, 'policy_ids': [], 'allowed_idps': []}
            mc.sync_access_app.reset_mock()
            route.write({'is_protected': False})
        payload = mc.sync_access_app.call_args.args[0]
        self.assertFalse(payload['enabled'])
        self.assertEqual(payload['existing_app_id'], 'app-1')
        self.assertFalse(route.access_app_id)

    def test_protected_route_unlink_tears_down_own_app(self):
        """刪 protected route → 用 unlink 前捕捉的 app_id/policy cf ids 打
        enabled=False teardown（行斷前捕捉，避免孤兒）。"""
        mc = self._mock()
        sa, route = self._active_sa(mc, protected=True)
        with patch(MOCK_GET_CLIENT, return_value=mc):
            r2 = self.Route.create({'security_access_id': sa.id,
                                    'subdomain_prefix': 'as-protected',
                                    'is_protected': True})
            # r2 的 app_id 已回填
            self.assertEqual(r2.access_app_id, 'app-1')
            captured_app = r2.access_app_id
            mc.sync_access_app.reset_mock()
            r2.unlink()
        self.assertTrue(mc.sync_access_app.called,
                        "unlinking a protected route must tear its app down")
        payload = mc.sync_access_app.call_args.args[0]
        self.assertFalse(payload['enabled'])
        self.assertEqual(payload['existing_app_id'], captured_app)

    def test_protected_route_unlink_teardown_failure_rolls_back_keeps_row(self):
        """刪 protected route 時 Access teardown 失敗 → fail loud + 保留 row。

        _teardown_route_access 內部吞 Exception 回 False。修前 unlink() 忽略回傳
        值 → row 已被 super().unlink() 刪除 → 該 route 的 CF Access app +
        reusable policies 永久孤兒、無 error 信號、無重試錨點（且 SA cleanup 的
        route_ids.filtered('access_app_id') 掃不到已刪 row）。修後 unlink 對
        teardown 失敗 raise → savepoint rollback → row 保留，與 sibling 的
        route-sync leg / policy unlink resync 對稱（[[two-stage-remote-
        provision-idempotency]]：每條失敗路徑保留 stored id 讓 retry 安全）。
        """
        mc = self._mock()
        sa, route = self._active_sa(mc, protected=True)
        with patch(MOCK_GET_CLIENT, return_value=mc):
            r2 = self.Route.create({'security_access_id': sa.id,
                                    'subdomain_prefix': 'as-teardown-fail',
                                    'is_protected': True})
            self.assertEqual(r2.access_app_id, 'app-1')
            r2_id = r2.id
            # teardown 的 sync_access_app（enabled=False）失敗
            mc.sync_access_app.side_effect = Exception('cf access down')
            with self.assertRaises(Exception):
                r2.unlink()
        # row 必須存活 — teardown 失敗，DB 仍須引用它以保留重試錨點
        self.assertTrue(self.Route.browse(r2_id).exists(),
                        "failed Access teardown must keep the route row")
        self.assertEqual(self.Route.browse(r2_id).access_app_id, 'app-1',
                         "stored access_app_id retained as retry anchor")

    def test_login_session_change_resyncs_route(self):
        """改 route 自己的 login_*/session_duration → re-sync 該 route。"""
        mc = self._mock()
        sa, route = self._active_sa(mc, protected=True)
        with patch(MOCK_GET_CLIENT, return_value=mc):
            mc.sync_access_app.reset_mock()
            route.write({'session_duration': '8h', 'login_google': True})
        self.assertTrue(mc.sync_access_app.called)
        payload = mc.sync_access_app.call_args.args[0]
        self.assertEqual(payload['session_duration'], '8h')
        self.assertIn('google', payload['login_idp_types'])

    def test_unprotected_route_create_no_sync(self):
        """未 protected route create → 不打 operator（route sync no-op）。"""
        mc = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=mc):
            sa = self.SA.create({'name': 'AS GW2',
                                 'workspace_id': self.workspace.id})
            sa.write({'state': 'active', 'tunnel_id': 'tun-as2'})
            mc.sync_access_app.reset_mock()
            self.Route.create({'security_access_id': sa.id,
                               'subdomain_prefix': 'as-public',
                               'is_protected': False})
        mc.sync_access_app.assert_not_called()

    def test_write_honors_sa_skip_route_sync_on_live_protected_route(self):
        """sa_skip_route_sync route.write() 不可呼 operator，即使 SA 有 live
        tunnel + protected route。

        鏡像 create()/policy 的對稱性：migration 的 login/session re-home
        （post-migrate.py 用 sa_skip_route_sync 寫 login_google/login_email_otp/
        session_duration）必須只搬 DB state，不在升級途中對 live stg SA 觸發
        per-route CF Access app 建立，也不會因 operator 不可達而中斷整個
        migration。修前 write() 無視該 context key → 對 active+tunnel 的
        protected route 無條件 _sync_access_to_operator → 此測試紅。
        """
        mc = self._mock()
        sa, route = self._active_sa(mc, protected=True)
        with patch(MOCK_GET_CLIENT, return_value=mc):
            mc.sync_access_app.reset_mock()
            route.with_context(sa_skip_route_sync=True).write({
                'login_google': True,
                'login_email_otp': True,
                'session_duration': '8h',
            })
        mc.sync_access_app.assert_not_called()
        # DB state 仍寫入（只是不打 operator）
        self.assertTrue(route.login_google)
        self.assertTrue(route.login_email_otp)
        self.assertEqual(route.session_duration, '8h')

    def test_unlink_carries_deleted_policy_cf_id(self):
        """個別 policy unlink 不可孤兒：刪前捕捉 cf_policy_id 注入
        existing_policy_ids。"""
        mc = self._mock()
        sa, route = self._active_sa(mc, protected=True)
        with patch(MOCK_GET_CLIENT, return_value=mc):
            p1 = self.Policy.create({
                'route_id': route.id, 'name': 'p1', 'decision': 'allow',
                'rule_type': 'email_domain', 'rule_value': 'a.com'})
            p2 = self.Policy.create({
                'route_id': route.id, 'name': 'p2', 'decision': 'allow',
                'rule_type': 'email_domain', 'rule_value': 'b.com'})
            p1.cf_policy_id = 'cf-a'
            p2.cf_policy_id = 'cf-b'
            mc.sync_access_app.reset_mock()
            p2.unlink()
        self.assertTrue(mc.sync_access_app.called)
        payload = mc.sync_access_app.call_args.args[0]
        self.assertIn('cf-b', payload['existing_policy_ids'])
        self.assertIn('cf-a', payload['existing_policy_ids'])
        self.assertEqual([p['name'] for p in payload['policies']], ['p1'])

    def test_unlink_all_policies_carries_every_cf_id(self):
        mc = self._mock()
        sa, route = self._active_sa(mc, protected=True)
        with patch(MOCK_GET_CLIENT, return_value=mc):
            p1 = self.Policy.create({
                'route_id': route.id, 'name': 'p1', 'decision': 'allow',
                'rule_type': 'email_domain', 'rule_value': 'a.com'})
            p2 = self.Policy.create({
                'route_id': route.id, 'name': 'p2', 'decision': 'allow',
                'rule_type': 'email_domain', 'rule_value': 'b.com'})
            p1.cf_policy_id = 'cf-a'
            p2.cf_policy_id = 'cf-b'
            mc.sync_access_app.reset_mock()
            (p1 + p2).unlink()
        self.assertTrue(mc.sync_access_app.called)
        payload = mc.sync_access_app.call_args.args[0]
        self.assertEqual(set(payload['existing_policy_ids']), {'cf-a', 'cf-b'})
        self.assertEqual(payload['policies'], [])

    def test_policy_create_sync_failure_rolls_back_db_row(self):
        from odoo.addons.woow_paas_platform.services.paas_operator import (
            PaaSOperatorError,
        )
        mc = self._mock()
        sa, route = self._active_sa(mc, protected=True)
        with patch(MOCK_GET_CLIENT, return_value=mc):
            mc.sync_access_app.side_effect = PaaSOperatorError('cf down')
            with self.assertRaises(PaaSOperatorError):
                self.Policy.create({
                    'route_id': route.id, 'name': 'ghost-pol',
                    'decision': 'allow', 'rule_type': 'email_domain',
                    'rule_value': 'ghost.com'})
        self.assertFalse(self.Policy.search([('name', '=', 'ghost-pol')]))

    def test_policy_write_sync_failure_rolls_back_decision(self):
        from odoo.addons.woow_paas_platform.services.paas_operator import (
            PaaSOperatorError,
        )
        mc = self._mock()
        sa, route = self._active_sa(mc, protected=True)
        with patch(MOCK_GET_CLIENT, return_value=mc):
            pol = self.Policy.create({
                'route_id': route.id, 'name': 'flip-pol',
                'decision': 'allow', 'rule_type': 'email_domain',
                'rule_value': 'flip.com'})
            mc.sync_access_app.side_effect = PaaSOperatorError('cf down')
            with self.assertRaises(PaaSOperatorError):
                pol.write({'decision': 'deny'})
        self.assertEqual(pol.decision, 'allow')

    def test_policy_unlink_sync_failure_rolls_back_keeps_row(self):
        from odoo.addons.woow_paas_platform.services.paas_operator import (
            PaaSOperatorError,
        )
        mc = self._mock()
        sa, route = self._active_sa(mc, protected=True)
        with patch(MOCK_GET_CLIENT, return_value=mc):
            pol = self.Policy.create({
                'route_id': route.id, 'name': 'doomed-pol',
                'decision': 'deny', 'rule_type': 'email_domain',
                'rule_value': 'doomed.com'})
            pol_id = pol.id
            mc.sync_access_app.side_effect = PaaSOperatorError('cf down')
            with self.assertRaises(PaaSOperatorError):
                pol.unlink()
        self.assertTrue(self.Policy.browse(pol_id).exists())

    def test_writeback_maps_cf_policy_id_multi_policy(self):
        """多 policy writeback 順序：zip(route.access_policy_ids.sorted, returned)
        須各自對位（sequence,id 排序）。"""
        mc = self._mock()
        mc.sync_access_app.return_value = {
            'app_id': 'app-1', 'policy_ids': ['cf-low', 'cf-high'],
            'allowed_idps': ['idp-g']}
        sa, route = self._active_sa(mc, protected=True)
        with patch(MOCK_GET_CLIENT, return_value=mc):
            pol_high = self.Policy.create({
                'route_id': route.id, 'name': 'high', 'decision': 'deny',
                'rule_type': 'email_domain', 'rule_value': 'high.com',
                'sequence': 20})
            pol_low = self.Policy.create({
                'route_id': route.id, 'name': 'low', 'decision': 'allow',
                'rule_type': 'email_domain', 'rule_value': 'low.com',
                'sequence': 5})
        self.assertEqual(pol_low.cf_policy_id, 'cf-low')
        self.assertEqual(pol_high.cf_policy_id, 'cf-high')

    def test_unresolved_idp_types_persisted_per_route(self):
        """operator 的 unresolved_idp_types（fail-open 訊號）落 route 欄位 +
        route.to_dict 暴露。"""
        mc = self._mock()
        mc.sync_access_app.return_value = {
            'app_id': 'app-1', 'policy_ids': [], 'allowed_idps': ['idp-otp'],
            'unresolved_idp_types': ['google']}
        sa, route = self._active_sa(mc, protected=True)
        with patch(MOCK_GET_CLIENT, return_value=mc):
            route.write({'login_google': True})
        self.assertEqual(route.access_unresolved_idp_types, 'google')
        self.assertEqual(route.to_dict()['access_unresolved_idp_types'],
                         'google')
        # 後續乾淨 sync 清除舊警告
        mc.sync_access_app.return_value = {
            'app_id': 'app-1', 'policy_ids': [], 'allowed_idps': ['idp-g'],
            'unresolved_idp_types': []}
        with patch(MOCK_GET_CLIENT, return_value=mc):
            route.write({'session_duration': '8h'})
        self.assertFalse(route.access_unresolved_idp_types)

    def test_error_state_with_live_tunnel_still_syncs(self):
        """liveness gate：SA 'error' + live tunnel → route access 仍 sync。"""
        mc = self._mock()
        sa, route = self._active_sa(mc, protected=True)
        with patch(MOCK_GET_CLIENT, return_value=mc):
            sa.write({'state': 'error'})
            mc.sync_access_app.reset_mock()
            route.write({'session_duration': '8h'})
            config_synced = mc.sync_access_app.called
            mc.sync_access_app.reset_mock()
            self.Policy.create({
                'route_id': route.id, 'name': 'err-pol', 'decision': 'allow',
                'rule_type': 'email_domain', 'rule_value': 'err.com'})
            policy_synced = mc.sync_access_app.called
        self.assertTrue(config_synced)
        self.assertTrue(policy_synced)

    def test_pending_sa_route_access_does_not_sync(self):
        """liveness gate：pending（no tunnel）SA 上的 route access 變動不打
        operator。"""
        mc = MagicMock()
        with patch(MOCK_GET_CLIENT, return_value=mc):
            sa = self.SA.create({'name': 'AS pending',
                                 'workspace_id': self.workspace.id})
            route = self.Route.create({
                'security_access_id': sa.id,
                'subdomain_prefix': sa.canonical_name,
                'is_primary': True, 'is_protected': True})
            pol = self.Policy.create({
                'route_id': route.id, 'name': 'pend', 'decision': 'allow',
                'rule_type': 'email_domain', 'rule_value': 'pend.com'})
            pol.write({'decision': 'deny'})
            route.write({'session_duration': '8h'})
            pol.unlink()
        mc.sync_access_app.assert_not_called()


class TestProvisionPushesRouteAccess(TransactionCase):
    """configure-then-provision：route 在 pending 設 protected，provision 後
    每條 protected route 各 push 自己的 Access app。"""

    def setUp(self):
        super().setUp()
        self.SA = self.env['woow_paas_platform.security_access']
        self.Route = self.env['woow_paas_platform.security_access_route']
        self.Policy = self.env[
            'woow_paas_platform.security_access_access_policy']
        self.workspace = self.env['woow_paas_platform.workspace'].create(
            {'name': 'PPA WS'})

    def _mock(self):
        m = MagicMock()
        m.create_tunnel.return_value = {
            'tunnel_id': 'tun-ppa', 'tunnel_token': 'tok',
            'tunnel_name': 'paas-sa-x-y', 'dns_record_id': 'rec-1'}
        m.sync_tunnel_routes.return_value = {'routes': []}
        m.sync_access_app.return_value = {
            'app_id': 'app-ppa', 'policy_ids': ['pol-ppa'], 'allowed_idps': []}
        return m

    def test_first_provision_pushes_preconfigured_route_access(self):
        mc = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=mc):
            sa = self.SA.create({'name': 'PPA GW',
                                  'workspace_id': self.workspace.id})
            # 在 pending 建一條 protected route + policy（不應 sync）
            route = self.Route.create({
                'security_access_id': sa.id,
                'subdomain_prefix': sa.canonical_name,
                'is_primary': True, 'is_protected': True,
                'login_google': True})
            self.Policy.create({
                'route_id': route.id, 'name': 'corp', 'decision': 'allow',
                'rule_type': 'email_domain', 'rule_value': 'acme.com'})
            self.assertFalse(mc.sync_access_app.called,
                             "pending SA must not push Access prematurely")
            sa.action_provision()
        self.assertEqual(sa.state, 'active')
        self.assertTrue(mc.sync_access_app.called,
                        "provision must push pre-configured per-route Access")
        self.assertEqual(route.access_app_id, 'app-ppa')

    def test_provision_unprotected_routes_no_access_sync(self):
        mc = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=mc):
            sa = self.SA.create({'name': 'PPA GW2',
                                  'workspace_id': self.workspace.id})
            sa.action_provision()
        self.assertEqual(sa.state, 'active')
        # 預設 primary route 由 provision 建立且 is_protected=False → 無 access sync
        self.assertFalse(mc.sync_access_app.called)


class TestRouteAccessCleanup(TransactionCase):
    """SA 刪除時逐 protected route teardown 各自的 Access app。"""

    def setUp(self):
        super().setUp()
        self.SA = self.env['woow_paas_platform.security_access']
        self.Route = self.env['woow_paas_platform.security_access_route']
        self.workspace = self.env['woow_paas_platform.workspace'].create(
            {'name': 'AL WS'})

    def _mock(self):
        m = MagicMock()
        m.sync_tunnel_routes.return_value = {'routes': []}
        m.sync_access_app.return_value = {'app_id': None, 'policy_ids': [],
                                          'allowed_idps': []}
        return m

    def test_cleanup_tears_down_each_route_access(self):
        mc = self._mock()
        mc.sync_access_app.return_value = {
            'app_id': 'app-al', 'policy_ids': [], 'allowed_idps': []}
        with patch(MOCK_GET_CLIENT, return_value=mc):
            sa = self.SA.create({'name': 'AL GW',
                                 'workspace_id': self.workspace.id})
            sa.write({'state': 'active', 'tunnel_id': 'tun-al'})
            route = self.Route.create({
                'security_access_id': sa.id,
                'subdomain_prefix': sa.canonical_name,
                'is_primary': True, 'is_protected': True})
            # route.access_app_id 已回填 app-al
            self.assertEqual(route.access_app_id, 'app-al')
            # cleanup 用 enabled=False teardown
            mc.sync_access_app.return_value = {
                'app_id': None, 'policy_ids': [], 'allowed_idps': []}
            mc.sync_access_app.reset_mock()
            sa._do_cleanup()
        self.assertTrue(mc.sync_access_app.called)
        payload = mc.sync_access_app.call_args.args[0]
        self.assertFalse(payload['enabled'])
        self.assertEqual(payload['existing_app_id'], 'app-al')
        self.assertFalse(route.access_app_id)

    def test_cleanup_access_teardown_failure_does_not_report_clean(self):
        mc = self._mock()
        mc.sync_access_app.return_value = {
            'app_id': 'app-al-err', 'policy_ids': [], 'allowed_idps': []}
        with patch(MOCK_GET_CLIENT, return_value=mc):
            sa = self.SA.create({'name': 'AL GW err',
                                 'workspace_id': self.workspace.id})
            sa.write({'state': 'active', 'tunnel_id': 'tun-al-err'})
            route = self.Route.create({
                'security_access_id': sa.id,
                'subdomain_prefix': sa.canonical_name,
                'is_primary': True, 'is_protected': True})
            self.assertEqual(route.access_app_id, 'app-al-err')
            mc.sync_access_app.reset_mock()
            mc.sync_access_app.side_effect = Exception("operator down")
            sa._do_cleanup()
        # tunnel delete OK → tunnel_id 清除
        self.assertFalse(sa.tunnel_id)
        # access teardown 失敗 → route.access_app_id 仍在（不孤兒遺忘）
        self.assertEqual(route.access_app_id, 'app-al-err')
        self.assertEqual(sa.state, 'error')
        self.assertTrue(sa.error_message)


class TestAccessPolicyAcl(TransactionCase):
    """policy ir.rule (P2.5 Phase 2) — 3 條 access_policy rule 經 stored related
    security_access_id walk (security_access_id.workspace_id.access_ids...) 在
    read / create / write / unlink + cross-workspace 全路徑正確。

    為何 related-store walk 足夠（不需改成 route_id.security_access_id...walk）:
    security_access_id 是 store=True related on route_id.security_access_id，
    materialise 成真實 DB 欄位。create 時 Odoo 先 super().create() 物化 row、
    recompute + flush stored related 欄位，**才**對 create 動作跑
    _apply_ir_rules（用 SQL 依 rule domain 重讀 row 過濾）。所以 create ir.rule
    檢查讀到的 security_access_id 欄位已 populate，不是 stale/null。
    test_create_with_route_materialises_related_for_ir_rule 與
    test_owner_create_with_route_passes_create_rule 是這條時序的守線：
    若 related store 在 create ir.rule 檢查前未 flush，owner create 會被
    _owner_admin_manage rule (domain 走 security_access_id) 誤判跨/null SA 而擋。
    """

    def setUp(self):
        super().setUp()
        User = self.env['res.users']
        self.alice = User.create({
            'name': 'AP Alice', 'login': 'ap_acl_alice',
            'email': 'ap_acl_alice@test.example',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])]})
        self.mallory = User.create({
            'name': 'AP Mallory', 'login': 'ap_acl_mallory',
            'email': 'ap_acl_mallory@test.example',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])]})
        self.ws = self.env['woow_paas_platform.workspace'].create({
            'name': 'AP ACL WS', 'owner_id': self.alice.id})
        Access = self.env['woow_paas_platform.workspace_access']
        if not Access.search([('workspace_id', '=', self.ws.id),
                              ('user_id', '=', self.alice.id)]):
            Access.create({'workspace_id': self.ws.id,
                           'user_id': self.alice.id, 'role': 'owner'})
        self.sa = self.env['woow_paas_platform.security_access'].create({
            'name': 'AP ACL GW', 'workspace_id': self.ws.id})
        self.route = self.env['woow_paas_platform.security_access_route'].create({
            'security_access_id': self.sa.id,
            'subdomain_prefix': 'ap-acl-route'})
        self.pol = self.env[
            'woow_paas_platform.security_access_access_policy'].create({
                'route_id': self.route.id, 'name': 'p',
                'decision': 'allow', 'rule_type': 'email_domain',
                'rule_value': 'a.com'})

    def test_member_can_read_policy(self):
        v = self.env['woow_paas_platform.security_access_access_policy'
                     ].with_user(self.alice).search([('id', '=', self.pol.id)])
        self.assertEqual(len(v), 1)

    def test_non_member_cannot_read_policy(self):
        v = self.env['woow_paas_platform.security_access_access_policy'
                     ].with_user(self.mallory).search([('id', '=', self.pol.id)])
        self.assertEqual(len(v), 0)

    def _add_user_member(self):
        carol = self.env['res.users'].create({
            'name': 'AP Carol', 'login': 'ap_acl_carol',
            'email': 'ap_acl_carol@test.example',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])]})
        self.env['woow_paas_platform.workspace_access'].create({
            'workspace_id': self.ws.id, 'user_id': carol.id, 'role': 'user'})
        return carol

    def test_user_role_member_can_read_but_not_write_policy(self):
        from odoo.exceptions import AccessError
        carol = self._add_user_member()
        visible = self.env[
            'woow_paas_platform.security_access_access_policy'].with_user(
                carol).search([('id', '=', self.pol.id)])
        self.assertEqual(len(visible), 1)
        with self.assertRaises(AccessError):
            self.pol.with_user(carol).write({'decision': 'deny'})

    def test_user_role_member_cannot_create_or_unlink_policy(self):
        from odoo.exceptions import AccessError
        carol = self._add_user_member()
        with self.assertRaises(AccessError):
            self.env[
                'woow_paas_platform.security_access_access_policy'].with_user(
                    carol).create({
                        'route_id': self.route.id, 'name': 'evil',
                        'decision': 'allow', 'rule_type': 'email_domain',
                        'rule_value': 'attacker.com'})
        with self.assertRaises(AccessError):
            self.pol.with_user(carol).unlink()

    def test_owner_can_mutate_policy(self):
        new_pol = self.env[
            'woow_paas_platform.security_access_access_policy'].with_user(
                self.alice).create({
                    'route_id': self.route.id, 'name': 'owner-pol',
                    'decision': 'allow', 'rule_type': 'email_domain',
                    'rule_value': 'owner.com'})
        self.assertTrue(new_pol.exists())
        new_pol.with_user(self.alice).write({'decision': 'deny'})
        self.assertEqual(new_pol.decision, 'deny')
        new_pol.with_user(self.alice).unlink()
        self.assertFalse(new_pol.exists())

    # --- create-time related-store materialisation (Phase 2 timing守線) --- #
    def test_create_with_route_materialises_related_for_ir_rule(self):
        """create policy 帶 route_id → stored related security_access_id 在
        create ir.rule 檢查前已 populate（非 stale/null）。沒被 admin sudo 繞:
        以 owner alice (base.group_user, 非 group_system) create，必須通過走
        security_access_id 的 _owner_admin_manage create rule。若 related store
        在 create 階段尚未 flush，rule domain 比對到 null SA → AccessError，本
        測試紅。綠證明 related-store walk 在 create 路徑安全（不需改 walk）。"""
        Policy = self.env[
            'woow_paas_platform.security_access_access_policy']
        pol = Policy.with_user(self.alice).create({
            'route_id': self.route.id, 'name': 'rel-store',
            'decision': 'allow', 'rule_type': 'email_domain',
            'rule_value': 'rel.com'})
        # stored related materialised to the route's SA (the real DB column the
        # ir.rule domain traversed at create time).
        self.assertEqual(pol.security_access_id, self.sa)

    # --- cross-workspace deny (B1-class data leak 守線) --- #
    def _make_other_workspace_route(self):
        """獨立 workspace B（owner=mallory）+ 自己的 SA + route，與 alice 的
        workspace A 無交集。workspace.create 會自動給 owner_id 一筆 owner
        workspace_access，所以 mallory 對 B 是 owner、對 A 無任何 access。"""
        wsB = self.env['woow_paas_platform.workspace'].create({
            'name': 'AP ACL WS-B', 'owner_id': self.mallory.id})
        saB = self.env['woow_paas_platform.security_access'].create({
            'name': 'AP ACL GW-B', 'workspace_id': wsB.id})
        routeB = self.env[
            'woow_paas_platform.security_access_route'].create({
                'security_access_id': saB.id,
                'subdomain_prefix': 'ap-acl-route-b'})
        return wsB, saB, routeB

    def test_cross_workspace_owner_cannot_read_policy(self):
        """B workspace 的 owner (mallory) 不可讀 A workspace route 的 policy。
        mallory 在 B 有 owner role，但 walk A.policy.security_access_id.
        workspace_id.access_ids 不含 mallory → search 不可見。"""
        self._make_other_workspace_route()
        v = self.env[
            'woow_paas_platform.security_access_access_policy'].with_user(
                self.mallory).search([('id', '=', self.pol.id)])
        self.assertEqual(len(v), 0)

    def test_cross_workspace_owner_cannot_write_policy(self):
        """跨 workspace owner 不可改 A 的 policy（write rule 走 SA→ws→
        access_ids any owner/admin 只匹配 A 的 access 列，mallory 不在其中）。"""
        from odoo.exceptions import AccessError
        self._make_other_workspace_route()
        with self.assertRaises(AccessError):
            self.pol.with_user(self.mallory).write({'decision': 'deny'})

    def test_cross_workspace_owner_cannot_unlink_policy(self):
        from odoo.exceptions import AccessError
        self._make_other_workspace_route()
        with self.assertRaises(AccessError):
            self.pol.with_user(self.mallory).unlink()

    def test_cross_workspace_cannot_create_policy_on_foreign_route(self):
        """B 的 owner 不可在 A 的 route 上 create policy（create rule 走
        security_access_id→A.workspace→access_ids any owner/admin，mallory 不在
        → AccessError）。擋的是「持有自己 workspace 卻往別人 route 灌 policy」
        的跨租戶提權面。"""
        from odoo.exceptions import AccessError
        self._make_other_workspace_route()
        with self.assertRaises(AccessError):
            self.env[
                'woow_paas_platform.security_access_access_policy'].with_user(
                    self.mallory).create({
                        'route_id': self.route.id, 'name': 'cross-evil',
                        'decision': 'allow', 'rule_type': 'email_domain',
                        'rule_value': 'attacker.com'})

    def test_owner_create_with_route_passes_create_rule(self):
        """正向對照：A 的 owner 在自己 route 上 create policy 成功，且新 row
        security_access_id 正確（與上面 cross-workspace create-deny 對稱，確保
        deny 不是因為 rule 把所有 create 都擋掉的假綠）。"""
        Policy = self.env[
            'woow_paas_platform.security_access_access_policy']
        pol = Policy.with_user(self.alice).create({
            'route_id': self.route.id, 'name': 'own-create',
            'decision': 'allow', 'rule_type': 'email_domain',
            'rule_value': 'own.com'})
        self.assertTrue(pol.exists())
        self.assertEqual(pol.security_access_id, self.sa)
