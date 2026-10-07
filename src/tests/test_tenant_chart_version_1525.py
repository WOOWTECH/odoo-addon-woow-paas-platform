# -*- coding: utf-8 -*-
"""#1525：租戶不能指定服務的 chart 版本。

## 缺陷

``api_workspace_service(action='update', version=…)`` 只檢查 owner／admin，就把
``version`` 傳進 ``_update_service``；原本 ``resolved_version = version or
service.helm_chart_version or template.helm_chart_version``，租戶送的值優先、直接成為
operator ``upgrade_release`` 的 ``--version``。平台與 operator 都沒有白名單，不帶
``values`` 也不經過 #1488 的 key 白名單 ⇒ 任何 owner／admin 都能把服務換成 registry
裡的任何一版（降回有 console RBAC 洞的 hermes-agent 0.2.1、沒有 egress 收斂的
pi-agent 0.1.4），等於自行撤回平台的安全修正。

同一個效果還有一條 ORM 繞道：``helm_chart_version`` 本身沒有任何寫入守衛，而 #552 的
fallback 讓 reconfigure 以「服務自己的版本」釘版 ⇒ internal user 的 owner 可以先
``/web/dataset/call_kw`` 把帳本寫成任意版本、再按一次「儲存設定」。

## 本檔守的三件事

1. controller：``version`` 不再參與解析；有值且不等於這次會用的版本 ⇒ 回錯誤、任何
   寫入之前就擋下（不是靜默忽略）。沒帶、或帶的正好是目前版本 ⇒ 照舊。
2. model：``helm_chart_version`` 只有平台身分寫得進去（真 RPC 驗證），平台自己的寫入
   （reconfigure、改帳密、回復）走 ``_record_chart_version`` 仍然成立。
3. 系統管理員的後台 Redeploy（範本 pin）不受影響。
"""
import json
import uuid
from unittest.mock import MagicMock, patch

from odoo.exceptions import AccessError
from odoo.tests import HttpCase, tagged
from odoo.tests.common import TransactionCase

from odoo.addons.woow_paas_platform.controllers.paas import PaasController
from odoo.addons.woow_paas_platform.models.workspace_access import ROLE_OWNER
from ..models.cloud_service import _internal_provision

_CONTROLLER = 'odoo.addons.woow_paas_platform.controllers.paas'
_MODEL_CLIENT = (
    'odoo.addons.woow_paas_platform.services.paas_operator.get_paas_operator_client'
)


class _NonClosingCursor:
    """背景執行緒的 ``odoo.registry(db).cursor()`` 導回測試 transaction（同 #552 測試）。"""

    def __init__(self, cr):
        self._cr = cr

    def __enter__(self):
        return self._cr

    def __exit__(self, *exc_info):
        return False


class _SyncThread:
    def __init__(self, target=None, daemon=None):
        self._target = target

    def start(self):
        self._target()


def _new_group_user(env, name):
    """只有 ``base.group_user`` 的租戶帳號（自建 workspace 就是 owner 的那種）。"""
    login = '%s_%s' % (name, uuid.uuid4().hex[:8])
    return env['res.users'].sudo().create({
        'name': '%s 1525' % name,
        'login': login,
        'password': login,
        'email': '%s@test.example' % login,
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    }), login


class _Fixture:

    TEMPLATE_PIN = '0.3.0'
    SERVICE_VERSION = '0.2.0'

    def _make_template(self, slug):
        return self.env['woow_paas_platform.cloud_app_template'].create({
            'name': '1525 %s' % slug,
            'slug': slug,
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example.com/%s' % slug,
            'helm_chart_version': self.TEMPLATE_PIN,
            'workload_type': 'container',
            'post_deploy_init_type': 'none',
            'mcp_enabled': False,
            'helm_default_values': '{}',
        })

    def _make_service(self, template, workspace, chart_version, release):
        service = _internal_provision(self.env['woow_paas_platform.cloud_service']).create({
            'name': 'svc %s' % release,
            'workspace_id': workspace.id,
            'template_id': template.id,
            'state': 'running',
            'helm_namespace': 'paas-ws-t1525',
            'helm_release_name': release,
            'helm_values': json.dumps({'config': {'nonSensitive': {'model': 'm1'}}}),
        })
        # 平台身分（TransactionCase 的 superuser）寫入起始帳本。
        service.write({'helm_chart_version': chart_version or False})
        return service


@tagged('post_install', '-at_install')
class TestReconfigureRejectsTenantChartVersion1525(_Fixture, TransactionCase):
    """controller 的 ``version`` 參數：只接受「這次會用的版本」，其餘回錯誤。"""

    def setUp(self):
        super().setUp()
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': '1525 workspace',
        })
        self.template = self._make_template('t1525-reconf')

    def _run_update(self, service, version, env=None):
        client = MagicMock()
        client.upgrade_release.return_value = {'revision': 2}
        client.base_url = 'http://operator.test'
        client._session.headers = {'X-API-Key': 'k'}
        env = env or self.env
        fake_registry = MagicMock()
        fake_registry.cursor.return_value = _NonClosingCursor(self.env.cr)
        with patch(f'{_CONTROLLER}.get_paas_operator_client', return_value=client), \
                patch(f'{_CONTROLLER}.request', new=MagicMock(env=env)), \
                patch('odoo.addons.woow_paas_platform.services.paas_operator'
                      '.PaaSOperatorClient', return_value=client), \
                patch(f'{_CONTROLLER}.threading.Thread', _SyncThread), \
                patch('odoo.registry', return_value=fake_registry):
            result = PaasController()._update_service(
                service, values={}, version=version)
        return result, client

    def _sent_version(self, client):
        self.assertTrue(client.upgrade_release.call_args,
                        '前提：背景執行緒真的呼叫了 upgrade_release')
        return client.upgrade_release.call_args.kwargs['version']

    def test_without_version_pins_the_services_own_version(self):
        service = self._make_service(
            self.template, self.workspace, self.SERVICE_VERSION, 'svc-1525-none')

        result, client = self._run_update(service, None)

        self.assertTrue(result['success'], result)
        self.assertEqual(self._sent_version(client), self.SERVICE_VERSION)

    def test_current_version_is_accepted(self):
        """舊客戶端回傳目前版本：不是要換版，放行。"""
        service = self._make_service(
            self.template, self.workspace, self.SERVICE_VERSION, 'svc-1525-same')

        result, client = self._run_update(service, self.SERVICE_VERSION)

        self.assertTrue(result['success'], result)
        self.assertEqual(self._sent_version(client), self.SERVICE_VERSION)

    def test_other_version_is_rejected_before_any_write(self):
        """降版（稽核實例：hermes-agent 0.2.1、pi-agent 0.1.4）：擋下，且沒有任何副作用。"""
        service = self._make_service(
            self.template, self.workspace, self.SERVICE_VERSION, 'svc-1525-other')
        values_before = service.sudo().helm_values

        result, client = self._run_update(service, '0.1.4')

        self.assertFalse(result['success'], result)
        self.assertIn(self.SERVICE_VERSION, result['error'],
                      '錯誤訊息要說出這台服務目前的版本，租戶才知道為什麼被擋')
        client.upgrade_release.assert_not_called()
        service.invalidate_recordset()
        self.assertEqual(service.state, 'running', '被拒的請求不得推進 state')
        self.assertEqual(service.helm_chart_version, self.SERVICE_VERSION)
        self.assertEqual(service.sudo().helm_values, values_before)

    def test_template_pin_is_not_a_tenant_upgrade_path(self):
        """等於範本 pin、但服務目前是別版：也擋。

        升版是管理員 Redeploy 的職責（會一併刷新 image、核對版本，#1408）；讓租戶自己
        送 pin 等於繞過那條路。
        """
        service = self._make_service(
            self.template, self.workspace, self.SERVICE_VERSION, 'svc-1525-pin')

        result, client = self._run_update(service, self.TEMPLATE_PIN)

        self.assertFalse(result['success'], result)
        client.upgrade_release.assert_not_called()

    def test_legacy_service_without_ledger_accepts_the_template_pin(self):
        """帳本空（legacy）時這次會用的就是範本 pin，送它等於沒指定。"""
        service = self._make_service(
            self.template, self.workspace, False, 'svc-1525-legacy')

        result, client = self._run_update(service, self.TEMPLATE_PIN)

        self.assertTrue(result['success'], result)
        self.assertEqual(self._sent_version(client), self.TEMPLATE_PIN)

    def test_non_string_version_is_rejected(self):
        service = self._make_service(
            self.template, self.workspace, self.SERVICE_VERSION, 'svc-1525-list')

        result, client = self._run_update(service, [self.SERVICE_VERSION])

        self.assertFalse(result['success'], result)
        client.upgrade_release.assert_not_called()

    def test_tenant_identity_reconfigure_still_records_the_ledger(self):
        """平台自己的帳本寫入（legacy 服務補上 pin）在租戶身分下仍然成立。

        ``_update_service`` 的 ``service`` 對 internal user 是租戶身分；#1525 之後
        ``helm_chart_version`` 租戶身分寫不進去，平台改走 ``_record_chart_version``。
        """
        tenant, _login = _new_group_user(self.env, 'owner')
        workspace = self.env['woow_paas_platform.workspace'].with_user(tenant).create({
            'name': '1525 tenant workspace',
        })
        self.assertEqual(
            workspace.sudo().access_ids.filtered(lambda a: a.user_id == tenant).role,
            ROLE_OWNER, '前提：自建 workspace 的人就是 owner')
        service = self._make_service(self.template, workspace, False, 'svc-1525-tenant')

        result, client = self._run_update(
            service.with_user(tenant), None, env=self.env(user=tenant))

        self.assertTrue(result['success'], result)
        self.assertEqual(self._sent_version(client), self.TEMPLATE_PIN)
        service.invalidate_recordset()
        self.assertEqual(service.helm_chart_version, self.TEMPLATE_PIN)


@tagged('post_install', '-at_install')
class TestChartVersionLedgerIsPlatformManaged1525(_Fixture, TransactionCase):
    """model 守衛的陽性對照：平台的幾條帳本寫入都還在，系統管理員 Redeploy 不受影響。"""

    def setUp(self):
        super().setUp()
        self.tenant, _login = _new_group_user(self.env, 'ledger')
        self.workspace = self.env['woow_paas_platform.workspace'].with_user(
            self.tenant).create({'name': '1525 ledger workspace'})
        self.template = self._make_template('t1525-ledger')
        self.service = self._make_service(
            self.template, self.workspace, self.SERVICE_VERSION, 'svc-1525-ledger')

    def test_tenant_orm_write_is_rejected(self):
        with self.assertRaises(AccessError):
            self.service.with_user(self.tenant).write({'helm_chart_version': '0.1.4'})
        self.service.invalidate_recordset()
        self.assertEqual(self.service.helm_chart_version, self.SERVICE_VERSION)

    def test_tenant_can_still_write_other_fields(self):
        """陽性對照：擋下來的是這個欄位，不是租戶對這筆服務的寫入權。"""
        self.service.with_user(self.tenant).write({'name': 'renamed 1525'})
        self.assertEqual(self.service.name, 'renamed 1525')

    def test_record_chart_version_works_from_tenant_identity(self):
        self.service.with_user(self.tenant)._record_chart_version('0.2.5')
        self.service.invalidate_recordset()
        self.assertEqual(self.service.helm_chart_version, '0.2.5')

    def test_record_chart_version_ignores_unknown(self):
        """operator 沒回報版本（空值）時保留原值，未知不得覆蓋成空。"""
        self.service.with_user(self.tenant)._record_chart_version(False)
        self.assertEqual(self.service.helm_chart_version, self.SERVICE_VERSION)

    def test_tenant_rollback_records_the_reported_version(self):
        """回復的帳本回寫（operator 回報的事實）在租戶身分下仍然成立。"""
        client = MagicMock()
        client.rollback_release.return_value = {
            'message': 'ok', 'mode': 'upgrade_preserving', 'chart_version': '0.2.5'}
        tenant_env = self.env(user=self.tenant)
        with patch(f'{_CONTROLLER}.get_paas_operator_client', return_value=client), \
                patch(f'{_CONTROLLER}.request', new=MagicMock(env=tenant_env)):
            result = PaasController()._rollback_service(
                self.service.with_user(self.tenant), revision=1)
        self.assertTrue(result.get('success'), result)
        self.service.invalidate_recordset()
        self.assertEqual(self.service.helm_chart_version, '0.2.5')
        self.assertEqual(self.service.state, 'upgrading')

    def test_admin_redeploy_moves_to_the_template_pin(self):
        """系統管理員的後台 Redeploy：照範本 pin 升版、帳本記下實際版本，不受守衛影響。"""
        admin = self.env.ref('base.user_admin')
        client = MagicMock()
        client.get_release.return_value = {'name': 'svc-1525-ledger', 'revision': 3}
        client.upgrade_release.return_value = {
            'revision': 4, 'chart_version': self.TEMPLATE_PIN}
        with patch(_MODEL_CLIENT, return_value=client):
            self.service.with_user(admin).action_redeploy()
        self.assertEqual(
            client.upgrade_release.call_args.kwargs['version'], self.TEMPLATE_PIN)
        self.service.invalidate_recordset()
        self.assertEqual(self.service.helm_chart_version, self.TEMPLATE_PIN)


@tagged('post_install', '-at_install')
class TestTenantChartVersionOverRpc1525(_Fixture, HttpCase):
    """真 RPC：ir.rule／ACL 只在非 superuser 的請求裡生效，直呼 method 量不到。"""

    def setUp(self):
        super().setUp()
        self.owner, self.owner_login = _new_group_user(self.env, 'rpcowner')
        self.workspace = self.env['woow_paas_platform.workspace'].with_user(
            self.owner).create({'name': '1525 rpc workspace'})
        self.template = self._make_template('t1525-rpc')
        self.service = self._make_service(
            self.template, self.workspace, self.SERVICE_VERSION, 'svc-1525-rpc')

    def _json(self, url, params):
        body = json.dumps({'jsonrpc': '2.0', 'method': 'call', 'params': params})
        resp = self.url_open(url, data=body, headers={'Content-Type': 'application/json'})
        self.env.invalidate_all()
        return resp.json()

    def _call_kw(self, method, args, kwargs=None):
        return self._json('/web/dataset/call_kw', {
            'model': 'woow_paas_platform.cloud_service', 'method': method,
            'args': args, 'kwargs': kwargs or {}})

    def test_owner_cannot_write_the_ledger_over_call_kw(self):
        self.authenticate(self.owner_login, self.owner_login)
        # 陽性對照：同一個人對同一筆服務的一般寫入是成功的。
        ok = self._call_kw('write', [[self.service.id], {'name': 'rpc renamed'}])
        self.assertEqual(ok.get('result'), True, ok)

        denied = self._call_kw(
            'write', [[self.service.id], {'helm_chart_version': '0.1.4'}])

        self.assertIn('error', denied, denied)
        data = denied['error'].get('data') or {}
        self.assertIn('AccessError', data.get('name', ''), denied)
        self.assertIn('managed by the platform', data.get('message', ''), denied)
        self.assertEqual(self.service.helm_chart_version, self.SERVICE_VERSION)

    def test_owner_update_with_another_version_is_rejected(self):
        self.authenticate(self.owner_login, self.owner_login)
        client = MagicMock()
        with patch(f'{_CONTROLLER}.get_paas_operator_client', return_value=client):
            result = self._json(
                '/api/workspaces/%d/services/%d' % (self.workspace.id, self.service.id),
                {'action': 'update', 'version': '0.1.4'})

        payload = result.get('result') or {}
        self.assertFalse(payload.get('success'), result)
        self.assertIn(self.SERVICE_VERSION, payload.get('error', ''), result)
        client.upgrade_release.assert_not_called()
        self.assertEqual(self.service.state, 'running')
        self.assertEqual(self.service.helm_chart_version, self.SERVICE_VERSION)
