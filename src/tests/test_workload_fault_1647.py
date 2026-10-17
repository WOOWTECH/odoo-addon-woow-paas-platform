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
* 🔴 **值真的變了才寫**：健康的穩態、內容沒變的異常，對 cloud_service 列零寫入
  （``write()`` 次數為 0、``write_date`` 不動）——10 分鐘 cron 每趟 commit 所有 running 列
  會與每日對帳、生命週期 cron 的長交易互撞（review detect-B1／B2）；
* 🔴 全程 ``state`` 仍是 ``running``、``error_message`` 不變（repo-lint 第 19 條判準 5 另有
  形態層守衛）；
* 服務離開 running 時四格由 ``write()`` 集中清掉，非系統使用者寫不進這五格、也不會因為
  改 state 而 AccessError；
* ``_cron_check_running_workloads``：依 platform_monitor 的游標輪替（從游標之後、依 id 升冪、
  繞回開頭；成功、失敗、略過都推進，提前中止後下一趟從中止處之後開始——每次都失敗的服務
  不會把後面的餓死，review detect-B3）、逐台隔離、預算與連續傳輸錯誤會中止、舊 operator
  第一台之後就中止、operator 回錯只印一行 warning、改 0 筆也印一行 INFO；
* 每日對帳順帶回寫 workload 觀測（同樣值沒變不寫），出錯不連累 release 事實，既有那行
  log 不變；
* /woow payload 只給**已確認**的代碼，不帶叢集原文；
* 異常已對租戶確認時，/woow 其他原本只看 state 的訊號不再說它正常：詳情頁 Connection Info
  的連線狀態不是 ONLINE、工作區頁統計卡把它算進 Alerts 而不是 Healthy；狀態徽章仍跟著
  state（瀏覽器實走 F1；元件原始碼的靜態檢查，行為另由 ``tests/ui`` 的 node 測試驗）；
* 後台表單警示的每一段可見文字在 zh_TW.po 都有譯文（review detect-S2）；
* 後台列表徽章用的三個代碼標籤（英文與 zh_TW）短到放得下，完整的說明在欄位 help 與表單
  警示裡依代碼顯示的那一行，每個代碼都要有（瀏覽器實走 F5）；
* /woow 的三句租戶文案在 zh_TW.po 都有譯文，配額那一句（英文與譯文）只請租戶聯絡客服、
  不建議刪除／停用／停止服務來釋出配額，而且說的是「剩餘配額不足以啟動」、不說配額已滿
  （瀏覽器實走 F2）。

status 一律用 ``status_response()``／``workload_info()`` 組（形狀對帳到 operator 的 pydantic
宣告與打真端點產的 golden，見 ``operator_contract.py``）。
"""
import json
import logging
import os
import re
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from lxml import etree

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests.common import BaseCase, TransactionCase, tagged
from odoo.tools.translate import PoFileReader, xml_translate

from ..models.cloud_service import (
    WORKLOAD_CHECK_BUDGET_SECONDS,
    WORKLOAD_CHECK_TRANSPORT_ABORT,
    WORKLOAD_FAULT_CONFIRM_MINUTES,
    _OBSERVATION_CONTROL_FIELDS,
    _WORKLOAD_FAULT_FIELDS,
    _internal_provision,
)
from ..services.paas_operator import PaaSOperatorAPIError, PaaSOperatorTimeoutError
from ..services.workload_health import (
    DETAIL_LIMIT,
    EVIDENCE_CODES,
    FAULT_CODES,
    FAULT_CRASH_LOOP,
    FAULT_CREATE_FAILED,
    FAULT_NO_PODS,
    FAULT_NOT_READY,
    FAULT_OOM_KILLED,
    FAULT_QUOTA,
    VERDICT_FAULT,
    VERDICT_HEALTHY,
    VERDICT_UNKNOWN,
    VERDICT_UNSUPPORTED,
    WARNING_FRESH_SECONDS,
    workload_verdict,
)
from .operator_contract import (
    ABSENT,
    operator_client,
    pod_info,
    status_response,
    workload_info,
)

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
#: 撥回 write_date 用的固定過去時間（與任何一次真的寫入都不會相同）。
_REWOUND = datetime(2026, 1, 1, 0, 0, 0)
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
            # #1456 起「Pod 都在、卻沒有一顆就緒」另判 not_ready（見
            # TestWorkloadNotReadyVerdict1456）；這裡只留「有一顆以上就緒」的對照。
            'one of two pods is ready': _status(workload_info(
                desired_replicas=2, replicas=2, ready_replicas=1)),
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

    @contextmanager
    def _service_writes(self):
        """記下區塊內每一次對 cloud_service 的 ``write()``：``[(ids, 排序過的 vals key)]``。

        原本的 write 照常執行。spy 掛在 registry class 上（最外層），所以 ``write()`` 內部
        用 ``super()`` 補寫的那幾次不會另外算——要數的是「有幾個呼叫端對服務列下了 write」。
        """
        cls = type(self.Service)
        original = cls.write
        calls = []

        def _spy(records, vals):
            calls.append((tuple(records.ids), sorted(vals)))
            return original(records, vals)

        with patch.object(cls, 'write', _spy):
            yield calls

    def _rewind_write_date(self, services):
        """把 ``write_date`` 撥回固定的過去時間。

        測試交易內 ``cr.now()`` 是交易起點，每一次 ORM 寫入蓋上去的 write_date 都相同——
        不先撥回，「write_date 沒變」在同一個交易裡恆真、沒有鑑別力。
        """
        services.flush_recordset()
        self.env.cr.execute(
            'UPDATE %s SET write_date = %%s WHERE id IN %%s' % self.Service._table,
            (_REWOUND, tuple(services.ids)))
        services.invalidate_recordset(['write_date'])
        self.assertEqual(self._write_dates(services), dict.fromkeys(services.ids, _REWOUND),
                         'fixture 前提：write_date 確實被撥回')

    def _write_dates(self, services):
        """直接從 DB 讀 write_date（先 flush：還沒落地的 ORM 寫入也要算）。"""
        self.Service.flush_model()
        self.env.cr.execute(
            'SELECT id, write_date FROM %s WHERE id IN %%s' % self.Service._table,
            (tuple(services.ids),))
        return dict(self.env.cr.fetchall())

    def _assert_untouched(self, services, calls):
        """零寫入的兩個獨立證據：沒有任何 ``write()`` 呼叫，DB 裡的 write_date 也沒動。"""
        self.assertEqual(calls, [], '對 cloud_service 不得有任何 write')
        self.assertEqual(self._write_dates(services), dict.fromkeys(services.ids, _REWOUND),
                         'write_date 不得變動')

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

    def _age_first_sight(self, svc, minutes):
        """把「第一次看到」那一趟整個往前挪：since 與那一趟留下的寫入時間一起挪。

        只挪 since 的話，列上會變成「寫入時間 − since ≥ 門檻」，等於替被測程式把確認做完了。
        """
        first_seen = fields.Datetime.now() - timedelta(minutes=minutes)
        svc.write({'workload_fault_since': first_seen, 'workload_checked_at': first_seen})
        return first_seen

    def test_no_pods_without_evidence_waits_before_alerting(self):
        svc = self._service('nopods')

        outcome = svc._apply_workload_observation(_status(_blocked()))

        self.assertEqual(outcome, 'raised')
        self.assertEqual(svc.workload_fault_code, 'no_pods')
        self.assertFalse(svc._workload_fault_confirmed(), '換 Pod 的瞬間也會是 0：先不確認')
        self.assertFalse(svc.workload_fault_alerted_at)
        self.assertFalse(self._activities())

        # 還沒過門檻就再看到一次：仍不確認，而且內容沒變 ⇒ 不寫。
        self._age_first_sight(svc, WORKLOAD_FAULT_CONFIRM_MINUTES - 1)
        self._rewind_write_date(svc)
        with self._service_writes() as calls:
            outcome = svc._apply_workload_observation(_status(_blocked()))
        self.assertEqual(outcome, 'ongoing')
        self._assert_untouched(svc, calls)
        self.assertFalse(svc._workload_fault_confirmed())
        self.assertFalse(self._activities())

        # 同一段異常已經持續超過門檻，而且這一趟又看到了。
        first_seen = self._age_first_sight(svc, WORKLOAD_FAULT_CONFIRM_MINUTES + 1)
        self.assertFalse(svc._workload_fault_confirmed(),
                         'fixture 前提：列上還沒有「過了門檻又看到一次」的紀錄')
        outcome = svc._apply_workload_observation(_status(_blocked()))

        self.assertEqual(outcome, 'ongoing')
        self.assertTrue(svc._workload_fault_confirmed())
        self.assertEqual(svc.workload_fault_since, first_seen, '起點不變')
        self.assertGreater(svc.workload_checked_at, first_seen,
                           '確認的那一趟要留下一次寫入——之後的讀取端（/woow payload）靠它')
        self.assertTrue(svc.workload_fault_alerted_at)
        self.assertTrue(self._activities())
        self.assertEqual(svc.state, 'running')

        # 確認之後內容沒變：回到零寫入。
        self._rewind_write_date(svc)
        with self._service_writes() as calls:
            outcome = svc._apply_workload_observation(_status(_blocked()))
        self.assertEqual(outcome, 'ongoing')
        self._assert_untouched(svc, calls)
        self.assertTrue(svc._workload_fault_confirmed())

    def test_confirmation_needs_a_second_sighting_not_just_elapsed_time(self):
        """只被看到一次的 ``no_pods``，放再久也不算確認（讀取端不拿牆鐘的現在去減 since）。

        /woow 每次組 payload 都會問「確認了沒」。用「現在 − since」當判準的話，換 Pod 那一瞬間
        的 0 個 Pod 會在 5 分鐘後對租戶顯示成故障，一直到下一趟 cron 才清掉。
        """
        svc = self._service('glimpse')
        svc._apply_workload_observation(_status(_blocked()))
        self._age_first_sight(svc, WORKLOAD_FAULT_CONFIRM_MINUTES * 3)

        self.assertFalse(svc._workload_fault_confirmed())
        self.assertIsNone(svc._workload_fault_public())

        # 下一趟看到已經恢復：安靜地清掉，從頭到尾沒有通知、也沒有對租戶顯示過。
        self.assertEqual(svc._apply_workload_observation(_status(workload_info())), 'cleared')
        self.assertFalse(self._activities())

    def test_unchanged_fault_writes_nothing_the_second_time(self):
        """異常持續、代碼與 detail 都沒變 ⇒ 第二趟一格都不寫（review detect-B1）。"""
        svc = self._service('steady-fault')
        status = _status(_healthy_statefulset(), _quota_blocked())
        self.assertEqual(svc._apply_workload_observation(status), 'raised')
        self.assertTrue(svc.workload_fault_alerted_at, 'fixture 前提：這一段已通知')
        snapshot = (self._fault_values(svc), svc.workload_checked_at)
        notes = self._activities().mapped('note')
        self._rewind_write_date(svc)

        with self._service_writes() as calls:
            outcome = svc._apply_workload_observation(status)

        self.assertEqual(outcome, 'ongoing')
        self._assert_untouched(svc, calls)
        self.assertEqual((self._fault_values(svc), svc.workload_checked_at), snapshot)
        self.assertEqual(self._activities().mapped('note'), notes)

        # 陽性對照：內容真的變了（多一個被擋的 workload）就會寫，而且只寫變了的那幾格。
        with self._service_writes() as calls:
            svc._apply_workload_observation(_status(
                _quota_blocked(), _blocked(kind='StatefulSet', name='svc-1647-postgresql')))
        self.assertEqual(calls, [((svc.id,), ['workload_checked_at', 'workload_fault_detail'])])
        self.assertNotEqual(self._write_dates(svc)[svc.id], _REWOUND)

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

    def test_healthy_observation_writes_nothing(self):
        """健康、列上也沒有異常值 ⇒ 一格都不寫，連「查過了」的時間都不留（review detect-B1）。"""
        svc = self._service('healthy')
        self.assertFalse(svc.workload_checked_at)
        self._rewind_write_date(svc)

        with self._service_writes() as calls:
            outcomes = [svc._apply_workload_observation(_status(workload_info()))
                        for _round in range(2)]

        self.assertEqual(outcomes, ['healthy', 'healthy'])
        self._assert_untouched(svc, calls)
        self.assertFalse(svc.workload_checked_at)
        self.assertEqual(self._fault_values(svc), (False, False, False, False))
        self.assertFalse(self._activities())

    def test_recovery_after_a_fault_returns_to_zero_writes(self):
        """恢復那一趟寫一次（清空），之後又是健康的穩態。"""
        svc = self._service('recovered')
        svc._apply_workload_observation(_status(_quota_blocked()))
        healthy = _status(workload_info())

        with self._service_writes() as calls:
            self.assertEqual(svc._apply_workload_observation(healthy), 'cleared')
        self.assertEqual(len(calls), 1, calls)
        cleared_at = svc.workload_checked_at
        self.assertTrue(cleared_at, '恢復是一次觀測結果的寫入')

        self._rewind_write_date(svc)
        with self._service_writes() as calls:
            self.assertEqual(svc._apply_workload_observation(healthy), 'healthy')
        self._assert_untouched(svc, calls)
        self.assertEqual(svc.workload_checked_at, cleared_at)

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
                self.assertFalse(clean.workload_checked_at, '寫入時間也不動')
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
                                 'checked_at 說的是「最後一次寫入觀測結果的時間」，'
                                 '不屬於某一段異常')

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
                         '「最後一次寫入觀測結果的時間」不屬於某一段異常，離開 running 時不清')


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
        # 輪替游標記在 monitor 單例上：每條測試都從頭開始。
        self._set_cursor(0)

    def _set_cursor(self, service_id):
        self.singleton.write({'workload_check_cursor': service_id})

    def _cursor(self):
        self.singleton.invalidate_recordset(['workload_check_cursor'])
        return self.singleton.workload_check_cursor

    def _run(self, client):
        """跑 10 分鐘 cron（mock operator；TransactionCase 禁 commit／rollback ⇒ no-op）。"""
        with patch(_CLIENT, return_value=client), \
                patch.object(self.env.cr, 'commit', lambda: None), \
                patch.object(self.env.cr, 'rollback', lambda: None):
            self.Service._cron_check_running_workloads()
        self.Service.invalidate_model()
        return client

    def _run_healthy(self):
        return self._run(operator_client(get_status=_status(workload_info())))

    def _client_failing_for(self, failing, error):
        """``failing`` 裡的服務每次都拋 ``error``，其餘回健康。每趟給一個新的 client，
        呼叫序列才不會跨趟累加。"""
        names = {svc.helm_release_name for svc in failing}
        client = operator_client()

        def _answer(namespace, release_name, include_vm=False, include_workloads=False):
            if release_name in names:
                raise error
            return _status(workload_info())

        client.get_status.side_effect = _answer
        return client

    def _client_answering(self, answers):
        """``answers``：服務 → 那一台的 status；沒列到的回健康。"""
        by_name = {svc.helm_release_name: status for svc, status in answers.items()}
        client = operator_client()

        def _answer(namespace, release_name, include_vm=False, include_workloads=False):
            return by_name.get(release_name, _status(workload_info()))

        client.get_status.side_effect = _answer
        return client

    @staticmethod
    def _polled(client):
        return [c.kwargs.get('release_name') for c in client.get_status.call_args_list]

    @staticmethod
    def _names(*services):
        return [svc.helm_release_name for svc in services]

    @staticmethod
    def _summary(captured):
        lines = [line for line in captured.output if 'workload-check: running' in line]
        return lines[0] if lines else ''

    def test_cron_record_is_wired(self):
        cron = self.env.ref('woow_paas_platform.ir_cron_check_running_workloads')
        self.assertTrue(cron.active)
        self.assertEqual(cron.code.strip(), 'model._cron_check_running_workloads()')
        self.assertEqual((cron.interval_number, cron.interval_type), (10, 'minutes'))

    def test_only_running_services_with_release_coordinates_are_checked(self):
        running = self._service('run')
        self._service('deploying', state='deploying')
        self._service('suspended', state='suspended')
        self._service('norelease', helm_release_name=False)
        self._service('nons', helm_namespace=False)

        client = self._run_healthy()

        self.assertEqual(self._polled(client), [running.helm_release_name])
        kwargs = client.get_status.call_args.kwargs
        self.assertIs(kwargs.get('include_workloads'), True)
        self.assertNotIn('include_vm', kwargs)
        self.assertEqual(kwargs.get('namespace'), running.helm_namespace)

    # ---------------- 健康的穩態不寫服務列（review detect-B1／B2） ---------------- #

    def test_healthy_steady_state_never_writes_a_service_row(self):
        """連跑兩趟、每一台都健康 ⇒ 對 cloud_service 的 write 次數為 0，write_date 不動。

        這條 cron 每 10 分鐘跑一次、逐台 commit：只要每趟在每台上留一格時間戳，就等於每
        10 分鐘 commit 一次所有 running 列，每日對帳與生命週期 cron（REPEATABLE READ 的長
        交易）之後一寫那些列就是 serialization failure。
        """
        services = self.Service.browse()
        for tag in ('a', 'b', 'c'):
            services |= self._service('steady-%s' % tag)
        self._rewind_write_date(services)

        with self._service_writes() as calls, \
                self.assertLogs(_MODEL_LOGGER, level=logging.INFO) as captured:
            first = self._run_healthy()
            second = self._run_healthy()

        self.assertEqual(self._polled(first), self._names(*services), '前提：每一台都真的查了')
        self.assertEqual(self._polled(second), self._names(*services))
        self._assert_untouched(services, calls)
        for svc in services:
            self.assertFalse(svc.workload_checked_at, '健康的服務不留「查過了」的時間')
        summaries = [line for line in captured.output if 'workload-check: running' in line]
        self.assertEqual(len(summaries), 2)
        for line in summaries:
            self.assertIn('本趟檢查 3 筆', line)
            self.assertIn('健康 3', line)

    def test_full_passes_do_not_rewrite_the_cursor(self):
        """每趟都查得完時游標停在同一個 id：monitor 那一列在穩態也是零寫入。"""
        services = [self._service('pass-%s' % tag) for tag in ('a', 'b', 'c')]
        self._run_healthy()
        self.assertEqual(self._cursor(), services[-1].id, '前提：第一趟把游標推到最後一台')

        monitor_cls = type(self.Monitor)
        original = monitor_cls.write
        monitor_writes = []

        def _spy(records, vals):
            monitor_writes.append(sorted(vals))
            return original(records, vals)

        with patch.object(monitor_cls, 'write', _spy):
            client = self._run_healthy()

        self.assertEqual(self._polled(client), self._names(*services))
        self.assertEqual(monitor_writes, [], '游標值沒變就不寫')
        self.assertEqual(self._cursor(), services[-1].id)

    def test_unchanged_fault_is_not_rewritten_on_the_next_run(self):
        """異常還在、內容沒變：第二趟對那一台也是零寫入。"""
        faulty = self._service('still-down')
        fine = self._service('fine')
        both = faulty | fine
        blocked = self._client_answering({faulty: _status(_quota_blocked())})
        self._run(blocked)
        self.assertEqual(faulty.workload_fault_code, 'quota_exceeded', '前提：第一趟寫進去了')
        self.assertTrue(faulty.workload_fault_alerted_at)
        snapshot = (self._fault_values(faulty), faulty.workload_checked_at)
        notes = self._activities().mapped('note')
        self._rewind_write_date(both)

        with self._service_writes() as calls, \
                self.assertLogs(_MODEL_LOGGER, level=logging.INFO) as captured:
            self._run(self._client_answering({faulty: _status(_quota_blocked())}))

        self._assert_untouched(both, calls)
        self.assertEqual((self._fault_values(faulty), faulty.workload_checked_at), snapshot)
        self.assertEqual(self._activities().mapped('note'), notes, '不重複通知')
        self.assertIn('持續中 1', self._summary(captured))
        self.assertIn('健康 1', self._summary(captured))

    # ---------------- 輪替游標（review detect-B3） ---------------- #

    def test_rotation_starts_after_the_cursor_and_wraps_around(self):
        a, b, c, d = (self._service('rot-%s' % tag) for tag in 'abcd')
        self.assertLess(a.id, d.id, 'fixture 前提：id 依建立順序遞增')

        # 游標在中間：從它之後開始，繞回開頭，最後才輪到游標指的那一台。
        self._set_cursor(b.id)
        self.assertEqual(self._polled(self._run_healthy()), self._names(c, d, a, b))
        self.assertEqual(self._cursor(), b.id, '查完整輪，游標回到原地')

        # 游標在最後一台：整輪從頭開始。
        self._set_cursor(d.id)
        self.assertEqual(self._polled(self._run_healthy()), self._names(a, b, c, d))

        # 游標指到已經不存在、比所有 id 都大的服務：當分界用，一樣從頭開始。
        self._set_cursor(d.id + 1000)
        self.assertEqual(self._polled(self._run_healthy()), self._names(a, b, c, d))
        self.assertEqual(self._cursor(), d.id)

        # 從沒跑過（0）：依 id 升冪。
        self._set_cursor(0)
        self.assertEqual(self._polled(self._run_healthy()), self._names(a, b, c, d))
        self.assertEqual(self._cursor(), d.id)

    def test_persistent_timeouts_do_not_starve_the_other_services(self):
        """兩台持續逾時：連跑三趟，其餘服務每一趟（第二趟起）都查得到。

        舊做法用「最後一次成功檢查的時間」排序——每次都失敗的服務永遠排最前面，連續 2 次
        傳輸錯誤的中止讓每一趟都死在同樣那兩台，後面的服務一台都輪不到。
        """
        self.assertEqual(WORKLOAD_CHECK_TRANSPORT_ABORT, 2, '本測試的呼叫序列以 2 為前提')
        down_a, down_b, ok_c, ok_d, ok_e = (self._service('starve-%s' % tag) for tag in 'abcde')
        down = (down_a, down_b)
        timeout = PaaSOperatorTimeoutError('timed out')

        with self.assertLogs(_MODEL_LOGGER, level=logging.WARNING):
            runs = [self._polled(self._run(self._client_failing_for(down, timeout)))
                    for _round in range(3)]

        self.assertEqual(runs[0], self._names(down_a, down_b), '第一趟：前兩台逾時就中止')
        self.assertEqual(self._cursor(), down_b.id, '中止時游標停在最後處理的那一台')
        expected = self._names(ok_c, ok_d, ok_e, down_a, down_b)
        self.assertEqual(runs[1], expected, '第二趟：從中止處之後開始，其餘三台先查')
        self.assertEqual(runs[2], expected, '第三趟：仍然查得到其餘三台')

    def test_one_failing_service_does_not_stop_the_others(self):
        good_a = self._service('good-a')
        bad = self._service('bad')
        good_b = self._service('good-b')

        with self.assertLogs(_MODEL_LOGGER, level=logging.INFO) as captured:
            client = self._run(self._client_failing_for(
                [bad], RuntimeError('operator exploded')))

        self.assertEqual(self._polled(client), self._names(good_a, bad, good_b),
                         '一台失敗不得中斷整趟')
        self.assertIn('本趟檢查 2 筆', self._summary(captured))
        self.assertIn('健康 2', self._summary(captured))
        self.assertIn('讀取失敗 1 筆', self._summary(captured))
        self.assertNotIn('提前中止', self._summary(captured))
        errors = [r for r in captured.records if r.levelno >= logging.ERROR]
        self.assertEqual(len(errors), 1, '沒預期到的例外要留 traceback')
        self.assertTrue(errors[0].exc_info)
        self.assertIn('id=%s' % bad.id, errors[0].getMessage())
        self.assertEqual(self._cursor(), good_b.id)

    def test_operator_api_error_is_one_warning_line_without_traceback(self):
        """operator 有回應但對這一台回錯（例：DB 是 running、叢集上 release 已不在 → 404）。

        那種服務每 10 分鐘都會回同樣的錯：一行 warning 就夠，不要每趟一份 traceback；
        也不算傳輸錯誤（operator 是通的），不得讓「連續傳輸錯誤」的中止提早發生。
        """
        gone_a = self._service('gone-a')
        gone_b = self._service('gone-b')
        fine = self._service('still-here')
        not_found = PaaSOperatorAPIError('Release not found', status_code=404)

        with self.assertLogs(_MODEL_LOGGER, level=logging.INFO) as captured:
            client = self._run(self._client_failing_for([gone_a, gone_b], not_found))

        self.assertEqual(self._polled(client), self._names(gone_a, gone_b, fine),
                         '連續兩台回 404 不是 operator 掛了：不中止')
        self.assertNotIn('提前中止', self._summary(captured))
        self.assertIn('本趟檢查 1 筆', self._summary(captured))
        self.assertIn('讀取失敗 2 筆', self._summary(captured))
        for gone in (gone_a, gone_b):
            lines = [r for r in captured.records if 'id=%s ' % gone.id in r.getMessage()]
            self.assertEqual(len(lines), 1, [r.getMessage() for r in lines])
            self.assertEqual(lines[0].levelno, logging.WARNING)
            self.assertFalse(lines[0].exc_info, '不帶 traceback')
            self.assertIn('404', lines[0].getMessage())
            self.assertIn('Release not found', lines[0].getMessage())
        self.assertFalse([r for r in captured.records if r.levelno >= logging.ERROR])
        self.assertEqual(self._cursor(), fine.id, '回錯的那兩台也算處理過')

    def test_budget_stops_the_run_and_the_next_run_resumes_after_it(self):
        a, b, c = (self._service('budget-%s' % tag) for tag in 'abc')
        client = operator_client(get_status=_status(workload_info()))
        # 第一次是起點、第二次是第一台之前；之後每次都已經超過預算。
        ticks = iter([0, 0])

        def _clock():
            return next(ticks, WORKLOAD_CHECK_BUDGET_SECONDS + 1)

        with patch('%s._workload_check_clock' % _MODEL_MODULE, new=_clock), \
                self.assertLogs(_MODEL_LOGGER, level=logging.INFO) as captured:
            self._run(client)

        self.assertEqual(self._polled(client), self._names(a), '第二台之前就超過預算')
        self.assertIn('running 3 筆', self._summary(captured))
        self.assertIn('本趟檢查 1 筆', self._summary(captured))
        self.assertIn('預算用完未檢查 2 筆', self._summary(captured))
        self.assertEqual(self._cursor(), a.id, '預算用完也要寫回游標')

        # 下一趟（預算夠）：從沒查到的那兩台開始，最後才回到已經查過的那一台。
        self.assertEqual(self._polled(self._run_healthy()), self._names(b, c, a))

    def test_two_consecutive_transport_errors_abort_the_run(self):
        services = [self._service('down-%s' % tag) for tag in ('a', 'b', 'c')]
        client = operator_client()
        client.get_status.side_effect = PaaSOperatorTimeoutError('timed out')

        with self.assertLogs(_MODEL_LOGGER, level=logging.WARNING) as captured:
            self._run(client)

        self.assertEqual(self._polled(client), self._names(*services[:2]),
                         'operator 掛了就不逐台等 timeout')
        self.assertTrue(any('本趟中止' in line for line in captured.output))
        for svc in services:
            self.assertEqual(svc.state, 'running')
            self.assertEqual(self._fault_values(svc), (False, False, False, False))
        self.assertEqual(self._cursor(), services[1].id, '中止也要寫回游標')

        # operator 恢復後的下一趟：從中止處之後開始。
        self.assertEqual(self._polled(self._run_healthy()),
                         self._names(services[2], services[0], services[1]))

    def test_a_success_between_transport_errors_resets_the_streak(self):
        services = [self._service('flaky-%s' % tag) for tag in ('a', 'b', 'c', 'd')]
        client = operator_client()
        client.get_status.side_effect = [
            PaaSOperatorTimeoutError('timed out'), _status(workload_info()),
            PaaSOperatorTimeoutError('timed out'), _status(workload_info()),
        ]

        with self.assertLogs(_MODEL_LOGGER, level=logging.INFO) as captured:
            self._run(client)

        self.assertEqual(self._polled(client), self._names(*services), '不連續的傳輸錯誤不中止')
        self.assertIn('本趟檢查 2 筆', self._summary(captured))
        self.assertIn('讀取失敗 2 筆', self._summary(captured))
        self.assertNotIn('提前中止', self._summary(captured))

    def test_legacy_operator_aborts_after_the_first_service(self):
        """舊 operator 對每一台的回答都一樣：第一台之後就不必再打。"""
        services = [self._service('legacy-%s' % tag) for tag in ('a', 'b', 'c')]
        client = operator_client(get_status=_legacy_status())

        with self.assertLogs(_MODEL_LOGGER, level=logging.INFO) as captured:
            self._run(client)

        self.assertEqual(self._polled(client), self._names(services[0]))
        self.assertTrue(any('operator 尚未回報 workloads' in line for line in captured.output))
        for svc in services:
            self.assertFalse(svc.workload_checked_at)
            self.assertEqual(self._fault_values(svc), (False, False, False, False))
        self.assertFalse(self._activities())
        self.assertEqual(self._cursor(), services[0].id, '舊 operator 中止也要寫回游標')

        # 之後每一趟各問下一台，問完一輪繞回第一台——不是每趟都問同一台。
        asked = []
        for _round in range(3):
            with self.assertLogs(_MODEL_LOGGER, level=logging.INFO):
                asked += self._polled(
                    self._run(operator_client(get_status=_legacy_status())))
        self.assertEqual(asked, self._names(services[1], services[2], services[0]))

    def test_skipped_service_still_advances_the_cursor(self):
        """輪到時已經離開 running 的服務被略過——也算處理過，游標照樣推進。"""
        first = self._service('skip-first')
        leaving = self._service('skip-leaving')
        client = operator_client()

        def _answer(namespace, release_name, include_vm=False, include_workloads=False):
            # 第一台檢查到一半，第二台被別的交易改成 upgrading（例：租戶按了重新啟動）。
            leaving.write({'state': 'upgrading'})
            return _status(workload_info())

        client.get_status.side_effect = _answer

        with self.assertLogs(_MODEL_LOGGER, level=logging.INFO) as captured:
            self._run(client)

        self.assertEqual(self._polled(client), self._names(first), '離開 running 的不打 operator')
        self.assertIn('本趟檢查 1 筆', self._summary(captured))
        self.assertEqual(self._cursor(), leaving.id)

    def test_cursor_write_failure_does_not_fail_the_cron(self):
        """游標只是「下一趟從哪裡開始」的提示：寫不進去只記 warning，cron 不拋錯。"""
        services = [self._service('cursor-%s' % tag) for tag in ('a', 'b')]
        monitor_cls = type(self.Monitor)
        original = monitor_cls.write
        attempts = []

        def _boom(records, vals):
            if 'workload_check_cursor' in vals:
                attempts.append(dict(vals))
                raise RuntimeError('monitor row is busy')
            return original(records, vals)

        with patch.object(monitor_cls, 'write', _boom), \
                self.assertLogs(_MODEL_LOGGER, level=logging.INFO) as captured:
            client = self._run_healthy()

        self.assertEqual(attempts, [{'workload_check_cursor': services[-1].id}],
                         '前提：確實試著寫回游標，而且只試一次')
        self.assertEqual(self._polled(client), self._names(*services))
        warnings = [r for r in captured.records
                    if r.levelno == logging.WARNING and '游標寫回失敗' in r.getMessage()]
        self.assertEqual(len(warnings), 1)
        self.assertIn('本趟檢查 2 筆', self._summary(captured), '結尾那行 INFO 照印')
        self.assertEqual(self._cursor(), 0, '沒寫進去：下一趟從舊游標接著查')

    def test_no_client_or_no_services_is_quiet(self):
        # 沒有服務：不取 client、不拋錯、不動游標。
        client = operator_client()
        self._run(client)
        client.get_status.assert_not_called()
        # 有服務但 operator 沒設定：不拋錯、什麼都不寫。
        svc = self._service('noclient')
        self._rewind_write_date(svc)
        with self._service_writes() as calls, \
                self.assertLogs(_MODEL_LOGGER, level=logging.INFO) as captured:
            self._run(None)
        self.assertTrue(any('operator client 未設定' in line for line in captured.output))
        self._assert_untouched(svc, calls)
        self.assertEqual(self._cursor(), 0)

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
            self._run_healthy()

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

    def _workload_writes(self, calls):
        """spy 記下的 write 裡，動到 workload 觀測欄位的那幾次。"""
        return [(ids, keys) for ids, keys in calls
                if any(key.startswith('workload_') for key in keys)]

    def test_reconcile_asks_for_workloads(self):
        svc = self._service('ask')
        client = self._reconcile(operator_client(
            get_status=_status(workload_info(), release=self._RELEASE)))

        kwargs = client.get_status.call_args.kwargs
        self.assertIs(kwargs.get('include_workloads'), True)
        self.assertEqual(kwargs.get('release_name'), svc.helm_release_name)

    def test_reconcile_writes_no_workload_field_when_nothing_changed(self):
        """每日對帳的順帶回寫同樣「值沒變不寫」：健康的服務、內容沒變的異常都不碰 workload 欄位。

        對帳自己每天那一次 release 事實的 write 照常（前提斷言），所以這裡數的是「哪幾次 write
        帶了 workload 欄位」，不是 write 的總次數。
        """
        healthy = self._service('daily-healthy')
        faulty = self._service('daily-faulty')

        def _client():
            client = operator_client()

            def _answer(namespace, release_name, include_vm=False, include_workloads=False):
                if release_name == faulty.helm_release_name:
                    return _status(_quota_blocked(), release=self._RELEASE)
                return _status(workload_info(), release=self._RELEASE)

            client.get_status.side_effect = _answer
            return client

        # 第一輪：異常的那一台第一次被看到 ⇒ 寫；健康的那一台不寫 workload 欄位。
        with self._service_writes() as calls:
            self._reconcile(_client())
        self.assertEqual(faulty.workload_fault_code, 'quota_exceeded')
        self.assertTrue(faulty.workload_fault_alerted_at)
        touched = {ids for ids, _keys in self._workload_writes(calls)}
        self.assertEqual(touched, {(faulty.id,)}, '健康的服務不得被寫 workload 欄位')
        self.assertFalse(healthy.workload_checked_at)
        snapshot = (self._fault_values(faulty), faulty.workload_checked_at)

        # 第二輪：兩台的觀測都沒變 ⇒ 一次帶 workload 欄位的 write 都沒有。
        with self._service_writes() as calls, \
                self.assertLogs(_MODEL_LOGGER, level=logging.INFO) as captured:
            self._reconcile(_client())
        synced = {ids for ids, keys in calls if 'last_release_sync_at' in keys}
        self.assertEqual(synced, {(healthy.id,), (faulty.id,)},
                         '前提：release 事實那一次 write 兩台都照常發生')
        self.assertEqual(self._workload_writes(calls), [])
        self.assertEqual((self._fault_values(faulty), faulty.workload_checked_at), snapshot)
        self.assertFalse(healthy.workload_checked_at)
        observed = [line for line in captured.output
                    if 'workload-observation (daily reconcile)' in line]
        self.assertIn('持續中 1', observed[0])
        self.assertIn('健康 1', observed[0])

    def test_reconcile_does_not_touch_the_rotation_cursor(self):
        """輪替游標是 10 分鐘 cron 自己的：每日對帳不讀也不寫。"""
        self._service('daily-cursor')
        self.singleton.write({'workload_check_cursor': 7})

        self._reconcile(operator_client(
            get_status=_status(workload_info(), release=self._RELEASE)))

        self.singleton.invalidate_recordset(['workload_check_cursor'])
        self.assertEqual(self.singleton.workload_check_cursor, 7)

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


# ====================================================================== #
#  /woow：其他以 state 為準的訊號                                            #
# ====================================================================== #

def _read_paas_source(*parts):
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'static', 'src', 'paas', *parts)
    with open(path, encoding='utf-8') as handle:
        return handle.read()


@tagged('post_install', '-at_install')
class TestWorkloadFaultFrontendSignals1647(BaseCase):
    """異常已對租戶確認時，同一個畫面上其他的訊號不可以再說它正常（瀏覽器實走 F1）。

    實走看到的是：banner 說「This service is not running right now」，下面 Connection Info
    是綠色的 ONLINE，工作區頁統計卡是 Healthy 1／Alerts 0——這兩處原本只看 ``state``。

    這裡是讀原始碼的靜態檢查（基底用 ``BaseCase``，不碰資料庫）：守「元件有接上 helper」與
    「helper 用的是 banner 那一個判準」。行為（哪些輸入得到哪個結果）由
    ``tests/ui/test_service_presentation_workload_1647.mjs`` 把 getter 本體切出來執行，但
    ``tests/ui/*.mjs`` 沒有任何 CI 會跑，所以接線要在這裡守一份。

    🔴 狀態徽章不在此列：它跟著 ``state`` 走、維持 Running 是 #1647 明定的設計。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.presentation = _read_paas_source('utils', 'service_presentation.js')
        cls.overview_js = _read_paas_source('pages', 'service', 'tabs', 'OverviewTab.js')
        cls.overview_xml = _read_paas_source('pages', 'service', 'tabs', 'OverviewTab.xml')
        cls.workspace_js = _read_paas_source('pages', 'workspace', 'WorkspaceDetailPage.js')

    def _getter(self, source, name):
        match = re.search(r'get %s\(\)\s*\{(.*?)\n    \}' % name, source, re.S)
        self.assertTrue(match, '找不到 %s getter' % name)
        return match.group(1)

    def _function(self, name):
        match = re.search(
            r'export function %s\(\w+\)\s*\{(.*?)\n\}' % name, self.presentation, re.S)
        self.assertTrue(match, 'service_presentation.js 找不到 export function %s' % name)
        return match.group(1)

    def _imported_from_presentation(self, source):
        match = re.search(
            r'import\s*\{([^}]*)\}\s*from\s*"[./]*/utils/service_presentation";', source)
        self.assertTrue(match, '沒有從 utils/service_presentation import 任何東西')
        return {name.strip() for name in match.group(1).split(',') if name.strip()}

    def test_connection_status_is_not_online_while_a_fault_is_confirmed(self):
        self.assertIn('hasConfirmedWorkloadFault',
                      self._imported_from_presentation(self.overview_js))
        body = self._getter(self.overview_js, 'connectionStatus')
        self.assertRegex(
            body,
            r'if \(state === "running"\) \{\s*'
            r'return hasConfirmedWorkloadFault\(this\.props\.service\) \? "offline" : "online";',
            'running 但異常已確認時要回 offline（與 banner 相符的既有狀態）')
        self.assertNotRegex(body, r'state === "running"\)\s*return "online"',
                            'running 不可以無條件回 online')

    def test_connection_status_keeps_the_three_existing_kinds(self):
        """不為了 workload 異常新增 state：getter 回的與模板認得的都還是原本三種。"""
        body = self._getter(self.overview_js, 'connectionStatus')
        returned = set(re.findall(r'"([a-z_]+)"\s*[;:]', body))
        self.assertEqual(returned, {'online', 'updating', 'offline'}, body)
        self.assertEqual(
            set(re.findall(r"connectionStatus === '(\w+)'", self.overview_xml)),
            {'online', 'updating'}, '模板只分 online／updating，其餘一律 OFFLINE')
        self.assertNotIn('workload_fault', self.overview_xml,
                         '模板不自己判斷，連線狀態一律走 connectionStatus')

    def test_workspace_stats_count_a_confirmed_fault_as_an_alert(self):
        self.assertIn('countServiceHealth',
                      self._imported_from_presentation(self.workspace_js))
        body = self._getter(self.workspace_js, 'computedStats')
        self.assertIn(
            'const { healthy: runningCount, alerts: errorCount } = countServiceHealth(services);',
            body)
        self.assertNotIn('.state ===', body, '統計卡不可以自己只數 state')
        self.assertRegex(body, r'_t\("Healthy"\), value: String\(runningCount\)')
        self.assertRegex(body, r'_t\("Alerts"\), value: String\(errorCount\)')

    def test_helpers_use_the_same_rule_as_the_banner(self):
        """只讀 payload 的 ``workload_fault``（後端已確認才給值），前端不另算一份。"""
        rule = re.search(
            r'\nfunction getConfirmedWorkloadFault\(service\)\s*\{(.*?)\n\}',
            self.presentation, re.S)
        self.assertTrue(rule, '找不到 getConfirmedWorkloadFault')
        self.assertIn('service?.state !== "running"', rule.group(1))
        self.assertIn('service.workload_fault', rule.group(1))
        for name in ('getWorkloadFaultText', 'getWorkloadFaultLabel',
                     'hasConfirmedWorkloadFault'):
            with self.subTest(function=name):
                self.assertIn('getConfirmedWorkloadFault(service)', self._function(name))

        count = self._function('countServiceHealth')
        fault = count.find('hasConfirmedWorkloadFault(service)')
        running = count.find('service?.state === "running"')
        self.assertGreaterEqual(fault, 0, 'countServiceHealth 要用 hasConfirmedWorkloadFault')
        self.assertGreater(running, fault,
                           '先看異常、再看 running：否則異常的服務會先被算成 Healthy')
        self.assertNotIn('workload_fault', count.replace('hasConfirmedWorkloadFault', ''),
                         'countServiceHealth 不自己讀 workload_fault')

    def test_status_badge_still_follows_state(self):
        """狀態徽章維持跟著 state（異常時仍是 Running）——本票明定，不因為這次修正而改。"""
        for parts, expected in (
                (('pages', 'service', 'ServiceDetailPage.xml'),
                 '<StatusBadge status="service.state"'),
                (('components', 'service-card', 'ServiceCard.xml'),
                 '<StatusBadge status="props.service.state"')):
            with self.subTest(file=parts[-1]):
                source = _read_paas_source(*parts)
                badges = re.findall(r'<StatusBadge\b[^>]*>', source)
                self.assertEqual(len(badges), 1, badges)
                self.assertTrue(badges[0].startswith(expected), badges[0])
                self.assertNotIn('workload', badges[0].lower())


# ====================================================================== #
#  譯文                                                                    #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestWorkloadFaultTranslations1647(TransactionCase):
    """新字串的 zh_TW 譯文（review detect-S2）。

    判準用 Odoo 自己的工具：term 由 ``xml_translate`` 從**實際的 view arch** 抽，po 由
    ``PoFileReader`` 讀——runtime 載入譯文時用的就是這兩樣。手抄 msgid 的話，抽取規則一猜錯
    （例：``<strong>`` 會連標籤一起成為 term、``<field/>`` 後面的文字各自成為一個 term）
    測試照樣綠，而畫面上是半中半英。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        po = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'i18n', 'zh_TW.po')
        cls.entries = list(PoFileReader(po))

    def _translations(self, entry_type, name, imd_name):
        return {e['src']: e['value'] for e in self.entries
                if e['type'] == entry_type and e.get('name') == name
                and e.get('imd_name') == imd_name}

    def _alert_terms(self):
        """後台表單那塊 workload 警示（含它裡面所有可見文字）被 Odoo 拆成的翻譯 term。"""
        view = self.env.ref('woow_paas_platform.view_cloud_service_form')
        arch = etree.fromstring(view.with_context(lang='en_US').arch_db)
        alerts = arch.xpath(
            "//div[@role='alert'][.//field[@name='workload_fault_detail']]")
        self.assertEqual(len(alerts), 1, 'fixture 前提：表單上找得到那一塊 workload 警示')
        terms = []

        def _collect(term):
            terms.append(term)
            return None

        xml_translate(_collect, etree.tostring(alerts[0], encoding='unicode'))
        return terms

    def test_every_visible_text_of_the_backend_alert_is_translated(self):
        terms = self._alert_terms()
        # 陽性對照：抽取真的有抓到東西，而且包含 <field/> 之間的那兩段文字。
        self.assertGreaterEqual(len(terms), 3, terms)
        self.assertTrue(any(term.startswith('<strong>') for term in terms), terms)
        self.assertTrue([term for term in terms if not term.startswith('<')], terms)
        translated = self._translations(
            'model_terms', 'ir.ui.view,arch_db', 'view_cloud_service_form')
        for term in terms:
            with self.subTest(term=term):
                self.assertTrue(translated.get(term),
                                'zh_TW.po 缺這一段的譯文（msgid 必須與抽出的 term 完全相同）')

    def test_backend_alert_renders_fully_in_traditional_chinese(self):
        """把譯文套回去之後，警示裡不該還剩英文的句子片段。"""
        view = self.env.ref('woow_paas_platform.view_cloud_service_form')
        arch = etree.fromstring(view.with_context(lang='en_US').arch_db)
        alert = arch.xpath("//div[@role='alert'][.//field[@name='workload_fault_detail']]")[0]
        translated = self._translations(
            'model_terms', 'ir.ui.view,arch_db', 'view_cloud_service_form')

        rendered = etree.fromstring(xml_translate(
            translated.get, etree.tostring(alert, encoding='unicode')))

        text = ' '.join(''.join(rendered.itertext()).split())
        for english in ('This service', 'first seen', 'last change recorded', 'since',
                        'last checked', 'rejected', 'are missing', 'other than'):
            self.assertNotIn(english, text)
        self.assertIn('首次觀測於', text)
        self.assertIn('觀測結果最後寫入於', text)
        self.assertIn('拒絕建立 Pod', text)

    def test_new_field_labels_and_help_are_translated(self):
        fields_with_help = (
            ('cloud_service', 'workload_checked_at'),
            ('platform_monitor', 'workload_check_cursor'),
        )
        for model, field in fields_with_help:
            record = self.env['woow_paas_platform.%s' % model]
            imd_name = 'field_woow_paas_platform_%s__%s' % (model, field)
            definition = record._fields[field]
            for name, source in (('ir.model.fields,field_description', definition.string),
                                 ('ir.model.fields,help', definition.help)):
                with self.subTest(field=field, part=name):
                    self.assertTrue(source, 'fixture 前提：欄位有 string 與 help')
                    self.assertTrue(
                        self._translations('model', name, imd_name).get(source),
                        'zh_TW.po 缺譯文，或 msgid 與欄位定義的文字不一致：%r' % source)

    #: 後台列表徽章標籤的寬度上限，單位是半形字（全形字算 2 格）。
    #:
    #: 列表用 badge 顯示這一格，欄寬由列表自動分配，標籤太長會被截斷而且沒有 tooltip
    #: （瀏覽器實走 F5）。1440px 實測：30 格的「Blocked by the namespace quota」、23 格的
    #: 「No pods (cause unknown)」、21 格的「Pod creation rejected」、20 格的「被 namespace
    #: 配額擋下」都被截斷；13 格的「Quota blocked」（徽章 105px、欄寬 115px）與 8 格的中文
    #: 標籤放得下。上限取欄名「Workload Fault」的字數 14——比它長的沒有量過放不放得下。
    #:
    #: ⚠ 這是 1440px 的結論。視窗更窄（實測 1280px 以下）時列表會把這一欄壓到最小寬度，
    #: 13 格的英文標籤仍然被截（欄名自己也被截）；那要靠欄位固定寬度，不是更短的標籤。
    _BADGE_LABEL_MAX_COLUMNS = len('Workload Fault')

    @staticmethod
    def _columns(text):
        return sum(2 if unicodedata.east_asian_width(char) in 'WF' else 1 for char in text)

    def _fault_code_labels(self):
        """``[(代碼, 英文標籤, zh_TW 標籤)]``；譯文缺席時第三格是 None。"""
        field = self.env['woow_paas_platform.cloud_service']._fields['workload_fault_code']
        self.assertEqual({value for value, _label in field.selection}, set(FAULT_CODES),
                         'selection 必須正好是 workload_health 的全部代碼（#1647 三個＋#1456 三個）')
        self.assertEqual(len(FAULT_CODES), 6, 'fixture 前提：代碼數')
        labels = []
        for value, label in field.selection:
            imd_name = ('selection__woow_paas_platform_cloud_service__workload_fault_code__%s'
                        % value)
            translated = self._translations(
                'model', 'ir.model.fields.selection,name', imd_name)
            labels.append((value, label, translated.get(label)))
        return labels

    def test_fault_code_labels_fit_the_list_badge(self):
        """列表徽章的標籤（英文與 zh_TW）都有譯文，而且短到放得下。"""
        for value, english, chinese in self._fault_code_labels():
            with self.subTest(code=value):
                self.assertTrue(chinese,
                                'zh_TW.po 缺譯文，或 msgid 與 selection 標籤不一致：%r' % english)
                for label in (english, chinese):
                    self.assertLessEqual(
                        self._columns(label), self._BADGE_LABEL_MAX_COLUMNS,
                        '標籤太長，列表的徽章會被截斷：%r' % label)

    def test_fault_code_help_explains_every_label(self):
        """標籤改短之後，欄位 help（英文與 zh_TW）要逐一說明每個標籤的意思。"""
        field = self.env['woow_paas_platform.cloud_service']._fields['workload_fault_code']
        chinese_help = self._translations(
            'model', 'ir.model.fields,help',
            'field_woow_paas_platform_cloud_service__workload_fault_code').get(field.help)
        self.assertTrue(chinese_help, 'zh_TW.po 缺 help 的譯文，或 msgid 與欄位定義不一致')
        for value, english, chinese in self._fault_code_labels():
            with self.subTest(code=value):
                self.assertIn('%s: ' % english, field.help)
                self.assertIn('%s：' % chinese, chinese_help)

    def test_backend_alert_explains_every_fault_code(self):
        """表單警示裡每個代碼各有一行完整說明（只在該代碼時顯示），而且有譯文。

        列表的標籤短，管理員在表單上要讀得到「這個代碼是什麼意思」；新增代碼卻沒補這一行，
        表單上就只剩一個看不出意思的短標籤。
        """
        view = self.env.ref('woow_paas_platform.view_cloud_service_form')
        arch = etree.fromstring(view.with_context(lang='en_US').arch_db)
        alert = arch.xpath("//div[@role='alert'][.//field[@name='workload_fault_detail']]")[0]
        translated = self._translations(
            'model_terms', 'ir.ui.view,arch_db', 'view_cloud_service_form')
        explanations = []
        for value, english, _chinese in self._fault_code_labels():
            lines = alert.xpath('./div[@invisible="workload_fault_code != \'%s\'"]' % value)
            with self.subTest(code=value):
                self.assertEqual(len(lines), 1, '表單警示要有這個代碼專屬的一行說明')
                text = ' '.join(''.join(lines[0].itertext()).split())
                self.assertGreater(len(text), 2 * len(english),
                                   '這一行要是完整的說明，不是把短標籤再寫一次：%r' % text)
                self.assertTrue(translated.get(text), 'zh_TW.po 缺這一行的譯文：%r' % text)
                explanations.append(text)
        self.assertEqual(len(set(explanations)), len(explanations), '每個代碼的說明各不相同')

    def _tenant_fault_strings(self):
        """/woow 的 workload 異常文案（詳情頁 banner＋服務卡片）在 JS 裡的 `_t()` 原文。"""
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'static', 'src', 'paas', 'utils', 'service_presentation.js')
        with open(path, encoding='utf-8') as handle:
            source = handle.read()
        strings = [text for text in re.findall(r'_t\(\s*"((?:[^"\\]|\\.)*)"', source)
                   if 'not running' in text.lower()]
        # 陽性對照：至少抽到配額、一般、卡片提示三句（下限，不釘死句數），且不需要反跳脫。
        self.assertGreaterEqual(len(strings), 3, strings)
        self.assertFalse([text for text in strings if '\\' in text], strings)
        return strings

    def _code_translations(self):
        return {e['src']: e['value'] for e in self.entries if e['type'] == 'code'}

    def test_tenant_fault_strings_are_translated(self):
        translated = self._code_translations()
        for text in self._tenant_fault_strings():
            with self.subTest(text=text):
                self.assertTrue(translated.get(text),
                                'zh_TW.po 缺譯文，或 msgid 與 JS 裡的文字不一致')

    def test_quota_text_only_points_the_tenant_to_support(self):
        """配額擋下的租戶文案（英文與 zh_TW）只請租戶聯絡客服。

        不得建議刪除／停用／停止服務或改小方案來釋出 CPU 或記憶體配額：工作區的上限是存活
        服務的方案規格加總 × 3（另有下限，``_compute_ns_quota``），刪掉一台上限少 3 份、用量
        只少約 1 份，剩餘空間通常變小而不是變大。租戶做得到又有效的只有聯絡客服。
        """
        quota = [text for text in self._tenant_fault_strings() if 'quota' in text.lower()]
        self.assertTrue(quota, 'fixture 前提：找得到配額那一句')
        translated = self._code_translations()
        for english in quota:
            chinese = translated.get(english) or ''
            with self.subTest(text=english):
                self.assertIn('contact support', english)
                self.assertNotRegex(
                    english,
                    r'(?i)\b(delet\w*|remov\w*|stop\w*|paus\w*|suspend\w*|disabl\w*'
                    r'|shut\w* down|turn\w* off|downgrad\w*|smaller|free\w* up)\b')
                self.assertIn('聯絡客服', chinese)
                for word in ('刪除', '移除', '停用', '停止', '暫停', '關閉', '降級', '較小'):
                    self.assertNotIn(word, chinese)

    def test_quota_text_says_not_enough_left_rather_than_full(self):
        """配額擋下的租戶文案（英文與 zh_TW）說「剩餘配額不足以啟動」，不說配額已滿。

        被擋下的常見形狀是「還有剩，但不夠這台啟動」（事故：上限 12、其他 workload 佔 8、
        這台要 8）。寫成已滿／用完的話，同一個租戶在工作區頁的配額面板看到的是「還剩
        4 vCPU」，兩邊對不起來（瀏覽器實走 F2）。
        """
        quota = [text for text in self._tenant_fault_strings() if 'quota' in text.lower()]
        self.assertTrue(quota, 'fixture 前提：找得到配額那一句')
        translated = self._code_translations()
        for english in quota:
            chinese = translated.get(english) or ''
            with self.subTest(text=english):
                self.assertNotRegex(
                    english, r'(?i)\b(full|used up|exhausted|ran out|run out|no\b.*\bleft)\b')
                self.assertRegex(english, r'not have enough\b.*\bquota left to start')
                for word in ('已滿', '滿了', '用完', '用盡', '耗盡'):
                    self.assertNotIn(word, chinese)
                self.assertIn('配額不足以啟動', chinese)


# ====================================================================== #
#  #1456：Pod 都在、卻沒有一顆就緒（CrashLoopBackOff／OOMKilled／未就緒）      #
# ====================================================================== #
#
# 商用驗收 2026-10-16：n8n 250 連續被殺進 CrashLoopBackOff（80–110 秒無法就緒）、Code 節點
# 配 3000 MB 被 OOMKilled，平台 ``state`` 全程 running、/woow 一直「線上」
# （DATA-N8N-009-1／010-1）。#1647 只認「期望 ≥1、實際 0」，Pod 在但沒有一顆 ready 不算。

_CRASH_WAITING = {'container': 'n8n', 'init_container': False, 'reason': 'CrashLoopBackOff',
                  'message': 'back-off 1m20s restarting failed container=n8n'}


def _unready(**overrides):
    """期望 1、Pod 1 顆、就緒 0 顆的 Recreate Deployment。"""
    return workload_info(**{
        'name': 'svc-1456-n8n', 'replicas': 1, 'ready_replicas': 0, 'updated_replicas': 1,
        'strategy': 'Recreate', **overrides})


def _unready_pod(**overrides):
    """那顆沒有就緒的 Pod（ReplicaSet 建的，平台認得它是 workload 的 Pod）。"""
    return pod_info(**{
        'name': 'svc-1456-n8n-7c9d8b6f5-x2k4q', 'ready': '0/1', 'restarts': 5,
        'owner_kind': 'ReplicaSet', **overrides})


@tagged('post_install', '-at_install')
class TestWorkloadNotReadyVerdict1456(BaseCase):
    """純函式：``workload_verdict`` 的第二類異常（Pod 在、沒有一顆就緒）。"""

    def _verdict(self, status, now=_NOW):
        return workload_verdict(status, now)

    def test_pods_exist_but_none_is_ready(self):
        verdict, code, detail = self._verdict(_status(_healthy_statefulset(), _unready()))
        self.assertEqual((verdict, code), (VERDICT_FAULT, FAULT_NOT_READY))
        self.assertIn('Deployment svc-1456-n8n: 0 of 1 pods ready, 1 exist (Recreate)', detail)
        self.assertNotIn('svc-1647-postgresql', detail, '健康的 workload 不點名')

    def test_crash_loop_from_the_pod_waiting_reason(self):
        status = _status(_unready(), pods=[_unready_pod(waiting=_CRASH_WAITING)])
        verdict, code, detail = self._verdict(status)
        self.assertEqual((verdict, code), (VERDICT_FAULT, FAULT_CRASH_LOOP))
        self.assertIn('svc-1456-n8n-7c9d8b6f5-x2k4q', detail)
        self.assertIn('CrashLoopBackOff', detail)
        self.assertIn('restarts 5', detail)

    def test_oom_killed_wins_over_crash_loop(self):
        """記憶體不足造成的反覆重啟：報 OOMKilled（處理方式是調方案／limits，不是查程式）。"""
        status = _status(_unready(), pods=[_unready_pod(
            waiting=_CRASH_WAITING, last_terminated_reason='OOMKilled')])
        verdict, code, detail = self._verdict(status)
        self.assertEqual((verdict, code), (VERDICT_FAULT, FAULT_OOM_KILLED))
        self.assertIn('last terminated: OOMKilled', detail)
        self.assertIn('CrashLoopBackOff', detail)

    def test_only_unready_workload_pods_are_evidence(self):
        """證據只從「沒就緒、沒在收尾、由 ReplicaSet／StatefulSet 建的 Pod」來。"""
        not_evidence = [
            # 就緒的 Pod（另一個 workload 的）帶著舊的 OOMKilled。
            _unready_pod(name='svc-1456-db-0', ready='1/1', owner_kind='StatefulSet',
                         last_terminated_reason='OOMKilled'),
            # 收尾中的舊 Pod。
            _unready_pod(name='svc-1456-old', waiting=_CRASH_WAITING,
                         deletion_timestamp='2026-10-16T01:00:00Z'),
            # Job 的 Pod（備份、init）：不是服務本身。
            _unready_pod(name='svc-1456-backup-x', owner_kind='Job',
                         waiting=_CRASH_WAITING, last_terminated_reason='OOMKilled'),
            # 認不出 owner 的 Pod（舊 operator 沒有身分欄位）：不猜。
            _unready_pod(name='svc-1456-bare', owner_kind=None, waiting=_CRASH_WAITING),
            # 形狀不對的元素略過。
            'not-a-pod', None, _unready_pod(name='svc-1456-weird', ready=None,
                                            waiting='CrashLoopBackOff'),
        ]
        verdict, code, detail = self._verdict(_status(_unready(), pods=not_evidence))
        self.assertEqual((verdict, code), (VERDICT_FAULT, FAULT_NOT_READY))
        for name in ('svc-1456-db-0', 'svc-1456-old', 'svc-1456-backup-x', 'svc-1456-bare'):
            self.assertNotIn(name, detail)

    def test_legacy_operator_without_the_new_pod_key(self):
        """舊 operator 的 Pod 沒有 ``last_terminated_reason``：CrashLoopBackOff 仍看得到。"""
        crash = _unready_pod(waiting=_CRASH_WAITING, last_terminated_reason=ABSENT)
        self.assertNotIn('last_terminated_reason', crash, 'fixture 前提：key 缺席')
        self.assertEqual(self._verdict(_status(_unready(), pods=[crash]))[1], FAULT_CRASH_LOOP)
        quiet = _unready_pod(last_terminated_reason=ABSENT)
        self.assertEqual(self._verdict(_status(_unready(), pods=[quiet]))[1], FAULT_NOT_READY)

    def test_healthy_when_at_least_one_pod_is_ready_or_none_is_wanted(self):
        cases = {
            'one of two ready': _unready(desired_replicas=2, replicas=2, ready_replicas=1),
            'scaled to zero': _unready(desired_replicas=0, replicas=0),
            'ready': workload_info(),
            # 縮到 0 的途中：期望 0、舊 Pod 還在收尾。
            'scaling down': _unready(desired_replicas=0, replicas=1),
        }
        for label, workload in cases.items():
            with self.subTest(case=label):
                status = _status(workload, pods=[_unready_pod(waiting=_CRASH_WAITING)])
                self.assertEqual(self._verdict(status), (VERDICT_HEALTHY, None, ''))

    def test_no_pods_takes_precedence(self):
        """同時有「一顆都沒有」與「沒有一顆就緒」：沿用 #1647 的代碼（中斷最徹底的那一種）。"""
        status = _status(_quota_blocked(), _unready(),
                         pods=[_unready_pod(waiting=_CRASH_WAITING)])
        self.assertEqual(self._verdict(status)[1], FAULT_QUOTA)
        self.assertEqual(self._verdict(_status(_blocked(), _unready()))[1], FAULT_NO_PODS)

    def test_malformed_ready_counts_are_skipped(self):
        garbage = [_unready(ready_replicas=None), _unready(ready_replicas='zero')]
        self.assertEqual(self._verdict(_status(*garbage)), (VERDICT_HEALTHY, None, ''))

    def test_new_codes_need_a_second_sighting(self):
        """門檻：三個新代碼都要「首次看到滿 5 分鐘後又看到一次」才確認（不是第一次就確認）。

        running 服務每 10 分鐘才看一次；第一次就確認的話，一次剛好撞上的重啟（單次 OOM 約
        35 秒、CrashLoopBackOff 的一次 back-off）會對租戶顯示「沒有在執行」直到下一趟——
        最多 10 分鐘。要改成第一次就確認，把代碼加進 ``EVIDENCE_CODES`` 即可。
        """
        for code in (FAULT_CRASH_LOOP, FAULT_OOM_KILLED, FAULT_NOT_READY):
            with self.subTest(code=code):
                self.assertIn(code, FAULT_CODES)
                self.assertNotIn(code, EVIDENCE_CODES)

    def test_detail_is_truncated(self):
        pods = [_unready_pod(name='svc-1456-p%02d' % i, waiting=_CRASH_WAITING)
                for i in range(40)]
        verdict, _code, detail = self._verdict(_status(_unready(), pods=pods))
        self.assertEqual(verdict, VERDICT_FAULT)
        self.assertLessEqual(len(detail), DETAIL_LIMIT)


@tagged('post_install', '-at_install')
class TestWorkloadNotReadyObservation1456(_WorkloadCase):
    """model 層：新代碼走 #1647 同一條寫欄位／確認／通知／恢復的路，不改 state。"""

    def setUp(self):
        super().setUp()
        self.assertTrue(self.Monitor._alert_recipients()[1],
                        'fixture 前提：至少一位 active 的系統管理員')

    def _crash_status(self):
        return _status(_unready(), pods=[_unready_pod(
            waiting=_CRASH_WAITING, last_terminated_reason='OOMKilled')])

    def _age_first_sight(self, svc, minutes):
        first_seen = fields.Datetime.now() - timedelta(minutes=minutes)
        svc.write({'workload_fault_since': first_seen, 'workload_checked_at': first_seen})
        return first_seen

    def test_confirmed_on_a_later_sighting_then_alerted_and_shown(self):
        svc = self._service('oom')

        self.assertEqual(svc._apply_workload_observation(self._crash_status()), 'raised')
        self.assertEqual(svc.workload_fault_code, FAULT_OOM_KILLED)
        self.assertIn('OOMKilled', svc.workload_fault_detail)
        self.assertFalse(svc._workload_fault_confirmed(), '第一次看到先不確認')
        self.assertIsNone(svc._workload_fault_public())
        self.assertFalse(svc.workload_fault_alerted_at)
        self.assertFalse(self._activities())

        self._age_first_sight(svc, WORKLOAD_FAULT_CONFIRM_MINUTES + 1)
        self.assertEqual(svc._apply_workload_observation(self._crash_status()), 'ongoing')

        self.assertTrue(svc._workload_fault_confirmed())
        self.assertEqual(svc._workload_fault_public(), FAULT_OOM_KILLED)
        self.assertTrue(svc.workload_fault_alerted_at)
        activities = self._activities()
        self.assertTrue(activities)
        for activity in activities:
            self.assertIn('id=%s' % svc.id, activity.note)
            self.assertIn('OOMKilled', activity.note)
        self.assertEqual(svc.state, 'running', '不改 state')
        self.assertFalse(svc.error_message)

    def test_a_single_glimpse_recovers_silently(self):
        """一次剛好撞上的重啟：下一趟已恢復 ⇒ 安靜清掉，沒有通知、沒有對租戶顯示。"""
        svc = self._service('glimpse-1456')
        svc._apply_workload_observation(_status(_unready(), pods=[
            _unready_pod(waiting=_CRASH_WAITING)]))
        self.assertEqual(svc.workload_fault_code, FAULT_CRASH_LOOP, 'fixture 前提')

        self.assertEqual(svc._apply_workload_observation(_status(workload_info())), 'cleared')
        self.assertEqual(self._fault_values(svc), (False, False, False, False))
        self.assertFalse(self._activities())

    def test_recovery_after_an_alert_is_noted(self):
        svc = self._service('recover-1456')
        svc._apply_workload_observation(_status(_unready()))
        self._age_first_sight(svc, WORKLOAD_FAULT_CONFIRM_MINUTES + 1)
        svc._apply_workload_observation(_status(_unready()))
        self.assertTrue(svc.workload_fault_alerted_at, 'fixture 前提：通知過')
        marker = 'id=%s' % svc.id

        self.assertEqual(svc._apply_workload_observation(_status(workload_info())), 'cleared')
        recovered = [b for b in self._monitor_bodies() if marker in b]
        self.assertTrue(recovered, 'monitor 的 chatter 要留下恢復紀錄')
        self.assertNotIn('have pods again', ' '.join(recovered),
                         '恢復那一行不能只講「Pod 回來了」：not_ready 的 Pod 一直都在')

    def test_monitor_texts_cover_both_kinds_and_are_translated(self):
        """通知與恢復的文字要同時說得通「沒有 Pod」與「沒有一顆就緒」，而且有 zh_TW 譯文。"""
        po = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'i18n', 'zh_TW.po')
        translated = {e['src']: e['value'] for e in PoFileReader(po) if e['type'] == 'code'}
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'models', 'platform_monitor.py')
        with open(path, encoding='utf-8') as handle:
            source = handle.read()
        start = source.index('def _report_workload_fault')
        end = source.index('def _note_workload_recovered')
        tail = source[end:end + 3000]
        texts = []
        for chunk in (source[start:end], tail):
            for match in re.finditer(r"_\(\s*((?:'[^']*'\s*)+)", chunk):
                texts.append(''.join(re.findall(r"'([^']*)'", match.group(1))))
        note = [t for t in texts if t.startswith('These services are shown as running')]
        recovered = [t for t in texts if 'id=%(id)s): ' in t]
        self.assertEqual(len(note), 1, texts)
        self.assertEqual(len(recovered), 1, texts)
        self.assertIn('none of their pods is ready', note[0])
        self.assertIn('has none', note[0])
        for text in note + recovered:
            with self.subTest(text=text[:40]):
                self.assertTrue(translated.get(text), 'zh_TW.po 缺譯文或 msgid 不一致')
