# -*- coding: utf-8 -*-
"""#1317：只掛 `base.group_user`（非系統管理員）的 workspace owner／admin 經 `/woow`
開通雲端服務必定失敗。

## 缺陷（2026-10-02 正式站 uid 41 實際撞到，畫面只有「建立服務時發生錯誤」）

`controllers/paas.py` 的 `_create_service` 拿的是租戶身分的 recordset：

`ingress_enabled`（預設 True，正式站所有範本都是）或帶 `mcp_api_key` 的範本，
create 之後會 `service.write({'deploy_params': ...})`，而 `deploy_params` 帶
`groups='base.group_system'` ⇒ `check_field_access_rights` 直接 AccessError。
（n8n 範本放在 create vals 的 `n8n_owner_*` 同樣限 group_system，但 Odoo 的
create 不做欄位級檢查，實測不受影響；A3 釘住 n8n 這條路徑整條可用。）

例外被 controller 收成 `{'success': False, 'error': '建立服務時發生錯誤'}`、整筆 rollback。

## 為什麼既有測試抓不到

所有開通測試都用 superuser 的 workspace recordset 呼叫 `_create_service`（或以
`admin` 登入），`check_field_access_rights` 對 `env.su` 一律放行；Portal 會員經
`_PaaSCapability` 拿到 sudo recordset，也走不到這條。本檔一律 **HttpCase＋真
internal 非系統管理員 session＋真 HTTP JSON-RPC**，`sudo()` 只用在 fixture 與
raw-DB 對照。

## 分組

- **A 系列**（修前紅）：owner／admin 開通成功，且伺服器端值確實落地（raw SQL 讀，
  因為欄位對租戶是遮蔽的）。
- **B 系列**（修前修後都要綠）：平台身分只包那一次寫入——role 閘、workspace
  閘、欄位讀取遮蔽一條都沒有被順帶打開。
"""
import json

from odoo.exceptions import AccessError
from odoo.tests.common import HttpCase, new_test_user, tagged

MOD = 'woow_paas_platform'
N8N_SPECS = json.dumps({'required': [
    {'key': '_init.owner_email', 'label': 'n8n Admin Email', 'type': 'text'},
    {'key': '_init.owner_password', 'label': 'n8n Admin Password', 'type': 'password'},
], 'optional': []})


@tagged('post_install', '-at_install')
class TestMemberCreateService1317(HttpCase):

    def setUp(self):
        super().setUp()
        Template = self.env['%s.cloud_app_template' % MOD]
        self.tpl = Template.create({
            'name': 'Member Create 1317', 'slug': 'member-create-1317',
            'helm_repo_url': '', 'helm_chart_name': 'oci://example.invalid/m1317',
            'helm_chart_version': '1.0.0',
        })
        self.assertTrue(self.tpl.ingress_enabled, '前提：預設 ingress_enabled ⇒ 會寫 deploy_params')
        self.n8n_tpl = Template.create({
            'name': 'Member Create n8n 1317', 'slug': 'member-create-n8n-1317',
            'helm_repo_url': '', 'helm_chart_name': 'oci://example.invalid/m1317n8n',
            'helm_chart_version': '1.0.0', 'post_deploy_init_type': 'n8n',
            'helm_value_specs': N8N_SPECS,
        })
        self.plan = self.env['sh.subscription.plan'].create({
            'name': 'Member Create 1317 Plan', 'sh_duration': 1, 'sh_unit': 'month',
            'sh_plan_price': 100, 'service_type': 'cloud_service',
            'sh_no_of_billing_cycle': -1,
        })
        self.env['product.template'].sudo().create({
            'name': 'Member Create 1317 Subscription', 'type': 'service',
            'sh_product_subscribe': True, 'sh_subscription_plan_id': self.plan.id,
            'list_price': 100,
        })
        (self.tpl | self.n8n_tpl).write({'subscription_plan_ids': [(6, 0, [self.plan.id])]})

        def internal(login):
            user = new_test_user(self.env, login=login, password=login, groups='base.group_user')
            self.assertFalse(user.has_group('base.group_system'), '前提：非系統管理員')
            self.assertFalse(user.share, '前提：internal，不是 Portal')
            return user

        self.owner = internal('m1317_owner')
        self.admin_member = internal('m1317_admin')
        self.user_member = internal('m1317_user')
        self.outsider = internal('m1317_outsider')
        self.ws = self.env['%s.workspace' % MOD].create({
            'name': 'ws-1317', 'owner_id': self.owner.id})
        Access = self.env['%s.workspace_access' % MOD]
        Access.create({'workspace_id': self.ws.id, 'user_id': self.admin_member.id, 'role': 'admin'})
        Access.create({'workspace_id': self.ws.id, 'user_id': self.user_member.id, 'role': 'user'})

    # -- helpers ---------------------------------------------------------

    def _create(self, login, template, name, values=None):
        self.authenticate(login, login)
        resp = self.url_open('/api/workspaces/%s/services' % self.ws.id, data=json.dumps({
            'jsonrpc': '2.0', 'method': 'call', 'id': 1317, 'params': {
                'action': 'create', 'template_id': template.id, 'name': name,
                'plan_id': self.plan.id, 'values': values or {},
            }}), headers={'Content-Type': 'application/json'})
        parsed = resp.json()
        # anti-probe：排除 404／JSON-RPC 例外，否則「被擋」與「探針壞了」長得一樣
        self.assertIn('result', parsed, 'JSON-RPC 層失敗：%r' % parsed)
        return parsed['result']

    def _raw(self, service_id, column):
        self.env.flush_all()
        self.env.cr.execute(
            'SELECT %s FROM woow_paas_platform_cloud_service WHERE id = %%s' % column, (service_id,))
        row = self.env.cr.fetchone()
        return row and row[0]

    def _services(self, name):
        return self.env['%s.cloud_service' % MOD].sudo().search([
            ('workspace_id', '=', self.ws.id), ('name', '=', name)])

    # -- A：修前紅 --------------------------------------------------------

    def test_A1_internal_owner_creates_ingress_service(self):
        result = self._create('m1317_owner', self.tpl, 'svc-a1')
        self.assertTrue(result.get('success'), '一般內部 owner 開通失敗：%r' % result)
        sid = result['data']['id']
        params = json.loads(self._raw(sid, 'deploy_params') or '{}')
        self.assertEqual(params.get('expose_config', {}).get('enabled'), True,
                         'deploy_params 沒有落地：%r' % params)
        self.assertEqual(params['expose_config']['subdomain'], result['data']['subdomain'])

    def test_A2_internal_admin_member_creates_service(self):
        result = self._create('m1317_admin', self.tpl, 'svc-a2')
        self.assertTrue(result.get('success'), '一般內部 admin 開通失敗：%r' % result)
        self.assertTrue(self._raw(result['data']['id'], 'deploy_params'))

    def test_A3_internal_owner_creates_n8n_and_owner_credentials_land(self):
        result = self._create('m1317_owner', self.n8n_tpl, 'svc-a3', {
            '_init.owner_email': 'owner-1317@example.invalid',
            '_init.owner_password': 'Passw0rdX',
        })
        self.assertTrue(result.get('success'), '一般內部 owner 開通 n8n 失敗：%r' % result)
        sid = result['data']['id']
        self.assertEqual(self._raw(sid, 'n8n_owner_email'), 'owner-1317@example.invalid')
        self.assertEqual(self._raw(sid, 'n8n_owner_password'), 'Passw0rdX')

    # -- B：修前修後都要綠（沒有擴大授權）---------------------------------

    def test_B1_user_role_member_still_cannot_create(self):
        result = self._create('m1317_user', self.tpl, 'svc-b1')
        self.assertFalse(result.get('success'), 'role=user 竟可開通：%r' % result)
        self.assertFalse(self._services('svc-b1'), 'role=user 被拒卻留下記錄')

    def test_B2_non_member_cannot_create(self):
        result = self._create('m1317_outsider', self.tpl, 'svc-b2')
        self.assertFalse(result.get('success'), '非成員竟可開通：%r' % result)
        self.assertFalse(self._services('svc-b2'))

    def test_B3_owner_still_cannot_read_server_only_fields(self):
        result = self._create('m1317_owner', self.n8n_tpl, 'svc-b3', {
            '_init.owner_email': 'owner-1317@example.invalid',
            '_init.owner_password': 'Passw0rdX',
        })
        self.assertTrue(result.get('success'), '前提：開通成功（A3 同條件）：%r' % result)
        svc = self.env['%s.cloud_service' % MOD].with_user(self.owner).browse(result['data']['id'])
        for field in ('deploy_params', 'n8n_owner_password'):
            with self.subTest(field=field), self.assertRaises(AccessError):
                svc.read([field])
        self.assertNotIn('Passw0rdX', json.dumps(result), '回應不得回傳 owner 密碼')
