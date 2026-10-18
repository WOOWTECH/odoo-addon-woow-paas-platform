# -*- coding: utf-8 -*-
"""#1414／#1456：帳本 ``helm_revision`` 在更新／回復後不得落後，回復對話框的「目前」以部署中版本為準。

商用驗收 7 個缺陷檔（AI-HER-007-1、AI-LLM-017-4、AI-OD-004-3、AI-OD-006-2、DATA-N8N-009-2、
INFRA-TS-002-1、INFRA-TS-003-1）：更新／回復後帳本落後 1 版 2.5～9 分鐘以上，回復對話框把舊版
標成「目前」。根因兩段：

1. operator ``_parse_release_info`` 讀 ``info.revision``，但 helm install／upgrade 的 JSON 把版次
   放在頂層 ``version`` ⇒ 一律回報 0（operator 端守線見
   ``extra/paas-operator/tests/test_release_revision_1414.py``）。
2. 平台拿 0 寫進帳本（``.get('revision', …)`` 鍵存在就用），或在單調分支永遠不寫；回復路徑
   完全不寫帳本；對話框又用帳本標「目前」。

本檔守平台這一半：只收正整數 revision（``reported_helm_revision``）、回復當場記下新 revision、
對話框的「目前」改由 helm history 的部署中版本決定（``annotate_rollback_floor`` 的
``is_current``）。背景 upgrade／redeploy／首裝／poll 的回寫守線放在各自既有的測試檔
（``test_platform_ux_r8``、``test_chart_version_ledger_552``、``test_poll_mirror_release_facts_627``）。
"""
import json
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.woow_paas_platform.controllers.paas import PaasController
from ..models.cloud_service import _internal_provision
from ..services.chart_version import annotate_rollback_floor, reported_helm_revision

_CONTROLLER = 'odoo.addons.woow_paas_platform.controllers.paas'


def _rev(number, version, status):
    return {'revision': number, 'chart': 'tailscale-node-%s' % version, 'status': status,
            'updated': '2026-10-16T00:00:00Z', 'app_version': '1', 'description': ''}


# INFRA-TS-003-1 的形狀：rev 3 已部署，帳本還停在 2。
HISTORY = [
    _rev(1, '0.1.9', 'superseded'),
    _rev(2, '0.1.9', 'superseded'),
    _rev(3, '0.1.9', 'deployed'),
]


@tagged('post_install', '-at_install')
class TestHelmRevisionHelpers1414(TransactionCase):
    """純函式：可寫進帳本的 revision、對話框的「目前」。"""

    def test_reported_helm_revision_accepts_only_positive_ints(self):
        """operator 讀不到版次時回 0（「不知道」）——0／缺值不得寫進帳本。"""
        self.assertEqual(reported_helm_revision(7), 7)
        self.assertEqual(reported_helm_revision(1), 1)
        for bad in (0, -1, None, True, False, '7', 'x', 3.0, {}):
            with self.subTest(bad=bad):
                self.assertIsNone(reported_helm_revision(bad))

    def test_deployed_revision_is_current(self):
        self.assertEqual([r['is_current'] for r in annotate_rollback_floor(HISTORY)],
                         [False, False, True])
        self.assertNotIn('is_current', HISTORY[2], '不得改呼叫端的物件')

    def test_failed_upgrade_is_not_current(self):
        history = [_rev(1, '0.1.9', 'deployed'), _rev(2, '0.2.0', 'failed')]
        self.assertEqual([r['is_current'] for r in annotate_rollback_floor(history)],
                         [True, False], '升級失敗的新版不是目前；前一版仍部署中')

    def test_update_in_progress_keeps_the_deployed_one_current(self):
        history = [_rev(4, '0.1.9', 'superseded'), _rev(5, '0.1.9', 'deployed'),
                   _rev(6, '0.1.9', 'pending-upgrade')]
        self.assertEqual([r['is_current'] for r in annotate_rollback_floor(history)],
                         [False, True, False])

    def test_no_deployed_revision_marks_nothing(self):
        """沒有部署中的版本就不標（前端退回帳本值）。"""
        self.assertEqual(
            [r['is_current'] for r in annotate_rollback_floor([_rev(1, '0.1.9', 'failed')])],
            [False])


@tagged('post_install', '-at_install')
class TestRollbackRecordsRevision1414(TransactionCase):
    """回復建立新 revision——帳本當場記下，不等 poll／每日對帳。"""

    def setUp(self):
        super().setUp()
        self.controller = PaasController()
        self.admin_user = self.env.ref('base.user_admin')
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'Helm revision 1414 workspace',
            'owner_id': self.admin_user.id,
        })
        self.template = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'Helm revision 1414',
            'slug': 'helm-revision-1414',
            'category': 'ai_llm',
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example/helm-revision-1414',
            'helm_chart_version': '0.1.9',
            'mcp_enabled': False,
            'helm_default_values': '{}',
        })
        self.service = _internal_provision(
            self.env['woow_paas_platform.cloud_service']).create({
                'name': 'Svc 1414',
                'workspace_id': self.workspace.id,
                'template_id': self.template.id,
                'state': 'running',
                'helm_namespace': 'paas-ws-rev1414',
                'helm_release_name': 'svc-rev1414',
                'helm_chart_version': '0.1.9',
                'helm_revision': 3,
                'helm_values': json.dumps({}),
            })

    def _rollback(self, result, revision=2):
        client = MagicMock()
        client.get_revisions.return_value = {'revisions': HISTORY}
        client.rollback_release.return_value = result
        with patch(f'{_CONTROLLER}.get_paas_operator_client', return_value=client), \
                patch(f'{_CONTROLLER}.request',
                      new=MagicMock(env=self.env(user=self.admin_user))):
            return self.controller._rollback_service(self.service, revision=revision)

    def test_reported_revision_reaches_the_ledger(self):
        response = self._rollback({'message': 'ok', 'mode': 'rollback',
                                   'chart_version': '0.1.9', 'revision': 4})
        self.assertTrue(response.get('success'), response)
        self.assertEqual(self.service.state, 'upgrading', '前提：回復真的送出了')
        self.assertEqual(self.service.helm_revision, 4)

    def test_missing_or_zero_revision_uses_history(self):
        """operator 沒回報或回 0 ⇒ history 最大值＋1（與設定同步錨點同一個數），不寫 0。"""
        for result in ({'message': 'ok', 'mode': 'rollback', 'revision': 0},
                       {'message': 'ok', 'mode': 'rollback'}):
            with self.subTest(result=result):
                self.service.sudo().write({'state': 'running', 'helm_revision': 3})
                response = self._rollback(result)
                self.assertTrue(response.get('success'), response)
                self.assertEqual(self.service.helm_revision, 4)
