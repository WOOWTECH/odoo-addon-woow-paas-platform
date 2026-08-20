"""#778 配套 3 後半：計費靜默死亡的營運監控告警。

判準＝「凍結偵測」：存在 in_progress 且 overdue 超過 N 天的訂閱，且其
``woow_last_payment_failure_at`` 也凍結超過 N 天（或從未 stamp）。dunning
活著時，due 訂閱每天要嘛成功（next_payment 推進、不再 due）要嘛失敗
（failure_at 被 stamp 成當下）——兩個欄位同時凍結 N 天＝沒有任何計費路徑
碰過它，正是 #778 對撞（或 per-sub exception 被吞）的直接症狀。

刻意不用原字面「N 天零新 cycle invoice」當主判準：長期扣款失敗的訂閱
首日建發票、之後每日 reuse 同一張未結清發票（``_get_or_create_cycle_
invoice``），N 天後「零新發票」會對正常運作中的 dunning 誤報。

告警形態沿 ecpay_invoice_cron._notify_admin_on_error 慣例：mail.activity
派給 account.group_account_manager 成員；同 summary 的 activity 尚未處理
時不重複建（處理完 activity 消失，再犯再告警）。
"""
from datetime import timedelta

from odoo import fields
from odoo.tests.common import TransactionCase

# 與實作端常數刻意雙寫（同步哨兵，同 test_billing_ssot_guard 慣例）。
ALERT_DAYS_PARAM = 'woow_paas_platform.billing_silence_alert_days'
ALERT_SUMMARY = '計費路徑靜默：到期訂閱連續多日未被處理'


class _SilenceFixture(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Sub = self.env['sh.subscription.subscription'].sudo()
        self.ICP = self.env['ir.config_parameter'].sudo()
        self.Activity = self.env['mail.activity'].sudo()
        self.plan = self.env['sh.subscription.plan'].create({
            'name': 'Silence Plan', 'sh_plan_price': 499.0, 'sh_duration': 1,
            'sh_unit': 'month', 'sh_no_of_billing_cycle': -1, 'sh_trial': False,
        })
        product_tmpl = self.env['product.template'].sudo().create({
            'name': 'Silence Product', 'type': 'service',
            'sh_product_subscribe': True, 'sh_subscription_plan_id': self.plan.id,
            'list_price': 499.0,
        })
        self.product = product_tmpl.product_variant_id
        self.partner = self.env['res.partner'].create({'name': 'Silence Partner'})
        # 告警受派人：確保 manager group 至少有一個 user（測試自足，
        # 不依賴 demo/admin 的群組狀態）。
        self.manager = self.env['res.users'].sudo().create({
            'name': 'Billing Manager 778', 'login': 'billing_mgr_778',
            'groups_id': [(4, self.env.ref('account.group_account_manager').id)],
        })

    def _overdue_sub(self, overdue_days, failure_at=None):
        """建一張 overdue 指定天數的 in_progress 訂閱。"""
        today = fields.Date.today()
        sub = self.Sub.create({
            'sh_partner_id': self.partner.id,
            'sh_subscription_plan_id': self.plan.id,
            'product_id': self.product.id,
            'sh_plan_price': 499.0, 'sh_qty': 1,
            'sh_unit': 'month', 'sh_recurrency': 1,
            'sh_start_date': today - timedelta(days=60),
            'sh_end_date': today + timedelta(days=300),
            'sh_date_of_next_payment': today - timedelta(days=overdue_days),
            'state': 'in_progress',
        })
        if failure_at is not None:
            sub.write({'woow_last_payment_failure_at': failure_at})
        return sub

    def _alerts(self):
        return self.Activity.search([('summary', '=', ALERT_SUMMARY)])


class TestBillingSilenceAlert(_SilenceFixture):
    def test_alert_created_for_frozen_overdue_sub(self):
        # 陽性場景：overdue 超過門檻、failure_at 從未 stamp ＝ 兩路皆死。
        sub = self._overdue_sub(overdue_days=5)

        self.Sub._cron_billing_silence_alert()

        alerts = self._alerts()
        self.assertTrue(
            alerts,
            "凍結 5 天的到期訂閱必須觸發告警 activity（#778 靜默死亡症狀）",
        )
        self.assertIn(
            self.manager.id, alerts.mapped('user_id').ids,
            "告警必須派給 account.group_account_manager 成員",
        )
        self.assertIn(sub.id, alerts.mapped('res_id'))

    def test_no_alert_when_dunning_recently_stamped(self):
        # dunning 活著：failure_at 是昨天 ⇒ 有計費路徑在碰它，不告警。
        self._overdue_sub(
            overdue_days=5,
            failure_at=fields.Datetime.now() - timedelta(days=1),
        )

        self.Sub._cron_billing_silence_alert()

        self.assertFalse(
            self._alerts(),
            "failure_at 仍在更新＝dunning 運作中，不得誤報",
        )

    def test_no_alert_when_no_overdue_subscription(self):
        self._overdue_sub(overdue_days=5).write({'state': 'cancel'})

        self.Sub._cron_billing_silence_alert()

        self.assertFalse(self._alerts())

    def test_dedup_while_alert_open(self):
        self._overdue_sub(overdue_days=5)

        self.Sub._cron_billing_silence_alert()
        first = self._alerts()
        self.Sub._cron_billing_silence_alert()

        self.assertEqual(
            self._alerts().ids, first.ids,
            "同一波靜默在 activity 未被處理前不得重複告警",
        )

    def test_zero_days_disables_alert(self):
        self.ICP.set_param(ALERT_DAYS_PARAM, '0')
        self._overdue_sub(overdue_days=30)

        self.Sub._cron_billing_silence_alert()

        self.assertFalse(
            self._alerts(),
            "門檻 0 ＝ 停用告警（沿 max_consecutive_payment_failures 慣例）",
        )
