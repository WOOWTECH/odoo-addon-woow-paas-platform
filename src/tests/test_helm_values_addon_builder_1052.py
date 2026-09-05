"""#1052：``_build_odoo_addon_helm_values`` 的 characterization test。

替 6b（addons path 字面量 → 模組常數 ``ODOO_SELECTED_ADDONS_PATH``）鎖住「零行為變更」：
本檔在 6b **之前**就 commit 且必須綠，6b 之後仍綠。斷言的是形狀與不變式、不是逐字
golden（逐字 golden 改一個 echo 訊息就紅，卻多守不到任何東西）：

* 沒有 addon → 空 dict（呼叫端靠這個判斷「不用 deep-merge」）。
* third_party／custom addon → initContainer（名 ``addon-selector``、與主容器同一顆
  image）＋ emptyDir volume ＋ mount，且 initContainer 與 post-init script 兩段 ``sed``
  **append 同一條路徑**。
* built_in addon → 不需要 initContainer，但 post-init script 仍在（首次開機
  odoo.conf 尚不存在時的兜底）。
* ``_remove_addon_helm_values`` 是 builder 的反函式：只拔 addon-selector 相關項、不碰別人的。
* 冪等判準 ``grep -q "selected-addons"`` 的 marker 必須是路徑的子字串——把路徑改成不含
  marker 的值，sed 會在每次 redeploy 都再 append 一次。

fixture 不走 ``write()``：每個案例各自 ``create`` 一筆服務，避開 addon 變更的 write 路徑
守衛（#477 家族），因為這裡要測的只有 builder 的純函式輸出。
"""
from odoo.tests.common import TransactionCase, tagged

from ..models.cloud_service import _internal_provision

#: operator 側 ``update_module_list.py`` 讀的 odoo.conf 就是被這條路徑 append 過的那份，
#: 所以它是跨系統的固定值，這裡以字面量釘住、不從實作 import（否則實作改了測試跟著改，等於沒守）。
_PINNED_PATH = '/opt/bitnami/odoo/selected-addons'
_IMAGE = 'registry.example.test/paas-odoo:1052'


@tagged('post_install', '-at_install')
class TestOdooAddonHelmValuesShape(TransactionCase):

    def setUp(self):
        super().setUp()
        admin = self.env.ref('base.user_admin')
        self.tpl = self.env.ref('woow_paas_platform.cloud_app_odoo')
        self.ws = self.env['woow_paas_platform.workspace'].create({
            'name': 'WS 1052 addon builder', 'owner_id': admin.id})
        Addon = self.env['woow_paas_platform.cloud_odoo_addon']
        # source_type 預設 third_party（要走 initContainer 複製）。
        self.third_party = Addon.create({'name': 'CRM 1052', 'technical_name': 'crm_1052'})
        self.built_in = Addon.create({
            'name': 'Mail 1052', 'technical_name': 'mail_1052', 'source_type': 'built_in'})
        self._seq = 0

    def _build(self, addons):
        self._seq += 1
        svc = _internal_provision(self.env['woow_paas_platform.cloud_service']).create({
            'workspace_id': self.ws.id, 'template_id': self.tpl.id,
            'name': 'svc-1052-%d' % self._seq, 'reference_id': 'test-1052-%d' % self._seq,
            'state': 'running', 'subdomain': 'svc-1052-%d' % self._seq,
            'helm_namespace': 'paas-ws-test-1052',
            'helm_release_name': 'svc-1052-%d' % self._seq,
            'odoo_addon_ids': [(6, 0, addons.ids)],
        })
        return svc, svc._build_odoo_addon_helm_values(_IMAGE)

    def test_no_addons_is_empty_dict(self):
        _svc, values = self._build(self.env['woow_paas_platform.cloud_odoo_addon'])
        self.assertEqual(values, {})

    def test_third_party_addon_yields_init_container_volume_and_post_init(self):
        _svc, values = self._build(self.third_party)
        self.assertEqual(
            set(values),
            {'initContainers', 'extraVolumes', 'extraVolumeMounts', 'customPostInitScripts'})
        (init,) = values['initContainers']
        self.assertEqual((init['name'], init['image']), ('addon-selector', _IMAGE))
        self.assertIn('for addon in crm_1052;', init['args'][0])
        self.assertEqual(values['extraVolumes'], [{'name': 'selected-addons', 'emptyDir': {}}])
        self.assertEqual(
            values['extraVolumeMounts'],
            [{'name': 'selected-addons', 'mountPath': _PINNED_PATH}])
        self.assertIn('00-addons-path.sh', values['customPostInitScripts'])

    def test_both_sed_lines_append_the_same_pinned_path(self):
        _svc, values = self._build(self.third_party)
        needle = '|&,%s|' % _PINNED_PATH
        self.assertIn(needle, values['initContainers'][0]['args'][0])
        self.assertIn(needle, values['customPostInitScripts']['00-addons-path.sh'])

    def test_built_in_addon_needs_no_init_container(self):
        _svc, values = self._build(self.built_in)
        self.assertEqual(set(values), {'customPostInitScripts'})

    def test_remove_is_the_inverse_of_build_and_leaves_others_alone(self):
        svc, values = self._build(self.third_party)
        values['initContainers'].append({'name': 'other-init'})
        values['extraVolumes'].append({'name': 'other-vol'})
        values['extraVolumeMounts'].append({'name': 'other-vol', 'mountPath': '/x'})
        values['customPostInitScripts']['99-other.sh'] = '#!/bin/bash'
        svc._remove_addon_helm_values(values)
        self.assertEqual(values, {
            'initContainers': [{'name': 'other-init'}],
            'extraVolumes': [{'name': 'other-vol'}],
            'extraVolumeMounts': [{'name': 'other-vol', 'mountPath': '/x'}],
            'customPostInitScripts': {'99-other.sh': '#!/bin/bash'},
        })

    def test_idempotency_marker_is_a_substring_of_the_path(self):
        _svc, values = self._build(self.third_party)
        for script in (values['initContainers'][0]['args'][0],
                       values['customPostInitScripts']['00-addons-path.sh']):
            self.assertIn('grep -q "selected-addons"', script)
        self.assertIn('selected-addons', _PINNED_PATH)
