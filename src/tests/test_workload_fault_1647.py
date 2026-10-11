# -*- coding: utf-8 -*-
"""#1647：服務顯示 running，但某個 workload 期望有 Pod、實際一顆都沒有。

2026-10-10 正式站 svc 201（omnigent）：server 與同 namespace 內 PG 的 CPU limit 被調成合計
16，namespace 的 ``limits.cpu`` 上限是 12。Recreate Deployment 的舊 Pod 先被砍、新 Pod 被 quota
拒絕（ReplicaSet ``FailedCreate: exceeded quota``），0 顆 Pod 近 6 小時。平台紀錄一直是
``running``、沒有錯誤訊息，/woow 與後台都沒有任何訊號。

守的事：

* ``workload_verdict``（純函式）：只認「期望 ≥1、實際 0」；舊 operator（回應沒有
  ``workloads_included``）判不支援——缺席不是訊號；有 ``FailedCreate`` 證據分 quota／其他，
  沒有證據是 ``no_pods``；
* ``_apply_workload_observation``：有證據的第一次觀測就寫欄位並通知系統管理員（每段異常
  一次、同 summary 未處理前一台一行），沒有證據要持續 5 分鐘；恢復後清空並追加「已恢復」；
  不支援／查詢失敗一格都不動；
* 🔴 全程 ``state`` 仍是 ``running``、``error_message`` 不變（repo-lint 第 19 條判準 5 另有
  形態層守衛）；
* 服務離開 running 時四格由 ``write()`` 集中清掉，非系統使用者寫不進這五格、也不會因為
  改 state 而 AccessError；
* ``_cron_check_running_workloads``：最久沒查的優先、逐台隔離、預算與連續傳輸錯誤會中止、
  舊 operator 第一台之後就中止、改 0 筆也印一行 INFO；
* 每日對帳順帶回寫 workload 觀測，出錯不連累 release 事實，既有那行 log 不變；
* /woow payload 只給**已確認**的代碼，不帶叢集原文。

status 一律用 ``status_response()``／``workload_info()`` 組（形狀對帳到 operator 的 pydantic
宣告與打真端點產的 golden，見 ``operator_contract.py``）。
"""
import json
import logging
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests.common import BaseCase, TransactionCase, tagged

from ..models.cloud_service import (
    WORKLOAD_CHECK_BUDGET_SECONDS,
    WORKLOAD_FAULT_CONFIRM_MINUTES,
    _OBSERVATION_CONTROL_FIELDS,
    _WORKLOAD_FAULT_FIELDS,
    _internal_provision,
)
from ..services.paas_operator import PaaSOperatorTimeoutError
from ..services.workload_health import (
    DETAIL_LIMIT,
    FAULT_CREATE_FAILED,
    FAULT_NO_PODS,
    FAULT_QUOTA,
    VERDICT_FAULT,
    VERDICT_HEALTHY,
    VERDICT_UNKNOWN,
    VERDICT_UNSUPPORTED,
    WARNING_FRESH_SECONDS,
    workload_verdict,
)
from .operator_contract import ABSENT, operator_client, status_response, workload_info

_MODEL_MODULE = 'odoo.addons.woow_paas_platform.models.cloud_service'
_MODEL_LOGGER = _MODEL_MODULE
_MONITOR_LOGGER = 'odoo.addons.woow_paas_platform.models.platform_monitor'
_CTRL_MODULE = 'odoo.addons.woow_paas_platform.controllers.paas'
# cron 是函式內 import ⇒ patch 來源模組（unittest-mock-dual-namespace-patch）。
_CLIENT = 'odoo.addons.woow_paas_platform.services.paas_operator.get_paas_operator_client'

_SERVICE = 'woow_paas_platform.cloud_service'
_MONITOR = 'woow_paas_platform.platform_monitor'

#: 事故當下的拒絕原文（quota 名稱與數字是事故 namespace 的形狀）。
_QUOTA_MESSAGE = (
    'pods "svc-1647-server-7d9c8b6f5-" is forbidden: exceeded quota: '
    'paas-ws-test-1647-quota, requested: limits.cpu=8, used: limits.cpu=8, '
    'limited: limits.cpu=12')
_OTHER_MESSAGE = ('pods "svc-1647-server-7d9c8b6f5-" is forbidden: violates PodSecurity '
                  '"restricted:latest"')
_NOW = datetime(2026, 10, 10, 23, 40, 0)
_FRESH = '2026-10-10T23:31:12Z'
_STALE = '2026-10-10T22:00:00Z'


def _blocked(**overrides):
    """期望 1、實際 0 的 Recreate Deployment（沒有任何證據；證據由呼叫端明寫）。"""
    return workload_info(**{
        'name': 'svc-1647-server', 'replicas': 0, 'ready_replicas': 0,
        'updated_replicas': 0, 'strategy': 'Recreate', **overrides})


def _replica_failure(message=_QUOTA_MESSAGE, reason='FailedCreate'):
    return {'reason': reason, 'message': message, 'last_transition': '2026-10-10T18:07:45Z'}


def _warning(message=_QUOTA_MESSAGE, last_seen=_FRESH, reason='FailedCreate'):
    return {'reason': reason, 'message': message, 'count': 23, 'last_seen': last_seen}


def _quota_blocked(**overrides):
    return _blocked(replica_failure=_replica_failure(), **overrides)


def _healthy_statefulset():
    return workload_info(kind='StatefulSet', name='svc-1647-postgresql')


def _status(*workloads, **overrides):
    """帶 include_workloads 的 operator 回應。"""
    return status_response(workloads_included=True, workloads=list(workloads), **overrides)


def _legacy_status(**overrides):
    """舊 operator：三個 workload key 都**缺席**（不是 False／None）。"""
    return status_response(workloads_included=ABSENT, workloads=ABSENT,
                           workload_retrieval_error=ABSENT, **overrides)


# ====================================================================== #
#  純函式：workload_verdict                                                #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestWorkloadVerdict1647(BaseCase):
    """純函式測試，基底用 ``BaseCase``（同 ``test_woow_odoo_readiness_1251`` 的理由）。"""

    def _verdict(self, status, now=_NOW):
        return workload_verdict(status, now)

    def test_unsupported_when_the_operator_does_not_report_workloads(self):
        """缺席不是訊號：只有 ``workloads_included is True`` 才算有回報。"""
        blocked = [_quota_blocked()]
        for label, flag in (('absent', ABSENT), ('false', False), ('string', 'true'),
                            ('none', None), ('one', 1), ('mock', MagicMock())):
            with self.subTest(flag=label):
                status = status_response(workloads_included=flag, workloads=blocked)
                self.assertEqual(self._verdict(status), (VERDICT_UNSUPPORTED, None, ''))
        self.assertEqual(self._verdict(_legacy_status()), (VERDICT_UNSUPPORTED, None, ''))
        for not_a_dict in (None, [], 'deployed', MagicMock()):
            with self.subTest(status=repr(not_a_dict)[:20]):
                self.assertEqual(self._verdict(not_a_dict), (VERDICT_UNSUPPORTED, None, ''))

    def test_unknown_when_the_lookup_failed(self):
        failed = status_response(workloads_included=True, workloads=None,
                                 workload_retrieval_error='deployments is forbidden')
        self.assertEqual(self._verdict(failed), (VERDICT_UNKNOWN, None, ''))
        # 有錯誤時即使帶著清單也不信。
        both = _status(_quota_blocked(), workload_retrieval_error='partial')
        self.assertEqual(self._verdict(both), (VERDICT_UNKNOWN, None, ''))
        # 旗標是 True 但清單不是 list（None＝沒查到）。
        self.assertEqual(
            self._verdict(status_response(workloads_included=True, workloads=None)),
            (VERDICT_UNKNOWN, None, ''))

    def test_healthy_shapes(self):
        cases = {
            'all workloads have pods': _status(workload_info(), _healthy_statefulset()),
            'scaled to zero (suspended)': _status(workload_info(
                desired_replicas=0, replicas=0, ready_replicas=0, updated_replicas=0)),
            'no workloads at all': _status(),
            # 範圍外的對照：RollingUpdate 新 Pod 被擋、舊 Pod 仍在服務。
            'old pod still serving': _status(workload_info(
                replicas=1, ready_replicas=1, updated_replicas=0,
                replica_failure=_replica_failure())),
            # Pod 存在但不 ready 是 readiness 的事，不是「建不出來」。
            'pod exists but is not ready': _status(workload_info(
                replicas=1, ready_replicas=0)),
        }
        for label, status in cases.items():
            with self.subTest(case=label):
                self.assertEqual(self._verdict(status), (VERDICT_HEALTHY, None, ''))

    def test_quota_exceeded_from_the_replica_failure_condition(self):
        verdict, code, detail = self._verdict(_status(_quota_blocked()))
        self.assertEqual((verdict, code), (VERDICT_FAULT, FAULT_QUOTA))
        self.assertIn('Deployment svc-1647-server: 0 of 1 pods exist (Recreate)', detail)
        self.assertIn('FailedCreate: ' + _QUOTA_MESSAGE, detail)

    def test_quota_exceeded_from_a_fresh_statefulset_event(self):
        """StatefulSet 沒有 ReplicaFailure condition：證據是新鮮的 FailedCreate 事件。"""
        sts = _blocked(kind='StatefulSet', name='svc-1647-postgresql', strategy='RollingUpdate',
                       last_warning=_warning('create Pod svc-1647-postgresql-0 failed: '
                                             + _QUOTA_MESSAGE))
        verdict, code, detail = self._verdict(_status(sts))
        self.assertEqual((verdict, code), (VERDICT_FAULT, FAULT_QUOTA))
        self.assertIn('StatefulSet svc-1647-postgresql: 0 of 1 pods exist', detail)

    def test_create_failed_when_the_rejection_is_not_about_quota(self):
        for label, workload in (
                ('condition', _blocked(replica_failure=_replica_failure(_OTHER_MESSAGE))),
                ('event', _blocked(last_warning=_warning(_OTHER_MESSAGE))),
                ('condition without message', _blocked(
                    replica_failure=_replica_failure(message=None)))):
            with self.subTest(source=label):
                verdict, code, detail = self._verdict(_status(workload))
                self.assertEqual((verdict, code), (VERDICT_FAULT, FAULT_CREATE_FAILED))
                self.assertIn('FailedCreate', detail)

    def test_no_pods_when_there_is_no_usable_evidence(self):
        cases = {
            'no condition, no event': _blocked(),
            'event older than the freshness window': _blocked(last_warning=_warning(
                last_seen=_STALE)),
            'event time cannot be parsed': _blocked(last_warning=_warning(
                last_seen='yesterday')),
            'event without a time': _blocked(last_warning=_warning(last_seen=None)),
            'event is not FailedCreate': _blocked(last_warning=_warning(
                reason='FailedScheduling')),
            'condition is not FailedCreate': _blocked(replica_failure=_replica_failure(
                reason='ProgressDeadlineExceeded')),
        }
        for label, workload in cases.items():
            with self.subTest(case=label):
                verdict, code, detail = self._verdict(_status(workload))
                self.assertEqual((verdict, code), (VERDICT_FAULT, FAULT_NO_PODS))
                self.assertIn('no controller warning found', detail)

    def test_event_freshness_boundary(self):
        """剛好在窗口邊界上算新鮮；多一秒就不算。"""
        def stamp(seconds_ago):
            return (_NOW - timedelta(seconds=seconds_ago)).strftime('%Y-%m-%dT%H:%M:%SZ')

        on_edge = _blocked(last_warning=_warning(last_seen=stamp(WARNING_FRESH_SECONDS)))
        past_edge = _blocked(last_warning=_warning(last_seen=stamp(WARNING_FRESH_SECONDS + 1)))
        # 小數秒與 +00:00 的寫法也讀得懂（只取前 19 個字元）。
        fractional = _blocked(last_warning=_warning(last_seen='2026-10-10T23:31:12.123456Z'))
        self.assertEqual(self._verdict(_status(on_edge))[1], FAULT_QUOTA)
        self.assertEqual(self._verdict(_status(past_edge))[1], FAULT_NO_PODS)
        self.assertEqual(self._verdict(_status(fractional))[1], FAULT_QUOTA)

    def test_only_the_blocked_workload_is_named(self):
        """一個健康的 StatefulSet 加一個被擋的 Deployment：仍判異常，detail 只點名被擋的。"""
        verdict, code, detail = self._verdict(
            _status(_healthy_statefulset(), _quota_blocked()))
        self.assertEqual((verdict, code), (VERDICT_FAULT, FAULT_QUOTA))
        self.assertIn('svc-1647-server', detail)
        self.assertNotIn('svc-1647-postgresql', detail)

    def test_quota_wins_over_other_evidence_and_every_blocked_workload_is_listed(self):
        other = _blocked(name='svc-1647-worker', replica_failure=_replica_failure(_OTHER_MESSAGE))
        bare = _blocked(kind='StatefulSet', name='svc-1647-cache', strategy=None)
        verdict, code, detail = self._verdict(_status(other, bare, _quota_blocked()))
        self.assertEqual((verdict, code), (VERDICT_FAULT, FAULT_QUOTA))
        self.assertEqual(detail.count('; '), 2, detail)
        self.assertIn('StatefulSet svc-1647-cache: 0 of 1 pods exist — no controller warning '
                      'found', detail)

    def test_malformed_elements_are_skipped(self):
        """轉型失敗或不是 dict 的元素略過，不拖垮其餘元素的判定。"""
        garbage = ['not-a-dict', None, _blocked(desired_replicas='many'),
                   _blocked(replicas=None)]
        self.assertEqual(self._verdict(_status(*garbage)), (VERDICT_HEALTHY, None, ''))
        verdict, code, _detail = self._verdict(_status(*garbage, _quota_blocked()))
        self.assertEqual((verdict, code), (VERDICT_FAULT, FAULT_QUOTA))

    def test_detail_is_truncated(self):
        many = [_blocked(name='svc-1647-w%02d' % i,
                         replica_failure=_replica_failure(_QUOTA_MESSAGE)) for i in range(20)]
        verdict, _code, detail = self._verdict(_status(*many))
        self.assertEqual(verdict, VERDICT_FAULT)
        self.assertEqual(len(detail), DETAIL_LIMIT)
        self.assertTrue(detail.endswith('…'))


# ====================================================================== #
#  model 層共用 fixture                                                    #
# ====================================================================== #

class _WorkloadCase(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Service = cls.env[_SERVICE]
        cls.Monitor = cls.env[_MONITOR]
        cls.template = cls.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'Workload 1647', 'slug': 'workload-1647',
            'helm_repo_url': 'https://charts.example.com', 'helm_chart_name': 'chart',
            'helm_chart_version': '1.0.0', 'workload_type': 'container',
            'post_deploy_init_type': 'none', 'mcp_enabled': False,
        })
        # 非系統的 workspace owner：只有 base.group_user（su-env-exempt-guard-false-green：
        # 守衛測試要退出 su，TransactionCase 預設的 superuser env 走不到 deny 分支）。
        cls.owner = cls.env['res.users'].create({
            'name': 'Owner 1647', 'login': 'owner1647@tenant1647.tw',
            'groups_id': [(6, 0, [cls.env.ref('base.group_user').id])],
        })
        cls.workspace = cls.env['woow_paas_platform.workspace'].create({
            'name': 'WS 1647', 'owner_id': cls.owner.id})

    def setUp(self):
        super().setUp()
        self.singleton = self.Monitor._singleton()
        # 這個 DB 裡可能留有同 summary 的 activity；本檔的斷言限定在自己造成的那些。
        self._activities().unlink()

    def _service(self, tag, state='running', **extra):
        vals = {
            'name': 'svc-1647-%s' % tag, 'workspace_id': self.workspace.id,
            'template_id': self.template.id, 'state': state,
            'helm_namespace': 'paas-ws-test-1647', 'helm_release_name': 'svc-1647-%s' % tag,
        }
        vals.update(extra)
        return _internal_provision(self.Service).create(vals)

    def _activities(self):
        return self.env['mail.activity'].sudo().search([
            ('res_model', '=', _MONITOR), ('res_id', '=', self.singleton.id),
            ('summary', '=', self.Monitor.SUMMARY_WORKLOAD_FAULT)])

    def _monitor_bodies(self):
        """monitor chatter 的訊息內文。每次重讀：``message_ids`` 的 inverse 不是真的
        Many2one，新建的 mail.message 不會自動進已快取的 one2many——沿用快取的話「沒有新增
        訊息」的斷言會恆綠。"""
        self.singleton.invalidate_recordset(['message_ids'])
        return [str(body) for body in self.singleton.message_ids.mapped('body')]

    def _fault_values(self, svc):
        return tuple(svc[name] for name in _WORKLOAD_FAULT_FIELDS)

    def _flag(self, svc, code='quota_exceeded', minutes_ago=60):
        """直接寫一段「已通知」的異常（superuser env；等同平台自己寫的）。"""
        since = fields.Datetime.now() - timedelta(minutes=minutes_ago)
        svc.write({
            'workload_fault_code': code, 'workload_fault_detail': 'Deployment x: 0 of 1',
            'workload_fault_since': since, 'workload_fault_alerted_at': since,
            'workload_checked_at': since,
        })
        return since


# ====================================================================== #
#  _apply_workload_observation                                            #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestWorkloadObservation1647(_WorkloadCase):

    def setUp(self):
        super().setUp()
        self.recipients = self.Monitor._alert_recipients()[1]
        self.assertTrue(self.recipients, 'fixture 前提：至少一位 active 的系統管理員')

    def test_fault_with_evidence_is_recorded_and_alerted_on_first_sight(self):
        svc = self._service('raise')

        outcome = svc._apply_workload_observation(
            _status(_healthy_statefulset(), _quota_blocked()))

        self.assertEqual(outcome, 'raised')
        self.assertEqual(svc.workload_fault_code, 'quota_exceeded')
        self.assertIn('svc-1647-server', svc.workload_fault_detail)
        self.assertIn('exceeded quota', svc.workload_fault_detail)
        self.assertTrue(svc.workload_fault_since)
        self.assertEqual(svc.workload_checked_at, svc.workload_fault_since)
        self.assertTrue(svc.workload_fault_alerted_at, '有 FailedCreate 證據 ⇒ 第一次就通知')
        activities = self._activities()
        self.assertEqual(set(activities.mapped('user_id')), set(self.recipients),
                         '每位系統管理員一張')
        self.assertEqual(len(activities), len(self.recipients))
        for activity in activities:
            self.assertIn('id=%s' % svc.id, activity.note)
            self.assertIn('paas-ws-test-1647', activity.note)
            self.assertIn('exceeded quota', activity.note)
        self.assertEqual(svc.state, 'running')
        self.assertFalse(svc.error_message)

    def test_same_fault_is_reported_only_once(self):
        svc = self._service('once')
        svc._apply_workload_observation(_status(_quota_blocked()))
        since = svc.workload_fault_since
        alerted = svc.workload_fault_alerted_at
        notes = self._activities().mapped('note')

        # 同一段異常中 detail 變了（多一個被擋的 workload）：只更新欄位，不重發。
        outcome = svc._apply_workload_observation(_status(
            _quota_blocked(), _blocked(kind='StatefulSet', name='svc-1647-postgresql')))

        self.assertEqual(outcome, 'ongoing')
        self.assertEqual(svc.workload_fault_since, since, '起點不變')
        self.assertEqual(svc.workload_fault_alerted_at, alerted)
        self.assertIn('svc-1647-postgresql', svc.workload_fault_detail, 'detail 跟著最新觀測')
        self.assertEqual(self._activities().mapped('note'), notes, '不新增 activity、不追加行')

    def test_second_service_gets_a_line_on_the_same_activity(self):
        first = self._service('first')
        first._apply_workload_observation(_status(_quota_blocked()))
        before = self._activities()
        self.assertTrue(before)

        second = self._service('second')
        outcome = second._apply_workload_observation(_status(_quota_blocked()))

        self.assertEqual(outcome, 'raised')
        after = self._activities()
        self.assertEqual(after, before, '同 summary 未處理前不重開')
        for activity in after:
            self.assertIn('id=%s' % first.id, activity.note)
            self.assertIn('id=%s' % second.id, activity.note)
        self.assertTrue(
            second.workload_fault_alerted_at,
            '追加到既有 activity 也算通知落地——否則第二台每一輪都會重複追加一行')
        notes = after.mapped('note')
        second._apply_workload_observation(_status(_quota_blocked()))
        self.assertEqual(self._activities().mapped('note'), notes)

    def test_no_pods_without_evidence_waits_before_alerting(self):
        svc = self._service('nopods')

        outcome = svc._apply_workload_observation(_status(_blocked()))

        self.assertEqual(outcome, 'raised')
        self.assertEqual(svc.workload_fault_code, 'no_pods')
        self.assertFalse(svc._workload_fault_confirmed(), '換 Pod 的瞬間也會是 0：先不確認')
        self.assertFalse(svc.workload_fault_alerted_at)
        self.assertFalse(self._activities())

        # 同一段異常已經持續超過門檻。
        svc.write({'workload_fault_since': fields.Datetime.now() - timedelta(
            minutes=WORKLOAD_FAULT_CONFIRM_MINUTES + 1)})
        outcome = svc._apply_workload_observation(_status(_blocked()))

        self.assertEqual(outcome, 'ongoing')
        self.assertTrue(svc._workload_fault_confirmed())
        self.assertTrue(svc.workload_fault_alerted_at)
        self.assertTrue(self._activities())
        self.assertEqual(svc.state, 'running')

    def test_recovery_clears_the_flag_and_notes_it(self):
        svc = self._service('recover')
        svc._apply_workload_observation(_status(_quota_blocked()))
        since = svc.workload_fault_since
        self.assertTrue(svc.workload_fault_alerted_at, '前提：這一段通知過')
        marker = 'id=%s' % svc.id
        self.assertFalse([b for b in self._monitor_bodies() if marker in b])

        outcome = svc._apply_workload_observation(
            _status(workload_info(), _healthy_statefulset()))

        self.assertEqual(outcome, 'cleared')
        self.assertEqual(self._fault_values(svc), (False, False, False, False))
        self.assertTrue(svc.workload_checked_at)
        for activity in self._activities():
            self.assertEqual(str(activity.note).count(marker), 2,
                             'activity 應該多一行已恢復（原本的一行＋恢復的一行）')
            self.assertIn(str(since), str(activity.note))
        self.assertTrue([b for b in self._monitor_bodies() if marker in b],
                        'monitor 的 chatter 要留下恢復紀錄')
        self.assertEqual(svc.state, 'running')

        # 下一段異常是新的一段：重新通知。
        svc._apply_workload_observation(_status(_quota_blocked()))
        self.assertTrue(svc.workload_fault_alerted_at)

    def test_recovery_of_an_unconfirmed_fault_is_silent(self):
        """沒通知過的（未確認就恢復的 no_pods）不必再說「已恢復」。"""
        svc = self._service('blip')
        svc._apply_workload_observation(_status(_blocked()))
        self.assertFalse(svc.workload_fault_alerted_at)
        marker = 'id=%s' % svc.id

        outcome = svc._apply_workload_observation(_status(workload_info()))

        self.assertEqual(outcome, 'cleared')
        self.assertEqual(self._fault_values(svc), (False, False, False, False))
        self.assertFalse([b for b in self._monitor_bodies() if marker in b],
                         '沒通知過的異常恢復時不該留下「已恢復」')
        self.assertFalse(self._activities())

    def test_healthy_observation_only_stamps_the_check_time(self):
        svc = self._service('healthy')
        self.assertFalse(svc.workload_checked_at)

        outcome = svc._apply_workload_observation(_status(workload_info()))

        self.assertEqual(outcome, 'healthy')
        self.assertTrue(svc.workload_checked_at)
        self.assertEqual(self._fault_values(svc), (False, False, False, False))
        self.assertFalse(self._activities())

    def test_unsupported_and_unknown_touch_nothing(self):
        clean = self._service('clean')
        flagged = self._service('flagged')
        since = self._flag(flagged)
        snapshot = self._fault_values(flagged)
        cases = (
            ('unsupported', _legacy_status()),
            ('unsupported', status_response(workloads_included=False,
                                            workloads=[_quota_blocked()])),
            ('unsupported', MagicMock()),
            ('unknown', status_response(workloads_included=True, workloads=None,
                                        workload_retrieval_error='forbidden')),
        )
        for expected, status in cases:
            with self.subTest(outcome=expected):
                self.assertEqual(clean._apply_workload_observation(status), expected)
                self.assertEqual(flagged._apply_workload_observation(status), expected)
                self.assertFalse(clean.workload_checked_at, '連 checked_at 都不動')
                self.assertEqual(flagged.workload_checked_at, since)
                self.assertEqual(self._fault_values(flagged), snapshot,
                                 '查不到不是恢復：既有的旗標不得被清掉')
        self.assertFalse(self._activities())

    def test_alert_without_recipients_is_retried(self):
        svc = self._service('norecipient')
        todo = self.env.ref('mail.mail_activity_data_todo')
        nobody = (todo, self.env['res.users'])

        with patch.object(type(self.Monitor), '_alert_recipients', return_value=nobody), \
                self.assertLogs(_MONITOR_LOGGER, level=logging.WARNING):
            outcome = svc._apply_workload_observation(_status(_quota_blocked()))

        self.assertEqual(outcome, 'raised')
        self.assertEqual(svc.workload_fault_code, 'quota_exceeded', '欄位照寫，後台看得到')
        self.assertFalse(svc.workload_fault_alerted_at,
                         '通知沒落地就不記標記，否則這一段異常的告警會永久消失')
        self.assertFalse(self._activities())

        # 有收件者之後，下一輪補發。
        svc._apply_workload_observation(_status(_quota_blocked()))
        self.assertTrue(svc.workload_fault_alerted_at)
        self.assertTrue(self._activities())

    def test_state_and_error_message_are_never_touched(self):
        """🔴 餵 release failed＋被擋的 workload 也不准改 state（那是短 poll 才會做的事）。"""
        svc = self._service('state')
        svc.write({'error_message': 'left over note'})
        statuses = (
            status_response('failed', workloads_included=True, workloads=[_quota_blocked()],
                            release={'status': 'failed', 'revision': 9,
                                     'description': 'upgrade failed: boom'}),
            _status(_blocked()),
            _status(workload_info()),
            _legacy_status(),
        )
        for status in statuses:
            svc._apply_workload_observation(status)
            self.assertEqual((svc.state, svc.error_message), ('running', 'left over note'))
        self.assertTrue(svc.workload_checked_at, '前提：這幾輪確實有寫入，否則上面零鑑別力')

    def test_non_running_service_is_a_no_op(self):
        for state in ('upgrading', 'suspended', 'error', 'deploying'):
            with self.subTest(state=state):
                svc = self._service('idle-%s' % state, state=state)
                outcome = svc._apply_workload_observation(_status(_quota_blocked()))
                self.assertEqual(outcome, 'unknown')
                self.assertFalse(svc.workload_checked_at)
                self.assertEqual(self._fault_values(svc), (False, False, False, False))
                self.assertEqual(svc.state, state)
        self.assertFalse(self._activities())


# ====================================================================== #
#  生命週期與寫入守衛                                                      #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestWorkloadFaultLifecycle1647(_WorkloadCase):

    def test_leaving_running_clears_the_fault(self):
        for state in ('upgrading', 'suspended', 'error', 'deleting'):
            with self.subTest(state=state):
                svc = self._service('leave-%s' % state)
                checked = self._flag(svc)

                svc.write({'state': state})

                self.assertEqual(self._fault_values(svc), (False, False, False, False))
                self.assertEqual(svc.workload_checked_at, checked,
                                 'checked_at 說的是「最後一次看過」，不屬於某一段異常')

    def test_same_state_write_keeps_the_fault(self):
        svc = self._service('same')
        self._flag(svc)
        snapshot = self._fault_values(svc)

        svc.write({'state': 'running'})
        svc.write({'name': 'svc-1647-renamed'})

        self.assertEqual(self._fault_values(svc), snapshot)

    def test_workspace_owner_can_change_the_state_of_a_flagged_service(self):
        """四格裡有兩格帶 groups='base.group_system'：清除不 sudo 的話，owner 對自己的服務
        做任何會改 state 的操作（重新啟動、Redeploy…）都會 AccessError。"""
        svc = self._service('owner')
        self._flag(svc)
        as_owner = svc.with_user(self.owner)
        self.assertFalse(self.owner.has_group('base.group_system'), 'fixture 前提')
        # 陽性對照：owner 對這筆服務本來就寫得動。
        as_owner.write({'name': 'svc-1647-owner-renamed'})

        as_owner.write({'state': 'upgrading'})

        self.assertEqual(svc.state, 'upgrading')
        self.assertEqual(self._fault_values(svc), (False, False, False, False))

    def test_tenant_cannot_write_the_observation_fields(self):
        """租戶寫得進去就能藏掉或偽造告警。只斷言有拋（不在 with 區塊後讀 DB——
        ``assertRaises`` 會把區塊回捲，見 pitfall odoo-assertraises-savepoint-rollback）。"""
        svc = self._service('guard')
        as_owner = svc.with_user(self.owner)
        as_owner.write({'name': 'svc-1647-guard-renamed'})   # 陽性對照：不是記錄級擋下
        now = fields.Datetime.now()
        attempts = {
            'workload_fault_code': 'no_pods',
            'workload_fault_detail': 'forged',
            'workload_fault_since': now,
            'workload_fault_alerted_at': now,
            'workload_checked_at': now,
        }
        for name, value in attempts.items():
            with self.subTest(field=name):
                with self.assertRaises(AccessError) as denied:
                    as_owner.write({name: value})
                self.assertIn(name, str(denied.exception))
        # 清空也不行（藏掉告警）。
        self._flag(svc)
        with self.assertRaises(AccessError):
            as_owner.write({'workload_fault_code': False, 'workload_fault_since': False})

    def test_all_five_fields_are_guarded(self):
        fault_fields = {'workload_fault_code', 'workload_fault_detail',
                        'workload_fault_since', 'workload_fault_alerted_at'}
        # 子集而不是等值／個數：日後再加一格描述異常的欄位是合法演進
        # （phase-versioned-test-assertion），但加了就必須一併受守衛保護。
        self.assertTrue(fault_fields <= set(_WORKLOAD_FAULT_FIELDS))
        self.assertTrue(set(_WORKLOAD_FAULT_FIELDS) <= _OBSERVATION_CONTROL_FIELDS,
                        '離開 running 時會被清掉的欄位，每一格都必須擋住非平台身分的寫入')
        self.assertIn('workload_checked_at', _OBSERVATION_CONTROL_FIELDS)
        self.assertNotIn('workload_checked_at', _WORKLOAD_FAULT_FIELDS,
                         '「最後一次看過」不屬於某一段異常，離開 running 時不清')


# ====================================================================== #
#  _cron_check_running_workloads                                          #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestRunningWorkloadCron1647(_WorkloadCase):

    def setUp(self):
        super().setUp()
        # 這個 DB 裡可能已有別的測試建的服務；本 class 的順序／計數類斷言會被鄰居污染
        # （同 test_reconcile_release_facts_627i 的做法）。
        self.Service.search([]).unlink()

    def _run(self, client):
        """跑 10 分鐘 cron（mock operator；TransactionCase 禁 commit／rollback ⇒ no-op）。"""
        with patch(_CLIENT, return_value=client), \
                patch.object(self.env.cr, 'commit', lambda: None), \
                patch.object(self.env.cr, 'rollback', lambda: None):
            self.Service._cron_check_running_workloads()
        self.Service.invalidate_model()
        return client

    @staticmethod
    def _polled(client):
        return [c.kwargs.get('release_name') for c in client.get_status.call_args_list]

    def test_cron_record_is_wired(self):
        cron = self.env.ref('woow_paas_platform.ir_cron_check_running_workloads')
        self.assertTrue(cron.active)
        self.assertEqual(cron.code.strip(), 'model._cron_check_running_workloads()')
        self.assertEqual((cron.interval_number, cron.interval_type), (10, 'minutes'))

    def test_only_running_services_with_release_coordinates_are_checked(self):
        running = self._service('run')
        deploying = self._service('deploying', state='deploying')
        suspended = self._service('suspended', state='suspended')
        no_release = self._service('norelease', helm_release_name=False)
        no_namespace = self._service('nons', helm_namespace=False)

        client = self._run(operator_client(get_status=_status(workload_info())))

        self.assertEqual(self._polled(client), [running.helm_release_name])
        kwargs = client.get_status.call_args.kwargs
        self.assertIs(kwargs.get('include_workloads'), True)
        self.assertNotIn('include_vm', kwargs)
        self.assertEqual(kwargs.get('namespace'), running.helm_namespace)
        self.assertTrue(running.workload_checked_at)
        for other in (deploying, suspended, no_release, no_namespace):
            self.assertFalse(other.workload_checked_at)

    def test_least_recently_checked_goes_first(self):
        now = fields.Datetime.now()
        # 建立順序刻意與期望順序不同：依 id 排會得到 recent, old, never_a, never_b。
        recent = self._service('recent')
        old = self._service('old')
        never_a = self._service('never-a')
        never_b = self._service('never-b')
        recent.write({'workload_checked_at': now - timedelta(hours=1)})
        old.write({'workload_checked_at': now - timedelta(hours=2)})

        client = self._run(operator_client(get_status=_status(workload_info())))

        self.assertEqual(self._polled(client), [
            never_a.helm_release_name, never_b.helm_release_name,
            old.helm_release_name, recent.helm_release_name])

    def test_one_failing_service_does_not_stop_the_others(self):
        good_a = self._service('good-a')
        bad = self._service('bad')
        good_b = self._service('good-b')
        client = operator_client()

        def _status_or_boom(namespace, release_name, include_vm=False, include_workloads=False):
            if release_name == bad.helm_release_name:
                raise RuntimeError('operator exploded')
            return _status(workload_info())

        client.get_status.side_effect = _status_or_boom
        with self.assertLogs(_MODEL_LOGGER, level=logging.INFO) as captured:
            self._run(client)

        self.assertEqual(len(self._polled(client)), 3)
        self.assertTrue(good_a.workload_checked_at)
        self.assertTrue(good_b.workload_checked_at, '一台失敗不得中斷整趟')
        self.assertFalse(bad.workload_checked_at)
        summary = [line for line in captured.output if 'workload-check: running' in line]
        self.assertIn('本趟檢查 2 筆', summary[0])
        self.assertIn('讀取失敗 1 筆', summary[0])

    def test_budget_stops_the_run_and_reports_what_is_left(self):
        for tag in ('a', 'b', 'c'):
            self._service('budget-%s' % tag)
        client = operator_client(get_status=_status(workload_info()))
        # 第一次是起點、第二次是第一台之前；之後每次都已經超過預算。
        ticks = iter([0, 0])

        def _clock():
            return next(ticks, WORKLOAD_CHECK_BUDGET_SECONDS + 1)

        with patch('%s._workload_check_clock' % _MODEL_MODULE, new=_clock), \
                self.assertLogs(_MODEL_LOGGER, level=logging.INFO) as captured:
            self._run(client)

        self.assertEqual(client.get_status.call_count, 1, '第二台之前就超過預算')
        summary = [line for line in captured.output if 'workload-check: running' in line]
        self.assertIn('running 3 筆', summary[0])
        self.assertIn('本趟檢查 1 筆', summary[0])
        self.assertIn('預算用完未檢查 2 筆', summary[0])

    def test_two_consecutive_transport_errors_abort_the_run(self):
        services = [self._service('down-%s' % tag) for tag in ('a', 'b', 'c')]
        client = operator_client()
        client.get_status.side_effect = PaaSOperatorTimeoutError('timed out')

        with self.assertLogs(_MODEL_LOGGER, level=logging.WARNING) as captured:
            self._run(client)

        self.assertEqual(client.get_status.call_count, 2, 'operator 掛了就不逐台等 timeout')
        self.assertTrue(any('本趟中止' in line for line in captured.output))
        for svc in services:
            self.assertFalse(svc.workload_checked_at)
            self.assertEqual(svc.state, 'running')

    def test_a_success_between_transport_errors_resets_the_streak(self):
        services = [self._service('flaky-%s' % tag) for tag in ('a', 'b', 'c', 'd')]
        client = operator_client()
        client.get_status.side_effect = [
            PaaSOperatorTimeoutError('timed out'), _status(workload_info()),
            PaaSOperatorTimeoutError('timed out'), _status(workload_info()),
        ]

        with self.assertLogs(_MODEL_LOGGER, level=logging.WARNING):
            self._run(client)

        self.assertEqual(client.get_status.call_count, 4, '不連續的傳輸錯誤不中止')
        self.assertEqual([bool(svc.workload_checked_at) for svc in services],
                         [False, True, False, True])

    def test_legacy_operator_aborts_after_the_first_service(self):
        """舊 operator 對每一台的回答都一樣：第一台之後就不必再打。"""
        services = [self._service('legacy-%s' % tag) for tag in ('a', 'b', 'c')]
        client = operator_client(get_status=_legacy_status())

        with self.assertLogs(_MODEL_LOGGER, level=logging.INFO) as captured:
            self._run(client)

        self.assertEqual(client.get_status.call_count, 1)
        self.assertTrue(any('operator 尚未回報 workloads' in line for line in captured.output))
        for svc in services:
            self.assertFalse(svc.workload_checked_at)
            self.assertEqual(self._fault_values(svc), (False, False, False, False))
        self.assertFalse(self._activities())

    def test_no_client_or_no_services_is_quiet(self):
        # 沒有服務：不取 client、不拋錯。
        client = operator_client()
        self._run(client)
        client.get_status.assert_not_called()
        # 有服務但 operator 沒設定：不拋錯、什麼都不寫。
        svc = self._service('noclient')
        self._run(None)
        self.assertFalse(svc.workload_checked_at)

    def test_fault_is_recorded_but_state_stays_running(self):
        svc = self._service('fault')
        client = operator_client(get_status=_status(_quota_blocked()))

        with self.assertLogs(_MODEL_LOGGER, level=logging.INFO) as captured:
            self._run(client)

        self.assertEqual(svc.workload_fault_code, 'quota_exceeded')
        self.assertTrue(svc.workload_fault_alerted_at)
        self.assertTrue(self._activities())
        self.assertEqual(svc.state, 'running')
        self.assertFalse(svc.error_message)
        named = [line for line in captured.output
                 if '新異常' in line and 'id=%s' % svc.id in line]
        self.assertTrue(named, '新異常要點名是哪一台')
        self.assertIn('quota_exceeded', named[0])

        # 恢復：下一趟自動清掉，並點名。
        client = operator_client(get_status=_status(workload_info()))
        with self.assertLogs(_MODEL_LOGGER, level=logging.INFO) as captured:
            self._run(client)
        self.assertEqual(self._fault_values(svc), (False, False, False, False))
        self.assertEqual(svc.state, 'running')
        self.assertTrue([line for line in captured.output
                         if '恢復' in line and 'id=%s' % svc.id in line])

    def test_logs_even_when_nothing_changed(self):
        self._service('quiet')

        # ⚠ 數值 logging.INFO 而非字串 'INFO'（#492，repo-lint assertlogs-numeric-level）。
        with self.assertLogs(_MODEL_LOGGER, level=logging.INFO) as captured:
            self._run(operator_client(get_status=_status(workload_info())))

        summary = [line for line in captured.output if 'workload-check: running' in line]
        self.assertTrue(
            summary, '改 0 筆也要 log——否則「一直沒有異常」與「cron 根本沒跑」的日誌畫面相同')
        self.assertIn('running 1 筆', summary[0])
        self.assertIn('新異常 0', summary[0])
        self.assertIn('健康 1', summary[0])
        self.assertIn('預算用完未檢查 0 筆', summary[0])


# ====================================================================== #
#  每日對帳順帶回寫                                                        #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestReconcileAppliesWorkloadObservation1647(_WorkloadCase):

    _RELEASE = {'status': 'deployed', 'revision': 7, 'chart_version': '9.9.9'}

    def setUp(self):
        super().setUp()
        self.Service.search([]).unlink()

    def _reconcile(self, client):
        with patch(_CLIENT, return_value=client):
            self.Service._cron_reconcile_release_facts()
        self.Service.invalidate_model()
        return client

    def test_reconcile_asks_for_workloads(self):
        svc = self._service('ask')
        client = self._reconcile(operator_client(
            get_status=_status(workload_info(), release=self._RELEASE)))

        kwargs = client.get_status.call_args.kwargs
        self.assertIs(kwargs.get('include_workloads'), True)
        self.assertEqual(kwargs.get('release_name'), svc.helm_release_name)
        self.assertTrue(svc.workload_checked_at)

    def test_blocked_workload_is_flagged_and_release_facts_still_land(self):
        svc = self._service('blocked')

        self._reconcile(operator_client(
            get_status=_status(_quota_blocked(), release=self._RELEASE)))

        self.assertEqual(svc.workload_fault_code, 'quota_exceeded')
        self.assertTrue(svc.workload_fault_alerted_at)
        self.assertEqual((svc.helm_revision, svc.helm_chart_version), (7, '9.9.9'),
                         'release 事實照常回寫')
        self.assertTrue(svc.last_release_sync_at)
        self.assertEqual(svc.state, 'running')
        self.assertFalse(svc.error_message)

    def test_observation_failure_does_not_roll_back_release_facts(self):
        svc = self._service('boom')
        client = operator_client(get_status=_status(_quota_blocked(), release=self._RELEASE))

        with patch.object(type(self.Service), '_apply_workload_observation',
                          side_effect=RuntimeError('boom')) as observed, \
                self.assertLogs(_MODEL_LOGGER, level=logging.ERROR) as captured:
            self._reconcile(client)

        self.assertEqual(observed.call_count, 1, '前提：注入的例外確實被觸發')
        self.assertTrue(any('workload observation failed' in line for line in captured.output))
        self.assertEqual((svc.helm_revision, svc.helm_chart_version), (7, '9.9.9'),
                         'workload 觀測出錯不得把 release 事實一起回捲')
        self.assertFalse(svc.workload_fault_code)

    def test_legacy_operator_writes_no_workload_field(self):
        svc = self._service('legacy')

        self._reconcile(operator_client(get_status=_legacy_status(release=self._RELEASE)))

        self.assertEqual(svc.helm_revision, 7, '前提：這一輪確實有跑到')
        self.assertFalse(svc.workload_checked_at)
        self.assertEqual(self._fault_values(svc), (False, False, False, False))
        self.assertFalse(self._activities())

    def test_existing_log_line_is_untouched(self):
        """既有那行（``reconcile-release-facts: 掃描 …``）是契約；workload 觀測另起一行。"""
        svc = self._service('log')
        svc.write({'helm_revision': 7, 'helm_chart_version': '9.9.9'})

        with self.assertLogs(_MODEL_LOGGER, level=logging.INFO) as captured:
            self._reconcile(operator_client(
                get_status=_status(workload_info(), release=self._RELEASE)))

        reconcile_lines = [line for line in captured.output
                           if 'reconcile-release-facts' in line]
        self.assertEqual(len(reconcile_lines), 1, reconcile_lines)
        self.assertIn('掃描 1 筆 running', reconcile_lines[0])
        self.assertIn('0 筆漂移', reconcile_lines[0])
        self.assertNotIn('workload', reconcile_lines[0])
        workload_lines = [line for line in captured.output
                          if 'workload-observation (daily reconcile)' in line]
        self.assertEqual(len(workload_lines), 1, workload_lines)
        self.assertIn('健康 1', workload_lines[0])
        self.assertGreater(captured.output.index(workload_lines[0]),
                           captured.output.index(reconcile_lines[0]))


# ====================================================================== #
#  /woow payload                                                          #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestWorkloadFaultPayload1647(_WorkloadCase):

    def _format(self, service, include_details, user=None):
        from odoo.addons.woow_paas_platform.controllers.paas import PaasController
        env = self.env(user=user) if user else self.env
        with patch('%s.request' % _CTRL_MODULE, new=SimpleNamespace(env=env)):
            return PaasController()._format_service(service, include_details=include_details)

    def _confirmed(self, tag):
        svc = self._service(tag)
        svc._apply_workload_observation(_status(_quota_blocked()))
        self.assertTrue(svc._workload_fault_confirmed(), 'fixture 前提')
        return svc

    def test_confirmed_fault_reaches_both_payload_shapes(self):
        svc = self._confirmed('payload')
        for include_details in (False, True):
            with self.subTest(include_details=include_details):
                payload = self._format(svc, include_details)
                self.assertEqual(payload['workload_fault'], 'quota_exceeded')
                self.assertEqual(payload['state'], 'running', '狀態仍是 running')
                self.assertEqual(payload['error_message'], '', '不併進 error_message')

    def test_unconfirmed_clean_and_non_running_are_none(self):
        unconfirmed = self._service('unconfirmed')
        unconfirmed._apply_workload_observation(_status(_blocked()))
        self.assertEqual(unconfirmed.workload_fault_code, 'no_pods', 'fixture 前提')
        clean = self._service('clean')
        # 不是 running：旗標還在（不經 write() 的清除）也不給。
        stale = self._service('stale', state='upgrading')
        self._flag(stale)
        self.assertTrue(stale.workload_fault_code, 'fixture 前提')

        for svc in (unconfirmed, clean, stale):
            with self.subTest(service=svc.name):
                self.assertIn('workload_fault', self._format(svc, False))
                self.assertIsNone(self._format(svc, False)['workload_fault'])
                self.assertIsNone(svc._workload_fault_public())

    def test_payload_never_carries_the_cluster_detail(self):
        svc = self._confirmed('nodetail')
        self.assertIn('exceeded quota', svc.workload_fault_detail, 'fixture 前提')
        for include_details in (False, True):
            with self.subTest(include_details=include_details):
                dumped = json.dumps(self._format(svc, include_details), default=str)
                for leak in ('exceeded quota', 'svc-1647-server', 'limits.cpu',
                             'workload_fault_detail', 'workload_fault_since',
                             'workload_fault_alerted_at'):
                    self.assertNotIn(leak, dumped)

    def test_tenant_env_does_not_raise_access_error(self):
        """payload 讀的三格不帶 groups：租戶身分走得通（detail／alerted 兩格帶 groups）。"""
        svc = self._confirmed('tenant')
        as_owner = svc.with_user(self.owner)
        with self.assertRaises(AccessError):
            as_owner.read(['workload_fault_detail'])   # 鑑別項：帶 groups 的那格確實被擋

        for include_details in (False, True):
            with self.subTest(include_details=include_details):
                payload = self._format(as_owner, include_details, user=self.owner)
                self.assertEqual(payload['workload_fault'], 'quota_exceeded')
