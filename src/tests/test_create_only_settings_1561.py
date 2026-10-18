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
        # #1612：拒絕訊息裡的名稱就是配置頁顯示的 spec label（seed 已是中文）。
        self.assertIn('管理員 Email', error)
        self.assertIn('載入示範資料', error)

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


@tagged('post_install', '-at_install')
class TestCreateOnlyHints1479(TransactionCase):
    """#1479 補上 #1561 沒有的兩個提示（靜態讀檔＋seed 前提）。

    * 建立頁（HelmValueForm）：create_only 欄位下提示「建立後無法變更」；
    * 配置頁檢視模式：唯讀設定裡有管理員帳號時，指向頁首改密碼的按鈕——按鈕名稱跟
      ServiceDetailPage 同兩個鏡射欄位走（admin_credentials_settable ⇒「Admin
      Credentials」，否則「Reset Admin Password」），平台不能重設就不提示。
    """

    HELM_FORM_HINT = ('This setting can only be chosen now; it cannot be changed '
                      'after the service is created.')
    CREDENTIALS_HINT = ('To change the administrator password, use Admin Credentials '
                        'at the top of this page.')
    RESET_HINT = ('To change the administrator password, use Reset Admin Password '
                  'at the top of this page.')

    def setUp(self):
        super().setUp()
        config = os.path.join(_SRC, 'static', 'src', 'paas', 'components', 'config')
        with open(os.path.join(config, 'HelmValueForm.xml'), encoding='utf-8') as f:
            self.form_xml = f.read()
        with open(os.path.join(_TAB, 'ConfigurationTab.js'), encoding='utf-8') as f:
            self.js = f.read()
        with open(os.path.join(_TAB, 'ConfigurationTab.xml'), encoding='utf-8') as f:
            self.xml = f.read()

    def test_create_form_hints_create_only_fields(self):
        # required 與 optional 兩段各一處；判準與 isCreateOnlySpec 相同（嚴格 true）。
        self.assertEqual(self.form_xml.count('t-if="spec.create_only === true"'), 2)
        self.assertEqual(self.form_xml.count(self.HELM_FORM_HINT), 2)

    def test_admin_password_entry_reads_mirrored_capability_fields(self):
        start = self.js.find('get adminPasswordEntry() {')
        self.assertGreaterEqual(start, 0)
        body = self.js[start:self.js.find('\n    }\n', start)]
        self.assertIn('this.createOnlySpecs.length', body)
        self.assertIn('template.admin_reset_supported !== true', body)
        self.assertIn('template.admin_credentials_settable === true', body)

    def test_view_mode_points_to_the_matching_button(self):
        self.assertIn('t-if="adminPasswordEntry === \'credentials\'"', self.xml)
        self.assertIn('t-if="adminPasswordEntry === \'reset\'"', self.xml)
        self.assertIn(self.CREDENTIALS_HINT, self.xml)
        self.assertIn(self.RESET_HINT, self.xml)

    def test_odoo_template_can_reset_admin_password(self):
        # 前提：Odoo 範本平台能重設管理員密碼，配置頁才會出現改密碼的提示。
        self.assertTrue(_odoo_template(self.env).admin_reset_supported)

    def test_new_strings_are_translated(self):
        po = os.path.join(_SRC, 'i18n', 'zh_TW.po')
        code = {e['src']: e['value'] for e in PoFileReader(po) if e.get('type') == 'code'}
        for src in (self.HELM_FORM_HINT, self.CREDENTIALS_HINT, self.RESET_HINT):
            with self.subTest(src=src):
                self.assertTrue(code.get(src), 'zh_TW.po 缺中文：%r' % src)


@tagged('post_install', '-at_install')
class TestN8nOwnerFieldsCreateOnly(TransactionCase):
    """DATA-N8N-008-1：n8n 的 _init.owner_* 只在開通時生效，reconfigure 不得靜默吞掉。

    實測（n8n 250）：POST update values={'_init.owner_email': ...} 回 success=true、值沒生效，
    還各多一次 revision 內容完全相同的 helm upgrade。
    """

    def _n8n(self):
        tpl = self.env['woow_paas_platform.cloud_app_template'].search(
            [('slug', '=', 'n8n')], limit=1)
        self.assertTrue(tpl, 'n8n 範本 seed 未載入')
        return tpl

    def test_n8n_init_specs_are_create_only(self):
        self.assertEqual(
            set(self._n8n()._create_only_value_specs()),
            {'_init.owner_email', '_init.owner_password'})

    def test_changed_init_values_are_refused_and_unchanged_dropped(self):
        service = MagicMock()
        service.template_id = self._n8n()
        service._helm_values_dict.return_value = {}
        kept, error = PaasController._drop_create_only_values(
            service, {'_init.owner_email': 'x@example.com'})
        self.assertIn(REFUSED, error)
        self.assertIn('n8n 管理員 Email', error)
        _kept, error = PaasController._drop_create_only_values(
            service, {'_init.owner_password': 'NewPassw0rd'})
        self.assertIn(REFUSED, error)
        # 空值＝沒送（舊分頁整包送出時不得白白觸發 upgrade）
        kept, error = PaasController._drop_create_only_values(
            service, {'_init.owner_email': '', 'main.config.N8N_LOG_LEVEL': 'debug'})
        self.assertIsNone(error)
        self.assertEqual(kept, {'main.config.N8N_LOG_LEVEL': 'debug'})
