# -*- coding: utf-8 -*-
"""#1130：cloud_app_odoo template 切換到自家 woow-odoo chart（woow-paas/woow-paas-charts#81）。

依據：#1130 c16000 開工值表、2026-09-24 修訂段（#1160 resolution）、c14709（charts#81 R2）。

本檔鎖四件事：

1. **seed 只帶 tenant schema 收的鍵**——woow-odoo 的 ``values.schema.json`` 是
   ``additionalProperties: false``，殘留一個 Bitnami 鍵就在 ``helm install`` 當下失敗；
   這種錯只有真的部署才看得到，所以在這裡用 0.2.3 schema 的鍵集合先擋。
2. **平台注入**：三顆 PVC 的 storageClass、物件形 pull secret、expose 目標（release
   名＋80）、``addons.selected``、``identity.*``。
3. **初始管理員密碼**：平台鑄造、存 ``cloud_service_secret``、走 sensitive
   ``conf.adminPassword``（odoo 專屬 helper，不經 ``has_admin_gui``），三個部署呼叫點都
   送；不落 ``helm_values``。
4. **建立流程**（HttpCase，走使用者唯一入口）：一次性顯示、舊鍵 ``odooPassword`` 靜默
   丟棄、email 格式檢查。
"""
import json
from unittest.mock import MagicMock, patch

from odoo.tests import tagged
from odoo.tests.common import HttpCase, TransactionCase

from ..models.cloud_service import (
    WOOW_ODOO_SERVICE_PORT,
    _internal_provision,
)

WOOW_ODOO_CHART_REF = 'oci://jcr-prod.woowtech.io/woow-paas-docker-local/woow-odoo'
_CLIENT_PATH = (
    'odoo.addons.woow_paas_platform.services.paas_operator'
    '.get_paas_operator_client'
)
_CONTROLLER = 'odoo.addons.woow_paas_platform.controllers.paas'

#: woow-odoo 0.2.3 ``values.schema.json`` 頂層 properties 扣掉 tenant profile 拒收的
#: （``cron``／``ingress``／``extraEnv``／``networkPolicy`` 在 tenant 分支是 ``false``）。
#: 抄自 chart 原始碼（woow-paas/woow-paas-charts main b730d3f8）；chart 加鍵時這裡不必跟，
#: chart **拿掉**鍵時 seed 帶著它會在 install 失敗——那時本表要同步。
TENANT_TOP_LEVEL_KEYS = {
    'profile', 'nameOverride', 'fullnameOverride', 'global', 'imagePullSecrets',
    'image', 'replicas', 'resources', 'podSecurityContext',
    'containerSecurityContext', 'probes', 'persistence', 'addons', 'loadDemoData',
    'conf', 'smtp', 'credentials', 'postgres', 'nginx', 'upgradeGate', 'backup',
    'restore', 'identity',
}
#: 同上，``conf`` 的 properties 扣掉 tenant 拒收的 ``emailFrom``（tenant 分支 maxLength 0）。
TENANT_CONF_KEYS = {
    'adminEmail', 'adminPassword', 'workers', 'maxCronThreads', 'limitMemorySoft',
    'limitMemoryHard', 'limitTimeCpu', 'limitTimeReal', 'limitTimeRealCron',
    'limitRequest', 'dbMaxconn', 'proxyMode', 'logLevel', 'serverWideModules',
}


def _odoo_seed():
    return 'woow_paas_platform.cloud_app_odoo'


@tagged('post_install', '-at_install')
class TestWoowOdooSeedContract1130(TransactionCase):
    """seed 的形狀對齊 woow-odoo 0.2.3 tenant profile。"""

    def setUp(self):
        super().setUp()
        self.tpl = self.env.ref(_odoo_seed())
        self.values = json.loads(self.tpl.helm_default_values)

    def test_seed_targets_the_woow_odoo_chart(self):
        self.assertEqual(self.tpl.helm_chart_name, WOOW_ODOO_CHART_REF)
        self.assertEqual(self.tpl.helm_chart_version, '0.2.3')
        self.assertTrue(self.tpl._is_woow_odoo_chart())
        self.assertEqual(self.values['profile'], 'tenant')
        self.assertEqual(self.tpl.post_deploy_init_type, 'odoo',
                         'init dispatch 讀 post_deploy_init_type，換 chart 不得掉')
        self.assertEqual(self.tpl.default_port, WOOW_ODOO_SERVICE_PORT,
                         'woow-odoo Service port 是 80（nginx sidecar），不是 Bitnami 的 8069')

    def test_seed_values_only_use_keys_the_tenant_schema_accepts(self):
        """🔴 殘留任何 schema 外的鍵 ⇒ helm install 當下失敗（additionalProperties: false）。"""
        self.assertLessEqual(
            set(self.values), TENANT_TOP_LEVEL_KEYS,
            'seed 帶了 tenant schema 不收的頂層鍵：%s'
            % sorted(set(self.values) - TENANT_TOP_LEVEL_KEYS))
        self.assertLessEqual(
            set(self.values['conf']), TENANT_CONF_KEYS,
            'seed 帶了 tenant schema 不收的 conf 鍵：%s'
            % sorted(set(self.values['conf']) - TENANT_CONF_KEYS))
        self.assertNotIn('adminPassword', self.values['conf'],
                         '管理員密碼由平台鑄造、走 sensitive，不得寫進 seed')
        self.assertNotIn('credentials', self.values,
                         'tenant 一律拒收 credentials.*（三把密鑰 chart 自管）')
        self.assertNotIn('extensions', self.values.get('postgres', {}))
        self.assertEqual(self.values['postgres']['database'], 'odoo')

    def test_seed_image_is_pinned_by_digest(self):
        """tenant schema 強制 ``image.digest`` 非空（#1194 c14349）；chart 預設是全 0 佔位。"""
        image = self.values['image']
        self.assertEqual(image['registry'], 'jcr-prod.woowtech.io')
        self.assertEqual(image['repository'], 'woow-paas-docker-local/cloud-odoo')
        self.assertRegex(image['digest'], r'^sha256:[0-9a-f]{64}$')
        self.assertNotEqual(image['digest'], 'sha256:' + '0' * 64)

    def test_tenant_form_only_asks_for_login_email_and_demo_data(self):
        """#1160：``odooPassword`` 整個移除（不是改鍵名）；表單不收任何密碼。"""
        specs = self.tpl._parse_helm_value_specs()
        all_specs = specs.get('required', []) + specs.get('optional', [])
        self.assertEqual(
            {s['key'] for s in all_specs}, {'conf.adminEmail', 'loadDemoData'})
        self.assertFalse([s for s in all_specs if s.get('type') == 'password'])
        self.assertEqual([s['key'] for s in specs['required']], ['conf.adminEmail'])


@tagged('post_install', '-at_install')
class TestWoowOdooInjectors1130(TransactionCase):
    """每次部署都由 ``_assemble_deploy_values`` 注入的 woow-odoo 值。"""

    def setUp(self):
        super().setUp()
        admin = self.env.ref('base.user_admin')
        self.tpl = self.env.ref(_odoo_seed())
        self.ws = self.env['woow_paas_platform.workspace'].create({
            'name': 'WS 1130 injectors', 'owner_id': admin.id})
        Addon = self.env['woow_paas_platform.cloud_odoo_addon']
        self.third_party = Addon.create({
            'name': 'Z 1130', 'technical_name': 'z_addon_1130', 'source_type': 'third_party'})
        self.custom = Addon.create({
            'name': 'A 1130', 'technical_name': 'a_addon_1130', 'source_type': 'custom'})
        self.built_in = Addon.create({
            'name': 'Mail 1130', 'technical_name': 'mail_1130', 'source_type': 'built_in'})

    def _service(self, template=None, **extra):
        template = template or self.tpl
        vals = {
            'workspace_id': self.ws.id, 'template_id': template.id,
            'name': 'svc-1130', 'reference_id': 'test-1130-ref',
            'state': 'running', 'subdomain': 'svc-1130',
            'helm_namespace': 'paas-ws-test-1130',
            'helm_release_name': 'svc-1130abc',
        }
        vals.update(extra)
        svc = _internal_provision(self.env['woow_paas_platform.cloud_service']).create(vals)
        svc._apply_helm_values(json.loads(template.helm_default_values or '{}'))
        return svc

    def test_storage_class_lands_on_all_three_pvcs(self):
        """data／postgres／backup 三顆 PVC 的 storageClass 空值都 fail-loud，三個都注入。"""
        Svc = self.env['woow_paas_platform.cloud_service']
        values = Svc._inject_storage_class({}, 'odoo', 'longhorn-delete')
        self.assertEqual(values, {
            'persistence': {'storageClass': 'longhorn-delete'},
            'postgres': {'persistence': {'storageClass': 'longhorn-delete'}},
            'backup': {'persistence': {'storageClass': 'longhorn-delete'}},
        })
        self.assertNotIn('postgresql', values, 'Bitnami 子 chart 路徑不得再出現')

    def test_pull_secret_object_form(self):
        Svc = self.env['woow_paas_platform.cloud_service']
        values = Svc._inject_image_pull_secrets({}, 'paas-registry', as_objects=True)
        self.assertEqual(values['global']['imagePullSecrets'], [{'name': 'paas-registry'}])
        # 冪等＋把既有字串形轉成物件形（schema 只收物件）
        again = Svc._inject_image_pull_secrets(
            {'global': {'imagePullSecrets': ['other', {'name': 'paas-registry'}]}},
            'paas-registry', as_objects=True)
        self.assertEqual(again['global']['imagePullSecrets'],
                         [{'name': 'other'}, {'name': 'paas-registry'}])
        # 陽性對照：預設仍是原本的字串形（Bitnami 系 chart 不受影響）
        legacy = Svc._inject_image_pull_secrets({}, 'paas-registry')
        self.assertEqual(legacy['global']['imagePullSecrets'], ['paas-registry'])

    def test_expose_targets_the_release_service_on_port_80(self):
        """明帶 service_name／service_port，不交給 operator 的自動偵測（同 ns 有 postgres Service）。"""
        svc = self._service()
        self.assertEqual(svc._build_expose_config(), {
            'enabled': True, 'subdomain': 'svc-1130',
            'service_name': 'svc-1130abc', 'service_port': 80,
        })

    def test_expose_of_other_charts_is_unchanged(self):
        """陽性對照：非 woow-odoo 的 template 維持原本兩個鍵（不帶 service_*）。"""
        n8n = self.env.ref('woow_paas_platform.cloud_app_n8n')
        svc = self._service(template=n8n, name='svc-1130-n8n',
                            reference_id='test-1130-n8n', subdomain='svc-1130-n8n',
                            helm_release_name='svc-1130n8n')
        self.assertEqual(svc._build_expose_config(),
                         {'enabled': True, 'subdomain': 'svc-1130-n8n'})

    def test_assembled_values_carry_addons_identity_and_object_pull_secret(self):
        svc = self._service(odoo_addon_ids=[
            (6, 0, (self.third_party | self.custom | self.built_in).ids)])
        values = svc._assemble_deploy_values()['values']

        self.assertEqual(values['addons'], {'selected': ['a_addon_1130', 'z_addon_1130']},
                         '只複製 third_party／custom，且排序固定（同組勾選 render 位元相同）')
        self.assertEqual(values['identity'], {
            'cloudServiceId': str(svc.id), 'workspaceId': str(self.ws.id)})
        self.assertIn({'name': 'paas-registry'}, values['global']['imagePullSecrets'])
        for path in (('persistence',), ('postgres', 'persistence'),
                     ('backup', 'persistence')):
            node = values
            for key in path:
                node = node[key]
            self.assertTrue(node.get('storageClass'), path)
        for bitnami_key in ('initContainers', 'extraVolumes', 'extraVolumeMounts',
                            'customPostInitScripts'):
            self.assertNotIn(bitnami_key, values,
                             '%s 是 Bitnami 形 addon builder 的鍵，tenant schema 會擋' % bitnami_key)
        self.assertLessEqual(set(values), TENANT_TOP_LEVEL_KEYS)

    def test_deselecting_every_addon_sends_an_empty_list(self):
        """取消全部勾選 ⇒ 送空 list（不是沿用上次的清單）。"""
        svc = self._service(odoo_addon_ids=[(6, 0, self.third_party.ids)])
        self.assertEqual(svc._assemble_deploy_values()['values']['addons']['selected'],
                         ['z_addon_1130'])
        svc.write({'odoo_addon_ids': [(5, 0, 0)]})
        self.assertEqual(svc._assemble_deploy_values()['values']['addons'],
                         {'selected': []})

    def test_assembled_values_never_carry_the_admin_password(self):
        """``_assemble_deploy_values`` 是不含敏感值的稽核真相源：就算舊資料裡有，也剝掉。"""
        svc = self._service()
        svc.sudo().write({'helm_values': json.dumps({
            'conf': {'adminEmail': 'owner@example.com',
                     'adminPassword': 'LEAKED_IN_STORAGE_1130'}})})
        values = svc._assemble_deploy_values()['values']
        self.assertNotIn('LEAKED_IN_STORAGE_1130', json.dumps(values))
        self.assertEqual(values['conf']['adminEmail'], 'owner@example.com')


@tagged('post_install', '-at_install')
class TestWoowOdooAdminPassword1130(TransactionCase):
    """初始管理員密碼：鑄造、sensitive 通道、不落庫、三個呼叫點。"""

    def setUp(self):
        super().setUp()
        admin = self.env.ref('base.user_admin')
        self.tpl = self.env.ref(_odoo_seed())
        self.ws = self.env['woow_paas_platform.workspace'].create({
            'name': 'WS 1130 admin pw', 'owner_id': admin.id})
        self.Svc = self.env['woow_paas_platform.cloud_service']

    def _service(self, template=None, **extra):
        template = template or self.tpl
        vals = {
            'workspace_id': self.ws.id, 'template_id': template.id,
            'name': 'svc-1130-pw', 'reference_id': 'test-1130-pw',
            'state': 'running', 'subdomain': 'svc-1130-pw',
            'helm_namespace': 'paas-ws-test-1130', 'helm_release_name': 'svc-1130pw',
        }
        vals.update(extra)
        svc = _internal_provision(self.Svc).create(vals)
        svc._apply_helm_values(json.loads(template.helm_default_values or '{}'))
        return svc

    def test_create_mints_the_secret_and_sensitive_value_carries_it(self):
        svc = self._service()
        secret = svc._get_secret_record()
        self.assertTrue(secret and secret.admin_password,
                        'woow-odoo 服務建立時必須鑄造初始管理員密碼')
        self.assertFalse(secret.gateway_api_key, 'odoo 沒有 gateway，不得順手鑄 key')
        self.assertEqual(svc._build_odoo_sensitive_values(),
                         {'conf': {'adminPassword': secret.admin_password}})
        self.assertEqual(svc._build_deploy_sensitive_values(),
                         {'conf': {'adminPassword': secret.admin_password}},
                         'odoo 的 has_admin_gui 為 False，MCP 那一支必須是空的')
        self.assertFalse(self.tpl.has_admin_gui,
                         '⛔ 不得把 odoo 接上 has_admin_gui（#1141：會送 config.sensitive）')

    def test_context_password_is_the_one_stored(self):
        """controller 先鑄好、經 context 帶進 create()：存的必須是同一把（一次性顯示的那把）。"""
        svc = _internal_provision(
            self.Svc.with_context(mcp_admin_password='CTX_PW_1130')).create({
                'workspace_id': self.ws.id, 'template_id': self.tpl.id,
                'name': 'svc-1130-ctx', 'reference_id': 'test-1130-ctx',
                'helm_namespace': 'paas-ws-test-1130', 'helm_release_name': 'svc-1130ctx',
            })
        self.assertEqual(svc._get_secret_record().admin_password, 'CTX_PW_1130')

    def test_other_charts_get_no_odoo_sensitive_value(self):
        """陽性對照：閘門是 woow-odoo chart，不是 slug——Bitnami 形的 odoo template 不鑄造。"""
        legacy = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'Legacy Bitnami 1130', 'slug': 'legacy-odoo-1130',
            'helm_repo_url': '',
            'helm_chart_name': 'oci://registry-1.docker.io/bitnamicharts/odoo',
            'helm_chart_version': '28.2.10',
        })
        svc = self._service(template=legacy, name='svc-1130-legacy',
                            reference_id='test-1130-legacy',
                            helm_release_name='svc-1130legacy')
        self.assertFalse(svc._get_secret_record())
        self.assertEqual(svc._build_odoo_sensitive_values(), {})

    def test_strip_removes_only_the_admin_password(self):
        stripped = self.Svc._strip_mcp_sensitive_values({
            'conf': {'adminPassword': 'X', 'adminEmail': 'a@b.co', 'workers': 2}})
        self.assertEqual(stripped, {'conf': {'adminEmail': 'a@b.co', 'workers': 2}})

    def test_admin_email_check(self):
        check = self.Svc._woow_odoo_admin_email_error
        self.assertIsNone(check(self.tpl, {'conf': {'adminEmail': 'owner@example.com'}}))
        for bad in ('', 'owner', 'owner@', '@example.com', 'a b@example.com',
                    'owner@example', None, 42, 'x' * 250 + '@ex.com'):
            with self.subTest(bad=bad):
                self.assertTrue(check(self.tpl, {'conf': {'adminEmail': bad}}))
        self.assertTrue(check(self.tpl, {}), '缺 conf.adminEmail 也要擋')
        n8n = self.env.ref('woow_paas_platform.cloud_app_n8n')
        self.assertIsNone(check(n8n, {}), '非 woow-odoo template 不檢查')

    def test_redeploy_sends_the_admin_password_as_sensitive(self):
        svc = self._service()
        client = MagicMock()
        client.get_release.return_value = {'status': 'deployed'}
        client.upgrade_release.return_value = {'revision': 2, 'chart_version': '0.2.3'}
        with patch(_CLIENT_PATH, return_value=client):
            svc.action_redeploy()
        kwargs = client.upgrade_release.call_args.kwargs
        self.assertEqual(kwargs['sensitive_values'],
                         {'conf': {'adminPassword': svc._get_secret_record().admin_password}})
        self.assertNotIn('adminPassword', kwargs['values'].get('conf', {}),
                         'values（會被 helm get values 存下來）不得帶密碼')

    def test_reconfigure_sends_the_admin_password_as_sensitive(self):
        svc = self._service()
        from odoo.addons.woow_paas_platform.controllers.paas import PaasController

        client = MagicMock()
        client.upgrade_release.return_value = {'revision': 2}
        fake_request = MagicMock()
        fake_request.env = self.env

        class _SyncThread:
            def __init__(self, target=None, daemon=None):
                self._target = target

            def start(self):
                self._target()

        client_path = (
            'odoo.addons.woow_paas_platform.services.paas_operator.PaaSOperatorClient')
        with patch(f'{_CONTROLLER}.get_paas_operator_client', return_value=client), \
                patch(f'{_CONTROLLER}.request', new=fake_request), \
                patch(client_path, return_value=client), \
                patch(f'{_CONTROLLER}.threading.Thread', _SyncThread):
            result = PaasController()._update_service(
                svc, values={'conf.adminEmail': 'new-owner@example.com'}, version=None)

        self.assertTrue(result['success'], result)
        kwargs = client.upgrade_release.call_args.kwargs
        self.assertEqual(kwargs['sensitive_values'],
                         {'conf': {'adminPassword': svc._get_secret_record().admin_password}})
        self.assertEqual(kwargs['values']['conf']['adminEmail'], 'new-owner@example.com')
        self.assertNotIn('adminPassword', json.dumps(svc.sudo()._helm_values_dict()))

    def test_reconfigure_without_email_is_not_blocked_by_stored_values(self):
        """只改其他設定的 reconfigure：既有 values 沒有 conf.adminEmail（舊資料）也不得被擋。"""
        svc = self._service()
        svc.sudo().write({'helm_values': json.dumps({'loadDemoData': False})})
        from odoo.addons.woow_paas_platform.controllers.paas import PaasController

        client = MagicMock()
        client.upgrade_release.return_value = {'revision': 2}
        fake_request = MagicMock()
        fake_request.env = self.env

        class _SyncThread:
            def __init__(self, target=None, daemon=None):
                self._target = target

            def start(self):
                self._target()

        client_path = (
            'odoo.addons.woow_paas_platform.services.paas_operator.PaaSOperatorClient')
        with patch(f'{_CONTROLLER}.get_paas_operator_client', return_value=client), \
                patch(f'{_CONTROLLER}.request', new=fake_request), \
                patch(client_path, return_value=client), \
                patch(f'{_CONTROLLER}.threading.Thread', _SyncThread):
            result = PaasController()._update_service(
                svc, values={'loadDemoData': True}, version=None)
        self.assertTrue(result['success'], result)

    def test_reconfigure_rejects_a_malformed_email(self):
        svc = self._service()
        from odoo.addons.woow_paas_platform.controllers.paas import PaasController

        client = MagicMock()
        fake_request = MagicMock()
        fake_request.env = self.env
        with patch(f'{_CONTROLLER}.get_paas_operator_client', return_value=client), \
                patch(f'{_CONTROLLER}.request', new=fake_request):
            result = PaasController()._update_service(
                svc, values={'conf.adminEmail': 'not-an-email'}, version=None)

        self.assertFalse(result['success'])
        # 擋下的理由必須是 email 格式——不是「conf.adminEmail 不是可改的鍵」
        # （那會讓租戶永遠改不了 login email，卻與本條斷言長得一樣）。
        self.assertEqual(result['error'], 'Administrator email must be a valid email address')
        client.upgrade_release.assert_not_called()


@tagged('post_install', '-at_install')
class TestBitnamiLegacyServiceGuard1130(TransactionCase):
    """#1130 過渡守門：template 換成 woow-odoo 後，Bitnami 存量服務（2026-10：prod
    svc-f11b1cbb、stg svc-42487178）的 redeploy／reconfigure 在任何寫入前擋下、狀態不動。

    沒有這道，租戶按一次「儲存設定」或改 addon：狀態先寫 upgrading → 背景 helm upgrade
    被 woow-odoo schema 擋 → 服務翻成 error（Odoo 本身照跑）。正式守門屬 #1128。
    """

    # 舊 seed（Bitnami odoo 28.2.10）建立時存進 helm_values 的形狀（節錄）
    LEGACY_VALUES = {
        'global': {'security': {'allowInsecureImages': True}},
        'image': {'registry': 'jcr-prod.woowtech.io',
                  'repository': 'woow-paas-docker-local/cloud-odoo', 'tag': '20260814.0737'},
        'odooEmail': 'legacy@example.com', 'odooPassword': 'legacy-pw',
        'postgresql': {'enabled': True},
        'customPostInitScripts': {'odoo-conf-system.sh': '#!/bin/bash'},
    }

    def setUp(self):
        super().setUp()
        admin = self.env.ref('base.user_admin')
        self.tpl = self.env.ref(_odoo_seed())
        self.ws = self.env['woow_paas_platform.workspace'].create({
            'name': 'WS 1130 legacy', 'owner_id': admin.id})
        self.svc = _internal_provision(self.env['woow_paas_platform.cloud_service']).create({
            'workspace_id': self.ws.id, 'template_id': self.tpl.id,
            'name': 'svc-1130-legacy', 'reference_id': 'test-1130-legacy-svc',
            'state': 'running', 'subdomain': 'svc-1130-legacy',
            'helm_namespace': 'paas-ws-test-1130', 'helm_release_name': 'svc-1130lgcy',
        })
        self.svc.sudo().write({'helm_values': json.dumps(self.LEGACY_VALUES)})

    def test_reconfigure_is_refused_before_any_write(self):
        from odoo.addons.woow_paas_platform.controllers.paas import PaasController

        client = MagicMock()
        fake_request = MagicMock()
        fake_request.env = self.env
        with patch(f'{_CONTROLLER}.get_paas_operator_client', return_value=client), \
                patch(f'{_CONTROLLER}.request', new=fake_request):
            result = PaasController()._update_service(
                self.svc, values={'loadDemoData': True}, version=None)

        self.assertFalse(result['success'])
        self.assertIn('previous Odoo deployment', result['error'])
        self.assertEqual(self.svc.state, 'running', '狀態不得被改成 upgrading／error')
        self.assertEqual(self.svc.sudo()._helm_values_dict(), self.LEGACY_VALUES,
                         '被擋的 reconfigure 不得改動存的 values')
        client.upgrade_release.assert_not_called()

    def test_redeploy_is_refused_without_touching_the_operator(self):
        from odoo.exceptions import UserError

        client = MagicMock()
        with patch(_CLIENT_PATH, return_value=client):
            with self.assertRaises(UserError):
                self.svc.action_redeploy()
        client.get_release.assert_not_called()
        client.upgrade_release.assert_not_called()
        self.assertEqual(self.svc.state, 'running')
        self.assertFalse(self.svc.init_redeploy_requested)

    def test_woow_odoo_shaped_service_is_not_caught(self):
        """陽性對照：新形狀（含空 values 的 fixture）不是存量，放行。"""
        self.svc.sudo().write({'helm_values': self.tpl.helm_default_values})
        self.assertIsNone(self.svc._woow_odoo_legacy_release_error())
        self.svc.sudo().write({'helm_values': False})
        self.assertIsNone(self.svc._woow_odoo_legacy_release_error())


@tagged('post_install', '-at_install')
class TestWoowOdooCreateFlow1130(HttpCase):
    """走使用者唯一入口（``/api/workspaces/<id>/services`` 的 ``action='create'``）。"""

    def setUp(self):
        super().setUp()
        self.authenticate('admin', 'admin')
        admin = self.env.ref('base.user_admin')
        self.ws = self.env['woow_paas_platform.workspace'].create({
            'name': 'WS 1130 create', 'owner_id': admin.id})
        # 訂閱建立需要 company.sh_journal_id（同 test_cs_spec_332 的同段理由）。
        company = self.env.company.sudo()
        if not self.env['account.journal'].search([
            ('company_id', '=', company.id), ('type', '=', 'sale'),
        ], limit=1):
            self.env['account.journal'].sudo().create({
                'name': 'Test Sales 1130', 'code': 'TS113', 'type': 'sale',
                'company_id': company.id})
        self.env['res.company'].sudo()._ensure_subscription_journal()
        self.assertTrue(company.sh_journal_id)

        # 合成 plan／template：沿用 seed 的 woow-odoo values／specs，但不綁 seed 商品的
        # 定價與上架狀態（#478／#494 教訓）。
        seed = self.env.ref(_odoo_seed())
        self.plan = self.env['sh.subscription.plan'].create({
            'name': 'Plan 1130', 'sh_duration': 1, 'sh_unit': 'month',
            'sh_plan_price': 100, 'sh_no_of_billing_cycle': -1,
            'service_type': 'cloud_service',
            'cs_vcpu': 2, 'cs_ram_gb': 2.0, 'cs_storage_gb': 20, 'cs_odoo_workers': 2})
        self.env['product.template'].create({
            'name': 'Product 1130', 'type': 'service', 'list_price': 100,
            'sh_product_subscribe': True, 'sh_subscription_plan_id': self.plan.id})
        self.tpl = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'Odoo 1130', 'slug': 'odoo-1130',
            'helm_repo_url': '',
            'helm_chart_name': seed.helm_chart_name,
            'helm_chart_version': seed.helm_chart_version,
            'helm_default_values': seed.helm_default_values,
            'helm_value_specs': seed.helm_value_specs,
            'default_port': seed.default_port,
            'ingress_enabled': True,
            'post_deploy_init_type': 'odoo',
            'is_active': True, 'is_hidden': False,
            'subscription_plan_ids': [(6, 0, [self.plan.id])]})
        self.env.flush_all()

    def _create(self, name, values):
        body = json.dumps({'jsonrpc': '2.0', 'method': 'call', 'params': {
            'action': 'create', 'template_id': self.tpl.id, 'name': name,
            'plan_id': self.plan.id, 'values': values}})
        resp = self.url_open(
            '/api/workspaces/%s/services' % self.ws.id,
            data=body, headers={'Content-Type': 'application/json'})
        parsed = resp.json()
        self.assertIn('result', parsed, 'resp: %s' % parsed)
        self.env.invalidate_all()
        return parsed['result']

    def _services(self):
        return self.env['woow_paas_platform.cloud_service'].search([
            ('template_id', '=', self.tpl.id), ('workspace_id', '=', self.ws.id)])

    def test_create_shows_the_minted_password_once_and_never_stores_it(self):
        result = self._create('svc-1130-create', {
            'conf.adminEmail': 'owner@example.com',
            # 舊表單的鍵：create 靜默丟棄（#1160），不得落庫也不得擋下建立
            'odooPassword': 'LEGACY_FORM_PW_1130',
        })
        self.assertTrue(result.get('success'), result)
        data = result['data']
        svc = self._services()
        self.assertEqual(len(svc), 1)

        secret = svc._get_secret_record()
        self.assertTrue(data.get('admin_password'), '建立回應必須一次性帶出初始密碼')
        self.assertEqual(data['admin_password'], secret.admin_password,
                         '顯示的必須是真的會送進 chart 的那一把')
        self.assertEqual(data.get('admin_username'), 'owner@example.com')
        self.assertFalse(data['template']['admin_reset_supported'],
                         '前端依此顯示「Initial Password」文案；重設流程屬 #1186')

        stored_raw = svc.sudo().helm_values
        stored = json.loads(stored_raw)
        self.assertNotIn(secret.admin_password, stored_raw, '初始密碼不得落 helm_values')
        self.assertNotIn('LEGACY_FORM_PW_1130', stored_raw)
        self.assertNotIn('odooPassword', stored)
        self.assertEqual(stored['conf']['adminEmail'], 'owner@example.com')
        self.assertEqual(stored['conf']['workers'], 2, 'plan overlay 寫 typed conf.workers')
        self.assertNotIn(secret.admin_password, json.dumps(data.get('helm_values') or {}))

    def test_create_rejects_a_malformed_admin_email(self):
        result = self._create('svc-1130-bad', {'conf.adminEmail': 'not-an-email'})
        self.assertFalse(result.get('success'), result)
        self.assertFalse(self._services(), '被擋的建立不得留下服務列')
