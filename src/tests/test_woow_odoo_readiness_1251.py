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
from .operator_contract import operator_client, pod_info, status_response

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
