# -*- coding: utf-8 -*-
"""#1334：n8n 的 storageClass 改在首裝時釘進 helm_values；n8n 注入公開網址。

- 8gears n8n chart 2.0.1 讀 ``main.persistence.storageClass``；以前注入頂層
  ``persistence.storageClass``（chart 忽略）⇒ PVC 落在預設 SC ``longhorn``（Retain），
  刪服務後殘留 Released PV。
- PVC ``storageClassName`` 不可變：存量服務絕不能在 upgrade 時多出這個鍵，所以只在
  ``_execute_deploy``（首裝）寫進 helm_values，之後 upgrade 原樣帶出。
- n8n 的 ``WEBHOOK_URL``／``N8N_EDITOR_BASE_URL`` 以前沒設，webhook 與 OAuth callback
  網址都是 localhost。
- 範本說明的孤兒磁碟保留天數要與正式站 operator（``pvcGracePeriodDays: 90``）一致。
"""
import json
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged

from ..models.cloud_service import _internal_provision

_SC = 'longhorn-delete'


def _storage_classes(values, path=()):
    """values 裡所有 storageClass 鍵的 (路徑, 值)。"""
    found = []
    if isinstance(values, dict):
        for key, val in values.items():
            if key == 'storageClass':
                found.append((path + (key,), val))
            found.extend(_storage_classes(val, path + (key,)))
    return found


@tagged('post_install', '-at_install')
class TestN8nStorageClassPinnedAtInstall(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Service = _internal_provision(self.env['woow_paas_platform.cloud_service'])
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.storage_class_name', _SC)
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.paas_domain', 'woowtech.io')
        Template = self.env['woow_paas_platform.cloud_app_template']
        self.ws = self.env['woow_paas_platform.workspace'].create({'name': 'n8n 1334 WS'})

        def tpl(slug):
            return Template.create({
                'name': 'T %s' % slug, 'slug': slug,
                'helm_repo_url': 'https://charts.example.com',
                'helm_chart_name': slug, 'helm_chart_version': '1.0.0',
                'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
            })

        self.tpl_n8n = tpl('n8n')
        self.tpl_mcp = tpl('mcp-hermes')

    def _svc(self, template, release, helm_values, subdomain=None):
        vals = {
            'name': release,
            'workspace_id': self.ws.id,
            'template_id': template.id,
            'helm_release_name': release,
            'helm_namespace': 'paas-ws-n8n01334',
            'helm_values': json.dumps(helm_values),
        }
        if subdomain:
            vals['subdomain'] = subdomain
        return self.Service.create(vals)

    def _seed_like(self):
        return {
            'config': {'database': {'type': 'sqlite'}},
            'main': {
                'config': {'GENERIC_TIMEZONE': 'Asia/Taipei'},
                'persistence': {'enabled': True, 'type': 'dynamic', 'size': '2Gi'},
            },
        }

    # ---- 每次部署的 injector 對 n8n 不再動 storageClass ----------------------

    def test_per_deploy_injector_is_noop_for_n8n(self):
        values = self.Service._inject_storage_class(self._seed_like(), 'n8n', _SC)
        self.assertEqual(_storage_classes(values), [])
        self.assertNotIn('persistence', values, '頂層 persistence 是 chart 不讀的錯路徑')

    def test_other_templates_unchanged(self):
        values = self.Service._inject_storage_class({}, 'mcp-hermes', _SC)
        self.assertEqual(values, {'persistence': {'storageClass': _SC}})

    # ---- 存量服務：render 結果與以前相同（PVC 不可變） -------------------------

    def test_existing_service_without_pin_never_gets_storage_class(self):
        svc = self._svc(self.tpl_n8n, 'svc-n8nold01', self._seed_like())
        svc.write({'state': 'running'})
        values = svc._assemble_deploy_values()['values']
        self.assertEqual(_storage_classes(values), [])

    # ---- 首裝：寫進 helm_values，之後 upgrade 原樣帶出 ------------------------

    def test_pin_writes_main_persistence_storage_class(self):
        svc = self._svc(self.tpl_n8n, 'svc-n8nnew01', self._seed_like())
        svc._pin_install_storage_class()
        stored = svc._helm_values_dict()
        self.assertEqual(stored['main']['persistence']['storageClass'], _SC)
        # 其他值原樣保留
        self.assertEqual(stored['main']['persistence']['size'], '2Gi')
        self.assertEqual(stored['main']['config']['GENERIC_TIMEZONE'], 'Asia/Taipei')
        self.assertEqual(stored['config'], {'database': {'type': 'sqlite'}})
        # upgrade 組裝帶出同一個值、且只有這一處
        values = svc._assemble_deploy_values()['values']
        self.assertEqual(
            _storage_classes(values), [(('main', 'persistence', 'storageClass'), _SC)])

    def test_pin_keeps_explicit_value_and_is_idempotent(self):
        hv = self._seed_like()
        hv['main']['persistence']['storageClass'] = 'longhorn'
        svc = self._svc(self.tpl_n8n, 'svc-n8nexp01', hv)
        before = svc.helm_values
        svc._pin_install_storage_class()
        svc._pin_install_storage_class()
        self.assertEqual(svc.helm_values, before)

    def test_pin_is_noop_for_other_templates(self):
        svc = self._svc(self.tpl_mcp, 'svc-mcp00001', {'persistence': {'size': '1Gi'}})
        before = svc.helm_values
        svc._pin_install_storage_class()
        self.assertEqual(svc.helm_values, before)

    def test_fresh_install_pins_before_assembling(self):
        """_execute_deploy 在組裝 values 之前釘好 storageClass。"""
        svc = self._svc(self.tpl_n8n, 'svc-n8nins01', self._seed_like())
        svc.write({'state': 'pending'})
        seen = {}

        def fake_assemble(rec):
            seen['values'] = rec._helm_values_dict()
            raise RuntimeError('stop after assemble')

        with patch('odoo.addons.woow_paas_platform.services.paas_operator.'
                   'get_paas_operator_client', return_value=object()), \
                patch.object(type(self.Service), '_assemble_deploy_values',
                             autospec=True, side_effect=fake_assemble):
            with self.assertRaises(RuntimeError):
                svc._execute_deploy()
        self.assertEqual(seen['values']['main']['persistence']['storageClass'], _SC)

    # ---- 公開網址 ----------------------------------------------------------

    def test_public_urls_injected_for_n8n(self):
        svc = self._svc(self.tpl_n8n, 'svc-n8nurl01', self._seed_like(),
                        subdomain='paas-n8n-abc')
        config = svc._assemble_deploy_values()['values']['main']['config']
        self.assertEqual(config['WEBHOOK_URL'], 'https://paas-n8n-abc.woowtech.io/')
        self.assertEqual(config['N8N_EDITOR_BASE_URL'], 'https://paas-n8n-abc.woowtech.io/')
        self.assertEqual(config['GENERIC_TIMEZONE'], 'Asia/Taipei')

    def test_public_url_injection_is_readonly(self):
        svc = self._svc(self.tpl_n8n, 'svc-n8nurl02', self._seed_like(),
                        subdomain='paas-n8n-def')
        before = svc.helm_values
        svc._assemble_deploy_values()
        self.assertEqual(svc.helm_values, before)

    def test_explicit_public_url_is_kept(self):
        values = {'main': {'config': {'WEBHOOK_URL': 'https://n8n.example.com/'}}}
        out = self.Service._inject_n8n_public_urls(values, 'n8n', 'https://x.woowtech.io')
        self.assertEqual(out['main']['config']['WEBHOOK_URL'], 'https://n8n.example.com/')
        self.assertEqual(out['main']['config']['N8N_EDITOR_BASE_URL'], 'https://x.woowtech.io/')

    def test_public_url_noop_for_other_templates_and_empty_url(self):
        self.assertEqual(
            self.Service._inject_n8n_public_urls({'a': 1}, 'odoo', 'https://x.woowtech.io'),
            {'a': 1})
        self.assertEqual(self.Service._inject_n8n_public_urls({}, 'n8n', ''), {})

    def test_public_url_handles_null_blocks(self):
        out = self.Service._inject_n8n_public_urls(
            {'main': None}, 'n8n', 'https://x.woowtech.io/')
        self.assertEqual(out['main']['config']['WEBHOOK_URL'], 'https://x.woowtech.io/')


@tagged('post_install', '-at_install')
class TestOrphanDiskRetentionText(TransactionCase):
    """範本說明與刪除對話框的保留天數要跟正式站 operator（90 天）一致。"""

    def test_no_template_promises_7_day_retention(self):
        templates = self.env['woow_paas_platform.cloud_app_template'].with_context(
            active_test=False).search([])
        offenders = [
            t.slug for t in templates
            if '7 天後回收' in (t.full_description or '')
        ]
        self.assertEqual(offenders, [])

    def test_n8n_seed_does_not_hardcode_storage_class(self):
        tpl = self.env.ref('woow_paas_platform.cloud_app_n8n')
        values = json.loads(tpl.helm_default_values)
        self.assertEqual(_storage_classes(values), [],
                         'storageClass 由平台首裝時寫入，seed 不得寫死')
