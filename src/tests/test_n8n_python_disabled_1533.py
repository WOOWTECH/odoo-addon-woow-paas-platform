# -*- coding: utf-8 -*-
"""#1533：n8n 沒有外部 task runner，Python Code 節點一律失敗 ⇒ 部署時明確關掉 Python。

沙盒實測（n8n 2.35.3、8gears chart 2.0.1）：沒有 runner 時 Python 節點錯誤是「Python runner
unavailable: Python 3 is missing from this system」＋「Internal mode is intended only for
debugging」；設 ``N8N_PYTHON_ENABLED=false`` 後改成「This instance disallows Python
execution」，JavaScript 照常。這裡鎖：

- 部署組裝（``_assemble_deploy_values``）對 n8n 一律帶 ``main.config.N8N_PYTHON_ENABLED``
  ＝``'false'``——存量服務的 helm_values 是開通時凍結的，所以是注入而不是寫進範本預設。
- 服務自己的 helm_values 有明確值就不覆寫（將來加 runner 時個別服務可改 'true'）。
- 租戶不能自己打開（不在 helm_value_specs）；其他範本不受影響；組裝不寫 DB。
- 商品說明寫明「不提供 Python」。
"""
import json

from odoo.tests.common import TransactionCase, tagged

from ..models.cloud_service import _internal_provision


@tagged('post_install', '-at_install')
class TestN8nPythonDisabled1533(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Service = _internal_provision(self.env['woow_paas_platform.cloud_service'])
        self.seed = self.env.ref('woow_paas_platform.cloud_app_n8n')
        self.ws = self.env['woow_paas_platform.workspace'].create({'name': 'n8n 1533 WS'})

    def _svc(self, release, helm_values, template=None):
        return self.Service.create({
            'name': release,
            'workspace_id': self.ws.id,
            'template_id': (template or self.seed).id,
            'helm_release_name': release,
            'helm_namespace': 'paas-ws-n8n01533',
            'helm_values': json.dumps(helm_values),
        })

    def test_new_service_from_the_seed_gets_python_disabled(self):
        svc = self._svc('svc-n8npy001', json.loads(self.seed.helm_default_values))
        config = svc._assemble_deploy_values()['values']['main']['config']
        self.assertEqual(config['N8N_PYTHON_ENABLED'], 'false')
        self.assertEqual(config['GENERIC_TIMEZONE'], 'Asia/Taipei', '其他設定原樣保留')

    def test_existing_service_values_without_the_key_get_it(self):
        """存量服務：helm_values 是 #1533 以前開通時凍結的，下一次部署也要帶上。"""
        legacy = {'main': {'config': {'GENERIC_TIMEZONE': 'Asia/Taipei'}}}
        svc = self._svc('svc-n8npy002', legacy)
        before = svc.helm_values
        values = svc._assemble_deploy_values()['values']
        self.assertEqual(values['main']['config']['N8N_PYTHON_ENABLED'], 'false')
        self.assertEqual(svc.helm_values, before, '組裝不寫 DB')

    def test_explicit_service_value_is_kept(self):
        svc = self._svc('svc-n8npy003', {'main': {'config': {'N8N_PYTHON_ENABLED': 'true'}}})
        config = svc._assemble_deploy_values()['values']['main']['config']
        self.assertEqual(config['N8N_PYTHON_ENABLED'], 'true')

    def test_handles_missing_or_null_blocks(self):
        for values in ({}, {'main': None}, {'main': {'config': None}}):
            out = self.Service._inject_n8n_runtime_defaults(values, 'n8n')
            self.assertEqual(
                out['main']['config'],
                {'N8N_PYTHON_ENABLED': 'false', 'N8N_PROXY_HOPS': '1'}, values)

    def test_other_templates_are_untouched(self):
        for slug in ('odoo', 'mcp-n8n', 'hermes-agent'):
            self.assertEqual(
                self.Service._inject_n8n_runtime_defaults({'main': {'config': {}}}, slug),
                {'main': {'config': {}}}, slug)

    def test_tenant_cannot_turn_python_back_on(self):
        keys = self.seed._tenant_value_keys()
        self.assertFalse([k for k in keys if 'PYTHON' in k.upper() or 'RUNNER' in k.upper()], keys)
        _filtered, rejected = self.seed._filter_allowed_helm_values(
            {'main.config.N8N_PYTHON_ENABLED': 'true'})
        self.assertEqual(rejected, ['main.config.N8N_PYTHON_ENABLED'])

    def test_product_description_says_python_is_not_offered(self):
        self.assertIn('不提供 Python', self.seed.full_description)
        self.assertIn('JavaScript', self.seed.full_description)

    def test_proxy_hops_is_injected_for_new_and_existing_services(self):
        """DATA-N8N-007-2：cloudflared 後面要信任一跳 proxy，否則登入 IP 限流整個服務共用一個桶。"""
        svc = self._svc('svc-n8nhops01', {'main': {'config': {'GENERIC_TIMEZONE': 'Asia/Taipei'}}})
        config = svc._assemble_deploy_values()['values']['main']['config']
        self.assertEqual(config['N8N_PROXY_HOPS'], '1')
        explicit = self._svc('svc-n8nhops02', {'main': {'config': {'N8N_PROXY_HOPS': '2'}}})
        self.assertEqual(
            explicit._assemble_deploy_values()['values']['main']['config']['N8N_PROXY_HOPS'], '2',
            '服務自己有明確值就不覆寫')

    def test_tenant_cannot_change_proxy_hops(self):
        keys = self.seed._tenant_value_keys()
        self.assertFalse([k for k in keys if 'PROXY' in k.upper()], keys)
        _filtered, rejected = self.seed._filter_allowed_helm_values(
            {'main.config.N8N_PROXY_HOPS': '0'})
        self.assertEqual(rejected, ['main.config.N8N_PROXY_HOPS'])
