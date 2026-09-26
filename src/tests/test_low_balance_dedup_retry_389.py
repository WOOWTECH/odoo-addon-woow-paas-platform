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
* 排入成功 → 寫 marker，並把 ``mail.mail`` 記成載體；載體會顯式設為
  ``auto_delete=False``，避免 Odoo 自動刪除後失去投遞下場。
* 載體停在 ``exception``（投遞失敗）→ 視為沒送到 → 重送，但只在
  ``_LOW_BALANCE_RETRY_WINDOW_DAYS``（目前 7 天）的視窗內追送。
* 載體是 ``outgoing`` / ``sent`` / ``received`` → 不重送（去重仍然有效）。
* 載體是 ``cancel`` → 明確不重送，並留下 warning 供維運追查。
* 載體不存在 → 採保守 fallback，不重送。這條涵蓋升級前欄位為 NULL 的既有資料
  與被人工清掉的信，避免上線當天對所有欠費租戶補發一輪；不代表信已送出成功。
"""
from datetime import timedelta
from unittest.mock import patch

from odoo import api, fields
from odoo.tests.common import TransactionCase

_LOW_BALANCE_TEMPLATE_XMLID = 'woow_paas_platform.mail_template_low_balance'
#: prod 的 ``max_consecutive_payment_failures`` 預設。測試一律帶這個值，不要用
#: 0（＝停用 auto-cancel）當「同週期可以多跑幾 tick」的免費午餐——那正是 R1
#: review S2 指出的巧合依賴。
_REALISTIC_THRESHOLD = 6


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
            # 直接 create 不會跑 onchange 鏈，`sh_plan_price` 會留 0——而 D-3
            # 預警 cron 的第一道 guard 就是 `price <= 0: return`。不設的話那組
            # 測試會因為「cron 根本沒跑到」而紅，看起來像抓到缺陷，其實是 fixture
            # 壞掉（假 RED）。
            'sh_plan_price': 500.0,
        })
        self.assertEqual(
            self.subscription.sh_plan_price, 500.0,
            "fixture 前提：預警 cron 需要 sh_plan_price > 0 才會評估這筆訂閱。")
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

    def _fail_once(self, threshold=_REALISTIC_THRESHOLD):
        """跑一次 dunning cron 的失敗分支。

        預設帶**正常的** threshold（6，等同 prod 預設），不是 0。R1 review S2：
        原本這裡硬寫 0（＝關掉 auto-cancel），於是「同週期可以連跑好幾 tick」
        變成靠「threshold 被停用」這個巧合成立——而 threshold=0 恰好也是
        重送上限唯一的來源被抽掉的那個設定。要驗上限就明寫 0（見
        ``test_retry_window_bounds_resends_when_threshold_disabled``），不要讓
        一般測試順便依賴它。
        """
        self.Subscription._on_payment_failure(
            self.subscription, self.invoice, self.today, threshold,
        )

    def _run_pre_renewal_cron(self):
        """跑一次 D-3 預警 cron（marker 的**第二個**寫入者）。"""
        self.env['woow_paas_platform.low_balance_warning_cron'] \
            ._cron_warn_upcoming_renewal_low_balance()

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
        """載體不存在 → 採保守 fallback，不重送。

        低餘額通知載體已顯式設為 ``auto_delete=False``，因此查不到載體不代表
        送出成功。這條 fallback 涵蓋升級前的既有資料（欄位為 NULL）與被人工
        清掉的信，避免上線當天對所有欠費租戶補發一輪。
        """
        self._fail_once()
        self.subscription.woow_last_low_balance_mail_id.sudo().unlink()
        self.subscription.invalidate_recordset()

        self._fail_once()
        self.assertFalse(
            self._queued_mails(),
            "載體不存在時採保守 fallback，不該重送。")

    def test_cancelled_carrier_is_not_resent(self):
        """載體被取消（``cancel``）→ 不重送，但要留 warning（R1 review S4）。

        ``mail.mail.state='cancel'`` 只由 ``mail.cancel()`` 明寫（維運手動取消／
        程式主動放棄），不是投遞管道的暫時性故障——重送必然再得到一次 cancel，
        而且會蓋掉維運「不要送這封」的意思。所以判準要**顯式**把它列成不重送，
        而不是靠「`state != 'exception'` 剛好涵蓋到」。
        """
        self._fail_once()
        carrier = self.subscription.woow_last_low_balance_mail_id
        carrier.sudo().write({'state': 'cancel'})

        self._fail_once()
        self.assertEqual(
            len(self._queued_mails()), 1,
            "cancel 是明確的『不要送』，重送只會再拿到一次 cancel。")

    def test_carrier_is_not_auto_deleted(self):
        """載體必須關掉 ``auto_delete``（R1 review S3）。

        `addons/mail/models/mail_mail.py:304-305`（Odoo 18）：

            if not failure_type or failure_type in ['mail_email_invalid',
                                                    'mail_email_missing']:
                self.sudo().filtered(lambda mail: mail.auto_delete).unlink()

        也就是 auto_delete 的信被刪有**三種**成因，不只「送成功」——收件人
        email 無效或缺漏也會被刪。而 ``mail.template`` 沒顯式設 auto_delete 就
        吃預設 True，本平台的低餘額模板正是如此。若載體會被這樣刪掉，
        「查不到載體 ＝ 已送達」就會把「這個人的 email 根本是壞的」誤判成送到
        了，整個週期靜音——換一個殼的同一個 #389。

        所以凡是要當**投遞下場載體**的信，都在排入後顯式關掉 auto_delete。
        （SMTP 斷線 ``mail_smtp`` 不在上面那個刪除清單裡，所以 #389 本尊即使
        沒有這條也還是有效；這條堵的是同族的另外兩個入口。）
        """
        self._fail_once()
        carrier = self.subscription.woow_last_low_balance_mail_id
        self.assertFalse(
            carrier.auto_delete,
            "載體會被 auto_delete 清掉的話，invalid/missing email 會被誤判成已送達。")

    def test_retry_window_bounds_resends_when_threshold_disabled(self):
        """重送上限不得只靠 dunning threshold（R1 review S2）。

        ``max_consecutive_payment_failures = 0`` 是**合法設定**（Decision 3：
        0 ＝ 停用 auto-cancel）。那條路上訂閱永遠留在 ``in_progress``、
        ``next_payment <= today`` 也永遠成立，於是 dunning cron 每天都會重跑
        ——「投遞失敗就重送」在沒有獨立上限時就是每天一封、無限。

        改用與設定無關的視窗：同一個 warn_key 只追著重試
        ``_LOW_BALANCE_RETRY_WINDOW_DAYS`` 天。
        """
        overdue_key = self.today - timedelta(days=30)
        self.subscription.write({'sh_date_of_next_payment': overdue_key})
        self._fail_once(threshold=0)
        carrier = self.subscription.woow_last_low_balance_mail_id
        self.assertTrue(carrier, "第一封仍要送（視窗是給重送用的）")
        carrier.sudo().write({'state': 'exception'})

        self._fail_once(threshold=0)
        self.assertEqual(
            len(self._queued_mails()), 1,
            "同一個週期逾期 30 天還在每天重送＝把 threshold=0 當成無限額度。")

    # ------------------------------------------------------------------ #
    # marker 的第二個寫入者：D-3 預警 cron（R1 review S1）
    # ------------------------------------------------------------------ #

    def test_pre_renewal_cron_stamps_carrier(self):
        """預警 cron 也必須記載體——它寫的是**同一個** marker 欄位。

        兩個寫入者只要有一個不記載體，讀取端的「查不到載體＝已送達」就會把
        對方寫下的 marker 判成已送達。
        """
        self.subscription.write({
            'sh_date_of_next_payment': self.today + timedelta(days=3),
        })
        self._run_pre_renewal_cron()

        self.assertEqual(len(self._queued_mails()), 1)
        self.assertEqual(
            self.subscription.woow_last_low_balance_warned_for,
            self.today + timedelta(days=3),
        )
        self.assertTrue(
            self.subscription.woow_last_low_balance_mail_id,
            "預警 cron 寫了 marker 卻沒記載體——dunning 端會判成已送達。")

    def test_pre_renewal_cron_resends_when_previous_delivery_failed(self):
        """預警 cron 自己的去重也要看載體下場（D-3 送失敗，D-2 要補送）。"""
        self.subscription.write({
            'sh_date_of_next_payment': self.today + timedelta(days=3),
        })
        self._run_pre_renewal_cron()
        self._queued_mails().sudo().write({'state': 'exception'})

        self._run_pre_renewal_cron()
        self.assertEqual(
            len(self._queued_mails()), 2,
            "預警 cron 的去重仍停在『有 marker 就跳過』，投遞失敗不會補送。")

    def test_pre_renewal_failure_does_not_silence_dunning(self):
        """#389 的 prod 主場景：D-3 預警排信 → SMTP 掛 → D 日 dunning 靜音。

        時序（``warn_key`` 兩邊都是 ``sh_date_of_next_payment``，**同一個鍵**）：

        1. D-3：預警 cron 排信、寫 marker。修法前它不記載體。
        2. SMTP 掛：那封信停在 ``exception``，Odoo scheduler 只撿 ``outgoing``
           ⇒ 沒有人會重試它。
        3. D 日：扣款失敗，dunning 的 warn_key 恰好等於同一個 marker →
           讀取端看到「有 marker、查無載體」→ 判成已送達 → **整個週期靜音**。

        也就是說只修 dunning 那一個寫入者，#389 的主場景根本沒被修到。
        """
        renewal = self.today + timedelta(days=3)
        self.subscription.write({'sh_date_of_next_payment': renewal})

        # ① D-3 預警
        self._run_pre_renewal_cron()
        pre_renewal_mail = self._queued_mails()
        self.assertEqual(len(pre_renewal_mail), 1)
        self.assertEqual(
            self.subscription.woow_last_low_balance_warned_for, renewal)

        # ② SMTP 掛
        pre_renewal_mail.sudo().write({
            'state': 'exception', 'failure_reason': 'SMTP connection refused',
        })

        # ③ D 日 dunning（warn_key = sh_date_of_next_payment = 同一個鍵）
        self._fail_once()
        self.assertEqual(
            len(self._queued_mails()), 2,
            "D-3 的預警信投遞失敗後，D 日的扣款失敗必須補一封——這正是 #389 "
            "的 prod 主場景。",
        )

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
