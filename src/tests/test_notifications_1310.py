"""#1310：/api/notifications（header 鈴鐺通知中心）。

鈴鐺過去無 handler、紅點寫死；本端點彙整既有狀態成通知清單。測試走真實
HTTP routing（HttpCase＋JSON-RPC），覆蓋：
- 試用將結束／續約點數不足（含稅）只在 3 天視窗內出現
- error 狀態服務只列「自己可存取、未封存」工作區的
- 錢包異動是資訊類、不算待處理
- Portal 會員看不到別人的通知（隔離）
"""
from datetime import date, timedelta

from odoo.tests.common import HttpCase, new_test_user, tagged

from ..models.cloud_service import _internal_provision


@tagged('post_install', '-at_install')
class TestNotificationsEndpoint1310(HttpCase):

    def setUp(self):
        super().setUp()
        self.admin_user = self.env.ref('base.user_admin')
        self.partner = self.admin_user.partner_id
        self.Subscription = self.env['sh.subscription.subscription'].sudo()
        self.Plan = self.env['sh.subscription.plan'].sudo()
        self.Workspace = self.env['woow_paas_platform.workspace'].sudo()
        self.CloudService = self.env['woow_paas_platform.cloud_service'].sudo()
        self.Template = self.env['woow_paas_platform.cloud_app_template'].sudo()
        # Deterministic fixture: no pre-existing access rows / active subs /
        # wallet history leak into the assertions (transaction rolls back).
        self.env['woow_paas_platform.workspace_access'].sudo().search(
            [('user_id', '=', self.admin_user.id)]).unlink()
        self.Subscription.search([
            ('sh_partner_id', '=', self.partner.id), ('state', '=', 'in_progress'),
        ]).write({'state': 'close'})
        self.env['wallet.transaction'].sudo().search(
            [('partner_id', '=', self.partner.id)]).unlink()
        self._set_wallet(0.0)
        self.plan = self.Plan.create({
            'name': 'Notify Test Plan',
            'sh_duration': 1,
            'sh_unit': 'month',
            'sh_plan_price': 499.0,
            'sh_no_of_billing_cycle': -1,
        })
        self.tax_15 = self.env['account.tax'].sudo().create({
            'name': 'Notify Test Tax 15%',
            'amount_type': 'percent',
            'amount': 15.0,
            'type_tax_use': 'sale',
            'price_include_override': 'tax_excluded',
        })

    # -- helpers ---------------------------------------------------------

    def _set_wallet(self, target):
        card = self.partner.sudo().get_wallet_loyalty_card()
        if card:
            card.points = 0
        if target > 0:
            self.partner.sudo().add_wallet_points(target, description='Notify test seed')
        self.partner.sudo().invalidate_recordset(['wallet_balance'])

    def _notifications(self, login='admin', password='admin'):
        self.authenticate(login, password)
        body = self.url_open(
            '/api/notifications',
            data='{"jsonrpc":"2.0","method":"call","params":{}}',
            headers={'Content-Type': 'application/json'},
        ).json()
        result = body.get('result')
        self.assertTrue(result and result.get('success'), body)
        return result['data']

    def _sub(self, **vals):
        base = {
            'sh_partner_id': self.partner.id,
            'sh_subscription_plan_id': self.plan.id,
            'sh_plan_price': 499.0,
            'sh_recurrency': 1,
            'sh_unit': 'month',
            'sh_start_date': date.today(),
            'sh_date_of_next_payment': date.today() + timedelta(days=30),
            'sh_taxes_ids': [(6, 0, [self.tax_15.id])],
            'state': 'in_progress',
        }
        base.update(vals)
        return self.Subscription.create(base)

    def _types(self, data):
        return [item['type'] for item in data['items']]

    def _workspace_with_service(self, owner, state, name='Notify Svc'):
        template = self.Template.create({
            'name': 'Notify Test App %s' % name,
            'slug': 'notify-test-app-%s' % name.lower().replace(' ', '-'),
            'helm_repo_url': '',
            'helm_chart_name': 'oci://test/chart',
            'helm_chart_version': '1.0.0',
        })
        workspace = self.Workspace.with_user(owner).create({'name': 'Notify WS %s' % name})
        service = _internal_provision(self.CloudService).create({
            'name': name,
            'workspace_id': workspace.id,
            'template_id': template.id,
            'state': state,
        })
        return workspace, service

    # -- tests -----------------------------------------------------------

    def test_empty_state(self):
        data = self._notifications()
        self.assertEqual(data['items'], [])
        self.assertEqual(data['actionable_count'], 0)

    def test_trial_ending_only_inside_window(self):
        today = date.today()
        soon = self._sub(sh_trial=True, sh_trial_end_date=today + timedelta(days=2),
                         sh_date_of_next_payment=today + timedelta(days=3))
        self._sub(sh_trial=True, sh_trial_end_date=today + timedelta(days=10),
                  sh_date_of_next_payment=today + timedelta(days=11))
        self._set_wallet(10000.0)
        data = self._notifications()
        trial = [i for i in data['items'] if i['type'] == 'trial_ending']
        self.assertEqual(len(trial), 1, data)
        self.assertEqual(trial[0]['severity'], 'warning')
        self.assertEqual(trial[0]['route'], 'billing/subscriptions')
        self.assertIn(str(soon.id), trial[0]['id'])
        # 499 + 15% tax — the amount the charge will really deduct.
        self.assertIn('573.85', trial[0]['message'])
        self.assertEqual(data['actionable_count'], 1)

    def test_renewal_low_balance_uses_tax_included_total(self):
        self._sub(sh_date_of_next_payment=date.today() + timedelta(days=1))
        # 520 covers the untaxed 499 but not the taxed 573.85.
        self._set_wallet(520.0)
        data = self._notifications()
        low = [i for i in data['items'] if i['type'] == 'renewal_low_balance']
        self.assertEqual(len(low), 1, data)
        self.assertEqual(low[0]['severity'], 'error')
        self.assertEqual(low[0]['route'], 'billing/topup')

        self._set_wallet(1000.0)
        data = self._notifications()
        self.assertNotIn('renewal_low_balance', self._types(data))

    def test_service_error_listed_with_route(self):
        workspace, service = self._workspace_with_service(self.admin_user, 'error', 'Broken')
        self._workspace_with_service(self.admin_user, 'running', 'Healthy')
        data = self._notifications()
        errors = [i for i in data['items'] if i['type'] == 'service_error']
        self.assertEqual(len(errors), 1, data)
        self.assertEqual(errors[0]['route'], 'workspace/%s/services/%s' % (workspace.id, service.id))
        self.assertEqual(errors[0]['severity'], 'error')

    def test_service_error_in_archived_workspace_is_hidden(self):
        workspace, _service = self._workspace_with_service(self.admin_user, 'error', 'Archived')
        # #1297 now refuses to archive a workspace that still has services, so
        # this state only exists as legacy data (archived before the guard).
        # Reproduce that row shape directly instead of going through write().
        self.env.cr.execute(
            "UPDATE woow_paas_platform_workspace SET state = 'archived' WHERE id = %s",
            (workspace.id,))
        workspace.invalidate_recordset(['state'])
        data = self._notifications()
        self.assertNotIn('service_error', self._types(data))

    def test_wallet_activity_is_informational(self):
        self._set_wallet(300.0)
        data = self._notifications()
        activity = [i for i in data['items'] if i['type'] == 'wallet_activity']
        self.assertEqual(len(activity), 1, data)
        self.assertEqual(activity[0]['severity'], 'info')
        self.assertEqual(data['actionable_count'], 0)
        # Datetimes carry an explicit UTC offset for correct local rendering.
        self.assertTrue(activity[0]['ts'].endswith('+00:00'), activity[0]['ts'])

    def test_portal_member_sees_only_own_items(self):
        # Admin has an erroring service, a low-balance renewal and activity.
        self._workspace_with_service(self.admin_user, 'error', 'Admin Only')
        self._sub(sh_date_of_next_payment=date.today() + timedelta(days=1))
        self._set_wallet(10.0)
        new_test_user(self.env, login='notify_portal_1310', password='notify_portal_1310',
                      groups='base.group_portal')
        data = self._notifications('notify_portal_1310', 'notify_portal_1310')
        self.assertEqual(data['items'], [])
        self.assertEqual(data['actionable_count'], 0)

    def test_requires_login(self):
        body = self.url_open(
            '/api/notifications',
            data='{"jsonrpc":"2.0","method":"call","params":{}}',
            headers={'Content-Type': 'application/json'},
        ).json()
        self.assertIn('error', body)
        self.assertNotIn('result', body)
