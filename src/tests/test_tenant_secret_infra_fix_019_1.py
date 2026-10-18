# -*- coding: utf-8 -*-
"""INFRA-FIX-019-1：租戶憑證（ngrok authtoken、Tailscale auth key）不進 helm release 歷史。

## 缺陷（商用驗收 INFRA-FIX-019 唯讀對帳，Headscale 238）

helm 把**每個** revision 的 user values 與渲染後 manifest 存在
``sh.helm.release.v1.<release>.v<N>`` Secret。租戶的 ngrok token 走 ``ngrok.authtoken``
（一般 values）⇒ 清除之後 v19／v21／v22／v25 仍是 49 字元 token（指紋 5096c312a3f6）。
Tailscale 的 ``tailscale.authKey`` 同一條路。

## 修法（平台這一半；operator 見 ``extra/paas-operator/tests/test_tenant_secret_infra_fix_019_1.py``）

範本 password spec 標 ``secret_ref: {key, since_chart}``。部署的 chart 版本 ≥ ``since_chart``
時，``_assemble_deploy_values(chart_version=…)`` 把值拿出 values（原路徑寫 ``""``、加
``tenantSecret.{name, revision, nonEmptyKeys}``），值另外經 ``tenant_secret`` 交給 operator
寫進 ``<release>-tenant-secret``。``revision`` 是平台密鑰的 HMAC，不是 token 的雜湊（manifest
也在歷史裡，純雜湊＝可比對的指紋）。chart 版本較舊（含 reset 釘住的舊版）照舊走 values。
"""
import hashlib
import json
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.woow_paas_platform.controllers.paas import PaasController
from ..models.cloud_service import _internal_provision
from ..services.tenant_secret import (
    has_secret_refs,
    secret_ref_problems,
    secret_refs,
    split_tenant_secret,
    tenant_secret_name,
)

_MODEL_CLIENT = 'odoo.addons.woow_paas_platform.services.paas_operator.get_paas_operator_client'
TOKEN = '2abcDEFghiJKLmnoPQRstuVWXyz0123456789_ngrokTOKEN'
SPECS = {
    'required': [],
    'optional': [
        {'key': 'ngrok.enabled', 'label': 'ngrok', 'type': 'boolean'},
        {'key': 'ngrok.authtoken', 'label': 'Authtoken', 'type': 'password',
         'secret_ref': {'key': 'ngrok_authtoken', 'since_chart': '0.1.7'}},
        {'key': 'ngrok.domain', 'label': 'Domain', 'type': 'text'},
    ],
}
#: 範本 → (password spec 路徑, Secret 鍵, 起始 chart 版本)。chart 端契約：woow-paas-charts
#: headscale 0.1.7、tailscale 0.1.8、emqx 0.1.11 的 ``tenantSecret``。
SEEDED = {
    'woow_paas_platform.cloud_app_headscale': ('ngrok.authtoken', 'ngrok_authtoken', '0.1.7'),
    'woow_paas_platform.cloud_app_emqx': ('ngrok.authtoken', 'ngrok_authtoken', '0.1.11'),
    'woow_paas_platform.cloud_app_tailscale': ('tailscale.authKey', 'authkey', '0.1.8'),
}


def _fingerprint(value):
    return hashlib.sha256(value.encode()).hexdigest()[:12]


@tagged('post_install', '-at_install')
class TestTenantSecretSplit(TransactionCase):
    """純函式：拿出 values、HMAC revision、版本閘門。"""

    def test_split_moves_the_token_out_of_values(self):
        values = {'ngrok': {'enabled': True, 'authtoken': TOKEN, 'domain': 'x.ngrok-free.app'}}
        refs = secret_refs(SPECS, '0.1.7')
        new, payload = split_tenant_secret(values, refs, 'svc-aaaa1111', 'platform-key')
        self.assertNotIn(TOKEN, json.dumps(new))
        self.assertEqual(new['ngrok']['authtoken'], '', 'explicit "" so no reused value survives')
        self.assertEqual(new['ngrok']['domain'], 'x.ngrok-free.app')
        self.assertEqual(new['tenantSecret']['name'], 'svc-aaaa1111-tenant-secret')
        self.assertEqual(new['tenantSecret']['nonEmptyKeys'], ['ngrok_authtoken'])
        self.assertEqual(payload, {'name': 'svc-aaaa1111-tenant-secret',
                                   'data': {'ngrok_authtoken': TOKEN}})
        self.assertEqual(values['ngrok']['authtoken'], TOKEN, 'input is not mutated')

    def test_revision_is_not_a_token_fingerprint(self):
        refs = secret_refs(SPECS, '0.1.7')
        new, _ = split_tenant_secret({'ngrok': {'authtoken': TOKEN}}, refs, 'svc-a', 'k1')
        revision = new['tenantSecret']['revision']
        self.assertNotIn(_fingerprint(TOKEN), json.dumps(new))
        self.assertNotIn(hashlib.sha256(TOKEN.encode()).hexdigest()[:len(revision)], revision)
        other_key, _ = split_tenant_secret({'ngrok': {'authtoken': TOKEN}}, refs, 'svc-a', 'k2')
        self.assertNotEqual(revision, other_key['tenantSecret']['revision'],
                            'the revision depends on the platform key, not on the token alone')

    def test_revision_follows_the_value(self):
        refs = secret_refs(SPECS, '0.1.7')
        a, _ = split_tenant_secret({'ngrok': {'authtoken': TOKEN}}, refs, 'svc-a', 'k')
        b, _ = split_tenant_secret({'ngrok': {'authtoken': TOKEN}}, refs, 'svc-a', 'k')
        c, _ = split_tenant_secret({'ngrok': {'authtoken': ''}}, refs, 'svc-a', 'k')
        self.assertEqual(a['tenantSecret']['revision'], b['tenantSecret']['revision'],
                         'same value → same revision (a redeploy does not restart the pod)')
        self.assertNotEqual(a['tenantSecret']['revision'], c['tenantSecret']['revision'],
                            'a cleared token must restart the pod')

    def test_cleared_token_is_written_as_empty(self):
        refs = secret_refs(SPECS, '0.1.7')
        new, payload = split_tenant_secret({'ngrok': {'enabled': True}}, refs, 'svc-a', 'k')
        self.assertEqual(payload['data'], {'ngrok_authtoken': ''})
        self.assertEqual(new['tenantSecret']['nonEmptyKeys'], [])

    def test_token_is_trimmed_like_the_chart_did(self):
        refs = secret_refs(SPECS, '0.1.7')
        _, payload = split_tenant_secret(
            {'ngrok': {'authtoken': '  %s\n' % TOKEN}}, refs, 'svc-a', 'k')
        self.assertEqual(payload['data']['ngrok_authtoken'], TOKEN)

    def test_no_platform_key_refuses(self):
        with self.assertRaises(ValueError):
            split_tenant_secret({}, secret_refs(SPECS, '0.1.7'), 'svc-a', '')

    def test_version_gate(self):
        self.assertEqual(secret_refs(SPECS, '0.1.7'), [('ngrok.authtoken', 'ngrok_authtoken')])
        self.assertEqual(secret_refs(SPECS, '0.2.0'), [('ngrok.authtoken', 'ngrok_authtoken')])
        self.assertEqual(secret_refs(SPECS, '0.1.6'), [], 'older chart cannot read the Secret')
        self.assertEqual(secret_refs(SPECS, None), [], 'unknown version → legacy lane')
        self.assertEqual(secret_refs(SPECS, 'garbage'), [])
        values = {'ngrok': {'authtoken': TOKEN}}
        self.assertIs(split_tenant_secret(values, [], 'svc-a', 'k')[0], values)
        self.assertIsNone(split_tenant_secret(values, [], 'svc-a', 'k')[1])

    def test_problems(self):
        self.assertEqual(secret_ref_problems(SPECS['optional'][1]), [])
        self.assertTrue(secret_ref_problems(dict(SPECS['optional'][1], type='text')))
        self.assertTrue(secret_ref_problems(dict(
            SPECS['optional'][1], secret_ref={'key': 'a/b', 'since_chart': '0.1.7'})))
        self.assertTrue(secret_ref_problems(dict(
            SPECS['optional'][1], secret_ref={'key': 'k', 'since_chart': 'latest'})))
        self.assertTrue(has_secret_refs(SPECS))
        self.assertFalse(has_secret_refs({'optional': [{'key': 'a', 'type': 'password'}]}))
        self.assertEqual(tenant_secret_name('svc-x'), 'svc-x-tenant-secret')


@tagged('post_install', '-at_install')
class TestTenantSecretSeeds(TransactionCase):
    """seed：三支吃租戶憑證的範本都標了 secret_ref，而且全部範本的 secret_ref 都合格。"""

    def test_seeded_templates_declare_the_secret_ref(self):
        for xmlid, (path, secret_key, since) in SEEDED.items():
            with self.subTest(template=xmlid):
                specs = self.env.ref(xmlid)._parse_helm_value_specs_or_fail()
                spec = [s for s in specs.get('optional', []) + specs.get('required', [])
                        if s.get('key') == path]
                self.assertEqual(len(spec), 1, '%s must keep its %s spec' % (xmlid, path))
                self.assertEqual(spec[0].get('type'), 'password')
                self.assertEqual(spec[0].get('secret_ref'),
                                 {'key': secret_key, 'since_chart': since})

    def test_every_secret_ref_is_well_formed(self):
        templates = self.env['woow_paas_platform.cloud_app_template'].with_context(
            active_test=False).search([])
        seen = 0
        for template in templates:
            specs = template._parse_helm_value_specs_or_fail()
            if not isinstance(specs, dict):
                continue
            for group in ('required', 'optional'):
                for spec in specs.get(group) or []:
                    if isinstance(spec, dict) and 'secret_ref' in spec:
                        seen += 1
                        self.assertEqual(secret_ref_problems(spec), [],
                                         '%s %s' % (template.slug, spec.get('key')))
        self.assertGreaterEqual(seen, len(SEEDED), 'positive control: the probe sees the seeds')


@tagged('post_install', '-at_install')
class TestTenantSecretDeploy(TransactionCase):
    """部署路徑：組裝、Redeploy、reset、背景 upgrade 參數、回復保留路徑。"""

    def setUp(self):
        super().setUp()
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'INFRA-FIX-019-1 workspace'})
        self.template = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'Tenant secret probe',
            'slug': 'tenant-secret-probe',
            'category': 'automation',
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example/headscale',
            'helm_chart_version': '0.1.7',
            'mcp_enabled': False,
            'helm_value_specs': json.dumps(SPECS),
        })
        self.service = _internal_provision(self.env['woow_paas_platform.cloud_service']).create({
            'name': 'svc-ts019',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'state': 'running',
            'helm_namespace': 'paas-ws-ts019',
            'helm_release_name': 'svc-ts019',
            'helm_values': json.dumps({'ngrok': {'enabled': True, 'authtoken': TOKEN}}),
        })
        self.service.write({'helm_chart_version': '0.1.7', 'helm_revision': 3})

    def test_assemble_splits_for_a_supporting_chart(self):
        assembled = self.service._assemble_deploy_values(chart_version='0.1.7')
        self.assertNotIn(TOKEN, json.dumps(assembled['values']))
        self.assertEqual(assembled['values']['tenantSecret']['name'], 'svc-ts019-tenant-secret')
        self.assertEqual(assembled.get('tenant_secret'), {
            'name': 'svc-ts019-tenant-secret', 'data': {'ngrok_authtoken': TOKEN}})

    def test_assemble_keeps_the_legacy_lane_for_an_older_chart(self):
        for version in ('0.1.6', None):
            with self.subTest(version=version):
                assembled = self.service._assemble_deploy_values(chart_version=version)
                self.assertEqual(assembled['values']['ngrok']['authtoken'], TOKEN)
                self.assertNotIn('tenantSecret', assembled['values'])
                self.assertIsNone(assembled.get('tenant_secret'))

    def test_db_keeps_the_token(self):
        """DB 的 helm_values 不是缺陷所在（API 已遮罩）；修改配置要沿用它。"""
        self.service._assemble_deploy_values(chart_version='0.1.7')
        self.assertEqual(self.service._helm_values_dict()['ngrok']['authtoken'], TOKEN)

    def _client(self):
        client = MagicMock()
        client.get_release.return_value = {'name': 'svc-ts019', 'revision': 3}
        client.upgrade_preflight.return_value = {'ok': True, 'expansions': []}
        client.upgrade_release.return_value = {'revision': 4, 'chart_version': '0.1.7'}
        client.reset_release_config.return_value = {'helm_revision': 4, 'chart_version': '0.1.7'}
        return client

    def test_redeploy_sends_the_token_only_through_tenant_secret(self):
        client = self._client()
        with patch(_MODEL_CLIENT, return_value=client):
            self.service.action_redeploy()
        kwargs = client.upgrade_release.call_args.kwargs
        self.assertNotIn(TOKEN, json.dumps(kwargs['values']))
        self.assertNotIn(TOKEN, json.dumps(kwargs.get('sensitive_values') or {}))
        self.assertEqual(kwargs['tenant_secret']['data'], {'ngrok_authtoken': TOKEN})
        self.assertEqual(kwargs['values']['tenantSecret']['name'], 'svc-ts019-tenant-secret')

    def test_redeploy_on_an_older_pin_keeps_the_legacy_lane(self):
        self.template.write({'helm_chart_version': '0.1.6'})
        client = self._client()
        with patch(_MODEL_CLIENT, return_value=client):
            self.service.action_redeploy()
        kwargs = client.upgrade_release.call_args.kwargs
        self.assertIsNone(kwargs.get('tenant_secret'))
        self.assertEqual(kwargs['values']['ngrok']['authtoken'], TOKEN)

    def test_upgrade_kwargs_carry_tenant_secret(self):
        assembled = self.service._assemble_deploy_values(chart_version='0.1.7')
        kwargs = PaasController._upgrade_kwargs(
            self.service, '0.1.7', assembled['values'], tenant_secret=assembled.get('tenant_secret'))
        self.assertEqual(kwargs['tenant_secret']['data'], {'ngrok_authtoken': TOKEN})
        legacy = PaasController._upgrade_kwargs(self.service, '0.1.6', {'a': 1})
        self.assertNotIn('tenant_secret', legacy)

    def test_rollback_keeps_the_current_tenant_secret_ref(self):
        self.assertIn('tenantSecret', self.service._rollback_preserved_value_paths())
        self.assertIn('ngrok.authtoken', self.service._rollback_preserved_value_paths())


@tagged('post_install', '-at_install')
class TestTenantSecretClient(TransactionCase):
    """operator client：install／upgrade／reset 帶 ``tenant_secret``；預檢不送。"""

    def setUp(self):
        super().setUp()
        from odoo.addons.woow_paas_platform.services.paas_operator import PaaSOperatorClient
        self.client = PaaSOperatorClient('http://operator.test', 'k')
        self.payload = {'name': 'svc-a-tenant-secret', 'data': {'ngrok_authtoken': TOKEN}}

    def _body(self, method, *args, **kwargs):
        with patch.object(self.client, '_request', return_value={}) as request:
            getattr(self.client, method)(*args, **kwargs)
        return request.call_args.kwargs['data']

    def test_install_upgrade_reset_send_it(self):
        self.assertEqual(self._body('install_release', 'ns', 'svc-a', 'chart',
                                    tenant_secret=self.payload)['tenant_secret'], self.payload)
        self.assertEqual(self._body('upgrade_release', 'ns', 'svc-a',
                                    tenant_secret=self.payload)['tenant_secret'], self.payload)
        self.assertEqual(self._body('reset_release_config', 'ns', 'svc-a',
                                    tenant_secret=self.payload)['tenant_secret'], self.payload)

    def test_absent_means_not_sent(self):
        self.assertNotIn('tenant_secret', self._body('upgrade_release', 'ns', 'svc-a'))
        self.assertNotIn('tenant_secret', self._body('install_release', 'ns', 'svc-a', 'chart'))

    def test_preflight_never_sends_it(self):
        self.assertNotIn('tenant_secret', self._body(
            'upgrade_preflight', 'ns', 'svc-a', tenant_secret=self.payload))
