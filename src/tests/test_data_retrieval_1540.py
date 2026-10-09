"""#1540：停權／刪除後，服務 owner／admin 可以自助取回資料（限時唯讀入口）。

operator 端（``POST/GET/DELETE /api/releases/{ns}/{release}/retrieval``、``POST
/api/retrievals/sweep``）建／刪唯讀 pod 與 tunnel route；本檔釘住平台端：

* **權限**：只有工作區 owner／admin；user／guest 一律「權限不足」，非成員「找不到工作區」，
  operator 一次都不會被呼叫。
* **狀態閘**：只收停權中（``suspended``）的服務；執行中、範本沒有 profile 的一律拒絕。
  刻意**不**走訂閱閘——這個功能就是給停權／欠費的服務用的。
* **一次性密碼**：只出現在 open 的回應；session 的任何欄位都不含它。
* **同一台只能有一個入口**；operator 的錯誤碼轉成租戶看得懂的訊息（不透出 operator 原文）。
* **復權**：scale 1 之前先關入口（RWO）；關不掉不擋復權。
* **刪除**：``unlink()`` 記下保留中的磁碟（帳本），開著的入口改掛帳本列；從沒 running 過、
  範本不支援的服務不記。
* **cron**：關到期的入口、重試 closing、請 operator sweep、刪過期帳本列。
* 前端：按鈕讀 payload 的 ``data_retrieval``；視窗文案都有 zh_TW。
"""
import os
import re
from datetime import timedelta
from unittest.mock import MagicMock, call, patch

from psycopg2 import IntegrityError

from odoo import fields
from odoo.tests.common import TransactionCase, tagged
from odoo.tools import mute_logger
from odoo.tools.translate import PoFileReader

from odoo.addons.woow_paas_platform.controllers.paas import PaasController
from odoo.addons.woow_paas_platform.models.workspace_access import (
    ROLE_ADMIN, ROLE_GUEST, ROLE_USER,
)
from ..models.cloud_service import _internal_provision
from ..models.data_retrieval import (
    DATA_RETENTION_DAYS_PARAM,
    DEFAULT_DATA_RETENTION_DAYS,
    RETRIEVAL_TTL_HOURS,
)
from ..services.paas_operator import PaaSOperatorAPIError, PaaSOperatorConnectionError

_CLIENT_SEAM = (
    'odoo.addons.woow_paas_platform.services.paas_operator.get_paas_operator_client'
)
_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PAAS = os.path.join(_SRC, 'static', 'src', 'paas')

NS = 'paas-ws-1540test'
REL = 'svc-1540abcd'
HOST = 'paas-dl-0123456789ab.woowtech.io'
PASSWORD = 'one-time-Secret-1540-xyz'
PERMISSION_DENIED = 'Permission denied'
NOT_FOUND = 'Workspace not found or access denied'


def _open_result(**extra):
    result = {
        'status': 'opened',
        'hostname': HOST,
        'url': f'https://{HOST}/',
        'username': 'download',
        'password': PASSWORD,
        'opened_at': '2026-10-09T12:00:00Z',
        'expires_at': '2026-10-12T12:00:00Z',
        'data_expires_at': None,
        'pvc': f'{REL}-nextcloud-data',
        'profile': 'nextcloud',
        'route': 'created',
        'dns_failed': False,
    }
    result.update(extra)
    return result


def _client():
    client = MagicMock()
    client.open_data_retrieval.return_value = _open_result()
    client.get_data_retrieval.return_value = {'status': 'active'}
    client.close_data_retrieval.return_value = {'status': 'closed', 'route': 'deleted'}
    client.sweep_data_retrievals.return_value = {'checked': 0, 'closed': [], 'failed': []}
    return client


def _api_error(status_code, code):
    return PaaSOperatorAPIError(
        'API error', status_code=status_code, detail={'error_code': code, 'message': 'x'})


class _Fixture(TransactionCase):

    def setUp(self):
        super().setUp()
        self.controller = PaasController()
        self.Service = self.env['woow_paas_platform.cloud_service']
        self.Session = self.env['woow_paas_platform.data_retrieval'].sudo()
        self.Retained = self.env['woow_paas_platform.retained_service_data'].sudo()
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': '1540 Workspace',
        })
        Template = self.env['woow_paas_platform.cloud_app_template']
        self.template = Template.create({
            'name': 'Files 1540',
            'slug': 'files-1540',
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example.com/nextcloud',
            'helm_chart_version': '0.1.7',
            'post_deploy_init_type': 'none',
            'data_retrieval_profile': 'nextcloud',
        })
        self.plain_template = Template.create({
            'name': 'Plain 1540',
            'slug': 'plain-1540',
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example.com/plain',
            'helm_chart_version': '1.0.0',
            'post_deploy_init_type': 'none',
        })
        self.admin = self._member('dr-1540-admin', ROLE_ADMIN)
        self.user = self._member('dr-1540-user', ROLE_USER)
        self.guest = self._member('dr-1540-guest', ROLE_GUEST)
        self.outsider = self._user('dr-1540-outsider')

    def _user(self, login):
        return self.env['res.users'].create({
            'name': login, 'login': login,
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })

    def _member(self, login, role):
        user = self._user(login)
        self.env['woow_paas_platform.workspace_access'].create({
            'workspace_id': self.workspace.id, 'user_id': user.id, 'role': role,
        })
        return user

    def _service(self, state='suspended', template=None, **extra):
        vals = {
            'name': 'files-1540',
            'workspace_id': self.workspace.id,
            'template_id': (template or self.template).id,
            'state': state,
            'helm_namespace': NS,
            'helm_release_name': REL,
            'first_running_at': fields.Datetime.now() - timedelta(days=30),
        }
        vals.update(extra)
        return _internal_provision(self.Service).create(vals)

    def _as(self, user):
        return patch(
            'odoo.addons.woow_paas_platform.controllers.paas.request',
            new=MagicMock(env=self.env(user=user)),
        )

    def _call(self, user, service, action, client=None):
        client = client or _client()
        with patch(_CLIENT_SEAM, return_value=client), self._as(user):
            result = self.controller.api_service_data_retrieval(
                self.workspace.id, service.id, action=action)
        return result, client


# ---------------------------------------------------------------------------
# payload／能力判準
# ---------------------------------------------------------------------------


@tagged('post_install', '-at_install')
class TestDataRetrievalCapability1540(_Fixture):

    def test_capability_follows_state_and_template(self):
        suspended = self._service()
        self.assertEqual(suspended._data_retrieval_capability(),
                         {'supported': True, 'available': True})
        running = self._service(state='running', helm_release_name='svc-1540-run')
        self.assertEqual(running._data_retrieval_capability(),
                         {'supported': True, 'available': False})
        plain = self._service(template=self.plain_template, helm_release_name='svc-1540-pl')
        self.assertEqual(plain._data_retrieval_capability(),
                         {'supported': False, 'available': False})

    def test_seed_nextcloud_supports_retrieval(self):
        template = self.env.ref('woow_paas_platform.cloud_app_nextcloud')
        self.assertEqual(template.data_retrieval_profile, 'nextcloud')

    def test_list_payload_carries_capability(self):
        service = self._service()
        data = self.controller._format_service(service)
        self.assertEqual(data['data_retrieval'], {'supported': True, 'available': True})

    def test_session_only_in_managers_detail_payload(self):
        service = self._service()
        self._call(self.admin, service, 'open')
        with self._as(self.admin):
            managed = self.controller.api_workspace_service(
                self.workspace.id, service.id, action='get')
        self.assertTrue(managed['success'], managed)
        session = managed['data']['data_retrieval']['session']
        self.assertEqual(session['hostname'], HOST)
        self.assertEqual(session['url'], f'https://{HOST}/')
        self.assertNotIn('password', session)
        with self._as(self.user):
            viewer = self.controller.api_workspace_service(
                self.workspace.id, service.id, action='get')
        self.assertTrue(viewer['success'], viewer)
        self.assertNotIn('session', viewer['data']['data_retrieval'])


# ---------------------------------------------------------------------------
# 權限
# ---------------------------------------------------------------------------


@tagged('post_install', '-at_install')
class TestDataRetrievalPermissions1540(_Fixture):

    def test_user_and_guest_are_denied_without_calling_the_operator(self):
        service = self._service()
        for member in (self.user, self.guest):
            for action in ('status', 'open', 'close'):
                with self.subTest(login=member.login, action=action):
                    result, client = self._call(member, service, action)
                    self.assertEqual(result, {'success': False, 'error': PERMISSION_DENIED})
                    self.assertEqual(client.mock_calls, [])

    def test_outsider_sees_no_workspace(self):
        service = self._service()
        result, client = self._call(self.outsider, service, 'open')
        self.assertEqual(result, {'success': False, 'error': NOT_FOUND})
        self.assertEqual(client.mock_calls, [])

    def test_service_of_another_workspace_is_not_found(self):
        other_ws = self.env['woow_paas_platform.workspace'].create({'name': '1540 other'})
        other = _internal_provision(self.Service).create({
            'name': 'other', 'workspace_id': other_ws.id, 'template_id': self.template.id,
            'state': 'suspended', 'helm_namespace': 'paas-ws-other1540',
            'helm_release_name': 'svc-other1540',
        })
        result, client = self._call(self.admin, other, 'open')
        self.assertFalse(result['success'])
        self.assertEqual(client.mock_calls, [])

    def test_models_are_not_tenant_readable(self):
        for model in ('woow_paas_platform.data_retrieval',
                      'woow_paas_platform.retained_service_data'):
            with self.subTest(model=model):
                self.assertFalse(
                    self.env[model].with_user(self.admin).has_access('read'))


# ---------------------------------------------------------------------------
# 開／關
# ---------------------------------------------------------------------------


@tagged('post_install', '-at_install')
class TestDataRetrievalOpenClose1540(_Fixture):

    def test_admin_opens_and_password_is_never_stored(self):
        service = self._service()
        result, client = self._call(self.admin, service, 'open')
        self.assertTrue(result['success'], result)
        client.open_data_retrieval.assert_called_once_with(
            NS, REL, 'nextcloud', RETRIEVAL_TTL_HOURS)
        credentials = result['data']['credentials']
        self.assertEqual(credentials['password'], PASSWORD)
        self.assertEqual(credentials['username'], 'download')
        self.assertEqual(credentials['url'], f'https://{HOST}/')
        session = self.Session.search([('helm_release_name', '=', REL)])
        self.assertEqual(len(session), 1)
        self.assertEqual(session.state, 'active')
        self.assertEqual(session.hostname, HOST)
        self.assertEqual(session.service_id, service)
        self.assertEqual(session.opened_by_id, self.admin)
        self.assertEqual(str(session.expires_at), '2026-10-12 12:00:00')
        self.assertNotIn(PASSWORD, repr(session.read()[0]))
        # 回應裡的 session 也不含密碼（只有 credentials 帶一次）。
        self.assertNotIn(PASSWORD, repr(result['data']['session']))

    def test_owner_role_can_open(self):
        """owner 由 workspace.owner_id 決定（一個工作區只能有一個 owner）。"""
        owner = self._user('dr-1540-owner')
        workspace = self.env['woow_paas_platform.workspace'].create({
            'name': '1540 owned', 'owner_id': owner.id})
        service = self._service(workspace_id=workspace.id, helm_namespace='paas-ws-own1540')
        client = _client()
        with patch(_CLIENT_SEAM, return_value=client), self._as(owner):
            result = self.controller.api_service_data_retrieval(
                workspace.id, service.id, action='open')
        self.assertTrue(result['success'], result)
        client.open_data_retrieval.assert_called_once()

    def test_only_suspended_services(self):
        for state in ('running', 'error', 'upgrading', 'deploying'):
            with self.subTest(state=state):
                service = self._service(state=state, helm_release_name=f'svc-1540-{state}')
                result, client = self._call(self.admin, service, 'open')
                self.assertFalse(result['success'])
                self.assertIn('only available while the service is suspended', result['error'])
                client.open_data_retrieval.assert_not_called()

    def test_template_without_profile_is_refused(self):
        service = self._service(template=self.plain_template)
        result, client = self._call(self.admin, service, 'open')
        self.assertEqual(result, {'success': False,
                                  'error': 'This service does not support data retrieval.'})
        client.open_data_retrieval.assert_not_called()

    def test_second_open_is_refused_while_one_is_active(self):
        service = self._service()
        self._call(self.admin, service, 'open')
        result, client = self._call(self.admin, service, 'open')
        self.assertFalse(result['success'])
        self.assertIn('already open', result['error'])
        client.open_data_retrieval.assert_not_called()

    def test_operator_error_codes_become_tenant_messages(self):
        cases = [
            (_api_error(409, 'retrieval_pvc_in_use'), 'has not fully stopped yet'),
            (_api_error(409, 'retrieval_release_running'), 'still running'),
            (_api_error(409, 'retrieval_already_open'), 'already open'),
            (_api_error(409, 'data_retention_expired'), 'retention period'),
            (_api_error(409, 'retrieval_quota_exceeded'), 'quota'),
            (_api_error(409, 'retrieval_closing'), 'still shutting down'),
            (_api_error(502, 'cloudflare_error'), 'Could not open the data retrieval link'),
            (PaaSOperatorAPIError('Not Found', status_code=404, detail='Not Found'),
             'not available yet'),
            (PaaSOperatorConnectionError('down'), 'Could not open the data retrieval link'),
        ]
        for index, (error, expected) in enumerate(cases):
            with self.subTest(expected=expected):
                release = f'svc-1540-e{index}'
                service = self._service(helm_release_name=release)
                client = _client()
                client.open_data_retrieval.side_effect = error
                result, _c = self._call(self.admin, service, 'open', client)
                self.assertFalse(result['success'])
                self.assertIn(expected, result['error'])
                self.assertNotIn("'x'", result['error'])  # operator 原文不透出
                self.assertFalse(self.Session.search([('helm_release_name', '=', release)]))

    def test_manual_close(self):
        service = self._service()
        self._call(self.admin, service, 'open')
        result, client = self._call(self.admin, service, 'close')
        self.assertTrue(result['success'], result)
        client.close_data_retrieval.assert_called_once_with(NS, REL, 'paas-dl-0123456789ab')
        session = self.Session.search([('helm_release_name', '=', REL)])
        self.assertEqual((session.state, session.close_reason), ('closed', 'manual'))
        self.assertIsNone(result['data']['session'])
        # 關了之後可以再開一個新的。
        reopened, client2 = self._call(self.admin, service, 'open')
        self.assertTrue(reopened['success'], reopened)
        client2.open_data_retrieval.assert_called_once()

    def test_close_without_session_is_idempotent(self):
        result, client = self._call(self.admin, self._service(), 'close')
        self.assertTrue(result['success'], result)
        client.close_data_retrieval.assert_not_called()

    def test_close_operator_down_reports_error_and_keeps_session(self):
        service = self._service()
        self._call(self.admin, service, 'open')
        client = _client()
        client.close_data_retrieval.side_effect = PaaSOperatorConnectionError('down')
        result, _c = self._call(self.admin, service, 'close', client)
        self.assertFalse(result['success'])
        self.assertIn('Could not close', result['error'])
        self.assertEqual(self.Session.search([('helm_release_name', '=', REL)]).state, 'active')

    def test_route_cleanup_failure_marks_closing_then_cron_finishes(self):
        service = self._service()
        self._call(self.admin, service, 'open')
        client = _client()
        client.close_data_retrieval.side_effect = _api_error(502, 'route_cleanup_failed')
        result, _c = self._call(self.admin, service, 'close', client)
        self.assertTrue(result['success'], result)
        session = self.Session.search([('helm_release_name', '=', REL)])
        self.assertEqual(session.state, 'closing')
        self.assertEqual(result['data']['session']['state'], 'closing')
        cron_client = _client()
        with patch(_CLIENT_SEAM, return_value=cron_client):
            self.Session._cron_close_expired()
        self.assertEqual((session.state, session.close_reason), ('closed', 'manual'))
        cron_client.close_data_retrieval.assert_called_once_with(
            NS, REL, 'paas-dl-0123456789ab')

    def test_status_reports_live_readiness(self):
        service = self._service()
        self._call(self.admin, service, 'open')
        client = _client()
        client.get_data_retrieval.return_value = {'status': 'starting'}
        result, _c = self._call(self.admin, service, 'status', client)
        self.assertEqual(result['data']['session']['live_status'], 'starting')
        client.get_data_retrieval.assert_called_once_with(NS, REL)

    def test_status_not_found_on_cluster_closes_the_session(self):
        service = self._service()
        self._call(self.admin, service, 'open')
        client = _client()
        client.get_data_retrieval.return_value = {'status': 'not_found'}
        result, _c = self._call(self.admin, service, 'status', client)
        self.assertTrue(result['success'], result)
        self.assertIsNone(result['data']['session'])
        session = self.Session.search([('helm_release_name', '=', REL)])
        self.assertEqual(session.state, 'closed')

    def test_one_open_session_per_release_in_the_database(self):
        self.Session.create({
            'workspace_id': self.workspace.id, 'helm_namespace': NS,
            'helm_release_name': REL, 'profile': 'nextcloud', 'state': 'active'})
        with self.assertRaises(IntegrityError), mute_logger('odoo.sql_db'), \
                self.env.cr.savepoint():
            self.Session.create({
                'workspace_id': self.workspace.id, 'helm_namespace': NS,
                'helm_release_name': REL, 'profile': 'nextcloud', 'state': 'closing'})
        # closed 的稽核紀錄不受限。
        self.Session.create({
            'workspace_id': self.workspace.id, 'helm_namespace': NS,
            'helm_release_name': REL, 'profile': 'nextcloud', 'state': 'closed'})

    def test_does_not_go_through_the_subscription_gate(self):
        """停權超過寬限期時訂閱閘連 read 都擋；取回入口是給這種服務用的，不能被它擋下。"""
        service = self._service()
        with patch.object(type(service), '_check_operation_allowed',
                          side_effect=AssertionError('subscription gate must not run')):
            result, _c = self._call(self.admin, service, 'open')
        self.assertTrue(result['success'], result)


# ---------------------------------------------------------------------------
# 復權
# ---------------------------------------------------------------------------


@tagged('post_install', '-at_install')
class TestDataRetrievalResume1540(_Fixture):

    def _open_suspended(self):
        service = self._service(pre_suspend_state='running')
        self._call(self.admin, service, 'open')
        return service

    def test_resume_closes_the_entrance_before_scaling_up(self):
        service = self._open_suspended()
        client = _client()
        client.unpark_release_route.return_value = {'status': 'unparked'}
        with patch(_CLIENT_SEAM, return_value=client):
            service._do_resume()
        calls = [c for c in client.mock_calls
                 if c[0] in ('close_data_retrieval', 'scale_release')]
        self.assertEqual(calls, [
            call.close_data_retrieval(NS, REL, 'paas-dl-0123456789ab'),
            call.scale_release(REL, NS, replicas=1),
        ])
        session = self.Session.search([('helm_release_name', '=', REL)])
        self.assertEqual((session.state, session.close_reason), ('closed', 'resumed'))
        self.assertEqual(service.state, 'running')

    def test_close_failure_does_not_block_resume(self):
        service = self._open_suspended()
        client = _client()
        client.close_data_retrieval.side_effect = PaaSOperatorConnectionError('down')
        client.unpark_release_route.return_value = {'status': 'unparked'}
        with patch(_CLIENT_SEAM, return_value=client):
            service._do_resume()
        client.scale_release.assert_called_once_with(REL, NS, replicas=1)
        self.assertEqual(service.state, 'running')

    def test_resume_without_entrance_does_not_call_close(self):
        service = self._service(pre_suspend_state='running')
        client = _client()
        client.unpark_release_route.return_value = {'status': 'unparked'}
        with patch(_CLIENT_SEAM, return_value=client):
            service._do_resume()
        client.close_data_retrieval.assert_not_called()


# ---------------------------------------------------------------------------
# 刪除後：帳本
# ---------------------------------------------------------------------------


@tagged('post_install', '-at_install')
class TestRetainedServiceData1540(_Fixture):

    def _delete(self, service):
        with patch(_CLIENT_SEAM, return_value=_client()):
            service.unlink()

    def test_unlink_records_the_retained_disk(self):
        service = self._service(state='running')
        before = fields.Datetime.now()
        self._delete(service)
        row = self.Retained.search([('helm_release_name', '=', REL)])
        self.assertEqual(len(row), 1)
        self.assertEqual((row.name, row.workspace_id, row.template_id, row.helm_namespace,
                          row.data_retrieval_profile),
                         ('files-1540', self.workspace, self.template, NS, 'nextcloud'))
        delta = row.retained_until - before
        self.assertTrue(timedelta(days=DEFAULT_DATA_RETENTION_DAYS - 1) < delta
                        <= timedelta(days=DEFAULT_DATA_RETENTION_DAYS, minutes=5), delta)

    def test_retention_days_parameter(self):
        self.env['ir.config_parameter'].sudo().set_param(DATA_RETENTION_DAYS_PARAM, '7')
        before = fields.Datetime.now()
        self._delete(self._service())
        row = self.Retained.search([('helm_release_name', '=', REL)])
        self.assertLess(row.retained_until - before, timedelta(days=7, minutes=5))

    def test_no_record_for_never_running_or_unsupported(self):
        self._delete(self._service(first_running_at=False))
        self._delete(self._service(template=self.plain_template,
                                   helm_release_name='svc-1540-plain'))
        self.assertFalse(self.Retained.search([
            ('helm_release_name', 'in', (REL, 'svc-1540-plain'))]))

    def test_open_entrance_moves_to_the_ledger_row(self):
        service = self._service()
        self._call(self.admin, service, 'open')
        self._delete(service)
        row = self.Retained.search([('helm_release_name', '=', REL)])
        session = self.Session.search([('helm_release_name', '=', REL)])
        self.assertEqual(session.state, 'active')
        self.assertEqual(session.retained_data_id, row)
        self.assertFalse(session.service_id)

    def _retained(self, **extra):
        vals = {
            'name': 'deleted-1540', 'workspace_id': self.workspace.id,
            'template_id': self.template.id, 'helm_namespace': NS,
            'helm_release_name': 'svc-1540-del', 'data_retrieval_profile': 'nextcloud',
            'retained_until': fields.Datetime.now() + timedelta(days=30),
        }
        vals.update(extra)
        return self.Retained.create(vals)

    def test_list_only_for_managers_and_only_retained_rows(self):
        row = self._retained()
        self._retained(helm_release_name='svc-1540-gone',
                       retained_until=fields.Datetime.now() - timedelta(hours=1))
        with self._as(self.admin):
            listed = self.controller.api_workspace_retained_data(self.workspace.id)
        self.assertTrue(listed['success'], listed)
        self.assertEqual([r['id'] for r in listed['data']], [row.id])
        self.assertEqual(listed['data'][0]['data_retrieval'],
                         {'supported': True, 'available': True, 'session': None})
        for member in (self.user, self.guest):
            with self.subTest(login=member.login), self._as(member):
                self.assertEqual(
                    self.controller.api_workspace_retained_data(self.workspace.id),
                    {'success': False, 'error': PERMISSION_DENIED})

    def test_open_and_close_retained(self):
        row = self._retained()
        client = _client()
        with patch(_CLIENT_SEAM, return_value=client), self._as(self.admin):
            opened = self.controller.api_retained_data_retrieval(
                self.workspace.id, row.id, action='open')
            closed = self.controller.api_retained_data_retrieval(
                self.workspace.id, row.id, action='close')
        self.assertTrue(opened['success'], opened)
        self.assertEqual(opened['data']['credentials']['password'], PASSWORD)
        client.open_data_retrieval.assert_called_once_with(
            NS, 'svc-1540-del', 'nextcloud', RETRIEVAL_TTL_HOURS)
        self.assertTrue(closed['success'], closed)
        client.close_data_retrieval.assert_called_once()

    def test_expired_or_foreign_rows_are_refused(self):
        expired = self._retained(retained_until=fields.Datetime.now() - timedelta(minutes=1))
        other_ws = self.env['woow_paas_platform.workspace'].create({'name': '1540 other'})
        foreign = self._retained(workspace_id=other_ws.id, helm_release_name='svc-1540-f')
        client = _client()
        with patch(_CLIENT_SEAM, return_value=client), self._as(self.admin):
            late = self.controller.api_retained_data_retrieval(
                self.workspace.id, expired.id, action='open')
            stranger = self.controller.api_retained_data_retrieval(
                self.workspace.id, foreign.id, action='open')
        self.assertFalse(late['success'])
        self.assertIn('retention period', late['error'])
        self.assertEqual(stranger, {'success': False, 'error': 'Retained data not found'})
        client.open_data_retrieval.assert_not_called()


# ---------------------------------------------------------------------------
# cron
# ---------------------------------------------------------------------------


@tagged('post_install', '-at_install')
class TestDataRetrievalCron1540(_Fixture):

    def test_closes_expired_sweeps_and_purges(self):
        now = fields.Datetime.now()
        expired = self.Session.create({
            'workspace_id': self.workspace.id, 'helm_namespace': NS,
            'helm_release_name': REL, 'profile': 'nextcloud', 'state': 'active',
            'hostname': HOST, 'expires_at': now - timedelta(minutes=1)})
        fresh = self.Session.create({
            'workspace_id': self.workspace.id, 'helm_namespace': NS,
            'helm_release_name': 'svc-1540-fresh', 'profile': 'nextcloud', 'state': 'active',
            'expires_at': now + timedelta(hours=2)})
        stale_row = self.Retained.create({
            'name': 'old', 'workspace_id': self.workspace.id, 'helm_namespace': NS,
            'helm_release_name': 'svc-1540-old', 'data_retrieval_profile': 'nextcloud',
            'retained_until': now - timedelta(days=3)})
        busy_row = self.Retained.create({
            'name': 'busy', 'workspace_id': self.workspace.id, 'helm_namespace': NS,
            'helm_release_name': 'svc-1540-busy', 'data_retrieval_profile': 'nextcloud',
            'retained_until': now - timedelta(days=3)})
        self.Session.create({
            'workspace_id': self.workspace.id, 'helm_namespace': NS,
            'helm_release_name': 'svc-1540-busy', 'profile': 'nextcloud', 'state': 'closing',
            'retained_data_id': busy_row.id})
        client = _client()

        def _close(namespace, release, subdomain=None):
            if release != REL:
                raise PaaSOperatorConnectionError('down')
            return {'status': 'closed'}

        client.close_data_retrieval.side_effect = _close
        with patch(_CLIENT_SEAM, return_value=client):
            self.Session._cron_close_expired()
        self.assertEqual((expired.state, expired.close_reason), ('closed', 'expired'))
        self.assertEqual(fresh.state, 'active')
        client.sweep_data_retrievals.assert_called_once_with()
        self.assertFalse(stale_row.exists())
        self.assertTrue(busy_row.exists())

    def test_old_operator_404_on_sweep_is_quiet(self):
        client = _client()
        client.sweep_data_retrievals.side_effect = PaaSOperatorAPIError(
            'Not Found', status_code=404, detail='Not Found')
        with patch(_CLIENT_SEAM, return_value=client):
            self.Session._cron_close_expired()

    def test_cron_record_is_wired(self):
        cron = self.env.ref('woow_paas_platform.ir_cron_data_retrieval_maintenance')
        self.assertEqual(cron.model_id.model, 'woow_paas_platform.data_retrieval')
        self.assertIn('_cron_close_expired', cron.code)
        self.assertTrue(cron.active)


@tagged('post_install', '-at_install')
class TestDataRetrievalConfigParameter(TransactionCase):
    """``data_retention_days`` 是「模組常數＋get_param()」寫法，repo-lint 掃不到 ⇒ 這裡守。"""

    def test_registered_for_uninstall(self):
        from ..hooks import _CONFIG_PARAMETERS
        self.assertIn(DATA_RETENTION_DAYS_PARAM, _CONFIG_PARAMETERS)


# ---------------------------------------------------------------------------
# 前端契約（靜態讀檔）
# ---------------------------------------------------------------------------


def _read(*parts):
    with open(os.path.join(_PAAS, *parts), encoding='utf-8') as handle:
        return handle.read()


@tagged('post_install', '-at_install')
class TestDataRetrievalFrontend1540(TransactionCase):

    def test_button_gate_reads_the_payload(self):
        js = _read('pages', 'service', 'ServiceDetailPage.js')
        xml = _read('pages', 'service', 'ServiceDetailPage.xml')
        self.assertIn('this.service?.data_retrieval', js)
        self.assertIn('this.canManageService', js)
        self.assertIn('t-if="canRetrieveData"', xml)
        self.assertNotRegex(js, r"slug\s*===\s*['\"]nextcloud['\"]")

    def test_modal_strings_have_zh_tw(self):
        js = _read('components', 'modal', 'DataRetrievalModal.js')
        strings = set(re.findall(r'_t\("((?:[^"\\]|\\.)*)"', js))
        for page in (('pages', 'service', 'ServiceDetailPage.js'),
                     ('pages', 'workspace', 'WorkspaceDetailPage.js')):
            page_js = _read(*page)
            strings |= {s for s in re.findall(r'_t\("((?:[^"\\]|\\.)*)"', page_js)
                        if 'retain' in s.lower() or 'download link' in s.lower()
                        or 'suspended. You can open' in s}
        self.assertGreater(len(strings), 15)
        po_path = os.path.join(_SRC, 'i18n', 'zh_TW.po')
        translated = {entry['src']: entry['value'] for entry in PoFileReader(po_path)
                      if entry.get('type') == 'code'}
        missing = sorted(s for s in strings if not translated.get(s))
        self.assertEqual(missing, [], 'DataRetrieval strings without zh_TW: %s' % missing)
