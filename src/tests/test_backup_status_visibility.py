# -*- coding: utf-8 -*-
"""商用驗收 X-BK-006-1／DATA-ODOO-005-2／AI-LLM-009-1（#1190／#1265）：備份狀態要讓平台與租戶看得到。

2026-10-16 實測：

* X-BK-006-1：woow-odoo 備份卷滿時 Job 3 次重試後 BackoffLimitExceeded，唯一訊號是叢集通用
  KubeJobFailed 寄 ops 信箱（約 20 分延遲、與約 28 筆舊告警混在一起）；平台 readiness 刻意
  豁免備份 Job（#1251 Q6，正確），但平台也**沒有任何其他地方**記錄備份結果，客戶看到的
  服務永遠 running。
* DATA-ODOO-005-2：/woow 服務頁三個分頁沒有任何備份時間或狀態；service get 41 個欄位沒有
  backup 相關欄位。
* AI-LLM-009-1：LiteLLM 備份空間不足只在 db-agent sidecar log 印 WARNING，pod 重啟即消失。

守的事：

* ``backup_health``（純函式）：ok／failed／stale／unknown 的邊界（27h）、只前進不後退、
  讀取失敗保留舊值、不支援就是 unknown、原因代碼只收安全字元；
* ``_apply_backup_observation``：轉 failed 時寫事實並通知系統管理員**一次**；內容沒變零寫入
  （write 次數 0、write_date 不動）；恢復清掉告警標記；🔴 全程 state 仍是 running；
  非系統使用者寫不進這些欄位；
* ``_cron_refresh_backup_status``：只問有備份機制（woow-odoo／litellm）的 running 服務、
  舊 operator（404）第一台之後就中止、cron 有接線；
* /woow payload（service get）帶唯讀 ``backup``，沒有備份機制的範本 ``supported: false``、
  停權中 ``paused``；
* 通知中心：同工作區成員看得到 failed（error）／stale（warning），停權服務不列；
* 新字串的 zh_TW 譯文。
"""
import os
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests.common import BaseCase, HttpCase, TransactionCase, new_test_user, tagged
from odoo.tools.translate import PoFileReader

from ..models.cloud_service import _OBSERVATION_CONTROL_FIELDS, _internal_provision
from ..services import backup_health as bh
from ..services.paas_operator import PaaSOperatorAPIError
from .operator_contract import operator_client

_MODEL_MODULE = 'odoo.addons.woow_paas_platform.models.cloud_service'
_CTRL_MODULE = 'odoo.addons.woow_paas_platform.controllers.paas'
_CLIENT = 'odoo.addons.woow_paas_platform.services.paas_operator.get_paas_operator_client'
_SERVICE = 'woow_paas_platform.cloud_service'
_MONITOR = 'woow_paas_platform.platform_monitor'
_REWOUND = datetime(2026, 1, 1, 0, 0, 0)
_NOW = datetime(2026, 10, 17, 12, 0, 0)

_BACKUP_FIELDS = (
    'backup_health', 'last_backup_success_at', 'last_backup_failure_at',
    'backup_failure_reason', 'backup_consecutive_failures', 'backup_failure_alerted_at',
)


def _payload(**overrides):
    """operator backup-status 的回應形狀（extra/paas-operator/src/services/backup_status.py）。"""
    base = {
        'supported': True, 'source': 'odoo-cronjob', 'enabled': True, 'schedule': '8 2 * * *',
        'last_schedule_at': '2026-10-16T18:08:00Z', 'running': False, 'last_status': 'succeeded',
        'last_success_at': '2026-10-16T18:08:05Z', 'last_success_source': 'index',
        'last_failure_at': None, 'last_failure_reason': None, 'last_failure_message': None,
        'consecutive_failures': 0, 'status_recorded': True, 'recent': [],
        'observed_at': '2026-10-17T03:00:00Z',
    }
    base.update(overrides)
    return base


def _failed_payload(**overrides):
    return _payload(**{
        'last_status': 'failed', 'last_success_at': '2026-10-15T18:08:03Z',
        'last_failure_at': '2026-10-16T18:09:40Z', 'last_failure_reason': 'no_space',
        'consecutive_failures': 1, **overrides})


def _dt(text):
    return datetime.strptime(text, '%Y-%m-%dT%H:%M:%SZ')


@tagged('post_install', '-at_install')
class TestBackupHealthRules(BaseCase):
    """純函式：不碰 DB。"""

    def _facts(self, current=None, payload=None, deployed=None, now=_NOW):
        current = current or {}
        return bh.next_backup_facts(current, payload, deployed, now)

    def test_ok_stale_boundary_is_27_hours(self):
        success = _NOW - timedelta(hours=26, minutes=59)
        self.assertEqual(bh.compute_health(True, True, success, None, None, _NOW), 'ok')
        success = _NOW - timedelta(hours=27, minutes=1)
        self.assertEqual(bh.compute_health(True, True, success, None, None, _NOW), 'stale')

    def test_never_succeeded_is_unknown_until_27h_after_deploy(self):
        self.assertEqual(bh.compute_health(True, True, None, None, _NOW - timedelta(hours=26), _NOW),
                         'unknown')
        self.assertEqual(bh.compute_health(True, True, None, None, _NOW - timedelta(hours=28), _NOW),
                         'stale')

    def test_failure_newer_than_success_is_failed_older_is_not(self):
        success = _NOW - timedelta(hours=10)
        self.assertEqual(bh.compute_health(True, True, success, success + timedelta(minutes=1),
                                           None, _NOW), 'failed')
        self.assertEqual(bh.compute_health(True, True, success, success - timedelta(hours=1),
                                           None, _NOW), 'ok')
        # 從沒成功過、剛部署就失敗：仍是 failed（不等 27h）
        self.assertEqual(bh.compute_health(True, True, None, _NOW, _NOW, _NOW), 'failed')

    def test_unsupported_or_disabled_is_unknown(self):
        self.assertEqual(bh.compute_health(False, True, _NOW, None, None, _NOW), 'unknown')
        self.assertEqual(bh.compute_health(True, False, _NOW, _NOW, None, _NOW), 'unknown')

    def test_failed_payload_sets_facts(self):
        facts = self._facts(payload=_failed_payload())
        self.assertEqual(facts['backup_health'], 'failed')
        self.assertEqual(facts['last_backup_success_at'], _dt('2026-10-15T18:08:03Z'))
        self.assertEqual(facts['last_backup_failure_at'], _dt('2026-10-16T18:09:40Z'))
        self.assertEqual(facts['backup_failure_reason'], 'no_space')
        self.assertEqual(facts['backup_consecutive_failures'], 1)

    def test_success_only_moves_forward(self):
        newer = _dt('2026-10-17T02:00:00Z')
        facts = self._facts(current={'last_backup_success_at': newer},
                            payload=_payload(last_success_at='2026-10-16T18:08:05Z'))
        self.assertEqual(facts['last_backup_success_at'], newer)

    def test_read_failure_or_unrecorded_keeps_facts(self):
        current = {'last_backup_success_at': _dt('2026-10-16T18:08:05Z'),
                   'last_backup_failure_at': _dt('2026-10-17T02:00:00Z'),
                   'backup_failure_reason': 'no_space', 'backup_consecutive_failures': 2}
        for payload in (None, _payload(status_recorded=False, last_success_at=None)):
            facts = self._facts(current=current, payload=payload)
            self.assertEqual(facts['last_backup_failure_at'], current['last_backup_failure_at'])
            self.assertEqual(facts['backup_failure_reason'], 'no_space')
            self.assertEqual(facts['backup_consecutive_failures'], 2)
            self.assertEqual(facts['backup_health'], 'failed')

    def test_failure_records_gc_does_not_clear_failure_facts(self):
        """失敗 Job 3 天後被回收：operator 不再回報那次失敗，事實不能跟著消失。"""
        current = {'last_backup_failure_at': _dt('2026-10-16T18:09:40Z'),
                   'backup_failure_reason': 'no_space'}
        facts = self._facts(current=current, payload=_payload(last_success_at='2026-10-15T18:00:00Z'))
        self.assertEqual(facts['backup_failure_reason'], 'no_space')
        self.assertEqual(facts['backup_health'], 'failed')

    def test_reason_code_is_sanitized(self):
        facts = self._facts(payload=_failed_payload(last_failure_reason='<b>disk</b> full'))
        self.assertEqual(facts['backup_failure_reason'], 'unknown')
        facts = self._facts(payload=_failed_payload(last_failure_reason=None))
        self.assertEqual(facts['backup_failure_reason'], 'unknown')


class _BackupCase(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Service = cls.env[_SERVICE]
        cls.Monitor = cls.env[_MONITOR]
        Template = cls.env['woow_paas_platform.cloud_app_template']
        common = {'helm_repo_url': '', 'helm_chart_version': '0.2.14', 'workload_type': 'container',
                  'post_deploy_init_type': 'none', 'mcp_enabled': False}
        cls.odoo_template = Template.create({
            'name': 'Backup Odoo XBK', 'slug': 'backup-odoo-xbk',
            'helm_chart_name': 'oci://jcr-prod.woowtech.io/woow-paas-helm-local/woow-odoo', **common})
        cls.litellm_template = Template.create({
            'name': 'Backup LiteLLM XBK', 'slug': 'backup-litellm-xbk',
            'helm_chart_name': 'woow-paas/litellm', **common})
        cls.other_template = Template.create({
            'name': 'Backup Other XBK', 'slug': 'backup-other-xbk',
            'helm_chart_name': 'oci://jcr-prod.woowtech.io/woow-paas-helm-local/n8n', **common})
        cls.owner = cls.env['res.users'].create({
            'name': 'Owner XBK', 'login': 'owner-xbk@tenant-xbk.tw',
            'groups_id': [(6, 0, [cls.env.ref('base.group_user').id])],
        })
        cls.workspace = cls.env['woow_paas_platform.workspace'].create({
            'name': 'WS XBK', 'owner_id': cls.owner.id})

    def setUp(self):
        super().setUp()
        self.singleton = self.Monitor._singleton()
        self._activities().unlink()

    def _activities(self):
        return self.env['mail.activity'].sudo().search([
            ('res_model', '=', _MONITOR), ('res_id', '=', self.singleton.id),
            ('summary', '=', self.Monitor.SUMMARY_BACKUP_FAILED)])

    def _service(self, tag, template=None, state='running', **extra):
        vals = {
            'name': 'svc-xbk-%s' % tag, 'workspace_id': self.workspace.id,
            'template_id': (template or self.odoo_template).id, 'state': state,
            'helm_namespace': 'paas-ws-test-xbk', 'helm_release_name': 'svc-xbk-%s' % tag,
            'deployed_at': _NOW - timedelta(days=3),
        }
        vals.update(extra)
        return _internal_provision(self.Service).create(vals)

    @contextmanager
    def _service_writes(self):
        cls = type(self.Service)
        original = cls.write
        calls = []

        def _spy(records, vals):
            calls.append((tuple(records.ids), sorted(vals)))
            return original(records, vals)

        with patch.object(cls, 'write', _spy):
            yield calls

    def _rewind_write_date(self, services):
        services.flush_recordset()
        self.env.cr.execute(
            'UPDATE %s SET write_date = %%s WHERE id IN %%s' % self.Service._table,
            (_REWOUND, tuple(services.ids)))
        services.invalidate_recordset(['write_date'])

    def _write_date(self, service):
        self.Service.flush_model()
        self.env.cr.execute('SELECT write_date FROM %s WHERE id = %%s' % self.Service._table,
                            (service.id,))
        return self.env.cr.fetchone()[0]


@tagged('post_install', '-at_install')
class TestBackupObservation(_BackupCase):

    def test_failure_is_recorded_alerted_once_and_state_untouched(self):
        svc = self._service('fail')
        outcome = svc._apply_backup_observation(_failed_payload(), now=_NOW)
        self.assertEqual(outcome, 'raised')
        self.assertEqual(svc.backup_health, 'failed')
        self.assertEqual(svc.backup_failure_reason, 'no_space')
        self.assertEqual(svc.last_backup_success_at, _dt('2026-10-15T18:08:03Z'))
        self.assertTrue(svc.backup_failure_alerted_at)
        self.assertEqual(len(self._activities()), 1, '系統管理員收到一張告警')
        self.assertEqual(svc.state, 'running')
        self.assertFalse(svc.error_message)

        # 同一段失敗再看一次：零寫入、不重複通知
        self._rewind_write_date(svc)
        bodies_before = len(self.singleton.message_ids)
        with self._service_writes() as calls:
            outcome = svc._apply_backup_observation(_failed_payload(), now=_NOW)
        self.assertEqual(outcome, 'unchanged')
        self.assertEqual(calls, [])
        self.assertEqual(self._write_date(svc), _REWOUND)
        self.singleton.invalidate_recordset(['message_ids'])
        self.assertEqual(len(self.singleton.message_ids), bodies_before)

    def test_success_after_failure_clears_health_and_alert_flag(self):
        svc = self._service('recover')
        svc._apply_backup_observation(_failed_payload(), now=_NOW)
        outcome = svc._apply_backup_observation(
            _payload(last_success_at='2026-10-17T02:08:05Z'), now=_NOW)
        self.assertEqual(outcome, 'cleared')
        self.assertEqual(svc.backup_health, 'ok')
        self.assertFalse(svc.backup_failure_alerted_at)
        # 失敗事實保留（歷史），判定靠時間先後
        self.assertEqual(svc.backup_failure_reason, 'no_space')

    def test_healthy_steady_state_writes_nothing(self):
        svc = self._service('steady')
        svc._apply_backup_observation(_payload(), now=_NOW)
        self.assertEqual(svc.backup_health, 'ok')
        self._rewind_write_date(svc)
        with self._service_writes() as calls:
            self.assertEqual(svc._apply_backup_observation(_payload(), now=_NOW), 'unchanged')
        self.assertEqual(calls, [])
        self.assertEqual(self._write_date(svc), _REWOUND)

    def test_stale_by_time_without_new_operator_facts(self):
        svc = self._service('stale')
        svc._apply_backup_observation(_payload(), now=_NOW)
        later = _dt('2026-10-16T18:08:05Z') + timedelta(hours=28)
        self.assertEqual(svc._apply_backup_observation(None, now=later), 'stale')
        self.assertEqual(svc.backup_health, 'stale')
        self.assertEqual(svc.state, 'running')

    def test_litellm_no_space(self):
        svc = self._service('llm', template=self.litellm_template)
        svc._apply_backup_observation(_payload(
            source='litellm-db-agent', last_status='failed', last_success_at='2026-10-15T19:00:05Z',
            last_failure_at='2026-10-17T03:00:01Z', last_failure_reason='no_space',
            consecutive_failures=1), now=_NOW)
        self.assertEqual(svc.backup_health, 'failed')
        self.assertEqual(svc.backup_failure_reason, 'no_space')

    def test_tenant_cannot_write_backup_fields(self):
        svc = self._service('guard')
        self.assertTrue(set(_BACKUP_FIELDS) <= _OBSERVATION_CONTROL_FIELDS)
        for name, value in (('backup_health', 'ok'), ('backup_failure_reason', 'x'),
                            ('last_backup_success_at', fields.Datetime.now())):
            with self.subTest(field=name), self.assertRaises(AccessError):
                svc.with_user(self.owner).write({name: value})


@tagged('post_install', '-at_install')
class TestBackupCron(_BackupCase):

    def setUp(self):
        super().setUp()
        self.Service.search([]).unlink()
        self.singleton.write({'backup_check_cursor': 0})

    def _run(self, client):
        with patch(_CLIENT, return_value=client), \
                patch.object(self.env.cr, 'commit', lambda: None), \
                patch.object(self.env.cr, 'rollback', lambda: None):
            self.Service._cron_refresh_backup_status()
        self.Service.invalidate_model()
        return [c.kwargs.get('release_name') for c in client.get_backup_status.call_args_list]

    def test_cron_record_is_wired(self):
        cron = self.env.ref('woow_paas_platform.ir_cron_refresh_backup_status')
        self.assertTrue(cron.active)
        self.assertEqual(cron.code.strip(), 'model._cron_refresh_backup_status()')
        self.assertEqual((cron.interval_number, cron.interval_type), (1, 'hours'))

    def test_only_running_backup_capable_services_are_checked(self):
        odoo = self._service('odoo')
        llm = self._service('llm', template=self.litellm_template)
        self._service('n8n', template=self.other_template)
        self._service('susp', state='suspended')
        polled = self._run(operator_client(get_backup_status=_failed_payload()))
        self.assertEqual(sorted(polled), sorted([odoo.helm_release_name, llm.helm_release_name]))
        self.assertEqual(odoo.backup_health, 'failed')
        self.assertEqual(odoo.state, 'running')
        self.singleton.invalidate_recordset(['backup_check_cursor'])
        self.assertEqual(self.singleton.backup_check_cursor, max(odoo.id, llm.id))

    def test_old_operator_404_stops_after_first_service(self):
        self._service('a')
        self._service('b')
        client = operator_client()
        client.get_backup_status.side_effect = PaaSOperatorAPIError('Not Found', status_code=404)
        self.assertEqual(len(self._run(client)), 1)


@tagged('post_install', '-at_install')
class TestBackupPayloadAndNotifications(_BackupCase):

    def _format(self, service):
        from odoo.addons.woow_paas_platform.controllers.paas import PaasController
        with patch('%s.request' % _CTRL_MODULE, new=SimpleNamespace(env=self.env)):
            return PaasController()._format_service(service, include_details=True)

    def test_service_detail_carries_read_only_backup(self):
        svc = self._service('payload')
        svc._apply_backup_observation(_failed_payload(), now=_NOW)
        backup = self._format(svc)['backup']
        self.assertEqual(backup['supported'], True)
        self.assertEqual(backup['health'], 'failed')
        self.assertEqual(backup['failure_reason'], 'no_space')
        self.assertTrue(backup['last_success_at'].startswith('2026-10-15T18:08:03'))
        self.assertFalse(backup['paused'])
        self.assertEqual(backup['stale_hours'], 27)

    def test_template_without_backups_is_not_supported(self):
        self.assertEqual(self._format(self._service('nobk', template=self.other_template))['backup'],
                         {'supported': False})

    def test_suspended_service_is_paused(self):
        self.assertTrue(self._format(self._service('paused', state='suspended'))['backup']['paused'])

    def test_notification_center_lists_failed_and_stale_for_members(self):
        from odoo.addons.woow_paas_platform.controllers.notifications import NotificationsController
        # 建工作區時 owner 已自動取得 access；沒有才補（fixture 前提：owner 是成員）。
        access = self.env['woow_paas_platform.workspace_access'].sudo()
        if not access.search([('workspace_id', '=', self.workspace.id),
                              ('user_id', '=', self.owner.id)]):
            access.create({'workspace_id': self.workspace.id, 'user_id': self.owner.id,
                           'role': 'owner'})
        failed = self._service('nfail')
        failed._apply_backup_observation(_failed_payload(), now=_NOW)
        stale = self._service('nstale')
        stale._apply_backup_observation(_payload(last_success_at='2026-10-10T00:00:00Z'))
        suspended = self._service('nsusp')
        suspended._apply_backup_observation(_failed_payload(), now=_NOW)
        suspended.write({'state': 'suspended'})

        items = NotificationsController()._backup_items(self.env, self.owner)
        by_route = {item['route']: item for item in items}
        route = 'workspace/%s/services/%%s' % self.workspace.id
        self.assertEqual(by_route[route % failed.id]['severity'], 'error')
        self.assertEqual(by_route[route % failed.id]['type'], 'backup_failed')
        self.assertEqual(by_route[route % stale.id]['severity'], 'warning')
        self.assertNotIn(route % suspended.id, by_route)


@tagged('post_install', '-at_install')
class TestBackupNotificationsHttp(HttpCase):
    """controller 的 sudo() 讀取先以「使用者可存取的工作區」過濾：跨使用者看不到別人的備份告警。"""

    def _notifications(self, login):
        self.authenticate(login, login)
        body = self.url_open(
            '/api/notifications', data='{"jsonrpc":"2.0","method":"call","params":{}}',
            headers={'Content-Type': 'application/json'}).json()
        result = body.get('result')
        self.assertTrue(result and result.get('success'), body)
        return [item for item in result['data']['items'] if item['type'] == 'backup_failed']

    def test_backup_alert_only_reaches_members_of_that_workspace(self):
        member = new_test_user(self.env, login='xbk_member', password='xbk_member',
                               groups='base.group_portal')
        new_test_user(self.env, login='xbk_outsider', password='xbk_outsider',
                      groups='base.group_portal')
        template = self.env['woow_paas_platform.cloud_app_template'].sudo().create({
            'name': 'Backup Http XBK', 'slug': 'backup-http-xbk', 'helm_repo_url': '',
            'helm_chart_name': 'oci://jcr-prod.woowtech.io/woow-paas-helm-local/woow-odoo',
            'helm_chart_version': '0.2.14'})
        workspace = self.env['woow_paas_platform.workspace'].sudo().create({
            'name': 'WS Http XBK', 'owner_id': member.id})
        access = self.env['woow_paas_platform.workspace_access'].sudo()
        if not access.search([('workspace_id', '=', workspace.id), ('user_id', '=', member.id)]):
            access.create({'workspace_id': workspace.id, 'user_id': member.id, 'role': 'owner'})
        service = _internal_provision(self.env[_SERVICE].sudo()).create({
            'name': 'svc-xbk-http', 'workspace_id': workspace.id, 'template_id': template.id,
            'state': 'running', 'helm_namespace': 'paas-ws-test-xbk',
            'helm_release_name': 'svc-xbk-http'})
        service._apply_backup_observation(_failed_payload(), now=_NOW)

        member_items = self._notifications('xbk_member')
        self.assertEqual([i['route'] for i in member_items],
                         ['workspace/%s/services/%s' % (workspace.id, service.id)])
        self.assertEqual(self._notifications('xbk_outsider'), [])


@tagged('post_install', '-at_install')
class TestBackupTranslations(TransactionCase):

    def test_new_tenant_strings_have_zh_tw(self):
        po = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          'i18n', 'zh_TW.po')
        values = {e['src']: e['value'] for e in PoFileReader(po)}
        for msgid in (
            'Backups', 'Last successful backup', 'Last failed backup', 'Not checked yet',
            'The backup storage is full.', 'Backup failed', 'No recent backup',
            'No backups are taken while the service is suspended.',
            'The latest backup of %(name)s in workspace %(workspace)s failed. '
            'We have been notified; your service keeps running.',
            'Backup Health', 'Healthy', 'Failed', 'Overdue',
        ):
            with self.subTest(msgid=msgid):
                self.assertTrue(values.get(msgid), 'zh_TW.po 缺譯文')
