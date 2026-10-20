"""商用驗收 AUDITFIX noop-save-triggers-upgrade（X-ODOO-001-1）的 Odoo 迴歸測試。

## 缺陷

ws50 Odoo 239：owner 送 ``{'conf.adminEmail': <目前值>, 'loadDemoData': False}``（什麼都沒改）
⇒ success、helm revision 1→2、Pod 被換；rev1 vs rev2 的 ``conf.workers`` 2→3、
``limitMemoryHard/Soft`` → null、``limits.memory`` 2Gi→3Gi、``requests.cpu`` 2→500m，而
``allocated_ram_gb`` 仍是 2.0。

## 兩個根因，本檔各釘一條

1. **沒有變更也升級**：``_drop_create_only_values`` 把相同的 create_only 鍵拿掉之後，
   ``_update_service`` 不檢查 merged values 是否等於現值，照樣送背景 helm upgrade。
   PR #1798（service-update-api-validation）加了「有送值、正規化後與現值相同 ⇒ no_changes」
   的短路；``TestOdooNoopSaveAuditfix`` 用 Odoo 範本的真實 create_only spec 驗證它也蓋到
   X-ODOO-001-1（develop 上會紅：upgrade_release 被呼叫、state 變 upgrading）。
2. **資源 profile「漂移」**：查證結果不是開通與 reconfigure 兩條路算法不同——兩者都走
   ``_apply_plan_spec_overlay``。ws50 239 是 #1637（.225 migration，2026-10-10 使用者核可的
   容量研究）之前開通的：T1 方案從 2 GB／2 workers 改成 3 GB／3 workers、limitMemory 改交給
   chart 預設、requests 另算；#1158 的設計是「方案是規格真相源，任何 deploy／redeploy／
   reconfigure 都收斂到目前方案」。所以第一次 upgrade 就把舊 profile 收斂成新方案，而
   ``allocated_*`` 沒有跟著改。``TestOdooProvisionReconfigureParityAuditfix`` 釘住「以目前方案
   開通的服務，第一次 reconfigure 的方案鍵與開通時完全相同」——漂移只來自方案規格事後變更。
   要不要在 reconfigure 時收斂、收斂時 ``allocated_*``／ns quota 怎麼跟（會連動
   workspace-quota-auto-raise 的產品方向）屬產品決定，見 PR 描述。
"""
import copy
import json
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.woow_paas_platform.controllers.paas import PaasController
from ..models.cloud_service import _internal_provision

_CONTROLLER = 'odoo.addons.woow_paas_platform.controllers.paas'
_BG_CLIENT = 'odoo.addons.woow_paas_platform.services.paas_operator.PaaSOperatorClient'


class _SyncThread:
    def __init__(self, target=None, daemon=None, **kwargs):
        self._target = target

    def start(self):
        self._target()


class _OdooServiceFixture(TransactionCase):
    """照開通路徑造一台 Odoo 服務：範本預設 → 方案 overlay（開通模式）→ 首購訂閱。"""

    def setUp(self):
        super().setUp()
        self.admin = self.env.ref('base.user_admin')
        self.tpl = self.env['woow_paas_platform.cloud_app_template'].search(
            [('slug', '=', 'odoo')], limit=1)
        self.assertTrue(self.tpl, 'odoo 範本 seed 未載入')
        self.plan = self.env.ref('woow_paas_platform.plan_odoo_monthly')
        company = self.env.company.sudo()
        if not company.sh_journal_id:
            journal = self.env['account.journal'].sudo().search([('type', '=', 'sale')], limit=1)
            if journal:
                company.sh_journal_id = journal.id
        self.ws = self.env['woow_paas_platform.workspace'].create({
            'name': 'WS noop auditfix', 'owner_id': self.admin.id})
        Svc = self.env['woow_paas_platform.cloud_service']
        values = json.loads(self.tpl.helm_default_values or '{}')
        values.setdefault('conf', {})['adminEmail'] = 'owner@woowtech.io'
        values = Svc._apply_plan_spec_overlay(values, self.tpl, self.plan)
        self.provisioned = copy.deepcopy(values)
        self.svc = _internal_provision(Svc).create({
            'workspace_id': self.ws.id, 'template_id': self.tpl.id,
            'name': 'svc-noop-af', 'reference_id': 'test-noop-af-ref',
            'state': 'running', 'subdomain': 'svc-noop-af',
            'helm_namespace': 'paas-ws-noop-af', 'helm_release_name': 'svc-noopaf',
            'helm_chart_version': self.tpl.helm_chart_version or '0.2.7',
            **Svc._allocated_vals_from(self.tpl, self.plan),
        })
        self.svc._apply_helm_values(values)
        self.svc._create_and_confirm_subscription(self.admin.partner_id, self.plan)


@tagged('post_install', '-at_install')
class TestOdooNoopSaveAuditfix(_OdooServiceFixture):

    def _update(self, values):
        client = MagicMock()
        client.upgrade_release.return_value = {'revision': 2, 'chart_version': '0.2.7'}
        with patch(f'{_CONTROLLER}.get_paas_operator_client', return_value=client), \
                patch(f'{_CONTROLLER}.request', new=MagicMock(env=self.env(user=self.admin))), \
                patch(_BG_CLIENT, return_value=client), \
                patch(f'{_CONTROLLER}.threading.Thread', _SyncThread):
            result = PaasController()._update_service(self.svc, values=values, version=None)
        self.svc.invalidate_recordset()
        return result, client

    def test_saving_the_current_create_only_values_does_not_upgrade(self):
        """X-ODOO-001-1 的原始請求：值都沒變 ⇒ 不升級、不換 Pod、state 不動。"""
        before = self.svc._helm_values_dict()

        result, client = self._update(
            {'conf.adminEmail': 'owner@woowtech.io', 'loadDemoData': False})

        self.assertTrue(result.get('success'), result)
        self.assertTrue(result.get('no_changes'), result)
        client.upgrade_release.assert_not_called()
        self.assertEqual(self.svc.state, 'running')
        self.assertEqual(self.svc._helm_values_dict(), before)

    def test_changing_a_create_only_value_is_still_refused(self):
        """對照組：真的改了 create_only 值仍是拒絕（#1561），不是被短路吞成「沒有變更」。"""
        result, client = self._update({'conf.adminEmail': 'new-owner@woowtech.io'})

        self.assertFalse(result.get('success'), result)
        self.assertFalse(result.get('no_changes'), result)
        client.upgrade_release.assert_not_called()


@tagged('post_install', '-at_install')
class TestOdooProvisionReconfigureParityAuditfix(_OdooServiceFixture):

    def test_first_reconfigure_keeps_the_provisioned_plan_profile(self):
        """以目前方案開通的服務，reconfigure 組出的方案鍵（workers、記憶體、CPU）與開通時相同。"""
        assembled = self.svc._assemble_deploy_values()['values']

        self.assertEqual(assembled.get('conf', {}).get('workers'),
                         self.provisioned['conf']['workers'])
        self.assertEqual(assembled.get('conf', {}).get('limitMemoryHard'),
                         self.provisioned['conf'].get('limitMemoryHard'))
        self.assertEqual(assembled.get('resources'), self.provisioned.get('resources'))
        self.assertAlmostEqual(self.svc.allocated_ram_gb, self.plan.cs_ram_gb)
