# -*- coding: utf-8 -*-
"""Code Server 上架 seed 與「只有密碼、可自設」憑證形態的守線斷言。

契約來源：woow-paas-charts charts/code-server 0.1.1（code-server :8080 是唯一入口、自己的
密碼登入、沒有帳號也沒有 auth-proxy；PASSWORD 來自 chart Secret 的 admin_password；
改密碼不動 pod template；選填 auth.anthropicApiKey／openrouterApiKey／gitToken；
codeServer.publicUrl 的 host 當 --trusted-origins），paas-odoo-ci code-server/ image。

核心守線：
1. 只有密碼：admin_password_settable=True、沒有 admin_username_helm_path ⇒
   admin_credentials_settable=True（可 Reset 也可自設）但不能改帳號。
2. 上架（is_hidden=False）⟹ image 雙釘 tag@sha256（2026-09-30 釘 20260930.1503 公開；10-01 換 20260930.2345＋chart 0.1.1；10-03 換 20261002.2302＋chart 0.1.2）。
3. T1 = 2 vCPU / 4 Gi / 25 Gi：min_*、plan cs_*、兩顆 PVC 加總三處一致。
"""
import json
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.woow_paas_platform.controllers.paas import PaasController
from ..models.cloud_service import _internal_provision

CS_IMAGE = 'jcr-prod.woowtech.io/woow-paas-docker-local/code-server'
CS_CHART = 'oci://jcr-prod.woowtech.io/woow-paas-docker-local/code-server'
PINNED_TAG_RE = r'^\d{8}\.\d{4}@sha256:[0-9a-f]{64}$'
_OPERATOR_CLIENT = (
    'odoo.addons.woow_paas_platform.services.paas_operator.get_paas_operator_client'
)
GOOD_PASSWORD = 'Str0ng-Tenant-Pass!'


@tagged('post_install', '-at_install')
class TestCodeServerSeed(TransactionCase):

    def _tpl(self):
        return self.env.ref('woow_paas_platform.cloud_app_code_server')

    def _values(self):
        return json.loads(self._tpl().helm_default_values)

    def test_marketplace_fields(self):
        tpl = self._tpl()
        self.assertEqual(tpl.slug, 'code-server')
        self.assertEqual(tpl.category, 'ai_llm')
        self.assertEqual(tpl.default_port, 8080, 'code-server 本身就是唯一入口（無 auth-proxy）')
        self.assertTrue(tpl.ingress_enabled)
        self.assertEqual(tpl.max_per_partner, 0)
        self.assertFalse(tpl.mcp_enabled)

    def test_chart_oci_pin(self):
        tpl = self._tpl()
        self.assertEqual(tpl.helm_repo_url or '', '')
        self.assertEqual(tpl.helm_chart_name, CS_CHART)
        self.assertEqual(tpl.helm_chart_version, '0.1.2')

    def test_password_only_and_settable(self):
        tpl = self._tpl()
        self.assertTrue(tpl.has_admin_gui)
        self.assertEqual(tpl.admin_password_carrier, 'helm_secret')
        self.assertTrue(tpl.admin_reset_supported)
        self.assertTrue(tpl.admin_password_settable)
        self.assertTrue(tpl.admin_credentials_settable)
        self.assertFalse(tpl.admin_username_helm_path, 'code-server 沒有帳號')

    def test_image_from_jcr(self):
        v = self._values()
        self.assertEqual(v['image']['repository'], CS_IMAGE)
        self.assertNotIn('ghcr.io', v['image']['repository'])

    def test_listed_only_when_pinned(self):
        """上架 ⟹ image 雙釘 tag@sha256。佔位 tag 期間必須保持隱藏。"""
        tpl = self._tpl()
        self.assertTrue(tpl.is_active)
        self.assertFalse(tpl.conceal_instances)
        if not tpl.is_hidden:
            self.assertRegex(self._values()['image']['tag'], PINNED_TAG_RE)

    def test_resources_match_t1(self):
        """T1 = 2 vCPU / 4 Gi / 25 Gi：template min_*、plan cs_*、PVC 加總三處一致。"""
        tpl = self._tpl()
        v = self._values()
        self.assertEqual(tpl.min_vcpu, 2)
        self.assertEqual(tpl.min_ram_gb, 4.0)
        self.assertEqual(tpl.min_storage_gb, 25)
        pvc = v['persistence']
        self.assertEqual(
            int(pvc['workspace']['size'].rstrip('Gi')) + int(pvc['piData']['size'].rstrip('Gi')),
            tpl.min_storage_gb, 'workspace＋pi-data 必須等於 min_storage_gb（quota 對帳）')
        limits = v['codeServer']['resources']['limits']
        self.assertEqual(limits, {'cpu': '2', 'memory': '4Gi'})
        for plan in tpl.sudo().subscription_plan_ids:
            self.assertEqual(plan.cs_vcpu, 2, plan.name)
            self.assertEqual(plan.cs_ram_gb, 4.0, plan.name)
            self.assertEqual(plan.cs_storage_gb, 25, plan.name)

    def test_service_port_lockstep(self):
        v = self._values()
        self.assertEqual(v['service']['port'], 8080)
        self.assertEqual(v['service']['targetPort'], 8080)

    def test_optional_keys_are_password_fields_under_auth(self):
        """憑證（auth.*）一律 password；git 設定（QA CS-02／CS-03）是純文字、不是憑證，
        細節由 test_code_server_git_settings 守。"""
        specs = self._tpl()._parse_helm_value_specs()
        self.assertEqual(specs.get('required'), [])
        optional = {s['key']: s for s in specs.get('optional', [])}
        credentials = {'auth.anthropicApiKey', 'auth.openrouterApiKey', 'auth.gitToken'}
        git_settings = {'git.tokenHosts', 'git.userName', 'git.userEmail'}
        self.assertEqual(set(optional), credentials | git_settings)
        for key in credentials:
            self.assertEqual(optional[key]['type'], 'password', key)
        for key in git_settings:
            self.assertEqual(optional[key]['type'], 'text', key)

    def test_plans_linked_and_priced(self):
        plans = self._tpl().sudo().subscription_plan_ids
        m = self.env.ref('woow_paas_platform.plan_code_server_monthly')
        y = self.env.ref('woow_paas_platform.plan_code_server_yearly')
        self.assertIn(m, plans)
        self.assertIn(y, plans)
        self.assertEqual(m.sh_plan_price, 4500)
        self.assertEqual(y.sh_plan_price, 45000)
        for xmlid in ('product_code_server_monthly', 'product_code_server_yearly'):
            p = self.env.ref('woow_paas_platform.%s' % xmlid)
            self.assertTrue(p.sh_product_subscribe, xmlid)
            self.assertFalse(p.taxes_id, '%s 免稅（同其他 cloud service 產品）' % xmlid)

    def test_public_url_injected(self):
        """chart 取 publicUrl 的 host 當 --trusted-origins；只有平台知道 tenant host。"""
        CloudService = self.env['woow_paas_platform.cloud_service']
        values = CloudService._inject_allowed_origins(
            {}, 'code-server', 'https://paas-cs-abc.woowtech.io')
        self.assertEqual(
            values, {'codeServer': {'publicUrl': 'https://paas-cs-abc.woowtech.io'}})


@tagged('post_install', '-at_install')
class TestPasswordOnlyCredentials(TransactionCase):
    """admin_credentials_settable：只有密碼的模板可自設密碼、不能改帳號；既有模板不變。"""

    def setUp(self):
        super().setUp()
        self.controller = PaasController()
        self.admin_user = self.env.ref('base.user_admin')
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'Password-only workspace',
            'owner_id': self.admin_user.id,
        })
        self.template = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'Creds password-only',
            'slug': 'creds-password-only',
            'category': 'ai_llm',
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example/code-server',
            'helm_chart_version': '0.1.0',
            'mcp_enabled': False,
            'has_admin_gui': True,
            'admin_password_carrier': 'helm_secret',
            'admin_password_settable': True,
            'helm_default_values': json.dumps({}),
        })
        self.service = _internal_provision(
            self.env['woow_paas_platform.cloud_service'].with_context(
                mcp_admin_password='old-password-0000',
            )).create({
                'name': 'Svc password-only',
                'workspace_id': self.workspace.id,
                'template_id': self.template.id,
                'state': 'running',
                'helm_namespace': 'paas-ws-test',
                'helm_release_name': 'svc-pwonly',
                'helm_values': '{}',
            })

    def _secret_password(self):
        return self.env['woow_paas_platform.cloud_service_secret'].sudo().search(
            [('service_id', '=', self.service.id)], limit=1).admin_password

    def _call(self, **params):
        with patch('odoo.addons.woow_paas_platform.controllers.paas.request',
                   new=MagicMock(env=self.env(user=self.admin_user))):
            return self.controller.api_service_set_admin_credentials(
                self.workspace.id, self.service.id, **params)

    def test_compute(self):
        Tpl = self.env['woow_paas_platform.cloud_app_template']
        cases = [
            # has_gui, carrier, username_path, settable, expected
            (True, 'helm_secret', False, False, False),   # hermes-agent：只能 Reset
            (True, 'helm_secret', 'a.b', False, True),     # open-design／pi-agent
            (True, 'helm_secret', False, True, True),      # code-server
            (True, 'app_managed', False, False, False),    # omnigent
            (False, 'config_json', False, False, False),
        ]
        for has_gui, carrier, path, settable, expected in cases:
            tpl = Tpl.new({
                'has_admin_gui': has_gui, 'admin_password_carrier': carrier,
                'admin_username_helm_path': path, 'admin_password_settable': settable,
            })
            self.assertEqual(tpl.admin_credentials_settable, expected,
                             (has_gui, carrier, path, settable))

    def test_constraint_requires_helm_secret(self):
        for vals in ({'admin_password_carrier': 'config_json'},
                     {'admin_password_carrier': 'app_managed'},
                     {'has_admin_gui': False}):
            with self.assertRaises(ValidationError, msg=repr(vals)):
                with self.env.cr.savepoint():
                    self.template.write(vals)

    def test_existing_seed_templates_unchanged(self):
        """既有模板：可改帳號的照舊可自設；hermes-agent 仍只能 Reset。"""
        for xmlid in ('cloud_app_opendesign', 'cloud_app_pi_agent'):
            tpl = self.env.ref('woow_paas_platform.%s' % xmlid)
            self.assertTrue(tpl.admin_credentials_settable, xmlid)
            self.assertFalse(tpl.admin_password_settable, xmlid)
        hermes = self.env.ref('woow_paas_platform.cloud_app_hermes_agent')
        self.assertFalse(hermes.admin_credentials_settable)

    @patch(_OPERATOR_CLIENT)
    def test_tenant_password_applies_and_is_not_echoed(self, get_client):
        client = MagicMock()
        get_client.return_value = client

        result = self._call(password=GOOD_PASSWORD)

        self.assertTrue(result.get('success'), result)
        kwargs = client.reset_release_config.call_args.kwargs
        self.assertEqual(
            kwargs['sensitive_values']['config']['sensitive']['admin_password'],
            GOOD_PASSWORD)
        self.assertEqual(kwargs['admin_password_carrier'], 'helm_secret')
        self.assertEqual(self._secret_password(), GOOD_PASSWORD)
        self.assertNotIn('admin_password', result['data'])
        self.assertNotIn(GOOD_PASSWORD, json.dumps(result))
        self.assertIsNone(result['data']['admin_username'])
        self.assertIs(result['data']['template']['admin_credentials_settable'], True)
        self.assertIs(result['data']['template']['admin_username_editable'], False)

    @patch(_OPERATOR_CLIENT)
    def test_generated_password_delivered_once(self, get_client):
        get_client.return_value = MagicMock()

        result = self._call(password='')

        self.assertTrue(result.get('success'), result)
        self.assertEqual(result['data']['admin_password'], self._secret_password())
        self.assertNotEqual(self._secret_password(), 'old-password-0000')

    @patch(_OPERATOR_CLIENT)
    def test_username_is_refused(self, get_client):
        client = MagicMock()
        get_client.return_value = client

        result = self._call(username='someone', password=GOOD_PASSWORD)

        self.assertFalse(result.get('success'))
        self.assertIn('no username', result.get('error', ''))
        client.reset_release_config.assert_not_called()
        self.assertEqual(self._secret_password(), 'old-password-0000')

    @patch(_OPERATOR_CLIENT)
    def test_model_refuses_username(self, get_client):
        client = MagicMock()
        get_client.return_value = client
        with self.assertRaises(UserError):
            self.service._reset_admin_password(new_username='someone')
        client.reset_release_config.assert_not_called()

    def test_payload_mirrors_the_capability(self):
        payload = self.controller._format_service(self.service)
        self.assertIs(payload['template']['admin_credentials_settable'], True)
        self.assertIs(payload['template']['admin_username_editable'], False)
        self.assertIs(payload['template']['admin_reset_supported'], True)
        self.assertIsNone(payload['admin_username'])
