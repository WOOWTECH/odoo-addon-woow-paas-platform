# -*- coding: utf-8 -*-
"""#1417：部署逾時訊息說得出「卡在哪」，而且先給租戶一句看得懂的話（建立者的語言）。

2026-10-04 正式站 svc 147（Nextcloud）：Pod 從頭到尾停在 ``Init:0/1``（phase Pending），
事件是 ``FailedAttachVolume … not ready for workloads``（Longhorn 排程額度不足）。租戶看到：

    Deployment timed out after 30 minutes: pods not ready — pods are Running but never
    became ready — check container logs and the readiness probe

三個問題（票面）：方向錯（Pending 被說成 Running）、英文、寫給維運不是寫給租戶。

守的事：

* ``_is_container_ready`` 分開「沒跑起來（Pending）」與「Running 未 ready」，detail 帶 Pod
  名稱與 operator 報的原因（``PodInfo.waiting``／``last_warning``；舊 operator 沒有就照舊）；
* hint 依原因分型（掛載／排程含儲存額度、拉 image、反覆重啟、readiness、其他），有具體原因
  時不給籠統方向；
* ``error_message``＝租戶一句話＋「技術細節（給管理員）」，以**服務建立者**的語言組字串；
* 每個 hint、每句租戶說明都有 zh_TW 翻譯（漏翻會在畫面上冒出英文）。

detail 一律由判定本身（``_is_container_ready``）產生、不手抄（同
``TestWoowOdooContractPredicate.test_each_new_detail_is_matched_by_its_own_hint_first``
的理由：字串改了這裡要紅）。
"""
from datetime import timedelta
from unittest.mock import MagicMock

from odoo import fields
from odoo.tests.common import TransactionCase, tagged

from ..models.cloud_service import (
    TIMEOUT_CAUSE_CRASH,
    TIMEOUT_CAUSE_IMAGE,
    TIMEOUT_CAUSE_OTHER,
    TIMEOUT_CAUSE_READINESS,
    TIMEOUT_CAUSE_STORAGE,
    TIMEOUT_CAUSE_SUSPENDED,
    _TIMEOUT_HINTS,
    _internal_provision,
)
from .operator_contract import pod_info, status_response

_TIMEOUT_MINUTES = 30  # 容器型預設門檻（#489）

#: 10-04 事件的 Pod：Init 卡在 PodInitializing、最近一筆事件 FailedAttachVolume。
_INCIDENT_EVENT = {
    'reason': 'FailedAttachVolume',
    'message': 'AttachVolume.Attach failed for volume "pvc-9b1c" : rpc error: code = '
               'Aborted desc = volume pvc-9b1c is not ready for workloads',
    'count': 12,
    'last_seen': '2026-10-04T01:46:02Z',
}


def _waiting(reason, container='app', init=False, message=None):
    return {'container': container, 'init_container': init, 'reason': reason,
            'message': message}


def _event(reason, message='x'):
    return {'reason': reason, 'message': message, 'count': 1,
            'last_seen': '2026-10-04T01:00:00Z'}


def _pending(name='svc-147-nextcloud-0', **overrides):
    return pod_info(**{'name': name, 'phase': 'Pending', 'ready': '0/1', **overrides})


def _running_unready(name='svc-147-nextcloud-0', **overrides):
    return pod_info(**{'name': name, 'phase': 'Running', 'ready': '0/1', **overrides})


_INCIDENT_POD = _pending(
    ready='0/2',
    waiting=_waiting('PodInitializing', container='volume-permissions', init=True),
    last_warning=_INCIDENT_EVENT,
)


@tagged('post_install', '-at_install')
class TestTimeoutDiagnosis1417(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env['res.lang']._activate_lang('zh_TW')
        cls.Service = cls.env['woow_paas_platform.cloud_service']
        cls.template = cls.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'Timeout 1417', 'slug': 'timeout-1417',
            'helm_repo_url': 'https://charts.example.com', 'helm_chart_name': 'nextcloud',
            'helm_chart_version': '0.1.4', 'workload_type': 'container',
            'post_deploy_init_type': 'none', 'mcp_enabled': False,
        })
        cls.workspace = cls.env['woow_paas_platform.workspace'].create({'name': 'WS 1417'})

    def setUp(self):
        super().setUp()
        self.env['ir.config_parameter'].sudo().search([
            ('key', '=', 'woow_paas_platform.deploy_timeout_minutes')]).unlink()

    # ------------------------------------------------------------------ #
    #  helpers                                                             #
    # ------------------------------------------------------------------ #

    def _user(self, lang):
        return self.env['res.users'].create({
            'name': 'Tenant 1417 %s' % lang, 'login': 'tenant1417-%s@tenant1417.tw' % lang,
            'lang': lang, 'tz': 'Asia/Taipei',
        })

    def _service(self, creator=None):
        Service = self.Service
        if creator:
            Service = Service.with_user(creator).sudo()
        svc = _internal_provision(Service).create({
            'name': 'svc-1417', 'workspace_id': self.workspace.id,
            'template_id': self.template.id, 'state': 'deploying',
            'helm_namespace': 'paas-ws-test-1417', 'helm_release_name': 'svc-147',
            'deployed_at': fields.Datetime.now() - timedelta(minutes=_TIMEOUT_MINUTES + 1),
        })
        # 以 cron 同級身分 poll；create_uid 留在租戶身上（同 #1348 的測法）。
        return svc.with_env(self.env)

    def _time_out(self, pods, creator=None):
        svc = self._service(creator)
        client = MagicMock()
        client.get_status.return_value = status_response(pods=pods)
        svc._poll_and_update_status(client)
        svc.invalidate_recordset()
        self.assertEqual(svc.state, 'error', '前提：這一輪應該逾時')
        return svc

    def _detail(self, pods):
        ready, detail = self.Service._is_container_ready(status_response(pods=pods))
        self.assertFalse(ready)
        return detail

    def _classify(self, pods):
        return self._service()._timeout_classify(self._detail(pods))

    # ------------------------------------------------------------------ #
    #  Pending ≠ Running                                                   #
    # ------------------------------------------------------------------ #

    def test_pending_pod_is_not_described_as_running(self):
        """舊 operator（沒有 waiting／last_warning）也要分得出 Pending——phase 一直都有。"""
        legacy = {'name': 'svc-147-nextcloud-0', 'phase': 'Pending', 'ready': '0/2'}
        detail = self._detail([legacy])
        self.assertEqual(detail, 'pods not started: svc-147-nextcloud-0[Pending 0/2]')

        msg = self._time_out([legacy]).error_message
        self.assertIn('still Pending', msg)
        self.assertNotIn('Running but never became ready', msg)
        self.assertNotIn('readiness probe', msg)

    def test_running_unready_keeps_the_readiness_direction(self):
        detail = self._detail([_running_unready()])
        self.assertEqual(detail, 'pods not ready: svc-147-nextcloud-0[Running 0/1]')
        self.assertEqual(self._service()._timeout_classify(detail),
                         (TIMEOUT_CAUSE_READINESS, 'pods_not_ready'))

    def test_mixed_pods_are_listed_under_their_own_label(self):
        detail = self._detail([
            _pending('a-0'), _running_unready('b-0'),
            pod_info(name='c-0', phase='Running', ready='1/1'),
        ])
        self.assertEqual(detail, 'pods not started: a-0[Pending 0/1]; '
                                 'pods not ready: b-0[Running 0/1]')

    def test_detail_caps_the_pod_list(self):
        detail = self._detail([_pending('p-%d' % i) for i in range(5)])
        self.assertIn('p-0[', detail)
        self.assertIn('p-2[', detail)
        self.assertNotIn('p-3[', detail)
        self.assertTrue(detail.endswith('(+2 more)'), detail)

    def test_long_event_message_is_truncated(self):
        detail = self._detail([_pending(last_warning=_event('FailedMount', 'y' * 1000))])
        self.assertIn('…', detail)
        self.assertLess(len(detail), 400)

    # ------------------------------------------------------------------ #
    #  原因分型                                                            #
    # ------------------------------------------------------------------ #

    def test_each_reported_cause_gets_its_own_hint(self):
        cases = {
            'event=FailedAttachVolume': ([_INCIDENT_POD], TIMEOUT_CAUSE_STORAGE, 'volume_attach'),
            'event=FailedMount': ([_pending(last_warning=_event('FailedMount'))],
                                  TIMEOUT_CAUSE_STORAGE, 'volume_mount'),
            'event=FailedScheduling': (
                [_pending(last_warning=_event(
                    'FailedScheduling', '0/4 nodes are available: 4 Insufficient memory.'))],
                TIMEOUT_CAUSE_STORAGE, 'scheduling'),
            'waiting=ImagePullBackOff': (
                [_pending(waiting=_waiting('ImagePullBackOff'),
                          last_warning=_event('Failed', 'Failed to pull image'))],
                TIMEOUT_CAUSE_IMAGE, 'image_pull'),
            'waiting=ErrImagePull': ([_pending(waiting=_waiting('ErrImagePull'))],
                                     TIMEOUT_CAUSE_IMAGE, 'image_pull'),
            'waiting=CrashLoopBackOff': (
                [_running_unready(waiting=_waiting('CrashLoopBackOff'),
                                  last_warning=_event('BackOff'))],
                TIMEOUT_CAUSE_CRASH, 'crash_loop'),
            'init CrashLoopBackOff': (
                [_pending(waiting=_waiting('CrashLoopBackOff', 'migrate', init=True))],
                TIMEOUT_CAUSE_CRASH, 'crash_loop'),
            'waiting=CreateContainerConfigError': (
                [_pending(waiting=_waiting('CreateContainerConfigError'))],
                TIMEOUT_CAUSE_OTHER, 'container_config'),
            'event=Unhealthy': (
                [_running_unready(last_warning=_event('Unhealthy', 'Readiness probe failed'))],
                TIMEOUT_CAUSE_READINESS, 'pods_not_ready'),
            'Pending without a reason': ([_pending()], TIMEOUT_CAUSE_OTHER, 'pods_not_started'),
        }
        for case, (pods, cause, key) in cases.items():
            with self.subTest(case=case):
                self.assertEqual(self._classify(pods), (cause, key))

    def test_the_most_fundamental_cause_wins_across_pods(self):
        """磁碟掛不上 > 拉不到 image > 反覆重啟 > readiness：前面的是後面的根因。"""
        pods = [
            _running_unready('app-0', waiting=_waiting('CrashLoopBackOff')),
            _pending('img-0', waiting=_waiting('ImagePullBackOff')),
            _INCIDENT_POD,
        ]
        self.assertEqual(self._classify(pods), (TIMEOUT_CAUSE_STORAGE, 'volume_attach'))

    def test_reason_markers_precede_the_generic_needles(self):
        needles = [needle for needle, *_rest in _TIMEOUT_HINTS]
        for marker in (n for n in needles if n.startswith(('event=', 'waiting='))):
            with self.subTest(marker=marker):
                self.assertLess(needles.index(marker), needles.index('pods not started'))
                self.assertLess(needles.index(marker), needles.index('pods not ready'))

    def test_suspended_overrides_every_cause(self):
        svc = self._service()
        svc.pre_suspend_state = 'running'
        self.assertEqual(svc._timeout_classify(self._detail([_INCIDENT_POD])),
                         (TIMEOUT_CAUSE_SUSPENDED, 'suspended'))

    # ------------------------------------------------------------------ #
    #  error_message：租戶一句話＋技術細節，建立者語言                    #
    # ------------------------------------------------------------------ #

    def test_incident_message_in_english(self):
        svc = self._time_out([_INCIDENT_POD], creator=self._user('en_US'))
        msg = svc.error_message
        summary, _sep, technical = msg.partition('\nTechnical details for the administrator: ')
        self.assertTrue(technical, msg)
        self.assertIn('storage or computing resources', summary)
        self.assertIn('you do not need to do anything', summary)
        self.assertIn('recover automatically', summary)
        self.assertTrue(technical.startswith(
            'Deployment timed out after 30 minutes: pods not started: svc-147-nextcloud-0'
            '[Pending 0/2 waiting=PodInitializing(init container volume-permissions) '
            'event=FailedAttachVolume: AttachVolume.Attach failed'), technical)
        self.assertIn('Longhorn', technical)
        self.assertNotIn('readiness probe', msg)

    def test_incident_message_in_the_creators_chinese(self):
        """cron 的 env 語言不是租戶的：訊息依服務建立者的語言（同 #1348）。"""
        svc = self._time_out([_INCIDENT_POD], creator=self._user('zh_TW'))
        msg = svc.error_message
        self.assertTrue(msg.startswith('服務在 30 分鐘內沒能啟動完成：'), msg)
        self.assertIn('您不需要做任何操作', msg)
        self.assertIn('問題排除後服務會自動恢復', msg)
        self.assertIn('\n技術細節（給管理員）：部署在 30 分鐘後逾時：pods not started: '
                      'svc-147-nextcloud-0[Pending 0/2', msg)
        self.assertIn('event=FailedAttachVolume', msg)
        self.assertIn('磁碟掛不到節點上', msg)
        self.assertNotIn('Running but never', msg)
        self.assertNotIn('Technical details', msg)

    def test_tenant_summary_per_cause(self):
        svc = self._service()
        env = svc.with_context(lang='en_US').env
        for cause in (TIMEOUT_CAUSE_STORAGE, TIMEOUT_CAUSE_IMAGE, TIMEOUT_CAUSE_READINESS,
                      TIMEOUT_CAUSE_OTHER):
            with self.subTest(cause=cause):
                text = svc._timeout_tenant_summary(env, cause, 30)
                self.assertIn('you do not need to do anything', text.lower())
                self.assertIn('recover automatically', text)
        crash = svc._timeout_tenant_summary(env, TIMEOUT_CAUSE_CRASH, 30)
        self.assertIn('If you recently changed its settings', crash)
        self.assertIn('recover automatically', crash)
        suspended = svc._timeout_tenant_summary(env, TIMEOUT_CAUSE_SUSPENDED, 30)
        self.assertIn('suspended', suspended)
        self.assertIn('resume automatically', suspended)

    def test_every_hint_and_summary_is_translated(self):
        """漏一條 .po，畫面上就冒出英文——對全部 hint key 與分型逐一對照。"""
        svc = self._service()
        en = svc.with_context(lang='en_US').env
        zh = svc.with_context(lang='zh_TW').env
        keys = {key for _needle, key, _cause in _TIMEOUT_HINTS} | {'suspended'}
        for key in sorted(keys):
            with self.subTest(hint=key):
                self.assertNotEqual(svc._timeout_hint_text(zh, key),
                                    svc._timeout_hint_text(en, key))
        causes = {cause for _needle, _key, cause in _TIMEOUT_HINTS} | {
            TIMEOUT_CAUSE_SUSPENDED}
        for cause in sorted(causes):
            with self.subTest(cause=cause):
                self.assertNotEqual(svc._timeout_tenant_summary(zh, cause, 30),
                                    svc._timeout_tenant_summary(en, cause, 30))
