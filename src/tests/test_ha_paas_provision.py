"""HA PaaS 開通/狀態端點 ＋ 每帳號限一 DB backstop 測試（#336）。

- TestProvisionUniqueIndex：provision_partner_id 欄位 ＋ partial unique index。
- TestHAPaaSProvisionEndpoint：provision/status 端點契約 v1.1 shape 與冪等（Task 6）。
"""
import json
from datetime import timedelta

from odoo import fields
from odoo.tests.common import HttpCase, TransactionCase


class TestProvisionUniqueIndex(TransactionCase):
    """provision_partner_id 欄位 ＋ partial unique index（每帳號限一 DB backstop）。"""

    def test_field_exists(self):
        self.assertIn('provision_partner_id',
                      self.env['woow_paas_platform.cloud_service']._fields)

    def test_partial_unique_index_present(self):
        self.env.cr.execute("""
            SELECT indexdef FROM pg_indexes
            WHERE tablename = 'woow_paas_platform_cloud_service'
              AND indexname LIKE '%%provision_partner%%'
        """)
        rows = self.env.cr.fetchall()
        self.assertTrue(rows, 'partial unique index 應存在')
        self.assertIn('provision_partner_id', rows[0][0])
        self.assertIn('IS NOT NULL', rows[0][0].upper())

    def test_second_provision_row_same_partner_rejected(self):
        partner = self.env['res.partner'].create({'name': 'Dup Provision'})
        tpl = self.env.ref('woow_paas_platform.cloud_app_ha_paas')
        ws1 = self.env['woow_paas_platform.workspace'].create(
            {'name': 'w1', 'owner_id': self.env.user.id})
        ws2 = self.env['woow_paas_platform.workspace'].create(
            {'name': 'w2', 'owner_id': self.env.user.id})
        self.env['woow_paas_platform.cloud_service'].create({
            'workspace_id': ws1.id, 'template_id': tpl.id, 'name': 'ha1',
            'reference_id': 'r1', 'state': 'running', 'subdomain': 's1',
            'provision_partner_id': partner.id})
        from psycopg2 import IntegrityError
        with self.assertRaises(IntegrityError):
            with self.env.cr.savepoint():
                self.env['woow_paas_platform.cloud_service'].create({
                    'workspace_id': ws2.id, 'template_id': tpl.id, 'name': 'ha2',
                    'reference_id': 'r2', 'state': 'running', 'subdomain': 's2',
                    'provision_partner_id': partner.id})


class TestHAPaaSProvisionEndpoint(HttpCase):
    """provision/status 端點契約 v1.1 shape 與冪等（Task 6）。

    落地對 plan Task 6 Step 1 的修正（grep 實檔重核）：
    - oauth_token 欄位是 ``scope``（單數，非 plan 誤植的 ``scopes``）。
    - ``verify_oauth_token`` 先驗 ``is_access_token_valid()``（讀 ``expires_at``、
      ``datetime < False`` 會 TypeError）再驗 scope，故**每個** token fixture（含
      403 用的）都必給未來 ``expires_at``，否則會走成 401/500 而非預期碼。
    - 工作項5 stopgap 落地後 seed 已 is_active=True ＋非 TBD chart → 直接 deployable，
      故各 provision-flow 測試直接用 shipped seed（不再暫翻）；503 gate 改由測試顯式
      停用 template 觸發（見 ``test_provision_blocked_when_template_not_deployable``）。
    """

    def setUp(self):
        super().setUp()
        self.user = self.env['res.users'].create({
            'name': 'Prov User', 'login': 'provuser@test', 'email': 'provuser@test'})
        self.client = self.env.ref('woow_paas_platform.oauth_client_ha_app')
        self.token = self.env['woow_paas_platform.oauth_token'].create({
            'access_token': 'ha-provision-test-token', 'user_id': self.user.id,
            'client_id': self.client.id, 'scope': 'ha:provision',
            'expires_at': fields.Datetime.now() + timedelta(hours=1)})

    def _post(self, path, token='ha-provision-test-token'):
        # url_open 傳 data 即走 POST（Odoo 18 無 method 參數）
        return self.url_open(path, data='{}',
            headers={'Authorization': 'Bearer %s' % token,
                     'Content-Type': 'application/json'})

    def _get(self, path, token='ha-provision-test-token'):
        return self.url_open(path,
            headers={'Authorization': 'Bearer %s' % token})

    def test_status_none_before_provision(self):
        r = self._get('/api/ha-paas/status')
        self.assertEqual(r.status_code, 200)
        body = json.loads(r.content)
        self.assertEqual(body['status'], 'none')
        self.assertNotIn('ha_url', body)

    def test_missing_token_401(self):
        self.assertEqual(self.url_open('/api/ha-paas/status').status_code, 401)

    def test_insufficient_scope_403(self):
        self.env['woow_paas_platform.oauth_token'].create({
            'access_token': 'no-provision-scope', 'user_id': self.user.id,
            'client_id': self.client.id, 'scope': 'workspace:read',
            'expires_at': fields.Datetime.now() + timedelta(hours=1)})
        self.assertEqual(self._get('/api/ha-paas/status',
                                   token='no-provision-scope').status_code, 403)

    def test_seed_template_deployable_out_of_box(self):
        # 工作項5 stopgap 落地坐實：shipped seed 直接 deployable（is_active=True ＋
        # helm_chart_name 非 TBD 佔位）→ provision 直接建 service 回 202、不再 503。
        tpl = self.env.ref('woow_paas_platform.cloud_app_ha_paas')
        self.assertTrue(tpl.is_active, 'seed 應 is_active=True')
        self.assertFalse((tpl.helm_chart_name or '').startswith('TBD'),
                         'seed helm_chart_name 應為 pin 的 OCI、非 TBD 佔位')
        r = self._post('/api/ha-paas/provision')
        self.assertEqual(r.status_code, 202)
        self.assertEqual(json.loads(r.content)['status'], 'provisioning')
        self.assertEqual(
            self.env['woow_paas_platform.cloud_service'].search_count(
                [('provision_partner_id', '=', self.user.partner_id.id)]), 1,
            'deployable seed 的 provision 應建立 cloud_service')

    def test_provision_blocked_when_template_not_deployable(self):
        # 顯式停用 template（模擬 image 未備／維運停機）→ 不 deployable → 503 且不留 service
        self.env.ref('woow_paas_platform.cloud_app_ha_paas').write(
            {'is_active': False})
        r = self._post('/api/ha-paas/provision')
        self.assertEqual(r.status_code, 503)
        self.assertFalse(self.env['woow_paas_platform.cloud_service'].search(
            [('provision_partner_id', '=', self.user.partner_id.id)]),
            '未 deployable 時不得留下 cloud_service')

    def test_provision_idempotent_per_partner(self):
        r1 = self._post('/api/ha-paas/provision')
        self.assertIn(r1.status_code, (200, 202))
        CS = self.env['woow_paas_platform.cloud_service']
        self._post('/api/ha-paas/provision')
        self.assertEqual(CS.search_count(
            [('provision_partner_id', '=', self.user.partner_id.id)]), 1,
            '第二次 provision 不得再建')

    def test_status_ready_gives_ha_url_only_when_running(self):
        self._post('/api/ha-paas/provision')
        svc = self.env['woow_paas_platform.cloud_service'].search(
            [('provision_partner_id', '=', self.user.partner_id.id)], limit=1)
        svc.state = 'running'
        body = json.loads(self._get('/api/ha-paas/status').content)
        self.assertEqual(body['status'], 'ready')
        self.assertTrue(body.get('ha_url', '').startswith('https://'))

    def test_provision_running_returns_ready_with_ha_url(self):
        self._post('/api/ha-paas/provision')
        svc = self.env['woow_paas_platform.cloud_service'].search(
            [('provision_partner_id', '=', self.user.partner_id.id)], limit=1)
        svc.state = 'running'
        r = self._post('/api/ha-paas/provision')
        self.assertEqual(r.status_code, 200)
        body = json.loads(r.content)
        self.assertEqual(body['status'], 'ready')
        self.assertTrue(body['ha_url'].startswith('https://'))

    def test_provision_error_state_flips_pending_and_retries(self):
        self._post('/api/ha-paas/provision')
        CS = self.env['woow_paas_platform.cloud_service']
        svc = CS.search(
            [('provision_partner_id', '=', self.user.partner_id.id)], limit=1)
        svc.write({'state': 'error', 'error_message': 'boom'})
        r = self._post('/api/ha-paas/provision')
        self.assertEqual(r.status_code, 202)
        self.assertEqual(json.loads(r.content)['status'], 'provisioning')
        svc.invalidate_recordset()
        self.assertEqual(svc.state, 'pending', 'error→pending 重試')
        self.assertEqual(CS.search_count(
            [('provision_partner_id', '=', self.user.partner_id.id)]), 1,
            '重試不得再建 service')

    def test_provision_suspended_returns_409(self):
        self._post('/api/ha-paas/provision')
        svc = self.env['woow_paas_platform.cloud_service'].search(
            [('provision_partner_id', '=', self.user.partner_id.id)], limit=1)
        svc.state = 'suspended'
        r = self._post('/api/ha-paas/provision')
        self.assertEqual(r.status_code, 409)
        self.assertEqual(json.loads(r.content)['status'], 'suspended')

    def test_provision_deleting_returns_409_no_revive(self):
        self._post('/api/ha-paas/provision')
        svc = self.env['woow_paas_platform.cloud_service'].search(
            [('provision_partner_id', '=', self.user.partner_id.id)], limit=1)
        svc.state = 'deleting'
        r = self._post('/api/ha-paas/provision')
        self.assertEqual(r.status_code, 409)
        self.assertEqual(json.loads(r.content)['status'], 'deleting')
        # 不復活：state 維持 deleting（未被翻回 pending）
        svc.invalidate_recordset()
        self.assertEqual(svc.state, 'deleting')

    def test_status_suspended_and_deleting_direct_map(self):
        self._post('/api/ha-paas/provision')
        svc = self.env['woow_paas_platform.cloud_service'].search(
            [('provision_partner_id', '=', self.user.partner_id.id)], limit=1)
        for state, expected in (('suspended', 'suspended'), ('deleting', 'deleting')):
            svc.state = state
            body = json.loads(self._get('/api/ha-paas/status').content)
            self.assertEqual(body['status'], expected, '契約 v1.1 直映')
            self.assertNotIn('ha_url', body, '非 ready 不給 ha_url')

    def test_status_exhaustive_state_mapping(self):
        # 窮舉 8 個 cloud_service.state → 契約 v1.1 映射正確、ha_url 僅 ready
        self._post('/api/ha-paas/provision')
        svc = self.env['woow_paas_platform.cloud_service'].search(
            [('provision_partner_id', '=', self.user.partner_id.id)], limit=1)
        cases = {
            'pending': 'provisioning', 'deploying': 'provisioning',
            'initializing': 'provisioning', 'upgrading': 'provisioning',
            'running': 'ready', 'error': 'error',
            'suspended': 'suspended', 'deleting': 'deleting',
        }
        for state, expected in cases.items():
            svc.state = state
            body = json.loads(self._get('/api/ha-paas/status').content)
            self.assertEqual(body['status'], expected, 'state=%s' % state)
            if expected == 'ready':
                self.assertIn('ha_url', body)
            else:
                self.assertNotIn('ha_url', body, 'ha_url 僅 ready（state=%s）' % state)

    def test_partner_scoped_isolation(self):
        # 每帳號限一以 partner 為鍵：user B 的 token 看不到 user A 的 HA
        self._post('/api/ha-paas/provision')  # user A = self.user
        user_b = self.env['res.users'].create({
            'name': 'Prov User B', 'login': 'provuserb@test',
            'email': 'provuserb@test'})
        self.env['woow_paas_platform.oauth_token'].create({
            'access_token': 'token-user-b', 'user_id': user_b.id,
            'client_id': self.client.id, 'scope': 'ha:provision',
            'expires_at': fields.Datetime.now() + timedelta(hours=1)})
        body = json.loads(
            self._get('/api/ha-paas/status', token='token-user-b').content)
        self.assertEqual(body['status'], 'none',
                         'user B 不得看到 user A 的 HA（partner-scoped）')

    def test_provision_error_retry_does_not_inline_redeploy(self):
        # error→re-POST 只翻 pending + 觸發 cron，不 inline 呼叫 action_redeploy
        from unittest.mock import patch
        self._post('/api/ha-paas/provision')
        svc = self.env['woow_paas_platform.cloud_service'].search(
            [('provision_partner_id', '=', self.user.partner_id.id)], limit=1)
        svc.write({'state': 'error'})
        with patch(
            'odoo.addons.woow_paas_platform.models.cloud_service.'
            'CloudService.action_redeploy'
        ) as mock_redeploy:
            r = self._post('/api/ha-paas/provision')
        self.assertEqual(r.status_code, 202)
        mock_redeploy.assert_not_called()
