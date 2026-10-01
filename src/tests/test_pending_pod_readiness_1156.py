"""#1156：PG 已 Ready 不得讓 initContainer Pending 的服務提前就緒。

透過 poll 入口（``_poll_and_update_status``）觀察服務狀態；只替換 operator
boundary，不替換共用 readiness 判定。Job 分類不在本票範圍。#1159 起 list/get 純讀，
controller 那份 poll 已刪除，只剩 cron 這個入口。
"""
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged

from ..models.cloud_service import _internal_provision
from .operator_contract import operator_client, status_response


@tagged('post_install', '-at_install')
class TestPendingPodReadiness(TransactionCase):
    """poll 必須保留未完成的服務狀態。"""

    def setUp(self):
        super().setUp()
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': '1156 Workspace',
        })
        self.template = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': '1156 Container',
            'slug': 'pending-readiness-1156',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'chart',
            'helm_chart_version': '1.0.0',
            'workload_type': 'container',
            'post_deploy_init_type': 'none',
            'mcp_enabled': False,
        })
        self.service = _internal_provision(
            self.env['woow_paas_platform.cloud_service'],
        ).create({
            'name': '1156 Service',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'state': 'deploying',
            'helm_namespace': 'dev-sandbox-1156',
            'helm_release_name': 'svc-1156',
        })

    def _poll(self, pods, **status_overrides):
        client = operator_client(
            get_status=status_response(pods=pods, **status_overrides),
            init_odoo={'success': True, 'modules_added': 0, 'modules_updated': 0},
        )
        self.service._poll_and_update_status(client)
        client.get_status.assert_called_once()
        return client

    def test_postgres_ready_does_not_hide_pending_odoo(self):
        # initContainer 執行時 containerStatuses 可能尚不存在（0/0），
        # 也可能已回報尚未 ready 的主容器（0/1）。
        for ready in ('0/0', '0/1'):
            with self.subTest(ready=ready):
                self.service.write({'state': 'deploying'})
                self._poll([
                    {'name': 'postgres', 'phase': 'Running', 'ready': '1/1'},
                    {'name': 'odoo', 'phase': 'Pending', 'ready': ready},
                ])
                self.assertEqual(self.service.state, 'deploying')

    def test_terminating_old_pod_does_not_block_ready_replacement(self):
        for phase in ('Running', 'Pending'):
            with self.subTest(phase=phase):
                self.service.write({'state': 'upgrading'})
                self._poll([
                    {'name': 'old-odoo', 'phase': phase, 'ready': '0/1',
                     'deletion_timestamp': '2026-09-29T00:00:00Z'},
                    {'name': 'odoo', 'phase': 'Running', 'ready': '1/1'},
                ])
                self.assertEqual(self.service.state, 'running')

    def test_all_ready_pods_allow_running_with_legacy_or_null_timestamp(self):
        self.service.write({'state': 'deploying'})
        self._poll([
            {'name': 'postgres', 'phase': 'Running', 'ready': '1/1'},
            {'name': 'odoo', 'phase': 'Running', 'ready': '2/2',
             'deletion_timestamp': None},
        ])
        self.assertEqual(self.service.state, 'running')

    def test_terminal_old_pods_do_not_block_ready_replacement(self):
        for phase in ('Succeeded', 'Failed'):
            with self.subTest(phase=phase):
                self.service.write({'state': 'upgrading'})
                self._poll([
                    {'name': 'old-odoo', 'phase': phase, 'ready': '0/1'},
                    {'name': 'odoo', 'phase': 'Running', 'ready': '1/1'},
                ])
                self.assertEqual(self.service.state, 'running')

    def test_no_active_pods_cannot_mark_service_running(self):
        cases = [[],
                 [{'name': 'old', 'phase': 'Succeeded', 'ready': '0/1'}],
                 [{'name': 'old', 'phase': 'Failed', 'ready': '0/1'}],
                 [{'name': 'old', 'phase': 'Running', 'ready': '1/1',
                   'deletion_timestamp': '2026-09-29T00:00:00Z'}]]
        for pods in cases:
            with self.subTest(pods=pods):
                self.service.write({'state': 'deploying'})
                self._poll(pods)
                self.assertEqual(self.service.state, 'deploying')

    def test_non_ready_or_unknown_pod_blocks_ready_postgres(self):
        for phase, ready in (('Running', '0/1'), ('Unknown', '0/0')):
            with self.subTest(phase=phase):
                self.service.write({'state': 'deploying'})
                self._poll([
                    {'name': 'postgres', 'phase': 'Running', 'ready': '1/1'},
                    {'name': 'odoo', 'phase': phase, 'ready': ready},
                ])
                self.assertEqual(self.service.state, 'deploying')

    def test_pod_lookup_failure_still_blocks_running(self):
        self.service.write({'state': 'deploying'})
        self._poll([], pod_retrieval_error='Forbidden')
        self.assertEqual(self.service.state, 'deploying')

    def test_post_deploy_init_waits_until_pending_pod_is_ready(self):
        """pod 還沒 ready 不得進 init；ready 後進 ``initializing``。

        #1159：poll 不執行 init——init 只由獨立的 init cron 經單一入口
        ``_run_post_deploy_init`` 執行（真 cursor 行為在 ``test_init_execution_control_1257``
        與 ``test_init_exec_surface_1159``）；短 poll 觀測到 ready 時只把服務排進佇列。
        """
        self.template.post_deploy_init_type = 'odoo'
        self.service._open_init_generation()
        self.service.write({'state': 'deploying'})
        pods = [
            {'name': 'postgres', 'phase': 'Running', 'ready': '1/1'},
            {'name': 'odoo', 'phase': 'Pending', 'ready': '0/0'},
        ]
        with patch.object(type(self.service), '_run_post_deploy_init') as run_init:
            pending_client = self._poll(pods)
            self.assertEqual(self.service.state, 'deploying')
            run_init.assert_not_called()
            pods[1] = {'name': 'odoo', 'phase': 'Running', 'ready': '1/1'}
            ready_client = self._poll(pods)
        self.assertEqual(self.service.state, 'initializing')
        pending_client.init_odoo.assert_not_called()
        ready_client.init_odoo.assert_not_called()
        run_init.assert_not_called()
        self.assertTrue(self.service.init_ready_observed, '排進 init cron 的佇列')
