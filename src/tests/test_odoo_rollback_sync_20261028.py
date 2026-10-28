"""需在 Odoo 18 sandbox 執行：回復版本後，woow-odoo 租戶的平台記錄要與叢集一致。

2026-10-28 正式站重測（#2056 留言）的兩項觀察：

1. 回復到舊 revision 後，Helm values／Pod 只有舊的模組，平台的 ``odoo_addon_ids``（也就是模組分頁）卻
   仍是較新的勾選，之後在 Odoo 裡裝「平台以為有、Pod 沒有」的模組只會靜默停在 ``to install``。
2. Helm 回復把 chart conf Secret 的 ``admin-password`` 還原成舊 revision 的初始密碼，與 app DB 實際的
   密碼不符。

兩者都掛在回復後的設定同步（``_sync_rollback_config``）上，且都是 best-effort：讀不到、舊 operator、
目錄裡找不到的模組，都不得擋住原本的設定同步。
"""
import json
import logging
from unittest.mock import Mock, patch

from odoo import fields
from odoo.tests.common import TransactionCase, tagged

from ..models.cloud_service import _internal_provision
from ..services.paas_operator import PaaSOperatorAPIError, PaaSOperatorError

ADDONS_PATH = 'addons.selected'
CURRENT_PASSWORD = 'current-synthetic-admin-pw-1234'


@tagged('post_install', '-at_install')
class TestOdooRollbackSync(TransactionCase):
    def setUp(self):
        super().setUp()
        Addon = self.env['woow_paas_platform.cloud_odoo_addon']
        self.alpha = Addon.create({'name': 'Alpha', 'technical_name': 'qa_alpha', 'source_type': 'third_party'})
        self.beta = Addon.create({'name': 'Beta', 'technical_name': 'qa_beta', 'source_type': 'third_party'})
        self.gamma = Addon.create({'name': 'Gamma', 'technical_name': 'qa_gamma', 'source_type': 'custom'})
        self.core = Addon.create({'name': 'Core', 'technical_name': 'qa_core', 'source_type': 'built_in'})
        self.template = self.env.ref('woow_paas_platform.cloud_app_odoo')
        workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'Odoo rollback sync QA', 'owner_id': self.env.ref('base.user_admin').id})
        self.service = _internal_provision(self.env['woow_paas_platform.cloud_service']).create({
            'name': 'Odoo rollback sync QA', 'workspace_id': workspace.id, 'template_id': self.template.id,
            'state': 'upgrading', 'subdomain': 'qa-odoo-rb-sync', 'helm_namespace': 'paas-ws-qa',
            'helm_release_name': 'svc-qa-odoo', 'last_upgraded_at': fields.Datetime.now(),
            'helm_values': json.dumps({}),
            # 回復前的平台記錄：較新的勾選（含 beta），外加一個 built_in
            'odoo_addon_ids': [(6, 0, [self.alpha.id, self.beta.id, self.core.id])],
        })
        self.service._set_admin_password_secret(CURRENT_PASSWORD)
        self.service._record_rollback_config_sync(3)
        self.selected = ['qa_alpha', 'qa_gamma']          # 目標 revision 實際套用的清單
        self.client = Mock()
        self.client.get_config_projection.side_effect = self._projection
        self.client.sync_odoo_admin_secret.return_value = {'success': True, 'secret_sync': 'synced'}
        self.addon_projection_calls = []

    def _projection(self, namespace, release_name, revision, paths, excluded_paths=None):
        if list(paths) == [ADDONS_PATH]:
            self.addon_projection_calls.append(revision)
            return {'revision': revision, 'values': {ADDONS_PATH: list(self.selected)}, 'missing': []}
        return {'revision': revision, 'values': {}, 'missing': list(paths)}

    def _sync(self, revision=3):
        return self.service._sync_rollback_config(self.client, {'revision': revision})

    def _selected_names(self):
        return sorted(self.service.odoo_addon_ids.mapped('technical_name'))

    # ---- 模組勾選 ----

    def test_rollback_realigns_third_party_selection_with_the_target_revision(self):
        self.assertTrue(self._sync())
        # beta 不在目標 revision ⇒ 移除；gamma 在目標 revision ⇒ 補回；built_in 不受影響
        self.assertEqual(self._selected_names(), ['qa_alpha', 'qa_core', 'qa_gamma'])
        self.assertFalse(self.service.rollback_config_sync, '同步完成要清掉待辦')
        self.assertEqual(self.addon_projection_calls, [3], '只問目標 revision 的清單，且只問一次')

    def test_empty_target_selection_removes_every_third_party_module_but_keeps_built_in(self):
        self.selected = []
        self.assertTrue(self._sync())
        self.assertEqual(self._selected_names(), ['qa_core'])

    def test_unknown_module_in_the_target_revision_is_skipped_with_a_warning(self):
        self.selected = ['qa_alpha', 'qa_ghost']
        with self.assertLogs('odoo.addons.woow_paas_platform.models.cloud_service', level=logging.WARNING) as logs:
            self.assertTrue(self._sync())
        self.assertEqual(self._selected_names(), ['qa_alpha', 'qa_core'])
        self.assertTrue(any('qa_ghost' in line for line in logs.output))

    def test_archived_catalog_entry_in_the_target_revision_is_skipped_with_a_warning(self):
        """封存的模組平台其他路徑也視為不可選（``filtered('active')``）；硬補回只會留下看不見的關聯。"""
        self.gamma.active = False
        with self.assertLogs('odoo.addons.woow_paas_platform.models.cloud_service', level=logging.WARNING) as logs:
            self.assertTrue(self._sync())
        self.assertEqual(self._selected_names(), ['qa_alpha', 'qa_core'])
        self.assertTrue(any('qa_gamma' in line for line in logs.output))

    def test_operator_without_list_support_keeps_the_selection_and_does_not_block_the_sync(self):
        def refuse_list(namespace, release_name, revision, paths, excluded_paths=None):
            if list(paths) == [ADDONS_PATH]:
                raise PaaSOperatorAPIError('bounded scalar only', status_code=500)
            return {'revision': revision, 'values': {}, 'missing': list(paths)}
        self.client.get_config_projection.side_effect = refuse_list
        self.assertTrue(self._sync(), '舊 operator 不得讓回復後的設定同步卡住')
        self.assertEqual(self._selected_names(), ['qa_alpha', 'qa_beta', 'qa_core'])
        self.assertFalse(self.service.rollback_config_sync)

    def test_projection_for_another_revision_or_a_malformed_list_is_ignored(self):
        for bad in ({'revision': 99, 'values': {ADDONS_PATH: ['qa_alpha']}, 'missing': []},
                    {'revision': 3, 'values': {ADDONS_PATH: 'qa_alpha'}, 'missing': []},
                    {'revision': 3, 'values': {ADDONS_PATH: ['qa_alpha', 7]}, 'missing': []},
                    {'revision': 3, 'values': {}, 'missing': [ADDONS_PATH]}):
            self.client.get_config_projection.side_effect = None
            self.client.get_config_projection.return_value = bad
            self.service._record_rollback_config_sync(3)
            self.assertTrue(self._sync(), bad)
            self.assertEqual(self._selected_names(), ['qa_alpha', 'qa_beta', 'qa_core'], bad)

    def test_already_aligned_selection_is_not_rewritten(self):
        self.selected = ['qa_alpha', 'qa_beta']
        writes = []
        original_write = type(self.service).write

        def spy(record, vals, *args, **kwargs):
            writes.append(set(vals))
            return original_write(record, vals, *args, **kwargs)
        with patch.object(type(self.service), 'write', spy):
            self.assertTrue(self._sync())
        self.assertFalse([keys for keys in writes if 'odoo_addon_ids' in keys])
        self.assertEqual(self._selected_names(), ['qa_alpha', 'qa_beta', 'qa_core'])

    def test_selection_follows_the_ledger_write_atomically(self):
        pending = self.service.rollback_config_sync
        original_apply = type(self.service)._apply_rollback_odoo_addons

        def fail_after_write(record, names):
            original_apply(record, names)
            raise ValueError('synthetic write failure')
        with patch.object(type(self.service), '_apply_rollback_odoo_addons', fail_after_write):
            with self.assertRaises(ValueError):
                self._sync()
        self.assertEqual(self._selected_names(), ['qa_alpha', 'qa_beta', 'qa_core'], 'savepoint 回捲')
        self.assertEqual(self.service.rollback_config_sync, pending, '待辦保留，下一輪重試')

    # ---- conf Secret 密碼 ----

    def test_rollback_realigns_the_conf_secret_with_the_platform_password(self):
        self.assertTrue(self._sync())
        self.client.sync_odoo_admin_secret.assert_called_once_with(
            namespace='paas-ws-qa', release_name='svc-qa-odoo', password=CURRENT_PASSWORD)

    def test_no_secret_record_means_nothing_to_sync(self):
        self.env['woow_paas_platform.cloud_service_secret'].sudo().search(
            [('service_id', '=', self.service.id)]).unlink()
        self.assertTrue(self._sync())
        self.client.sync_odoo_admin_secret.assert_not_called()

    def test_secret_sync_failure_never_blocks_the_rollback_sync(self):
        self.client.sync_odoo_admin_secret.side_effect = PaaSOperatorError('synthetic outage')
        with self.assertLogs('odoo.addons.woow_paas_platform.models.cloud_service', level=logging.WARNING) as logs:
            self.assertTrue(self._sync())
        self.assertEqual(self._selected_names(), ['qa_alpha', 'qa_core', 'qa_gamma'])
        self.assertFalse(any(CURRENT_PASSWORD in line for line in logs.output))

    def test_old_operator_without_the_endpoint_is_skipped_quietly(self):
        self.client.sync_odoo_admin_secret.side_effect = PaaSOperatorAPIError('not found', status_code=404)
        with self.assertNoLogs('odoo.addons.woow_paas_platform.models.cloud_service', level=logging.WARNING):
            self.assertTrue(self._sync())

    # ---- 不適用的服務 ----

    def test_legacy_bitnami_release_is_not_touched(self):
        self.service._apply_helm_values({'postgresql': {'enabled': True}})
        self.service._record_rollback_config_sync(3)
        self.assertTrue(self._sync())
        self.assertEqual(self.addon_projection_calls, [])
        self.client.sync_odoo_admin_secret.assert_not_called()
        self.assertEqual(self._selected_names(), ['qa_alpha', 'qa_beta', 'qa_core'])

    def test_other_templates_never_ask_for_the_addon_list_or_sync_a_secret(self):
        other = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'Other QA', 'slug': 'qa-other-rb', 'category': 'ai_llm', 'helm_repo_url': '',
            'helm_chart_name': 'oci://qa/not-odoo', 'helm_chart_version': '0.1.0',
            'helm_value_specs': json.dumps({'optional': [{'key': 'config.timezone', 'type': 'text'}]}),
            'mcp_enabled': False, 'helm_default_values': '{}'})
        self.service.write({'template_id': other.id})
        self.service._record_rollback_config_sync(3)
        self.assertTrue(self._sync())
        self.assertEqual(self.addon_projection_calls, [])
        self.client.sync_odoo_admin_secret.assert_not_called()
