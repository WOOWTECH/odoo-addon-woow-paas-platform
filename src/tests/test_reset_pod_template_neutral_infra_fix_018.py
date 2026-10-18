# -*- coding: utf-8 -*-
"""INFRA-FIX-018（商用驗收 INFRA-HS-002-1）：reset-config 不得改 pod template（平台側）。

Headscale 238 真 UI 改帳密 3/3 次失敗：operator 的 helm_secret 路是 ``helm upgrade --atomic
--timeout 15s``，平台送的 values 是 DB 快照，快照的方案資源是舊 overlay 的頂層 ``resources``
（chart 把它 deep-merge 進 headplane），叢集現況卻是 #1637 之後的 ``headscale.resources`` ⇒ reset
把兩個容器的資源對調、pod template 改變、StatefulSet 滾動、RWO 重新 attach 超過 15 秒。

修法分兩半：operator 在 upgrade 前把會左右 pod template 的鍵釘到叢集現況（operator 端測試
``test_reset_pod_template_neutral_infra_fix_018.py``）；平台這半只負責「說出是哪些鍵」並隨請求送出，
由本檔守住：

1. 路徑與方案寫入路徑同源（讀 ``_PLAN_SPEC_VALUE_PATHS``，不自建 slug 名單）。
2. 只有 helm_secret 載體送（config_json／app_managed 一律空清單 ⇒ 請求內容與修前一致）。
3. 路徑真的上了 wire，且空清單不上 wire（舊 operator／舊行為零差異）。
"""
import json
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase

from ..models.cloud_service import _PLAN_SPEC_VALUE_PATHS, _internal_provision
from ..services.paas_operator import PaaSOperatorClient

_OPERATOR_CLIENT = (
    'odoo.addons.woow_paas_platform.services.paas_operator.get_paas_operator_client'
)


class TestResetPodTemplateValuePaths(TransactionCase):

    def _paths_for(self, template):
        service = self.env['woow_paas_platform.cloud_service'].new({
            'template_id': template.id,
        })
        return service._reset_pod_template_value_paths()

    def test_headscale_pins_both_the_new_and_the_legacy_resources_key(self):
        template = self.env.ref('woow_paas_platform.cloud_app_headscale')
        self.assertEqual(template.admin_password_carrier, 'helm_secret')
        self.assertEqual(
            self._paths_for(template), ['headscale.resources', 'resources'],
            'headscale 的方案資源在 headscale.resources；頂層 resources 是舊 overlay 殘值，'
            'chart 會把它蓋到 headplane——兩個都要以叢集為準，否則 reset 會把資源對調（238 實測）')

    def test_default_path_templates_pin_only_the_top_level_key(self):
        # hermes-agent：方案資源走預設的頂層 resources（沒有路徑搬家，也就沒有殘值要清）。
        template = self.env.ref('woow_paas_platform.cloud_app_hermes_agent')
        self.assertEqual(template.admin_password_carrier, 'helm_secret')
        self.assertNotIn(template.slug, _PLAN_SPEC_VALUE_PATHS)
        self.assertEqual(self._paths_for(template), ['resources'])

    def test_paths_follow_the_plan_spec_table_for_every_helm_secret_template(self):
        """結構不變式：每個 helm_secret 範本的第一條路徑＝方案寫入路徑（不是硬編清單）。"""
        templates = self.env['woow_paas_platform.cloud_app_template'].with_context(
            active_test=False,
        ).search([('admin_password_carrier', '=', 'helm_secret')])
        self.assertTrue(templates, '探針讀不到 helm_secret 範本——守線失去鑑別力')
        for template in templates:
            expected = _PLAN_SPEC_VALUE_PATHS.get(template.slug, {}).get(
                'resources', ('resources',))
            paths = self._paths_for(template)
            self.assertEqual(paths[0], '.'.join(expected), template.slug)
            self.assertLessEqual(len(paths), 2, template.slug)

    def test_non_helm_secret_carriers_send_nothing(self):
        templates = self.env['woow_paas_platform.cloud_app_template'].with_context(
            active_test=False,
        ).search([('admin_password_carrier', '!=', 'helm_secret')])
        self.assertTrue(templates, '探針讀不到非 helm_secret 範本——守線失去鑑別力')
        for template in templates:
            self.assertEqual(self._paths_for(template), [], template.slug)


class TestResetClientWire(TransactionCase):

    def _body(self, **kwargs):
        client = PaaSOperatorClient('http://operator.test', 'k')
        with patch.object(
                PaaSOperatorClient, '_request', return_value={'helm_revision': 2}) as req:
            client.reset_release_config(
                namespace='ns', release_name='rel', values={'a': 1},
                sensitive_values={'config': {'sensitive': {'admin_password': 'x'}}},
                **kwargs)
        args, call_kwargs = req.call_args
        self.assertEqual(args, ('POST', '/api/releases/ns/rel/reset-config'))
        return call_kwargs['data']

    def test_paths_are_sent_when_given(self):
        body = self._body(
            admin_password_carrier='helm_secret',
            preserve_value_paths=['headscale.resources', 'resources'])
        self.assertEqual(body['preserve_value_paths'], ['headscale.resources', 'resources'])

    def test_empty_or_missing_paths_do_not_change_the_request(self):
        for kwargs in ({}, {'preserve_value_paths': []}, {'preserve_value_paths': None}):
            self.assertNotIn('preserve_value_paths', self._body(**kwargs), kwargs)


class TestResetSendsThePaths(TransactionCase):
    """_reset_admin_password 真的把路徑交給 operator client（不只是 helper 存在）。"""

    def setUp(self):
        super().setUp()
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'Reset pod template workspace',
            'owner_id': self.env.ref('base.user_admin').id,
        })

    def _service(self, template, release):
        return _internal_provision(
            self.env['woow_paas_platform.cloud_service'].with_context(
                mcp_admin_password='old-password-0000',
            )).create({
                'name': 'Svc %s' % release,
                'workspace_id': self.workspace.id,
                'template_id': template.id,
                'state': 'running',
                'helm_namespace': 'paas-ws-test',
                'helm_release_name': release,
                'helm_values': json.dumps({
                    # 與 238 的 DB 快照同形：舊 overlay 的頂層 resources。
                    'resources': {
                        'limits': {'cpu': '1', 'memory': '1Gi'},
                        'requests': {'cpu': '1', 'memory': '1Gi'},
                    },
                }),
            })

    def _reset(self, service):
        client = MagicMock()
        client.reset_release_config.return_value = {'helm_revision': 2}
        with patch(_OPERATOR_CLIENT, return_value=client):
            service._reset_admin_password()
        return client.reset_release_config.call_args.kwargs

    def test_headscale_reset_sends_the_paths_and_still_sends_the_stored_snapshot(self):
        template = self.env.ref('woow_paas_platform.cloud_app_headscale')
        kwargs = self._reset(self._service(template, 'svc-fix018-hs'))
        self.assertEqual(
            kwargs['preserve_value_paths'], ['headscale.resources', 'resources'])
        # 平台仍送快照（由 operator 釘到叢集現況）；其餘旋鈕不變。
        self.assertTrue(kwargs['reset_values'])
        self.assertEqual(kwargs['admin_password_carrier'], 'helm_secret')

    def test_config_json_reset_sends_no_paths(self):
        template = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'MCP fix018', 'slug': 'mcp-fix018', 'category': 'mcp',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'mcp-server', 'helm_chart_version': '1.0.0',
            'mcp_enabled': False, 'has_admin_gui': True,
        })
        kwargs = self._reset(self._service(template, 'svc-fix018-mcp'))
        self.assertEqual(kwargs['admin_password_carrier'], 'config_json')
        self.assertEqual(kwargs['preserve_value_paths'], [])


class TestRevisionListHidesFailedRows(TransactionCase):
    """回復清單不列 failed revision（INFRA-HS-002 觀察：238 的 rev 8～13 全是 failed）。"""

    HISTORY = [
        {'revision': 7, 'status': 'superseded', 'chart': 'headscale-0.1.4',
         'description': 'Upgrade complete'},
        {'revision': 8, 'status': 'failed', 'chart': 'headscale-0.1.4',
         'description': 'Upgrade "svc" failed: context deadline exceeded'},
        {'revision': 9, 'status': 'failed', 'chart': 'headscale-0.1.4',
         'description': 'Release "svc" failed: context deadline exceeded'},
        {'revision': 10, 'status': 'deployed', 'chart': 'headscale-0.1.4',
         'description': 'Rollback to 7 (credentials preserved)'},
    ]

    def test_failed_rows_are_dropped_and_input_is_untouched(self):
        from ..services.chart_version import drop_failed_revisions
        kept = drop_failed_revisions(self.HISTORY)
        self.assertEqual([r['revision'] for r in kept], [7, 10])
        self.assertEqual(len(self.HISTORY), 4)

    def test_other_statuses_and_junk_survive(self):
        from ..services.chart_version import drop_failed_revisions
        rows = [{'revision': 1, 'status': 'pending-upgrade'},
                {'revision': 2, 'status': 'FAILED'},
                {'revision': 3},
                'not-a-dict']
        self.assertEqual(drop_failed_revisions(rows), [rows[0], rows[2], rows[3]])
        self.assertEqual(drop_failed_revisions(None), [])

    def test_the_rollback_baseline_still_reads_the_full_history(self):
        """過濾放在標註之後：基準（目前部署中的 revision）不受 failed 列影響。"""
        from ..services.chart_version import annotate_rollback_floor, drop_failed_revisions
        listing = drop_failed_revisions(annotate_rollback_floor(self.HISTORY))
        self.assertEqual([r['rollback_allowed'] for r in listing], [True, True])
        self.assertEqual([r['chart_version'] for r in listing], ['0.1.4', '0.1.4'])
