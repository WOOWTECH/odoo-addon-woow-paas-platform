# -*- coding: utf-8 -*-
"""商用驗收 n8n-credential-key-backup（DATA-N8N-013-1）：n8n 加密金鑰改由平台產生並保存。

缺陷：n8n 服務沒有設 ``N8N_ENCRYPTION_KEY``，n8n 第一次開機自己亂數產生一把、只寫在 PVC 的
``/home/node/.n8n/config``（0600）。credential 都用這把金鑰加密存在 SQLite 裡 ⇒ 只備份資料庫、
還原到沒有這個 config 的新實例時，credential 全部解不開（header-auth webhook 一律 500，QA 對照組
sbx-c 實測）。PVC 一壞，平台手上沒有任何東西能把 credential 救回來。

修法：開通 n8n 時平台產生一把金鑰，寫進服務的 ``main.secret.N8N_ENCRYPTION_KEY``——8gears chart
2.0.1 把 ``main.secret`` 每個鍵放進 ``<release>-n8n-app-secret``，再以 ``envFrom`` 變成容器環境變數
（與 ``N8N_API_KEY``、``N8N_SMTP_PASS`` 同一顆 Secret）。n8n 在 config 檔不存在時會用這個環境變數
寫出 config ⇒ PVC 上的金鑰與平台保存的同一把；金鑰同時留在平台 DB（helm_values）與 K8s Secret，
備份平台 DB 就帶得到。

這裡鎖：

- 開通（``_create_service``）一定帶金鑰、每個服務各一把、寫進 helm_values，部署組裝原樣送進 chart。
- **存量服務不補**：它們 PVC 上已經有 n8n 自己產生的金鑰，平台另給一把不同的，n8n 開機會報
  「Mismatching encryption keys」直接起不來。部署組裝不得替沒有金鑰的服務生一把。
- 已經有值就不覆寫（重跑、管理員搬遷時寫入的舊金鑰）。
- 租戶不能自己設、也不能改（不在 helm_value_specs）；服務詳情 API 全遮、不留前綴。
- 落庫的敏感值剝除不得把它剝掉（保存在平台端正是修法本身）。
- 其他範本不受影響。
"""
import json
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.woow_paas_platform.controllers.paas import PaasController
from ..models.cloud_service import _internal_provision

_CONTROLLER = 'odoo.addons.woow_paas_platform.controllers.paas'
_KEY = 'N8N_ENCRYPTION_KEY'


def _secret(values):
    return ((values or {}).get('main') or {}).get('secret') or {}


@tagged('post_install', '-at_install')
class TestN8nEncryptionKeyMint(TransactionCase):
    """純函式：``CloudService._mint_n8n_encryption_key``。"""

    def setUp(self):
        super().setUp()
        self.Service = self.env['woow_paas_platform.cloud_service']
        self.seed = self.env.ref('woow_paas_platform.cloud_app_n8n')

    def test_seed_values_get_a_strong_key_next_to_the_api_key(self):
        values = json.loads(self.seed.helm_default_values)
        out = self.Service._mint_n8n_encryption_key(values, 'n8n')
        secret = _secret(out)
        self.assertIn(_KEY, secret)
        # n8n 自己產生的是 randomBytes(24) 的 base64（32 字元）；平台的不得比它弱。
        self.assertGreaterEqual(len(secret[_KEY]), 32)
        self.assertEqual(secret['N8N_API_KEY'], 'set-by-platform-init', '既有 secret 鍵原樣保留')

    def test_each_call_gives_a_different_key(self):
        first = _secret(self.Service._mint_n8n_encryption_key({}, 'n8n'))[_KEY]
        second = _secret(self.Service._mint_n8n_encryption_key({}, 'n8n'))[_KEY]
        self.assertNotEqual(first, second)

    def test_existing_key_is_kept(self):
        values = {'main': {'secret': {_KEY: 'adopted-key-from-old-pvc-config-0001'}}}
        out = self.Service._mint_n8n_encryption_key(values, 'n8n')
        self.assertEqual(_secret(out)[_KEY], 'adopted-key-from-old-pvc-config-0001')

    def test_handles_missing_or_null_blocks(self):
        for values in ({}, {'main': None}, {'main': {'secret': None}}):
            out = self.Service._mint_n8n_encryption_key(values, 'n8n')
            self.assertTrue(_secret(out).get(_KEY), values)

    def test_other_templates_are_untouched(self):
        for slug in ('odoo', 'mcp-n8n', 'hermes-agent', 'litellm', None):
            self.assertEqual(
                self.Service._mint_n8n_encryption_key({'main': {'secret': {}}}, slug),
                {'main': {'secret': {}}}, slug)

    def test_key_survives_the_storage_strip(self):
        """helm_values 落庫前的敏感值剝除不得把金鑰剝掉——保存在平台端正是修法本身。"""
        values = {'main': {'secret': {_KEY: 'k' * 32, 'N8N_API_KEY': 'x'}}}
        stripped = self.Service._strip_mcp_sensitive_values(values)
        self.assertEqual(_secret(stripped)[_KEY], 'k' * 32)

    def test_tenant_cannot_set_the_key(self):
        keys = self.seed._tenant_value_keys()
        self.assertFalse([k for k in keys if 'ENCRYPTION' in k.upper()], keys)
        _filtered, rejected = self.seed._filter_allowed_helm_values(
            {'main.secret.N8N_ENCRYPTION_KEY': 'tenant-chosen'})
        self.assertEqual(rejected, ['main.secret.N8N_ENCRYPTION_KEY'])

    def test_key_is_fully_masked_in_the_api(self):
        key = 'Zk3vQp9sT1uV5wX7yA2bC4dE6fG8hJ0k'
        sanitized = PaasController._sanitize_helm_values({
            'main': {'secret': {_KEY: key, 'N8N_API_KEY': 'n8n_api_0123456789abcdef'}},
        }, self.seed)
        self.assertNotIn(key[:8], json.dumps(sanitized))
        self.assertEqual(sanitized['main']['secret'][_KEY], '***')
        self.assertNotIn('n8n_api_0123456789abcdef', json.dumps(sanitized), '既有遮罩照舊')


@tagged('post_install', '-at_install')
class TestN8nEncryptionKeyDeploy(TransactionCase):
    """部署組裝：只送服務自己保存的金鑰，不替存量服務生一把。"""

    def setUp(self):
        super().setUp()
        self.Service = _internal_provision(self.env['woow_paas_platform.cloud_service'])
        self.seed = self.env.ref('woow_paas_platform.cloud_app_n8n')
        self.ws = self.env['woow_paas_platform.workspace'].create({'name': 'n8n enc-key WS'})

    def _svc(self, release, helm_values):
        return self.Service.create({
            'name': release,
            'workspace_id': self.ws.id,
            'template_id': self.seed.id,
            'helm_release_name': release,
            'helm_namespace': 'paas-ws-n8nenc01',
            'helm_values': json.dumps(helm_values),
        })

    def test_stored_key_reaches_the_chart_values(self):
        values = {'main': {'secret': {_KEY: 'stored-key-0123456789abcdefghij01', 'N8N_API_KEY': 'k'}}}
        svc = self._svc('svc-n8nenc01', values)
        out = svc._assemble_deploy_values()['values']
        self.assertEqual(_secret(out)[_KEY], 'stored-key-0123456789abcdefghij01')

    def test_legacy_service_without_a_key_does_not_get_one(self):
        """存量服務：PVC 上已有 n8n 自己的金鑰，平台送一把不同的會讓 n8n 起不來。"""
        legacy = {'main': {'config': {'GENERIC_TIMEZONE': 'Asia/Taipei'},
                           'secret': {'N8N_API_KEY': 'real-api-key'}}}
        svc = self._svc('svc-n8nenc02', legacy)
        before = svc.helm_values
        out = svc._assemble_deploy_values()['values']
        self.assertNotIn(_KEY, _secret(out))
        self.assertEqual(svc.helm_values, before, '組裝不寫 DB')


@tagged('post_install', '-at_install')
class TestN8nEncryptionKeyOnCreate(TransactionCase):
    """端到端：經 controller 開通 n8n，金鑰寫進 helm_values。"""

    def setUp(self):
        super().setUp()
        self.controller = PaasController()
        self.tpl = self.env.ref('woow_paas_platform.cloud_app_n8n')
        self.plan = self.env.ref('woow_paas_platform.plan_n8n_monthly')
        self.assertIn(self.plan, self.tpl.subscription_plan_ids, '前置：seed 方案掛在範本上')
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'n8n enc-key create workspace',
        })
        self.member = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'n8n enc-key workspace admin',
            'login': 'n8n-enc-key-workspace-admin',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.env['woow_paas_platform.workspace_access'].create({
            'workspace_id': self.workspace.id,
            'user_id': self.member.id,
            'role': 'admin',
        })

    def _create(self, name, values=None):
        payload = {'_init.owner_email': 'owner@example.com', '_init.owner_password': 'Passw0rdEnc1'}
        payload.update(values or {})
        with patch(f'{_CONTROLLER}.request', new=MagicMock(env=self.env(user=self.member))):
            result = self.controller._create_service(
                workspace=self.workspace, template_id=self.tpl.id, name=name,
                values=payload, plan_id=self.plan.id)
        self.assertTrue(result.get('success'), result.get('error'))
        return result, self.env['woow_paas_platform.cloud_service'].browse(result['data']['id'])

    def test_new_service_stores_its_own_key(self):
        _result, svc = self._create('n8nenc-a')
        stored = _secret(svc._helm_values_dict())
        self.assertGreaterEqual(len(stored.get(_KEY) or ''), 32)
        self.assertEqual(stored.get('N8N_API_KEY'), 'set-by-platform-init')
        deployed = _secret(svc._assemble_deploy_values()['values'])
        self.assertEqual(deployed[_KEY], stored[_KEY], '部署送進 chart 的就是保存的那一把')

    def test_two_services_get_different_keys(self):
        _r1, svc1 = self._create('n8nenc-b')
        _r2, svc2 = self._create('n8nenc-c')
        self.assertNotEqual(_secret(svc1._helm_values_dict())[_KEY],
                            _secret(svc2._helm_values_dict())[_KEY])

    def test_tenant_supplied_key_is_dropped(self):
        _result, svc = self._create('n8nenc-d', {'main.secret.N8N_ENCRYPTION_KEY': 'tenant-chosen'})
        self.assertNotEqual(_secret(svc._helm_values_dict())[_KEY], 'tenant-chosen')

    def test_create_response_does_not_leak_the_key(self):
        result, svc = self._create('n8nenc-e')
        key = _secret(svc._helm_values_dict())[_KEY]
        self.assertNotIn(key, json.dumps(result, default=str))
        self.assertNotIn(key[:8], json.dumps(result, default=str))
