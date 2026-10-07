"""#1561：「只在開通時生效」的設定（spec ``create_only``）開通後唯讀。

2026-10-07 第二階段實測（N9）：Odoo 的「管理員 Email」「載入示範資料」開通後仍可在
配置頁修改，存檔會重建 Pod，但 woow-odoo chart 只在首次 ``-i base`` 套用它們——管理員
帳號不變、示範資料不會載入，畫面也沒有說明。

修法三層、同一個判準（spec 的 ``"create_only": true``）：

* seed：woow-odoo 的 ``conf.adminEmail``／``loadDemoData`` 標 ``create_only``；
* 後端 ``PaasController._drop_create_only_values``：值沒變當作沒送，值變了整筆拒絕
  （在改 addon 之前，不留半套變更）；
* 前端 ConfigurationTab：不進編輯表單，只在檢視模式唯讀列出並說明。
"""
import json
import os
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase, tagged
from odoo.tools.translate import PoFileReader

from odoo.addons.woow_paas_platform.controllers.paas import PaasController
from ..models.cloud_service import _internal_provision

_CONTROLLER = 'odoo.addons.woow_paas_platform.controllers.paas'
_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TAB = os.path.join(_SRC, 'static', 'src', 'paas', 'pages', 'service', 'tabs')
REFUSED = 'only take effect when the service is created'


def _odoo_template(env):
    return env['woow_paas_platform.cloud_app_template'].search(
        [('slug', '=', 'odoo')], limit=1)


@tagged('post_install', '-at_install')
class TestCreateOnlySeed1561(TransactionCase):

    def test_odoo_specs_are_create_only(self):
        tpl = _odoo_template(self.env)
        self.assertTrue(tpl, 'odoo 範本 seed 未載入')
        self.assertEqual(
            set(tpl._create_only_value_specs()), {'conf.adminEmail', 'loadDemoData'})

    def test_only_strict_true_counts(self):
        """seed 打錯字（字串 "true"）不得讓一格設定突然改不了：只認嚴格的 True。"""
        tpl = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'Create-only probe', 'slug': 'create-only-probe-1561',
            'category': 'web', 'helm_repo_url': '',
            'helm_chart_name': 'oci://example/probe', 'helm_chart_version': '0.1.0',
            'helm_value_specs': json.dumps({'required': [], 'optional': [
                {'key': 'a', 'label': 'A', 'type': 'text', 'create_only': True},
                {'key': 'b', 'label': 'B', 'type': 'text', 'create_only': 'true'},
                {'key': 'c', 'label': 'C', 'type': 'text'},
            ]}),
        })
        self.assertEqual(set(tpl._create_only_value_specs()), {'a'})


@tagged('post_install', '-at_install')
class TestCreateOnlyReconfigure1561(TransactionCase):

    def setUp(self):
        super().setUp()
        admin = self.env.ref('base.user_admin')
        self.tpl = _odoo_template(self.env)
        self.ws = self.env['woow_paas_platform.workspace'].create({
            'name': 'WS 1561', 'owner_id': admin.id})
        self.svc = _internal_provision(self.env['woow_paas_platform.cloud_service']).create({
            'workspace_id': self.ws.id, 'template_id': self.tpl.id,
            'name': 'svc-1561', 'reference_id': 'test-1561-ref',
            'state': 'running', 'subdomain': 'svc-1561',
            'helm_namespace': 'paas-ws-test-1561', 'helm_release_name': 'svc-1561abc',
        })
        values = json.loads(self.tpl.helm_default_values or '{}')
        values.setdefault('conf', {})['adminEmail'] = 'owner@woowtech.io'
        self.svc._apply_helm_values(values)

    def _drop(self, values):
        return PaasController._drop_create_only_values(self.svc, values)

    def test_unchanged_values_are_dropped(self):
        for values in ({'conf.adminEmail': 'owner@woowtech.io'},
                       {'conf.adminEmail': '  owner@woowtech.io '},
                       {'loadDemoData': False},
                       {'loadDemoData': 'false'}):
            with self.subTest(values=values):
                kept, error = self._drop(values)
                self.assertIsNone(error)
                self.assertEqual(kept, {})

    def test_missing_stored_value_compares_with_the_spec_default(self):
        values = self.svc.sudo()._helm_values_dict()
        values.pop('loadDemoData', None)
        self.svc.sudo().write({'helm_values': json.dumps(values)})
        kept, error = self._drop({'loadDemoData': False})
        self.assertIsNone(error)
        self.assertEqual(kept, {})

    def test_changed_values_are_refused_with_their_labels(self):
        _kept, error = self._drop({'conf.adminEmail': 'new@woowtech.io', 'loadDemoData': True})
        self.assertIn(REFUSED, error)
        self.assertIn('Administrator Email', error)
        self.assertIn('Load Demo Data', error)

    def test_other_keys_pass_through(self):
        kept, error = self._drop({'conf.adminEmail': 'owner@woowtech.io', 'other': 1})
        self.assertIsNone(error)
        self.assertEqual(kept, {'other': 1})

    def test_refusal_happens_before_any_write(self):
        """被拒的請求不得留下半套變更（改 addon 那段會先寫 odoo_addon_ids）。"""
        addon = self.env['woow_paas_platform.cloud_odoo_addon'].sudo().create({
            'name': 'Probe addon 1561', 'technical_name': 'probe_addon_1561',
            'source_type': 'built_in',
        })
        client = MagicMock()
        fake_request = MagicMock(env=self.env)
        before_values = self.svc.sudo()._helm_values_dict()
        before_addons = self.svc.odoo_addon_ids.ids
        with patch(f'{_CONTROLLER}.get_paas_operator_client', return_value=client), \
                patch(f'{_CONTROLLER}.request', new=fake_request):
            result = PaasController()._update_service(
                self.svc, values={'loadDemoData': True}, version=None,
                odoo_addon_ids=[addon.id])
        self.assertFalse(result['success'])
        self.assertIn(REFUSED, result['error'])
        client.upgrade_release.assert_not_called()
        self.assertEqual(self.svc.sudo()._helm_values_dict(), before_values)
        self.assertEqual(self.svc.odoo_addon_ids.ids, before_addons)
        self.assertEqual(self.svc.state, 'running')


@tagged('post_install', '-at_install')
class TestCreateOnlyFrontendContract1561(TransactionCase):
    """ConfigurationTab：create_only 不進編輯表單、檢視模式唯讀顯示（靜態讀檔）。"""

    def setUp(self):
        super().setUp()
        with open(os.path.join(_TAB, 'ConfigurationTab.js'), encoding='utf-8') as f:
            self.js = f.read()
        with open(os.path.join(_TAB, 'ConfigurationTab.xml'), encoding='utf-8') as f:
            self.xml = f.read()

    def test_editable_specs_exclude_create_only(self):
        start = self.js.find('get configurableSpecs() {')
        self.assertGreaterEqual(start, 0)
        body = self.js[start:self.js.find('\n    }\n', start)]
        self.assertIn('this.isCreateOnlySpec(s)', body)
        self.assertIn('spec?.create_only === true', self.js)

    def test_view_mode_lists_create_only_specs_read_only(self):
        self.assertIn('t-foreach="displaySpecs"', self.xml)
        self.assertIn('t-if="isCreateOnlySpec(spec)"', self.xml)
        self.assertIn('displaySpecs.length === 0', self.xml)

    def test_new_strings_are_translated(self):
        po = os.path.join(_SRC, 'i18n', 'zh_TW.po')
        code = {e['src']: e['value'] for e in PoFileReader(po) if e.get('type') == 'code'}
        for src in (
            'These settings only take effect when the service is created and cannot '
            'be changed afterwards: %(labels)s',
            'Set when the service was created; it cannot be changed afterwards.',
        ):
            with self.subTest(src=src):
                self.assertTrue(code.get(src), 'zh_TW.po 缺中文：%r' % src)
