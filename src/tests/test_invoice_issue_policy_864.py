"""#864 PR-C：發票開立政策 resolver（fail-closed）。

病：`res.company.auto_invoice` 的值域是 ``manual`` / ``automatic`` / ``hand in``
（vendored ``ecpay_invoice_tw/models/company.py``），而「該不該自動開票」這個
政策原本沒有任何載體——訂閱家族四個建票入口吃欄位 default ``not_required``、
儲值發票則寫死 ``pending``。

修法：把政策放進記錄的狀態值，由**一顆** resolver 依 ``auto_invoice`` 決定。
🔴 fail-closed：只有明確等於 ``'automatic'`` 才回 ``'pending'``（自動開票），
其餘值（含 ``hand in``、未來上游新增的值、缺 company）一律回 ``'manual'``。
⛔ 不得寫成 ``!= 'manual'``——那會讓未來新增的值默默變成自動開票。
"""
from datetime import date, timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class TestInvoiceIssuePolicyResolver864(TransactionCase):
    """`account.move._woow_resolve_invoice_issue_state()` 的完整真值表。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.AccountMove = cls.env['account.move']
        cls.company = cls.env.company

    def _resolve(self):
        return self.AccountMove._woow_resolve_invoice_issue_state(self.company)

    def _force_auto_invoice(self, raw_value):
        """繞過 Selection 驗證直接寫 DB，模擬上游未來新增的值。

        Selection 只在 ORM 寫入時驗證，上游 addon 升版新增值時 DB 裡就會出現
        本模組沒見過的字串——這正是 fail-closed 要擋的情境，用 stub 物件測不
        出來（stub 不會經過 ORM 讀取路徑）。
        """
        self.env.cr.execute(
            "UPDATE res_company SET auto_invoice = %s WHERE id = %s",
            (raw_value, self.company.id),
        )
        self.company.invalidate_recordset(['auto_invoice'])

    def test_automatic_resolves_to_pending(self):
        """`automatic` 是唯一會自動開票的值。"""
        self.company.auto_invoice = 'automatic'
        self.assertEqual(self._resolve(), 'pending')

    def test_manual_resolves_to_manual(self):
        """`manual`（上游 default）由人工開票。"""
        self.company.auto_invoice = 'manual'
        self.assertEqual(self._resolve(), 'manual')

    def test_hand_in_resolves_to_manual(self):
        """🔴 `hand in` 是第三個合法值，必須落到 manual（fail-closed 陽性證明）。

        這條是 `!= 'manual'` 寫法的判別式：那種寫法會讓 `hand in` 變成
        `pending`，也就是「人工填入」模式下平台照樣自動打 ECPay。
        """
        self.company.auto_invoice = 'hand in'
        self.assertEqual(self._resolve(), 'manual')

    def test_unknown_future_value_resolves_to_manual(self):
        """上游未來新增的值不得默默變成自動開票。"""
        self._force_auto_invoice('some_future_mode')
        self.assertEqual(self.company.auto_invoice, 'some_future_mode',
                         '前提檢查：DB 真的被寫進未知值，否則本測試無鑑別力')
        self.assertEqual(self._resolve(), 'manual')

    def test_empty_company_resolves_to_manual(self):
        """傳進空 recordset（呼叫端拿不到 company）也 fail-closed。"""
        empty = self.env['res.company']
        self.assertEqual(
            self.AccountMove._woow_resolve_invoice_issue_state(empty),
            'manual',
        )


class TestSubscriptionInvoiceStamp864(TransactionCase):
    """驗收 1／2／3／7：訂閱家族的 choke point 有沒有真的 stamp 出政策值。

    四個建票入口（上游 `sh_subscription_confirm` / `sh_generate_invoice` /
    `_sh_create_period_invoice` ＋ 平台 `subscription_lifecycle_mixin._skip_trial`）
    共用同一支 `_generate_invoice_vals()`，所以測 vals builder 就等於一次測到四個
    入口——⛔ 只測其中一個入口＝只測了四分之一。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Sub = cls.env['sh.subscription.subscription']

    def _make_sub(self):
        partner = self.env['res.partner'].create({'name': 'PolicyStamp864'})
        today = date.today()
        return self.Sub.sudo().create({
            'sh_partner_id': partner.id,
            'sh_unit': 'month',
            'sh_recurrency': 1,
            'sh_plan_price': 100.0,
            'sh_qty': 1,
            'sh_start_date': today - timedelta(days=1),
            'sh_end_date': today + timedelta(days=29),
            'sh_date_of_next_payment': today,
            'state': 'in_progress',
            'sh_trial': False,
        })

    def _stamped_state(self, policy):
        self.env.company.auto_invoice = policy
        sub = self._make_sub()
        return sub._generate_invoice_vals().get('woow_invoice_issue_state')

    def test_manual_policy_stamps_manual(self):
        """驗收 1：`auto_invoice='manual'` 下訂閱發票 stamp 出 `manual`。

        修法前這裡拿到的是 `None`（vals 根本不含這個 key）⇒ move 吃欄位 default
        `not_required` ⇒ 永遠不開票、連手動補救鈕都按不下去。
        """
        self.assertEqual(self._stamped_state('manual'), 'manual')

    def test_automatic_policy_stamps_pending(self):
        """驗收 2：`automatic` 下 stamp 出 `pending`，async cron 才撿得到。"""
        self.assertEqual(self._stamped_state('automatic'), 'pending')

    def test_hand_in_policy_stamps_manual(self):
        """驗收 3：`hand in` 下 stamp 出 `manual`（fail-closed 在呼叫點也成立）。"""
        self.assertEqual(self._stamped_state('hand in'), 'manual')

    def test_cycle_invoice_still_not_required_under_both_policies(self):
        """驗收 7：cycle invoice 兩種政策下都仍是 `not_required`（別改壞既有路徑）。

        cycle invoice 由 wallet 扣款結算（儲值時已開過 ECPay 發票），
        `_get_or_create_cycle_invoice` 在拿到 vals **之後**才顯式覆寫成
        `not_required` ⇒ 必須勝過本 PR 新增的 choke point stamp。
        """
        for policy in ('manual', 'automatic', 'hand in'):
            with self.subTest(policy=policy):
                self.env.company.auto_invoice = policy
                sub = self._make_sub()
                invoice, is_new = self.Sub._get_or_create_cycle_invoice(sub)
                self.assertTrue(is_new, '前提檢查：必須是新開的發票才有鑑別力')
                self.assertEqual(
                    invoice.woow_invoice_issue_state, 'not_required',
                    'cycle invoice 不得因為本 PR 的 choke point 而變成會開票',
                )


class TestTopupInvoiceStamp864(TransactionCase):
    """驗收 6：儲值發票（本 repo 目前唯一實際會跑的開票路徑）也吃政策。"""

    def setUp(self):
        super().setUp()
        self.partner = self.env['res.partner'].create({
            'name': 'TopupPolicy864',
            'email': 'topup864@test.com',
        })
        self.plan = self.env['woow_paas_platform.topup_plan'].search([], limit=1)
        if not self.plan:
            self.plan = self.env['woow_paas_platform.topup_plan'].create({
                'name': 'Policy Test Plan',
                'amount': 100.0,
                'credits': 100.0,
                'bonus': 0.0,
            })

    def _make_topup(self):
        return self.env['woow.wallet.topup'].sudo().create({
            'partner_id': self.partner.id,
            'plan_id': self.plan.id,
            'amount': self.plan.amount,
            'credits': self.plan.credits,
            'bonus': self.plan.bonus,
            'currency_id': self.env.ref('base.TWD').id,
            'source': 'manual',
            'state': 'pending',
        })

    def test_topup_invoice_manual_policy_stamps_manual(self):
        """驗收 6：`manual` 下儲值發票也是 `manual`。

        修法前這裡寫死 `'pending'` ⇒ 平台 async cron（完全不看 auto_invoice）
        照樣自動開票，「paas 選 manual」根本不生效（裁決 ④ 要修的那格）。
        """
        self.env.company.auto_invoice = 'manual'
        invoice = self._make_topup()._ensure_topup_invoice()
        self.assertEqual(invoice.woow_invoice_issue_state, 'manual')

    def test_topup_invoice_automatic_policy_stamps_pending(self):
        """`automatic` 下維持既有行為（`pending`）。"""
        self.env.company.auto_invoice = 'automatic'
        invoice = self._make_topup()._ensure_topup_invoice()
        self.assertEqual(invoice.woow_invoice_issue_state, 'pending')

    def test_topup_invoice_keeps_skip_upstream_hook_under_manual(self):
        """⛔ 陷阱守線：`woow_skip_upstream_hook` 與本政策正交，不得被順手動掉。

        它擋的是上游 `ecpay_invoice_tw` 的 inline hook 重複開票，與
        manual/automatic 政策是兩回事（設計 §PR-C「⛔ 陷阱」）。
        """
        self.env.company.auto_invoice = 'manual'
        invoice = self._make_topup()._ensure_topup_invoice()
        self.assertTrue(invoice.woow_skip_upstream_hook)


class TestCronAndRetryAcceptManual864(TransactionCase):
    """驗收 4／5：cron 不撿 `manual`，但手動補救鈕按得下去。"""

    def setUp(self):
        super().setUp()
        self.Cron = self.env['woow_paas_platform.ecpay_invoice_cron']
        self.partner = self.env['res.partner'].create({'name': 'CronPolicy864'})

    def _posted_invoice(self, issue_state):
        move = self.env['account.move'].sudo().create({
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'invoice_date': date.today(),
            'invoice_line_ids': [(0, 0, {
                'name': 'policy probe',
                'quantity': 1.0,
                'price_unit': 100.0,
            })],
            'woow_invoice_issue_state': issue_state,
        })
        move.action_post()
        # action_post 會觸發上游 hook，可能改寫狀態；重新釘成待測值。
        move.write({'woow_invoice_issue_state': issue_state})
        return move

    def test_cron_skips_manual_but_still_picks_pending(self):
        """驗收 4：cron domain 只揀 pending/failed，`manual` 不在其中。

        🔴 附陽性對照（同一次呼叫裡 `pending` 那張必須被揀到）——否則「manual
        沒被揀到」也可能只是探針壞掉／根本沒跑。
        """
        manual_move = self._posted_invoice('manual')
        pending_move = self._posted_invoice('pending')
        with patch.object(
                type(self.Cron), '_issue_one', autospec=True) as issue_one:
            self.Cron._cron_issue_pending_invoices()
        picked = {call.args[1].id for call in issue_one.call_args_list}
        self.assertIn(
            pending_move.id, picked,
            '陽性對照失敗：cron 連 pending 都沒揀到，本測試無鑑別力')
        self.assertNotIn(
            manual_move.id, picked,
            'manual 的發票不得被 async cron 自動開走（政策由 stamp 決定）')

    def test_retry_action_accepts_manual(self):
        """驗收 5：`manual` 下手動開票 action 按得下去並轉進開立流程。"""
        move = self._posted_invoice('manual')
        with patch.object(
                type(self.Cron), '_issue_one', autospec=True) as issue_one:
            self.assertTrue(move.action_woow_retry_ecpay_invoice())
        self.assertTrue(
            issue_one.called, 'action 必須真的把 move 交給開立流程')
        self.assertEqual(move.woow_invoice_issue_state, 'pending')

    def test_retry_action_still_rejects_not_required(self):
        """陰性對照：`not_required` 仍然拒絕（放寬只針對 `manual`）。"""
        move = self._posted_invoice('not_required')
        with self.assertRaises(UserError):
            move.action_woow_retry_ecpay_invoice()


class TestStaleManualInvoiceAlert864(TransactionCase):
    """`manual` 發票的老化告警：沒人開就是另一種靜默。

    🔴 本組最關鍵的一條是 `test_invoice_with_ecpay_number_is_not_stale`：判準
    必須是「ECPay 回傳的發票號碼是否非空」，⛔ 不是 `woow_invoice_issue_state`
    是否為 `issued`——`res.company.ecpay_demo_mode` 預設 True，demo 之下 cron
    照樣把 move 標成 `issued`，但發票只打到 ECPay 測試 API（設計 §0.6 第 3 條）。
    """

    #: 與 models/account_move_invoice_status.py 的 `_MANUAL_AGING_SUMMARY`
    #: 刻意雙寫，當同步哨兵：改了那邊沒改這邊，這組測試會紅。
    SUMMARY = '手動開票逾期：發票已過帳但 ECPay 發票號碼仍為空'
    PARAM = 'woow_paas_platform.manual_invoice_aging_days'

    def setUp(self):
        super().setUp()
        self.Move = self.env['account.move']
        self.ICP = self.env['ir.config_parameter'].sudo()
        self.partner = self.env['res.partner'].create({'name': 'AgingPolicy864'})
        self._incident_refs = set()
        self.addCleanup(self._purge_incidents)

    def _purge_incidents(self):
        """incident 由獨立 cursor commit，TransactionCase 的回捲清不掉它。"""
        if not self._incident_refs:
            return
        refs = list(self._incident_refs)
        with self.env.registry.cursor() as cr:
            cr.execute(
                'DELETE FROM mail_activity WHERE res_model = %s AND res_id IN '
                '(SELECT id FROM woow_billing_incident '
                ' WHERE external_ref = ANY(%s))',
                ('woow.billing.incident', refs))
            for table, model_column in (
                    ('mail_followers', 'res_model'),
                    ('mail_message', 'model')):
                cr.execute(
                    'DELETE FROM %s WHERE %s = %%s AND res_id IN '
                    '(SELECT id FROM woow_billing_incident '
                    ' WHERE external_ref = ANY(%%s))' % (table, model_column),
                    ('woow.billing.incident', refs))
            cr.execute(
                'DELETE FROM woow_billing_incident WHERE external_ref = ANY(%s)',
                (refs,))
            cr.commit()

    def _aged_manual_invoice(self, days_old=10, ecpay_number=None):
        move = self.Move.sudo().create({
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'invoice_date': date.today(),
            'invoice_line_ids': [(0, 0, {
                'name': 'aging probe',
                'quantity': 1.0,
                'price_unit': 100.0,
            })],
        })
        move.action_post()
        move.write({'woow_invoice_issue_state': 'manual'})
        if ecpay_number:
            move.write({
                'ecpay_invoice_id': self.env['uniform.invoice'].sudo().create({
                    'name': ecpay_number,
                    'company_id': self.env.company.id,
                }).id,
            })
        # create_date 由 ORM 自動填當下，只能用 SQL 往回撥。
        self.env.cr.execute(
            'UPDATE account_move SET create_date = %s WHERE id = %s',
            (fields.Datetime.now() - timedelta(days=days_old), move.id))
        move.invalidate_recordset(['create_date'])
        self._incident_refs.add('invoice:%s' % move.id)
        return move

    def _incident_of(self, move):
        """用新 cursor 讀：外層 TransactionCase 是 REPEATABLE READ 快照，
        看不見獨立 cursor 在它開始之後 commit 的列。"""
        with self.env.registry.cursor() as cr:
            cr.execute(
                'SELECT kind, state, occurrences, source_model, source_ref, '
                'assessment_reason FROM woow_billing_incident '
                'WHERE external_ref = %s', ('invoice:%s' % move.id,))
            return cr.fetchone()

    def _activity_count(self):
        return self.env['mail.activity'].sudo().search_count(
            [('summary', '=', self.SUMMARY)])

    def test_stale_manual_invoice_logs_incident_and_activity(self):
        move = self._aged_manual_invoice(days_old=10)
        self.Move._cron_alert_stale_manual_invoices()
        row = self._incident_of(move)
        self.assertTrue(row, '逾期的 manual 發票必須落一筆 incident')
        kind, state, occurrences, source_model, source_ref, reason = row
        self.assertEqual(kind, 'manual_invoice_stale')
        self.assertEqual(state, 'open')
        self.assertEqual(occurrences, 1)
        self.assertEqual(source_model, 'account.move')
        self.assertEqual(source_ref, move.id)
        self.assertEqual(reason, 'manual_invoice_aging')
        self.assertTrue(self._activity_count(), '必須同時建 mail.activity')

    def test_invoice_with_ecpay_number_is_not_stale(self):
        """🔴 判準是 ECPay 回傳的發票號碼非空，不是我們自己寫的 issue state。

        這張跟上一條的唯一差別就是 `ecpay_invoice_id.name` 有值——它已經真的
        開出來了，即使 issue state 還停在 `manual` 也不該被當成逾期。
        """
        move = self._aged_manual_invoice(days_old=10, ecpay_number='AB12345678')
        self.Move._cron_alert_stale_manual_invoices()
        self.assertIsNone(
            self._incident_of(move),
            '已取得 ECPay 發票號碼的發票不得被判為逾期未開')
        self.assertFalse(self._activity_count())

    def test_blank_ecpay_number_still_counts_as_not_issued(self):
        """掛著一張號碼是空字串的 uniform.invoice 不算已開票。

        判準是「號碼**非空**」而不是「有沒有掛 uniform.invoice 記錄」——
        `ecpay_invoice_id` 非空但 `name` 為空的 move 仍然是逾期未開。
        """
        move = self._aged_manual_invoice(days_old=10)
        move.write({
            'ecpay_invoice_id': self.env['uniform.invoice'].sudo().create({
                'name': '',
                'company_id': self.env.company.id,
            }).id,
        })
        self.Move._cron_alert_stale_manual_invoices()
        self.assertTrue(
            self._incident_of(move),
            '空字串發票號碼不得被當成已開票')

    def test_fresh_manual_invoice_is_not_stale(self):
        """未逾期的不告警（否則 manual 政策一啟用就天天噴）。"""
        move = self._aged_manual_invoice(days_old=1)
        self.Move._cron_alert_stale_manual_invoices()
        self.assertIsNone(self._incident_of(move))

    def test_threshold_comes_from_config_parameter(self):
        """門檻可設定：同一張 10 天的發票在 30 天門檻下不告警、1 天門檻下告警。"""
        move = self._aged_manual_invoice(days_old=10)
        self.ICP.set_param(self.PARAM, '30')
        self.Move._cron_alert_stale_manual_invoices()
        self.assertIsNone(
            self._incident_of(move), '門檻 30 天時 10 天的發票不該告警')
        self.ICP.set_param(self.PARAM, '1')
        self.Move._cron_alert_stale_manual_invoices()
        self.assertTrue(
            self._incident_of(move), '門檻 1 天時 10 天的發票必須告警')

    def test_invalid_threshold_falls_back_to_seven_days(self):
        """非法值不得炸掉 cron，且要退回內建預設 7 天（不是 0、不是無限大）。"""
        self.ICP.set_param(self.PARAM, 'not-a-number')
        self.assertEqual(self.Move._woow_manual_aging_days(), 7)
        old = self._aged_manual_invoice(days_old=10)
        fresh = self._aged_manual_invoice(days_old=3)
        self.Move._cron_alert_stale_manual_invoices()
        self.assertTrue(
            self._incident_of(old), '逾 7 天的必須告警（證明 fallback 不是 0）')
        self.assertIsNone(
            self._incident_of(fresh),
            '未逾 7 天的不得告警（證明 fallback 不是無限小）')

    def test_zero_threshold_disables_the_scan(self):
        """'0' ＝ 明確停用（沿 max_consecutive_payment_failures 慣例）。"""
        self.ICP.set_param(self.PARAM, '0')
        move = self._aged_manual_invoice(days_old=999)
        self.Move._cron_alert_stale_manual_invoices()
        self.assertIsNone(self._incident_of(move))
        self.assertFalse(self._activity_count())

    def test_second_run_is_idempotent(self):
        """重跑只累加 occurrences，不會多一筆 incident、也不重複轟炸 activity。"""
        move = self._aged_manual_invoice(days_old=10)
        self.Move._cron_alert_stale_manual_invoices()
        first_activities = self._activity_count()
        self.Move._cron_alert_stale_manual_invoices()
        row = self._incident_of(move)
        self.assertTrue(row)
        self.assertEqual(row[2], 2, 'upsert 應只把 occurrences 加到 2')
        with self.env.registry.cursor() as cr:
            cr.execute(
                'SELECT COUNT(*) FROM woow_billing_incident '
                'WHERE external_ref = %s', ('invoice:%s' % move.id,))
            self.assertEqual(cr.fetchone()[0], 1, '不得產生第二筆 incident')
        self.assertEqual(
            self._activity_count(), first_activities,
            '上一波 activity 還在時不得重複建（summary 即 dedup 鍵）')
