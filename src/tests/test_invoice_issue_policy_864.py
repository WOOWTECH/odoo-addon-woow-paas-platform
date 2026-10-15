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

from lxml import etree

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase
from odoo.tests import tagged
from odoo.tools.safe_eval import safe_eval


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
        # ECPay V4 上線（2026-10-15 裁決 1）起儲值發票改看平台參數；`company`
        # 模式＝沿用本 PR-C 的公司政策 resolver（這條守的就是那條路徑）。
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.topup_invoice_issue_mode', 'company')
        self.env.company.auto_invoice = 'manual'
        invoice = self._make_topup()._ensure_topup_invoice()
        self.assertEqual(invoice.woow_invoice_issue_state, 'manual')

    def test_topup_invoice_automatic_policy_stamps_pending(self):
        """`automatic` 下維持既有行為（`pending`）。"""
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.topup_invoice_issue_mode', 'company')
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

    def test_zero_threshold_falls_back_to_seven_days(self):
        """🔴 `'0'` **不是** disable，是 fail-safe 回 7 天（Eugene 2026-09-01 終判）。

        老化告警是安全／稽核機制。「把門檻設成 0 就靜默不告警」是未授權的產品
        語意——那種開關會讓稽核告警整條消失，而且資料面看起來跟「沒有逾期發票」
        一模一樣，正是本 issue 家族的根因（一堆 admin 可改的 gate 疊起來讓金流
        靜默卡死）。⛔ 本模組沒有 disable 逃生門。
        """
        self.ICP.set_param(self.PARAM, '0')
        self.assertEqual(self.Move._woow_manual_aging_days(), 7)
        old = self._aged_manual_invoice(days_old=10)
        fresh = self._aged_manual_invoice(days_old=3)
        self.Move._cron_alert_stale_manual_invoices()
        self.assertTrue(
            self._incident_of(old),
            "'0' 不得停用掃描：逾 7 天的發票仍然必須告警")
        self.assertIsNone(
            self._incident_of(fresh),
            '前提檢查：fallback 是 7 天，不是 0 天（否則未逾期的也會被告警，'
            '本測試就分不出「有跑」和「門檻變成 0」）')
        self.assertTrue(self._activity_count())

    def test_negative_threshold_falls_back_to_seven_days(self):
        """負值同樣 fail-safe 回 7——⛔ 不得變成另一條 disable 的側門。"""
        self.ICP.set_param(self.PARAM, '-30')
        self.assertEqual(self.Move._woow_manual_aging_days(), 7)
        old = self._aged_manual_invoice(days_old=10)
        fresh = self._aged_manual_invoice(days_old=3)
        self.Move._cron_alert_stale_manual_invoices()
        self.assertTrue(self._incident_of(old))
        self.assertIsNone(self._incident_of(fresh))

    def test_missing_param_falls_back_to_seven_days(self):
        """param 被刪掉／從未設定也是 7（不是 0、不是炸掉）。"""
        self.env['ir.config_parameter'].sudo().search(
            [('key', '=', self.PARAM)]).unlink()
        self.assertEqual(self.Move._woow_manual_aging_days(), 7)
        old = self._aged_manual_invoice(days_old=10)
        self.Move._cron_alert_stale_manual_invoices()
        self.assertTrue(self._incident_of(old))

    def test_second_run_does_not_realert_the_same_invoice(self):
        """重跑不重複告警同一張發票：不多一筆 incident、occurrences 不再累加。

        🔴 **#990 改變了這一格的語意，這不是把測試改綠**：原本每個 tick 都對
        每筆逾期 move 重呼一次 `_log`（所以 occurrences 會累加到 2），而那正是
        這張單要修的 N+1——N 筆 move ＝ N 條獨立交易，每天重來一次。

        #990 之後已告警的 move 由 `woow_aging_alerted_at` 帶出掃描 domain
        ⇒ 第二輪根本不會再碰它 ⇒ `occurrences` 停在 1。**資訊沒有損失**：
        「這張發票逾期多久沒人處理」改用 incident 的 `first_seen_at` 算年齡，
        ⛔ 不需要靠每日累加（累加的代價就是那個 N+1）。

        仍然必須成立的三件事沒有變：不得產生第二筆 incident（`external_ref`
        UNIQUE）、不得重複轟炸 activity（summary 即 dedup 鍵）、
        以及第二輪**確實有跑**（用 marker 證明它不是因為 cron 沒執行而綠）。
        """
        move = self._aged_manual_invoice(days_old=10)
        self.Move._cron_alert_stale_manual_invoices()
        first_activities = self._activity_count()

        move.invalidate_recordset(['woow_aging_alerted_at'])
        self.assertTrue(
            move.woow_aging_alerted_at,
            '前提檢查：第一輪必須標上 marker，否則下面的「第二輪不重跑」是恆真')

        self.Move._cron_alert_stale_manual_invoices()

        row = self._incident_of(move)
        self.assertTrue(row)
        self.assertEqual(
            row[2], 1,
            '#990：已告警的 move 離開 domain ⇒ 第二輪不再 _log ⇒ '
            'occurrences 停在 1（改前是 2，見本測試 docstring）')
        with self.env.registry.cursor() as cr:
            cr.execute(
                'SELECT COUNT(*) FROM woow_billing_incident '
                'WHERE external_ref = %s', ('invoice:%s' % move.id,))
            self.assertEqual(cr.fetchone()[0], 1, '不得產生第二筆 incident')
        self.assertEqual(
            self._activity_count(), first_activities,
            '上一波 activity 還在時不得重複建（summary 即 dedup 鍵）')


@tagged('post_install', '-at_install')
class TestManualRetryButtonVisibility864(TransactionCase):
    """驗收 5 的**另一半：UI 入口**。`manual` 下後台按鈕必須真的看得見。

    🔴 只驗 Python method 按得下去是不夠的：method 通得過、而 view 的
    ``invisible`` 表達式把按鈕藏起來時，管理員在真實 UI 上仍然無路可走——那是
    #864「連手動補救鈕都按不下去」的同一個病，只是換到 view 層。

    ⛔ 本測試刻意**不做**字串存在檢查（例如 ``'manual' in arch``）：那種寫法在
    舊的 ``invisible="woow_invoice_issue_state not in ('failed', 'error')"``
    底下**照樣會通過**（arch 裡本來就有 selection 的 manual 字樣），零鑑別力。
    改成把 ``invisible`` 表達式**實際求值**，逐個狀態問「這顆按鈕看不看得見」。
    """

    _BUTTON = 'action_woow_retry_ecpay_invoice'

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.view = cls.env.ref('ecpay_invoice_tw.ecpay_invoice_view_form')
        cls.manager = cls.env['res.users'].create({
            'name': 'Acct Manager 864',
            'login': 'acct_manager_864',
            'groups_id': [
                (4, cls.env.ref('base.group_user').id),
                (4, cls.env.ref('account.group_account_manager').id),
            ],
        })
        # 🔴 陰性對照要挑「看得到 account.move、但不是 manager」的身分。
        # 用完全沒有 account 權限的使用者是壞的對照：get_view 會先丟
        # AccessError，測試變成在驗「他讀不到 account.move」而不是在驗
        # 「這顆鈕是 manager 專屬」——後者才是本次修改真正該守的邊界。
        cls.billing_user = cls.env['res.users'].create({
            'name': 'Billing User 864',
            'login': 'billing_user_864',
            'groups_id': [
                (4, cls.env.ref('base.group_user').id),
                (4, cls.env.ref('account.group_account_invoice').id),
            ],
        })

    def _button_node(self, user):
        arch = self.env['account.move'].with_user(user).get_view(
            self.view.id, 'form')['arch']
        nodes = etree.fromstring(arch.encode()).xpath(
            "//button[@name='%s']" % self._BUTTON)
        return nodes[0] if nodes else None

    def _invisible_for(self, state):
        """把按鈕的 invisible 表達式對某個 issue state 求值。"""
        node = self._button_node(self.manager)
        self.assertIsNotNone(
            node, '前提檢查：account manager 必須拿得到這顆按鈕節點')
        expr = node.get('invisible')
        self.assertTrue(
            expr,
            '前提檢查：按鈕必須帶 invisible 條件；沒有條件的話本測試分不出'
            '「條件正確」與「條件被整個拿掉」')
        return bool(safe_eval(expr, {'woow_invoice_issue_state': state}))

    def test_button_is_visible_for_manual(self):
        """🔴 本次必修：`manual` 是「該開票、但依政策由人工開」，按鈕必須看得見。"""
        self.assertFalse(
            self._invisible_for('manual'),
            'manual 的發票在後台必須看得到手動開票鈕，否則 auto_invoice=manual '
            '的租戶永遠開不出發票（method 可呼叫不等於 UI 上按得到）')

    def test_button_stays_visible_for_failed_and_error(self):
        """既有兩格不得因為本次修改而消失（回歸守線）。"""
        for state in ('failed', 'error'):
            with self.subTest(state=state):
                self.assertFalse(self._invisible_for(state))

    def test_button_stays_hidden_where_the_action_refuses(self):
        """陰性對照：`action_woow_retry_ecpay_invoice()` 會 raise 的兩格仍要藏。

        按鈕若對這兩格也顯示，等於給管理員一顆按下去必定跳錯的鈕。
        """
        for state in ('not_required', 'issued'):
            with self.subTest(state=state):
                self.assertTrue(self._invisible_for(state))

    def test_button_is_not_offered_to_non_managers(self):
        """⛔ 授權不得被放寬：看得到發票、但不是 manager 的人拿不到這顆按鈕。"""
        self.assertIsNotNone(
            self._button_node(self.manager),
            '前提檢查：manager 拿得到節點，否則下面的 assertIsNone 零鑑別力')
        self.assertIsNone(
            self._button_node(self.billing_user),
            '本次修改只放寬「顯示於哪些狀態」，⛔ 不得放寬 groups')


class TestCronIgnoresDraftMoves864(TransactionCase):
    """必修 4 的機器化：cron 不撿 draft，與 issue state 無關。

    `wallet_topup._create_topup_invoice()` 的失敗分支註解原本宣稱「issue state
    維持 pending 所以 cron 不會撿」——PR-C 之後那句話是錯的（政策為 manual 時
    它就是 `'manual'`）。真正的原因是 move 還停在 `draft`，而
    `_cron_issue_pending_invoices` 的 domain 要求 `state='posted'`。
    這個 class 把那句修正後的敘述變成機器檢查，而不是只留一段散文。
    """

    def setUp(self):
        super().setUp()
        self.Cron = self.env['woow_paas_platform.ecpay_invoice_cron']
        self.partner = self.env['res.partner'].create({'name': 'DraftCron864'})

    def _move(self, issue_state, post):
        move = self.env['account.move'].sudo().create({
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'invoice_date': date.today(),
            'invoice_line_ids': [(0, 0, {
                'name': 'draft cron probe',
                'quantity': 1.0,
                'price_unit': 100.0,
            })],
        })
        if post:
            move.action_post()
        move.write({'woow_invoice_issue_state': issue_state})
        return move

    def test_draft_moves_are_never_picked_whatever_the_issue_state(self):
        draft_pending = self._move('pending', post=False)
        draft_manual = self._move('manual', post=False)
        posted_pending = self._move('pending', post=True)
        self.assertEqual(draft_pending.state, 'draft', '前提檢查')
        self.assertEqual(draft_manual.state, 'draft', '前提檢查')
        with patch.object(
                type(self.Cron), '_issue_one', autospec=True) as issue_one:
            self.Cron._cron_issue_pending_invoices()
        picked = {call.args[1].id for call in issue_one.call_args_list}
        self.assertIn(
            posted_pending.id, picked,
            '陽性對照失敗：cron 連 posted+pending 都沒撿，本測試無鑑別力')
        self.assertNotIn(draft_pending.id, picked)
        self.assertNotIn(
            draft_manual.id, picked,
            'draft 不被撿的原因是 state，不是 issue state')


class _EcpayHttpSpy:
    """ECPay **HTTP 邊界**的 spy：`ECPay_Invoice_Send.CheckOut()`。

    這是 SDK 裡真正要出網的那一層（它下面的 `process_data` 才是 requests，
    但那層要處理 AES 編解碼，做不出可用的假回應）。把 spy 放這裡的意義是：
    `create_ecpay_invoice()` 的**全部真實邏輯**都照跑——包含上游自己的重複開立
    守衛、`validate_ecpay_invoice()`、`uniform.invoice` 記錄建立、
    `ecpay_invoice_id` / `uniform_state` 的寫入——只有出網那一下被攔下來。

    ⛔ 刻意**不** mock `_issue_one`、也**不** mock `create_ecpay_invoice`：
    那樣只會驗到「守衛放行了」，驗不到「放行之後到底有沒有真的開票」。
    上一輪就是這樣漏掉靜默假成功的。
    """

    #: `ecpay_invoice_init(invoice, api_uri, method, ...)` 把 **method** 放進
    #: `Invoice_Method`、把 **api_uri** 放進 `Invoice_Url`。開立走
    #: ('B2CInvoice/Issue', 'INVOICE')、開立後回查走
    #: ('B2CInvoice/GetIssue', 'INVOICE_SEARCH')。
    #: ⚠ 分派與計數要看 `Invoice_Method`（'INVOICE' / 'INVOICE_SEARCH'），
    #: ⛔ 不是看 URI —— 用 URI 的字尾去比對 Invoice_Method 會**永遠不匹配**，
    #: 結果是「外呼次數恆為 0」，看起來像實作沒打 ECPay（探針壞掉的假紅）。
    _METHOD_ISSUE = 'INVOICE'
    _METHOD_QUERY = 'INVOICE_SEARCH'

    def __init__(self, invoice_no='AB12345678'):
        self.calls = []          # [(Invoice_Method, Invoice_Url), ...]
        self.invoice_no = invoice_no

    def __call__(self, arParameters=None, HashKey='', HashIV='',
                 Invoice_Method='', ServiceURL=''):
        self.calls.append((Invoice_Method, ServiceURL))
        if Invoice_Method == self._METHOD_QUERY:
            # 開立成功後上游會回查明細；Items 空即可（process_return_info 會把
            # Item* 併成空字串），ZeroTaxRateReason 是它會 del 掉的 key。
            # 回傳的其餘 key 都會被 write 進 uniform.invoice，故只能放真欄位。
            return {'RtnCode': 1, 'RtnMsg': 'ok',
                    'Items': [], 'ZeroTaxRateReason': ''}
        return {'RtnCode': 1, 'RtnMsg': '開立發票成功',
                'InvoiceNo': self.invoice_no}

    @property
    def issue_calls(self):
        """真正「開立」的外呼（不含開立後的回查）。"""
        return [c for c in self.calls if c[0] == self._METHOD_ISSUE]


class TestRetryRealCallPath864(TransactionCase):
    """🔴 走完整 call path（action → `_issue_one` → 上游 → ECPay HTTP 邊界）。

    上一輪的守衛測試把 `_issue_one` 整個 mock 掉，所以只證明了「守衛放行」，
    完全沒看到放行之後 `_issue_one` 會用 OR 短路**未打 ECPay 就 write
    `issued`／`issued_at`**——靜默假成功。本 class 就是要讓那種情況測得出來：
    唯一的 mock 在 ECPay 的 HTTP 邊界，觀測的是**外呼次數與最終狀態**。
    """

    _CHECKOUT = ('odoo.addons.ecpay_invoice_tw.sdk.ecpay_main.'
                 'ECPay_Invoice_Send.CheckOut')

    def setUp(self):
        super().setUp()
        self.Cron = self.env['woow_paas_platform.ecpay_invoice_cron']
        # validate_ecpay_invoice() 要求客戶有地址（或發票寄送地址）
        self.partner = self.env['res.partner'].create({
            'name': 'RealCallPath864',
            # validate_ecpay_invoice() 要求客戶有地址（或發票寄送地址）
            'street': 'No. 1, Test Rd.',
        })
        # prepare_item_list() 會取 line.product_id.name[:30] 與
        # line.product_uom_id.name[:6]；沒有 product 的行會讓空 recordset 的
        # name 變成 False 而炸在 False[:30]（'bool' object is not subscriptable），
        # 那會讓測試在**還沒走到 ECPay 邊界**之前就失敗，看起來像「沒外呼」。
        self.product = self.env['product.product'].create({
            'name': 'Real Call Path Service 864',
            'type': 'service',
        })

    def _posted_invoice(self, issue_state, ecpay_number=None,
                        uniform_state=None, attach_uniform=False):
        move = self.env['account.move'].sudo().create({
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'invoice_date': date.today(),
            'invoice_line_ids': [(0, 0, {
                'product_id': self.product.id,
                'name': 'real call path probe',
                'quantity': 1.0,
                'price_unit': 100.0,
            })],
        })
        move.action_post()
        vals = {'woow_invoice_issue_state': issue_state}
        if ecpay_number is not None or attach_uniform:
            vals['ecpay_invoice_id'] = self.env['uniform.invoice'].sudo().create({
                'name': ecpay_number or '',
                'company_id': self.env.company.id,
            }).id
        if uniform_state is not None:
            vals['uniform_state'] = uniform_state
        move.write(vals)
        return move

    def _retry_through_real_path(self, move):
        """回傳 (是否 UserError, spy)。⛔ 全程不 mock `_issue_one`。"""
        spy = _EcpayHttpSpy()
        with patch(self._CHECKOUT, new=spy):
            try:
                move.action_woow_retry_ecpay_invoice()
                raised = False
            except UserError:
                raised = True
        move.invalidate_recordset()
        return raised, spy

    # ---- (a) 已確認開立 → fail-closed、外呼 0 -------------------------

    def test_a_confirmed_issued_is_fail_closed_with_zero_ecpay_calls(self):
        move = self._posted_invoice(
            'manual', ecpay_number='AB12345678', uniform_state='invoiced')
        raised, spy = self._retry_through_real_path(move)
        self.assertTrue(raised, '已確認開立必須 fail-closed')
        self.assertEqual(spy.calls, [], 'ECPay 外呼必須是 0 次')

    # ---- (b) 失敗殘值（無成功號碼）→ 真的 retry、外呼 1 ---------------

    def test_b_failed_residual_without_number_really_reissues(self):
        """🔴 上一輪這條是假綠：mock 掉 `_issue_one` 之後看不到它沒打 ECPay。"""
        move = self._posted_invoice('failed', attach_uniform=True)
        self.assertTrue(move.ecpay_invoice_id, '前提：掛著殘值 id')
        self.assertFalse(move.ecpay_invoice_id.name, '前提：號碼是空的')
        raised, spy = self._retry_through_real_path(move)
        self.assertFalse(raised, '失敗殘值必須能真的 retry')
        self.assertEqual(
            len(spy.issue_calls), 1,
            '必須**真的**打一次 ECPay 開立——只把狀態寫成 issued 是靜默假成功')
        self.assertEqual(move.woow_invoice_issue_state, 'issued')
        self.assertEqual(
            move.ecpay_invoice_id.name, 'AB12345678',
            '成功後必須帶回 ECPay 真號碼')

    # ---- (c) invalid／Discounted ＋ 真號碼 → UserError、不 stamp -------

    def test_c_voided_or_discounted_with_number_raises_and_never_stamps(self):
        """🔴 `invalid`／`Discounted` 帶著真號碼 ⇒ fail-loud，⛔ 不得靜默 stamp。"""
        for uniform_state in ('invalid', 'Discounted'):
            with self.subTest(uniform_state=uniform_state):
                move = self._posted_invoice(
                    'manual', ecpay_number='AB9999999',
                    uniform_state=uniform_state)
                raised, spy = self._retry_through_real_path(move)
                self.assertTrue(
                    raised, '%s ＋ 真號碼必須 fail-loud' % uniform_state)
                self.assertEqual(spy.calls, [], 'ECPay 外呼必須是 0 次')
                self.assertNotEqual(
                    move.woow_invoice_issue_state, 'issued',
                    '⛔ 未打 ECPay 不得把狀態寫成 issued（靜默假成功）')

    def test_guard_also_covers_base_failed_and_error(self):
        """裁決要求：同一顆守衛順帶覆蓋 base 的 `failed`／`error` 兩格同洞。"""
        for issue_state in ('failed', 'error'):
            with self.subTest(issue_state=issue_state):
                move = self._posted_invoice(
                    issue_state, ecpay_number='AB8765432',
                    uniform_state='invoiced')
                raised, spy = self._retry_through_real_path(move)
                self.assertTrue(raised)
                self.assertEqual(spy.calls, [])

    def test_clean_manual_without_any_uniform_record_really_issues(self):
        """回歸守線：連 uniform.invoice 都沒掛的乾淨 manual 會真的開票。"""
        move = self._posted_invoice('manual')
        raised, spy = self._retry_through_real_path(move)
        self.assertFalse(raised)
        self.assertEqual(len(spy.issue_calls), 1)
        self.assertEqual(move.woow_invoice_issue_state, 'issued')

    # ---- cron 側走同一顆判準（不能有 OR/AND 兩把尺的差集）-------------

    def test_cron_issue_one_never_stamps_issued_without_calling_ecpay(self):
        """`_issue_one` 直呼：有真號碼時 ⛔ 不得 stamp issued／issued_at。"""
        move = self._posted_invoice(
            'pending', ecpay_number='AB5555555', uniform_state='invoiced')
        spy = _EcpayHttpSpy()
        with patch(self._CHECKOUT, new=spy):
            self.Cron._issue_one(move)
        move.invalidate_recordset()
        self.assertEqual(spy.calls, [], 'ECPay 外呼必須是 0 次')
        self.assertNotEqual(
            move.woow_invoice_issue_state, 'issued',
            '⛔ 未打 ECPay 卻寫 issued＝靜默假成功（這正是 inherited bug）')
