"""#389（code 部分）：低餘額通知的 dedup marker 必須跟著「這封信的下場」走。

## 缺陷

``_on_payment_failure``（``src/models/sh_subscription_fix.py``）原本是：

    if subscription.woow_last_low_balance_warned_for != warn_key:
        self._notify_low_balance(partner, invoice)
        subscription.write({'woow_last_low_balance_warned_for': warn_key})

marker 在**呼叫的當下**就寫死，而 ``_notify_low_balance`` 有兩個「什麼都沒送出去
也不會讓呼叫端知道」的出口：

1. 找不到 ``mail_template_low_balance`` → 記一行 warning 就 ``return``。
2. ``send_mail`` 丟例外 → ``except Exception`` 吞掉。

更關鍵的是第三條：``force_send=False`` 只把信**排進佇列**，真正的投遞是 Odoo 的
mail queue cron。佇列送失敗的信會停在 ``mail.mail.state == 'exception'``，而
Odoo 的 mail scheduler 只撿 ``outgoing``——**沒有任何人會重試它**。

三條路的後果一樣：marker 已經寫下去了，於是 ``sh_date_of_next_payment`` 不變的
整個續費週期內，這個租戶再也收不到低餘額提醒——而這正是 #389（prod SMTP 全
exception）當下實際發生的事。dunning cron 每天都會重跑這條路徑（filter 是
``next_payment <= today``），本來就有重試的節奏，只是被 marker 擋掉了。

## 修法契約

marker 從「我呼叫過通知函式」改成「**這個週期有一封還活著的通知**」：

* 排入佇列失敗（無 template／例外）→ 不寫 marker → 明天再試。
* 排入成功 → 寫 marker，並把 ``mail.mail`` 記成載體。
* 載體停在 ``exception``（投遞失敗）→ 視為沒送到 → 重送。
* 載體是 ``outgoing`` / ``sent`` → 不重送（去重仍然有效）。
* 載體不存在（Odoo 對 ``auto_delete`` 的信在送出成功後會刪掉；舊資料同樣是空）
  → 視為已送達 → 不重送。這條讓升級後的既有資料維持原本行為，不會在上線當天
  對所有欠費租戶補發一輪。

重送有天然上限：連續失敗到 ``max_consecutive_payment_failures``（預設 6）就
auto-cancel，訂閱離開 ``in_progress`` 後 cron 不再撈它。
"""
from datetime import timedelta
from unittest.mock import patch

from odoo import api, fields
from odoo.tests.common import TransactionCase

_LOW_BALANCE_TEMPLATE_XMLID = 'woow_paas_platform.mail_template_low_balance'


def _patch_template_missing():
    """只讓低餘額模板的 ``env.ref`` 查不到，其他 xmlid 照常。

    不動 ``ir.model.data`` 本體（那會連帶清 ormcache、影響同 run 的其他測試），
    也不整支 mock 掉 ``_notify_low_balance``（那就變成測 mock 而不是測那條
    early-return）。
    """
    real_ref = api.Environment.ref

    def fake_ref(env, xml_id, raise_if_not_found=True):
        if xml_id == _LOW_BALANCE_TEMPLATE_XMLID:
            if raise_if_not_found:
                raise ValueError('External ID not found: %s' % xml_id)
            return None
        return real_ref(env, xml_id, raise_if_not_found=raise_if_not_found)

    return patch.object(api.Environment, 'ref', fake_ref)


class TestLowBalanceDedupRetry(TransactionCase):
    """dedup marker 的四種載體狀態 × 兩種排入結果。"""

    def setUp(self):
        super().setUp()
        self.Subscription = self.env['sh.subscription.subscription']
        self.MailMail = self.env['mail.mail']
        self.today = fields.Date.today()

        self.partner = self.env['res.partner'].create({
            'name': 'Low Balance Retry Partner',
            'email': 'low_balance_retry@example.test',
            'wallet_balance': 0.0,
        })
        plan = self.env['sh.subscription.plan'].create({
            'name': 'Low Balance Retry Plan',
            'sh_plan_price': 500.0,
            'sh_duration': 1,
            'sh_unit': 'month',
            'sh_no_of_billing_cycle': -1,
        })
        product_tmpl = self.env['product.template'].create({
            'name': 'Low Balance Retry Product',
            'type': 'service',
            'sh_product_subscribe': True,
            'sh_subscription_plan_id': plan.id,
            'list_price': plan.sh_plan_price,
        })
        self.subscription = self.Subscription.create({
            'sh_partner_id': self.partner.id,
            'product_id': product_tmpl.product_variant_id.id,
            'sh_subscription_plan_id': plan.id,
        })
        self.subscription.write({
            'state': 'in_progress',
            'sh_date_of_next_payment': self.today,
        })
        self.invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'invoice_line_ids': [(0, 0, {
                'name': 'Low Balance Retry fee',
                'quantity': 1,
                'price_unit': 500.0,
            })],
        })
        # 這組測試的前提是模板存在（缺模板是另一條被測分支，見
        # test_missing_template_does_not_stamp_marker，那條自己 patch）。
        self.assertTrue(self.env.ref(
            'woow_paas_platform.mail_template_low_balance',
            raise_if_not_found=False,
        ), "mail_template_low_balance 必須存在，否則整組測試失去意義")

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #

    def _fail_once(self):
        """跑一次 dunning cron 的失敗分支（threshold=0 ＝ 關掉 auto-cancel）。"""
        self.Subscription._on_payment_failure(
            self.subscription, self.invoice, self.today, 0,
        )

    def _queued_mails(self):
        """本 partner 這條通知線上目前存在的 mail.mail（依 id 排序）。"""
        return self.MailMail.sudo().search(
            [('email_to', 'ilike', self.partner.email)], order='id',
        )

    # ------------------------------------------------------------------ #
    # 排入成功 → 記 marker ＋ 記載體
    # ------------------------------------------------------------------ #

    def test_successful_queue_stamps_marker_and_carrier(self):
        """成功排入佇列才記 marker，且必須留下可回查的載體。

        沒有載體就無從判斷「這封信後來到底送出去了沒」——重試語意會退化回
        原本那個「呼叫過就算數」的缺陷。
        """
        self._fail_once()
        self.assertEqual(
            self.subscription.woow_last_low_balance_warned_for, self.today)
        carrier = self.subscription.woow_last_low_balance_mail_id
        self.assertTrue(
            carrier,
            "marker 必須連同 mail.mail 載體一起記——否則無法判斷投遞下場。")
        self.assertEqual(carrier.state, 'outgoing')

    # ------------------------------------------------------------------ #
    # 排入失敗 → 不得記 marker（明天要重試）
    # ------------------------------------------------------------------ #

    def test_send_mail_exception_does_not_stamp_marker(self):
        """``send_mail`` 丟例外（被 ``_notify_low_balance`` 吞掉）不得記 marker。"""
        with patch.object(
            type(self.env['mail.template']), 'send_mail',
            side_effect=Exception('SMTP boom'),
        ):
            self._fail_once()
        self.assertFalse(
            self.subscription.woow_last_low_balance_warned_for,
            "信根本沒排進佇列卻記了 marker——這個續費週期就再也不會提醒了（#389）。")
        self.assertFalse(self.subscription.woow_last_low_balance_mail_id)

    def test_missing_template_does_not_stamp_marker(self):
        """模板不存在（``_notify_low_balance`` 提早 return）不得記 marker。"""
        with _patch_template_missing():
            self._fail_once()
        self.assertFalse(
            self.subscription.woow_last_low_balance_warned_for,
            "沒有模板就等於沒通知，marker 不得寫下去。")

    def test_failed_queue_is_retried_on_next_tick(self):
        """排入失敗當次不記 marker，隔天 cron 再跑就真的補送出去。"""
        with patch.object(
            type(self.env['mail.template']), 'send_mail',
            side_effect=Exception('SMTP boom'),
        ):
            self._fail_once()
        self.assertFalse(self._queued_mails())

        self._fail_once()  # 隔天那一 tick，SMTP 恢復
        self.assertTrue(
            self._queued_mails(),
            "第一次排入失敗後，下一次 tick 必須重試——否則整個週期靜音。")
        self.assertEqual(
            self.subscription.woow_last_low_balance_warned_for, self.today)

    # ------------------------------------------------------------------ #
    # 載體狀態決定要不要重送
    # ------------------------------------------------------------------ #

    def test_outgoing_carrier_is_not_resent(self):
        """信還在佇列裡（``outgoing``）→ 同週期不得重送（去重仍要有效）。"""
        self._fail_once()
        first = self._queued_mails()
        self.assertEqual(len(first), 1)

        self._fail_once()
        self.assertEqual(
            self._queued_mails(), first,
            "同一個週期、信還在排隊，不該再塞第二封（原本的 dedup 語意）。")

    def test_exception_carrier_is_resent(self):
        """投遞失敗（``exception``）→ 同週期必須重送。

        這是 #389 的核心：Odoo 的 mail scheduler 只撿 ``outgoing``，停在
        ``exception`` 的信沒有任何人會重試它。marker 若不看載體下場，租戶這個
        續費週期就永遠收不到提醒。
        """
        self._fail_once()
        carrier = self.subscription.woow_last_low_balance_mail_id
        carrier.sudo().write({
            'state': 'exception',
            'failure_reason': 'SMTP connection refused',
        })

        self._fail_once()
        mails = self._queued_mails()
        self.assertEqual(
            len(mails), 2,
            "投遞失敗的信沒有人會重試，必須由這條路徑補送。")
        new_carrier = self.subscription.woow_last_low_balance_mail_id
        self.assertNotEqual(
            new_carrier, carrier, "marker 的載體必須換成新送的那一封。")
        self.assertEqual(new_carrier.state, 'outgoing')

    def test_deleted_carrier_is_not_resent(self):
        """載體不存在 → 視為已送達，不重送。

        Odoo 對 ``auto_delete`` 的信在**送出成功後**會直接刪掉 mail.mail，所以
        「查不到載體」的正常成因就是送成功了。升級前的既有資料（欄位為 NULL）
        走同一條，於是上線當天不會對所有欠費租戶補發一輪。
        """
        self._fail_once()
        self.subscription.woow_last_low_balance_mail_id.sudo().unlink()
        self.subscription.invalidate_recordset()

        self._fail_once()
        self.assertFalse(
            self._queued_mails(),
            "載體被刪＝送出成功，不該重送。")

    # ------------------------------------------------------------------ #
    # 既有 per-cycle 語意不得回歸
    # ------------------------------------------------------------------ #

    def test_new_cycle_sends_again(self):
        """換一個續費週期（warn_key 變了）→ 照舊重新提醒一次。"""
        self._fail_once()
        self.assertEqual(len(self._queued_mails()), 1)

        self.subscription.write({
            'sh_date_of_next_payment': self.today + timedelta(days=30),
        })

        self._fail_once()
        self.assertEqual(
            len(self._queued_mails()), 2,
            "下一個續費週期必須重新提醒（per-cycle 去重，不是 per-subscription）。")
