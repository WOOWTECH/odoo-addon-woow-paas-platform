"""Regression tests for BUG-5: change_plan refund overshoot during trial.

Before the fix, the refund formula
``price * (sh_end_date - today) / (sh_end_date - sh_start_date)``
overshot when ``today < sh_start_date`` (trial period not yet over).
With a 7-day trial and a 30-day cycle, ``remaining_days (29)`` exceeded
``total_days (22)``, producing refund = 1.318 × price for a customer
who had not paid anything yet.

The fix lives on ``sh.subscription.subscription._compute_unused_refund``
(``models/sh_subscription_fix.py``); both the Smart Home change-plan
controller and the back-office Change-Plan wizard call it. These tests
exercise the helper directly (the source of truth) plus the wizard's
compute, since the controller path requires an HTTP request fixture.
"""
from datetime import date, timedelta

from odoo.tests.common import TransactionCase
from ..models.cloud_service import _internal_provision


class TestChangePlanRefund(TransactionCase):
    """BUG-5: ``_compute_unused_refund`` returns 0 during trial / pre-start."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.partner = cls.env.ref('base.partner_admin')
        cls.Sub = cls.env['sh.subscription.subscription'].sudo()

    def _make_sub(self, start_offset_days, next_payment_offset_days,
                  price=100.0, cycle_days=30):
        """Build a sub record without going through confirm/onchange.

        #327：視窗改以 sh_date_of_next_payment 推導，故顯式提供 next_payment；
        day-based cycle（sh_recurrency=cycle_days）使 cycle_days 固定、斷言穩定。
        """
        today = date.today()
        return self.Sub.new({
            'sh_partner_id': self.partner.id,
            'sh_start_date': today + timedelta(days=start_offset_days),
            'sh_date_of_next_payment': today + timedelta(days=next_payment_offset_days),
            'sh_plan_price': price,
            'sh_unit': 'day', 'sh_recurrency': cycle_days,
        })

    # --- BUG-5 regressions（trial guard 保留）---------------------------- #

    def test_refund_zero_during_trial_period(self):
        """today < sh_start_date — customer has not paid → refund = 0."""
        sub = self._make_sub(start_offset_days=7, next_payment_offset_days=7)
        self.assertEqual(sub._prorata_unused_refund(), 0)

    def test_refund_full_when_today_at_cycle_start(self):
        """today == cycle_start（next_payment − cycle）→ 全額（尚未消耗）。"""
        sub = self._make_sub(start_offset_days=0, next_payment_offset_days=30,
                             cycle_days=30)
        self.assertEqual(sub._prorata_unused_refund(), 100.0)

    # --- happy path ------------------------------------------------------ #

    def test_refund_prorated_mid_paid_cycle(self):
        """Half-way through a paid cycle → ~half refund."""
        # cycle_start=-15, remaining=15, cycle=30 → 50
        sub = self._make_sub(start_offset_days=-15, next_payment_offset_days=15,
                             cycle_days=30)
        self.assertEqual(sub._prorata_unused_refund(), 50.0)

    def test_refund_zero_at_next_payment(self):
        """today == next_payment（週期用盡）→ refund = 0（原 end-of-cycle 語意）。"""
        sub = self._make_sub(start_offset_days=-30, next_payment_offset_days=0,
                             cycle_days=30)
        self.assertEqual(sub._prorata_unused_refund(), 0)

    def test_refund_prorata_past_frozen_end_date(self):
        """#327 翻紅→翻綠：長壽 in_progress 活過凍結 sh_end_date（過期），但
        next_payment 在未來 → 退剩餘比例（不再恆退 0）。原 test_refund_zero_
        after_end_date 固化了「過期即退 0」的 bug，此處按新語意重寫。"""
        today = date.today()
        sub = self.Sub.new({
            'sh_partner_id': self.partner.id,
            'sh_start_date': today - timedelta(days=90),
            'sh_end_date': today - timedelta(days=60),          # 凍結 end 已過
            'sh_date_of_next_payment': today + timedelta(days=15),
            'sh_plan_price': 100.0, 'sh_unit': 'day', 'sh_recurrency': 30,
        })
        self.assertEqual(sub._prorata_unused_refund(), 50.0)

    # --- guards --------------------------------------------------------- #

    def test_refund_zero_when_next_payment_missing(self):
        """無 sh_date_of_next_payment（新視窗來源缺失）→ refund = 0。"""
        sub = self.Sub.new({
            'sh_partner_id': self.partner.id,
            'sh_start_date': date.today() - timedelta(days=5),
            'sh_date_of_next_payment': False,
            'sh_plan_price': 100.0, 'sh_unit': 'day', 'sh_recurrency': 30,
        })
        self.assertEqual(sub._prorata_unused_refund(), 0)

    def test_refund_zero_when_start_date_missing(self):
        """Missing sh_start_date（trial guard 需要）→ refund = 0。"""
        sub = self.Sub.new({
            'sh_partner_id': self.partner.id,
            'sh_start_date': False,
            'sh_date_of_next_payment': date.today() + timedelta(days=10),
            'sh_plan_price': 100.0, 'sh_unit': 'day', 'sh_recurrency': 30,
        })
        self.assertEqual(sub._prorata_unused_refund(), 0)

    def test_refund_zero_when_cycle_unresolvable(self):
        """sh_unit 非法 → _cycle_delta None → refund = 0（div-by-zero 護欄）。"""
        sub = self.Sub.new({
            'sh_partner_id': self.partner.id,
            'sh_start_date': date.today() - timedelta(days=5),
            'sh_date_of_next_payment': date.today() + timedelta(days=10),
            'sh_plan_price': 100.0, 'sh_unit': False, 'sh_recurrency': 1,
        })
        self.assertEqual(sub._prorata_unused_refund(), 0)

    # --- wizard parity --------------------------------------------------- #

    def test_wizard_refund_uses_helper_during_trial(self):
        """Wizard's ``refund_amount`` mirrors helper output (0 during trial).

        Smoke test that the wizard's compute routes through the same helper
        and so inherits the BUG-5 fix.
        """
        # Use real cloud_service + sub so the wizard's onchange chain works.
        Workspace = self.env['woow_paas_platform.workspace'].sudo()
        # ⚠ `owner_id` 是 `res.users` 的 m2o，原本塞的是 **partner id**（既有的
        # fixture bug，只是一直沒有東西去讀它）。#519 提案 B 會讀
        # `workspace_id.owner_id.partner_id` 來比對訂閱歸屬，於是它現在會出事。
        # 本 class 的 `cls.partner` 就是 `base.partner_admin`，對應的使用者是
        # `base.user_admin` ⇒ 兩邊對得上，訂閱歸屬成立。
        owner = self.env.ref('base.user_admin')
        self.assertEqual(owner.partner_id, self.partner,
                         'fixture 前提：workspace owner 的 partner 必須就是訂閱的 '
                         'sh_partner_id，否則測的不是本來要測的東西')
        ws = Workspace.create({
            'name': 'BUG5 ws', 'slug': 'bug5ws',
            'owner_id': owner.id,
        })
        Service = self.env['woow_paas_platform.cloud_service'].sudo()
        template = self.env['woow_paas_platform.cloud_app_template'].sudo().search([], limit=1)
        if not template:
            self.skipTest("No cloud_app_template available in test DB")
        svc = _internal_provision(Service).create({
            'name': 'BUG5 svc',
            'workspace_id': ws.id,
            'template_id': template.id,
            'subdomain': 'bug5svc',
            'state': 'pending',
        })
        # Build sub directly with trial dates (today < sh_start_date).
        today = date.today()
        sub = self.Sub.create({
            'sh_partner_id': self.partner.id,
            'sh_start_date': today + timedelta(days=7),
            'sh_date_of_next_payment': today + timedelta(days=7),
            'sh_end_date': today + timedelta(days=29),
            'sh_plan_price': 100.0,
            'sh_unit': 'month',
            'sh_recurrency': 1,
            'state': 'in_progress',
        })
        # #324：seed 一筆 done deduct 讓 payment gate 通過，使本測試回歸為驗證
        # trial 日期 clamp（BUG-5：today < sh_start_date → 0）而非被 gate 短路。
        self.env['wallet.transaction'].sudo().create({
            'partner_id': self.partner.id, 'transaction_type': 'deduct',
            'amount': -100.0, 'subscription_id': sub.id, 'state': 'done',
        })
        svc.subscription_id = sub.id
        plan_other = self.env['sh.subscription.plan'].sudo().search([], limit=1)
        wiz = self.env['woow_paas_platform.change_plan_wizard'].sudo().create({
            'cloud_service_id': svc.id,
            'current_plan_id': plan_other.id,  # placeholder, refund logic uses sub
            'new_plan_id': plan_other.id,
        })
        wiz._compute_refund()
        self.assertEqual(
            wiz.refund_amount, 0,
            "Wizard should not refund during trial (BUG-5)",
        )


class TestChangePlanRefundIsRefundTyped(TransactionCase):
    """#318: 換 plan 退點交易須為 refund 型（原本被記成 topup）。"""

    def setUp(self):
        from unittest.mock import MagicMock, patch
        from odoo.addons.woow_paas_platform.controllers.paas import PaasController
        super().setUp()
        self._patch = patch
        self._MagicMock = MagicMock
        self.controller = PaasController()
        self.Plan = self.env['sh.subscription.plan']
        self.WalletTx = self.env['wallet.transaction']
        self.plan_a = self._make_plan('CP Refund A', 300.0)
        self.plan_b = self._make_plan('CP Refund B', 500.0)
        template = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'CP Refund Tmpl', 'slug': 'cp-refund',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'cp-refund', 'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
            'subscription_plan_ids': [(6, 0, [self.plan_a.id, self.plan_b.id])],
        })
        self.owner = self.env['res.users'].create({
            'name': 'CP Refund Owner', 'login': 'cp_refund_owner',
            'email': 'cp_refund_owner@example.test',
        })
        self.owner.groups_id = [(6, 0, [self.env.ref('base.group_user').id])]
        ws = self.env['woow_paas_platform.workspace'].create({
            'name': 'CP Refund WS', 'owner_id': self.owner.id,
        })
        self.service = _internal_provision(self.env['woow_paas_platform.cloud_service']).create({
            'name': 'CP Refund Svc', 'workspace_id': ws.id,
            'template_id': template.id,
        })
        partner = ws.owner_id.partner_id
        self.service._create_and_confirm_subscription(partner, self.plan_a)
        self.partner = partner
        # #324：換 plan 退點也走 payment gate，fixture 補首購真扣款
        sub = self.service.sudo().subscription_id
        self.WalletTx.create({
            'partner_id': partner.id, 'transaction_type': 'deduct',
            'amount': -sub.sh_plan_price, 'subscription_id': sub.id,
            'state': 'done',
        })

    def _make_plan(self, name, price):
        plan = self.Plan.create({
            'name': name, 'sh_plan_price': price, 'sh_duration': 1,
            'sh_unit': 'month', 'sh_no_of_billing_cycle': -1, 'sh_trial': False,
        })
        self.env['product.template'].sudo().create({
            'name': name + ' P', 'type': 'service', 'sh_product_subscribe': True,
            'sh_subscription_plan_id': plan.id, 'list_price': price,
        })
        return plan

    def _refund_txns(self):
        return self.WalletTx.search([
            ('partner_id', '=', self.partner.id),
            ('transaction_type', '=', 'refund'),
        ])

    def test_change_plan_refund_is_refund_typed(self):
        req = self._MagicMock(env=self.env(user=self.owner))
        with self._patch(
            'odoo.addons.woow_paas_platform.controllers.paas.request', new=req):
            res = self.controller.api_service_change_plan(
                service_id=self.service.id, plan_id=self.plan_b.id)
        self.assertTrue(res.get('success'))
        refund_txns = self._refund_txns()
        self.assertEqual(len(refund_txns), 1)
        self.assertEqual(refund_txns.amount, 300.0)  # 當天換 → 全額退

    def test_wizard_change_plan_refund_is_refund_typed(self):
        """後台 wizard 路徑（api「四個入口」之一，與 controller 分開驗證，
        防 wizard 呼叫點被漏改／回退）。wizard 是 model method，免 patch
        request。"""
        wizard = self.env['woow_paas_platform.change_plan_wizard'].create({
            'cloud_service_id': self.service.id,
            'current_plan_id': self.plan_a.id,
            'new_plan_id': self.plan_b.id,
        })
        wizard.action_change_plan()
        refund_txns = self._refund_txns()
        self.assertEqual(len(refund_txns), 1)
        self.assertEqual(refund_txns.amount, 300.0)
