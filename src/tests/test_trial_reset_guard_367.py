"""#367：已訂閱用戶換方案不得重啟 7 天免費試用（trial reset guard）。

seed 的 251 plan 全 ``sh_trial=True``。舊行為：``_create_and_confirm_subscription``
對任何 trial plan 一律 ``_force_trial_dates_from_plan``，蓋掉第三方 sh_subscription
內建的 ``sub_exists``「既有 partner 不再給試用」保護，於是老客戶走 change-plan
（先關舊訂閱再建新訂閱）會以全新 7 天 trial 重啟、首期扣 0。

修法（per-entity，依 Eugene 2026-07-19 #367 留言的語意修正——試用綁 service/
entity、一生一次、不綁 partner）：建新 sub 前看「此 entity 是否已持有訂閱」
（``self.subscription_id``；change-plan 只把舊 sub 設 close、不解綁連結，resume
不重建 sub）——已持有 → 同一 service 的換方案/加席/暫停後重啟，不進 trial（走
active dates、首期實扣）；未持有 → 全新 service（含同一 partner 的第 2、3 個
service），各自享有自己的 7 天 trial。
"""
from datetime import date, timedelta

from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase

from odoo.addons.woow_paas_platform.controllers.paas import PaasController
from ..models.cloud_service import _internal_provision


class TestTrialResetGuard367(TransactionCase):
    """per-entity：同一 service 的換方案 / 加席 / 暫停後重啟不得重啟 trial；
    全新 service（含同 partner 的第二個 service）與全新首購各自享有 7 天 trial。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Plan = cls.env['sh.subscription.plan'].sudo()
        cls.ProductTmpl = cls.env['product.template'].sudo()
        cls.Template = cls.env['woow_paas_platform.cloud_app_template'].sudo()
        cls.Workspace = cls.env['woow_paas_platform.workspace'].sudo()
        cls.Service = cls.env['woow_paas_platform.cloud_service'].sudo()
        cls.Users = cls.env['res.users'].sudo()
        cls.Move = cls.env['account.move'].sudo()
        cls.controller = PaasController()

        # 基礎 7 天 trial plan + AC 席數變體 plan（換方案 / 加席用）。
        cls.plan_base = cls._make_trial_plan('367 Base (7d trial)', 100.0)
        cls.plan_variant = cls._make_trial_plan(
            '367 AC Seat Variant (7d trial)', 150.0, seats=1)

        cls.template = cls.Template.create({
            'name': '367 Tmpl', 'slug': 'guard-367',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'guard-367', 'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
            'subscription_plan_ids': [
                (6, 0, [cls.plan_base.id, cls.plan_variant.id])],
        })

    @classmethod
    def _make_trial_plan(cls, name, price, seats=0):
        vals = {
            'name': name, 'sh_plan_price': price,
            'sh_duration': 1, 'sh_unit': 'month', 'sh_no_of_billing_cycle': -1,
            'sh_trial': True, 'sh_trial_duration': 7, 'sh_trial_unit': 'day',
        }
        if seats:
            vals['ac_max_access_seats'] = seats
        plan = cls.Plan.create(vals)
        cls.ProductTmpl.create({
            'name': name + ' P', 'type': 'service',
            'sh_product_subscribe': True,
            'sh_subscription_plan_id': plan.id, 'list_price': price,
        })
        return plan

    def _fresh_partner_and_service(self, tag):
        """每個測試用獨立 owner/partner + 全新 service（entity），避免跨測試污染。"""
        owner = self.Users.create({
            'name': '367 Owner %s' % tag, 'login': '367_owner_%s' % tag,
            'email': '367_owner_%s@example.test' % tag,
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        ws = self.Workspace.create(
            {'name': '367 WS %s' % tag, 'owner_id': owner.id})
        svc = _internal_provision(self.Service).create({
            'name': '367 Svc %s' % tag, 'workspace_id': ws.id,
            'template_id': self.template.id,
        })
        return owner, ws.owner_id.partner_id, svc

    def _patch_request(self, env):
        return patch(
            'odoo.addons.woow_paas_platform.controllers.paas.request',
            new=MagicMock(env=env),
        )

    # -- 全新 partner 首購 → 仍給 7 天 trial ---------------------------- #

    def test_new_partner_first_purchase_gets_trial(self):
        _, partner, svc = self._fresh_partner_and_service('fresh')
        svc._create_and_confirm_subscription(partner, self.plan_base)
        sub = svc.sudo().subscription_id

        self.assertTrue(sub.sh_trial, '全新 partner 首購必須有 trial')
        today = date.today()
        expected_trial_end = today + timedelta(days=7) - timedelta(days=1)
        self.assertEqual(sub.sh_trial_end_date, expected_trial_end)
        # trial 期尚未開始計費 → sh_start_date 在未來 → confirm 不建首期發票（扣 0）。
        self.assertEqual(sub.sh_start_date, expected_trial_end + timedelta(days=1))
        self.assertFalse(
            self.Move.search([('sh_subscription_id', '=', sub.id)]),
            'trial 首購不應產生首期發票（扣 0）',
        )

    # -- 老 partner 換方案（change-plan 完整 HTTP 路徑）→ 無 trial + 實扣 - #

    def test_returning_partner_change_plan_no_trial(self):
        owner, partner, svc = self._fresh_partner_and_service('churn')
        # 首購（全新）→ trial。
        svc._create_and_confirm_subscription(partner, self.plan_base)
        self.assertTrue(svc.sudo().subscription_id.sh_trial)

        # 換方案：走完整 controller 路徑（先關舊訂閱再建新）。
        with self._patch_request(self.env(user=owner)):
            res = self.controller.api_service_change_plan(
                service_id=svc.id, plan_id=self.plan_variant.id)
        self.assertTrue(res['success'], res.get('error'))

        new_sub = svc.sudo().subscription_id
        self.assertFalse(
            new_sub.sh_trial,
            '#367：已訂閱過的 partner 換方案不得重啟 trial',
        )
        self.assertEqual(new_sub.sh_subscription_plan_id, self.plan_variant)
        # 首期正常實扣（非 0）：非 trial → sh_start_date=today → confirm 建首期發票。
        self.assertEqual(new_sub.sh_start_date, date.today())
        invoices = self.Move.search([('sh_subscription_id', '=', new_sub.id)])
        self.assertTrue(invoices, '#367：換方案首期必須實扣（產生發票）')
        self.assertGreater(
            sum(invoices.mapped('amount_total')), 0.0,
            '#367：首期發票金額必須 > 0（非 trial 的 0 元）',
        )

    # -- 老 partner 加 AC 席數變體 → 無 trial --------------------------- #

    def test_returning_partner_add_seat_variant_no_trial(self):
        owner, partner, svc = self._fresh_partner_and_service('seat')
        svc._create_and_confirm_subscription(partner, self.plan_base)
        self.assertTrue(svc.sudo().subscription_id.sh_trial)

        with self._patch_request(self.env(user=owner)):
            res = self.controller.api_service_change_plan(
                service_id=svc.id, plan_id=self.plan_variant.id)
        self.assertTrue(res['success'], res.get('error'))

        new_sub = svc.sudo().subscription_id
        self.assertFalse(
            new_sub.sh_trial,
            '#367：老 partner 換到 AC 席數變體同樣不得重啟 trial',
        )
        self.assertEqual(new_sub.sh_subscription_plan_id.ac_max_access_seats, 1)

    # -- per-entity 判別（load-bearing）：同 partner 第二個新 service → 有 trial - #

    def test_second_service_same_partner_gets_trial(self):
        """#367 per-entity 語意判別（最重要一條）：試用綁 service/entity、一生
        一次、不綁 partner。同一 partner 開第二個「新 service」是全新 entity
        （自己的 subscription_id 為空），必須享有自己的 7 天 trial。

        per-partner 舊實作（partner 已訂閱過就不給）會讓這條變紅——正是本次
        per-partner → per-entity 修正的判別點。
        """
        _, partner, svc1 = self._fresh_partner_and_service('multi')
        svc1._create_and_confirm_subscription(partner, self.plan_base)
        self.assertTrue(
            svc1.sudo().subscription_id.sh_trial, '首個 service 首購有 trial')

        # 同 partner 第二個 service = 全新 entity（自己沒訂閱過）→ 有自己的 trial。
        svc2 = _internal_provision(self.Service).create({
            'name': '367 Svc multi-2', 'workspace_id': svc1.workspace_id.id,
            'template_id': self.template.id,
        })
        svc2._create_and_confirm_subscription(partner, self.plan_base)
        sub2 = svc2.sudo().subscription_id
        self.assertTrue(
            sub2.sh_trial,
            '#367 per-entity：第二個新 service 是全新 entity，享有自己的 7 天 trial',
        )
        today = date.today()
        expected_trial_end = today + timedelta(days=7) - timedelta(days=1)
        self.assertEqual(sub2.sh_trial_end_date, expected_trial_end)
        # trial 期未開始計費 → sh_start_date 在未來 → 不建首期發票（扣 0）。
        self.assertEqual(sub2.sh_start_date, expected_trial_end + timedelta(days=1))
        self.assertFalse(
            self.Move.search([('sh_subscription_id', '=', sub2.id)]),
            '第二個 service 的 trial 首購同樣不應產生首期發票（扣 0）',
        )
