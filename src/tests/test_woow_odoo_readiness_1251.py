"""#1251：woow-odoo 服務的 readiness 依 Pod 身分判定（Q5／Q6／Q17／Q18／Q19／Q45）。

#1156 的通用規則是「所有非收尾 Pod 都 Running＋ready」。對 woow-odoo 它兩頭都錯：
- 負例 1：server 不存在、只剩 Running 的 backup Job（或 PG）時，通用規則回就緒；
- 負例 2：server／PG 健康、housekeeping Job Pending 時，通用規則回未就緒。

透過 cron（``_poll_and_update_status``）與 controller（``_update_service_status``）
兩個 poll 入口觀察服務狀態——兩者共用 ``_is_release_ready``（#423 的教訓），只測一邊
會漏掉另一邊被繞過。只替換 operator boundary 與 request env；Pod 一律用
``pod_info()`` 組（#1263 交接：沒有身分 key 的手寫 Pod 配 ``pod_identity_included``
預設 True，是真 operator 不會送出的組合）。
"""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from odoo import fields
from odoo.tests.common import TransactionCase, tagged

from ..controllers.paas import PaasController
from ..models.cloud_service import _internal_provision
from ..services.woow_odoo_readiness import (
    CONTRACT_MANAGED,
    CONTRACT_MISMATCH,
    chart_name_of,
    woow_odoo_contract,
    woow_odoo_ready,
)
from .operator_contract import ABSENT, operator_client, pod_info, status_response

_ENTRIES = ('cron', 'controller')


def _server(**overrides):
    """chart 的 web Pod：Deployment → ReplicaSet 直接擁有。"""
    return pod_info(**{
        'name': 'svc-1251-woow-odoo-6c9d7-x2k4p',
        'component': 'server',
        'owner_kind': 'ReplicaSet',
        **overrides,
    })


def _postgres(**overrides):
    """chart 的 PG Pod：StatefulSet 直接擁有。"""
    return pod_info(**{
        'name': 'svc-1251-woow-odoo-postgresql-0',
        'component': 'postgresql',
        'owner_kind': 'StatefulSet',
        **overrides,
    })


def _db_job(task, **overrides):
    """CronJob 產生的例行 Job Pod（backup／housekeeping 共用 component=db-backup）。"""
    return pod_info(**{
        'name': f'svc-1251-woow-odoo-{task}-29311520-7vq2m',
        'component': 'db-backup',
        'task': task,
        'owner_kind': 'Job',
        **overrides,
    })


@tagged('post_install', '-at_install')
class TestWoowOdooReadinessIdentity(TransactionCase):
    """woow-odoo 的就緒＝必要組健康；例行 Job 豁免；其他 Pod fail-closed。"""

    def setUp(self):
        super().setUp()
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': '1251 Workspace',
        })
        self.template = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': '1251 woow-odoo',
            'slug': 'woow-odoo-readiness-1251',
            'helm_repo_url': '',
            'helm_chart_name': (
                'oci://jcr-prod.woowtech.io/woow-paas-docker-local/woow-odoo'
            ),
            'helm_chart_version': '0.2.2',
            'workload_type': 'container',
            'post_deploy_init_type': 'none',
            'mcp_enabled': False,
        })
        self.service = self._make_service('1251 Service', 'svc-1251')

    def _make_service(self, name, release_name, state='deploying'):
        return _internal_provision(
            self.env['woow_paas_platform.cloud_service'],
        ).create({
            'name': name,
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'state': state,
            'helm_namespace': 'dev-sandbox-1251',
            'helm_release_name': release_name,
        })

    @staticmethod
    def _status(pods, chart='woow-odoo', **overrides):
        """同一輪 get_status：``release.chart`` 是 helm list 拆出來的實際 chart 名。"""
        release = {'status': 'deployed', 'revision': 1, 'chart': chart}
        return status_response(pods=pods, release=release, **overrides)

    def _poll(self, entry, status):
        client = operator_client(get_status=status)
        if entry == 'cron':
            self.service._poll_and_update_status(client)
        else:
            module = 'odoo.addons.woow_paas_platform.controllers.paas'
            with patch(f'{module}.get_paas_operator_client', return_value=client), \
                    patch(f'{module}.request', new=SimpleNamespace(env=self.env)):
                PaasController()._update_service_status(self.service)
        client.get_status.assert_called_once()

    def _assert_poll_keeps(self, status, state='deploying'):
        """兩個入口各 poll 一次，服務都必須留在轉移狀態。"""
        for entry in _ENTRIES:
            with self.subTest(entry=entry):
                self.service.write({'state': state})
                self._poll(entry, status)
                self.assertEqual(self.service.state, state)

    def _assert_poll_reaches_running(self, status, state='deploying'):
        for entry in _ENTRIES:
            with self.subTest(entry=entry):
                self.service.write({'state': state})
                self._poll(entry, status)
                self.assertEqual(self.service.state, 'running')

    # ------------------------------------------------------------------ #
    #  負例 1：Job／PG 不能冒充 server（Q5）                               #
    # ------------------------------------------------------------------ #

    def test_running_backup_job_cannot_stand_in_for_missing_server(self):
        """票面負例 1：server 不存在，只剩 Running 的 backup Job 與 PG。"""
        backup = _db_job('backup', phase='Running', ready='1/1')
        for pods in ([backup], [_postgres(), backup], [_postgres()]):
            with self.subTest(pods=[p['name'] for p in pods]):
                self._assert_poll_keeps(self._status(pods))

    # ------------------------------------------------------------------ #
    #  其他 active Pod：照 #1156 fail-closed（Q5／Q19）                    #
    # ------------------------------------------------------------------ #

    def test_pods_outside_the_contract_fail_closed(self):
        """server／PG 健康，但多出契約外的 Pod ⇒ 未就緒，detail 為 HARD 並點名該 Pod。"""
        outsiders = {
            # restore 只該在 `restoring` 出現（#1213），在轉移狀態看到就是違約。
            'db-restore': pod_info(name='svc-1251-restore-9f2k', component='db-restore',
                                   owner_kind='Job'),
            'unknown component': pod_info(name='svc-1251-cron-5d8', component='cron',
                                          owner_kind='ReplicaSet'),
            'missing label': pod_info(name='svc-1251-mystery', owner_kind='ReplicaSet'),
            # db-backup 只豁免白名單內的 task。
            'db-backup other task': _db_job('restore'),
            'db-backup no task': _db_job(None, name='svc-1251-db-backup-adhoc'),
            # label 是 server，但 Job 擁有 ⇒ 不是 chart 的 web Pod（#1129 語意）。
            'server label on a Job': _server(name='svc-1251-server-job', owner_kind='Job'),
        }
        for case, outsider in outsiders.items():
            with self.subTest(case=case):
                status = self._status([_server(), _postgres(), outsider])
                self._assert_poll_keeps(status)
                ready, detail = self.service._is_release_ready(status)
                self.assertFalse(ready)
                self.assertTrue(detail.startswith('HARD: '), detail)
                self.assertIn(outsider['name'], detail)

    # ------------------------------------------------------------------ #
    #  負例 2：例行 Job 豁免白名單（Q6）                                    #
    # ------------------------------------------------------------------ #

    def test_routine_db_jobs_do_not_block_a_healthy_service(self):
        """票面負例 2：server／PG 健康時，backup／housekeeping Job 不論 phase 都不擋。"""
        for task in ('backup', 'housekeeping'):
            for phase, ready in (('Pending', '0/1'), ('Running', '0/1'),
                                 ('Running', '1/1'), ('Unknown', '0/1')):
                with self.subTest(task=task, phase=phase, ready=ready):
                    job = _db_job(task, phase=phase, ready=ready)
                    self._assert_poll_reaches_running(
                        self._status([_server(), _postgres(), job]),
                    )

    def test_exempt_pods_are_listed_in_the_detail(self):
        """被豁免的 Pod 要出現在 detail：server 卡住時，看得出旁邊正跑著備份
        （例：Recreate 期間備份 Job 佔著 RWO PVC）。"""
        backup = _db_job('backup', phase='Running', ready='1/1')
        cases = {
            'server not ready': [_server(phase='Pending', ready='0/1'), _postgres(), backup],
            'server missing': [_postgres(), backup],
        }
        for case, pods in cases.items():
            with self.subTest(case=case):
                ready, detail = self.service._is_release_ready(self._status(pods))
                self.assertFalse(ready)
                self.assertIn(backup['name'], detail)
                self.assertIn('exempt', detail)

        # 就緒時 `_is_release_ready` 回的是其後 DNS 閘的 detail；豁免清單由共用判定
        # 本身帶出（#1213 finalize 直接用它）。
        ready, detail = woow_odoo_ready(self._status([_server(), _postgres(), backup]))
        self.assertTrue(ready)
        self.assertIn(backup['name'], detail)
        self.assertIn('exempt', detail)

    # ------------------------------------------------------------------ #
    #  必要組未就緒／收尾中的 Pod                                          #
    # ------------------------------------------------------------------ #

    def test_required_component_must_be_running_and_fully_ready(self):
        cases = {
            'server Running 0/1': [_server(ready='0/1'), _postgres()],
            'server Pending': [_server(phase='Pending', ready='0/1'), _postgres()],
            'server no container status': [_server(ready='0/0'), _postgres()],
            'postgres Running 0/1': [_server(), _postgres(ready='0/1')],
            'one of two servers not ready': [
                _server(), _server(name='svc-1251-woow-odoo-6c9d7-b7m2q', ready='0/1'),
                _postgres(),
            ],
        }
        for case, pods in cases.items():
            with self.subTest(case=case):
                self._assert_poll_keeps(self._status(pods))

    def test_finished_and_terminating_pods_are_ignored(self):
        """Recreate 換 Pod：舊 server 帶 deletion_timestamp、上一輪 Job 已 Failed／Succeeded。"""
        pods = [
            _server(name='svc-1251-old', ready='0/1',
                    deletion_timestamp='2026-09-30T01:00:00Z'),
            _server(),
            _postgres(),
            pod_info(name='svc-1251-restore-old', component='db-restore',
                     owner_kind='Job', phase='Succeeded', ready='0/1'),
            _db_job('backup', phase='Failed', ready='0/1'),
        ]
        self._assert_poll_reaches_running(self._status(pods), state='upgrading')

    # ------------------------------------------------------------------ #
    #  Q18：operator 沒回 Pod 身分 ⇒ fail-closed                           #
    # ------------------------------------------------------------------ #

    def test_operator_without_pod_identity_fails_closed(self):
        """缺 key（#1263 前的 operator 真實形狀）與 False 兩種都要擋。

        Pod 刻意帶齊身分：判準必須是 echo 旗標，不能從 Pod 欄位推斷
        （``.get('pod_identity_included', True)`` 這種寫錯的預設只測 False 抓不到）。
        """
        pods = [_server(), _postgres()]
        for flag in (ABSENT, False, None):
            with self.subTest(pod_identity_included=flag):
                status = self._status(pods, pod_identity_included=flag)
                self._assert_poll_keeps(status)
                ready, detail = self.service._is_release_ready(status)
                self.assertFalse(ready)
                self.assertTrue(
                    detail.startswith('HARD: operator lacks pod identity'), detail,
                )

    # ------------------------------------------------------------------ #
    #  Q5／Q17：依實際 release.chart 分流                                  #
    # ------------------------------------------------------------------ #

    def test_template_expects_woow_odoo_but_release_runs_another_chart(self):
        """#1128：in-place upgrade 換不了 chart ⇒ HARD，不能拿通用規則放行。"""
        healthy_generic = [pod_info(name='svc-1251-odoo-7f9-abc')]
        for case, release in {
            'Bitnami odoo': {'status': 'deployed', 'revision': 3, 'chart': 'odoo'},
            'chart unknown': {'status': 'deployed', 'revision': 3},
        }.items():
            with self.subTest(case=case):
                status = status_response(pods=healthy_generic, release=release)
                self._assert_poll_keeps(status, state='upgrading')
                ready, detail = self.service._is_release_ready(status)
                self.assertFalse(ready)
                self.assertTrue(detail.startswith('HARD: chart mismatch'), detail)

    def test_actual_chart_governs_even_when_template_says_otherwise(self):
        """template 仍是 Bitnami（cutover 窗口），叢集上實際跑 woow-odoo ⇒ 照 woow-odoo 判。"""
        self.template.helm_chart_name = 'oci://registry-1.docker.io/bitnamicharts/odoo'
        backup = _db_job('backup', phase='Running', ready='1/1')
        self._assert_poll_keeps(self._status([_postgres(), backup]))

    def test_other_charts_keep_the_generic_rule(self):
        """Bitnami odoo 與其他 chart 不受影響：無身分 Pod 照 #1156 判、舊 operator 不擋。"""
        self.template.helm_chart_name = 'oci://registry-1.docker.io/bitnamicharts/odoo'
        generic = [pod_info(name='svc-1251-odoo-7f9-abc'),
                   pod_info(name='svc-1251-postgresql-0')]
        self._assert_poll_reaches_running(
            self._status(generic, chart='odoo', pod_identity_included=ABSENT),
        )
        pending = [pod_info(name='svc-1251-odoo-7f9-abc', phase='Pending', ready='0/1'),
                   pod_info(name='svc-1251-postgresql-0')]
        self._assert_poll_keeps(self._status(pending, chart='odoo'))

    # ------------------------------------------------------------------ #
    #  Q45：逾時訊息的 hint                                                #
    # ------------------------------------------------------------------ #

    def _timeout_message(self, status):
        """把錨推到 #489 門檻（容器 30 分）之外再 poll 一次，回逾時訊息。"""
        self.service.write({
            'state': 'deploying',
            'deployed_at': fields.Datetime.now() - timedelta(minutes=31),
        })
        self._poll('cron', status)
        self.assertEqual(self.service.state, 'error', '前置：這一輪應該逾時')
        return self.service.error_message

    def test_timeout_hints_point_at_the_actual_cause(self):
        """新 HARD detail 各有自己的 hint，且不被 `'pods not ready'` 的通用 hint 先比中。"""
        pods_hint = 'readiness probe'   # `'pods not ready'` 那筆 hint 的特徵字
        cases = {
            'chart mismatch': (
                status_response(pods=[pod_info()],
                                release={'status': 'deployed', 'revision': 2,
                                         'chart': 'odoo'}),
                ('fresh install', '#1128'),
            ),
            'operator lacks pod identity': (
                self._status([_server(), _postgres()],
                             pod_identity_included=ABSENT),
                ('upgrade', 'paas-operator'),
            ),
            'required pods not ready': (
                self._status([_server(ready='0/1'), _postgres()]),
                (pods_hint, 'svc-1251-woow-odoo-6c9d7-x2k4p'),
            ),
            # 一顆 Pod 都沒有（quota 擋住 Pod 建立）：沿用通用 detail，接得上 quota 的 hint。
            'no pods found': (self._status([]), ('ResourceQuota',)),
            'pod lookup failed': (
                self._status([], pod_retrieval_error='pods is forbidden'),
                ('RBAC',),
            ),
        }
        for case, (status, expected) in cases.items():
            with self.subTest(case=case):
                msg = self._timeout_message(status)
                self.assertIn(case, msg)
                for needle in expected:
                    self.assertIn(needle, msg)
                if pods_hint not in expected:
                    self.assertNotIn(pods_hint, msg)

    # ------------------------------------------------------------------ #
    #  halted（server=0）不經這條 poll                                     #
    # ------------------------------------------------------------------ #

    def test_halted_service_never_reaches_the_readiness_check(self):
        """停權中的 woow-odoo 服務 server 被刻意關成 0——必要組判定對它永遠不成立，
        所以它根本不能進 poll：停權中的 upgrade 維持 `suspended`，而 cron 只撈轉移狀態。"""
        self.service.write({'state': 'suspended', 'pre_suspend_state': 'running'})
        deploying = self._make_service('1251 Deploying', 'svc-1251-deploying')
        client = operator_client(get_status=self._status([_server(), _postgres()]))
        with patch(
            'odoo.addons.woow_paas_platform.services.paas_operator'
            '.get_paas_operator_client',
            return_value=client,
        ):
            self.env['woow_paas_platform.cloud_service']._cron_check_deploying_services()

        polled = [c.kwargs.get('release_name') for c in client.get_status.call_args_list]
        self.assertEqual(polled, [deploying.helm_release_name])
        self.assertEqual(self.service.state, 'suspended')


class TestWoowOdooContractPredicate(TransactionCase):
    """Q17 共用判定本身（#1222 D11 分流、#1213 finalize 會直接呼叫）。"""

    def test_chart_name_of_accepts_every_template_ref_shape(self):
        for ref in ('oci://jcr-prod.woowtech.io/woow-paas-docker-local/woow-odoo',
                    'woow-paas/woow-odoo', 'woow-odoo', 'oci://registry/woow-odoo/'):
            with self.subTest(ref=ref):
                self.assertEqual(chart_name_of(ref), 'woow-odoo')
        self.assertIsNone(chart_name_of(False))
        self.assertEqual(
            chart_name_of('oci://registry-1.docker.io/bitnamicharts/odoo'), 'odoo',
        )

    def test_contract_follows_the_deployed_chart(self):
        woow = 'oci://jcr-prod.woowtech.io/woow-paas-docker-local/woow-odoo'
        bitnami = 'oci://registry-1.docker.io/bitnamicharts/odoo'
        cases = [
            # (release.chart, template helm_chart_name, 期望)
            ('woow-odoo', woow, CONTRACT_MANAGED),
            ('woow-odoo', bitnami, CONTRACT_MANAGED),     # 實際值為準
            ('odoo', woow, CONTRACT_MISMATCH),            # #1128
            (None, woow, CONTRACT_MISMATCH),              # 實際值未知
            ('odoo', bitnami, None),
            ('n8n', 'oci://8gears.container-registry.com/library/n8n', None),
            (None, None, None),
        ]
        for chart, expected_ref, expected in cases:
            with self.subTest(chart=chart, expected_ref=expected_ref):
                release = {'status': 'deployed', 'revision': 1}
                if chart is not None:
                    release['chart'] = chart
                status = status_response(release=release)
                self.assertEqual(woow_odoo_contract(status, expected_ref), expected)
