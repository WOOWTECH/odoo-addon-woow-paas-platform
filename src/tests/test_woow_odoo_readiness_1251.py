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
from types import SimpleNamespace
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged

from ..controllers.paas import PaasController
from ..models.cloud_service import _internal_provision
from ..services.woow_odoo_readiness import woow_odoo_ready
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
        self.service = _internal_provision(
            self.env['woow_paas_platform.cloud_service'],
        ).create({
            'name': '1251 Service',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'state': 'deploying',
            'helm_namespace': 'dev-sandbox-1251',
            'helm_release_name': 'svc-1251',
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
