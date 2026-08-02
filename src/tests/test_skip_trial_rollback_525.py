# -*- coding: utf-8 -*-
"""#525 A1：skip-trial 端點吞掉例外 → 錢包已扣、發票已開，畫面卻說失敗。

`api_service_skip_trial` 的 `except Exception` 把 `_skip_trial()` 整條鏈上的
例外吃掉、以 HTTP 200 + `success: False` 返回。例外沒有傳播到框架層 → request
正常結束 → **transaction 照常 commit**。`_skip_trial` 的寫入是分段的，例外噴在
哪一步，殘留就累積到哪（issue #525 A1 的表格）：

    subscription_lifecycle_mixin.py:502  訂閱脫離試用（sh_trial=False、
                                         sh_trial_end_date=昨天、sh_start_date
                                         ／sh_date_of_next_payment=今天）
    :511                                 多一張 out_invoice（含明細）
    account_move.py `_pay_from_wallet`   **錢包點數已扣**＋wallet.transaction
      的 `action_post()`／扣點之後      已 INSERT、發票已 posted 序號已消耗，
                                         但 account.payment 沒建、reconcile 沒做

兩個加重因子：

1. **帳務不一致**——錢從錢包扣走了，發票停在 posted／未付款，沒有 payment、
   沒有 reconcile。這不是「少建一筆資料」，是使用者的錢對不上帳。
2. **殘留把重試路徑鎖死**——脫離試用的 write 排在 invoice create **之前**，
   而 `_skip_trial` 的前置檢查是 `if not sub.sh_trial: raise UserError`。
   使用者看到失敗再按一次，會被自己剛剛的殘留擋掉：試用期燒掉、計費沒開始、
   且**無法自我修復**。

所以本檔的驗收面不是回應字串，而是**側效果**：錢包點數、wallet.transaction、
account.move、訂閱狀態四個實體，以及「再按一次會不會成功」。

## 注入方式（刻意零 mock 的那一條）

`_break_payment_journal()` 把公司的 `sh_paid_subscription_journal` 清掉並停用
所有 bank／cash journal，讓 `account_move.py::_pay_from_wallet` 走到
`raise UserError("No payment journal configured for company ...")`。那一行的
位置在 `action_post()` 與 `deduct_wallet_points()` **之後**——正是上表最下面
那一列的殘留形狀，而且是真實可發生的設定錯誤，不是 patch 出來的假路徑。

例外**分類**（AccessError／未預期例外）需要指定例外型別，才另外 patch
`account.payment.action_post`——同樣落在扣點之後，殘留形狀一致。

## 斷言的紅燈可造性

每個 test method 只放**一個**核心斷言。理由：一條測試的結果是 AND、報告只顯示
第一個失敗的斷言，把四個殘留面塞進同一條會讓「整條紅」掩蓋掉其他三個其實從沒
被獨立驗證過（#520 就是這樣讓 GREEN 後追加的斷言沒跑過 RED）。

**陽性對照**：`test_successful_skip_trial_*` 走同一條 HTTP 路徑成功扣款，斷言
點數確實減少、wallet.transaction 確實新增、發票確實開出、sh_trial 確實翻掉。
四個探針若失效（例如 HttpCase 的 transaction 隔離讓 request 內的寫入測試端本來
就看不到），這條會先紅——下面四條否定性斷言的「沒有發生」就不會是假綠。

## 兩道防空真值的閘（#529 批次 0 的形態，本檔自我稽核後補上）

1. **注入深度不是宣稱，是斷言**——上面「失敗點在扣點之後」若哪天不成立
   （`_pay_from_wallet` 被重排，journal 查找搬到 `deduct_wallet_points` 之前），
   殘留根本不會產生，四條「沒有殘留」的斷言就全部退化成恆真，輸出與真綠一模
   一樣。`test_*_injection_fires_after_the_wallet_debit` 兩條把這個前提釘成
   可執行的斷言：實測把 `deduct_wallet_points` 移到 `_pay_from_wallet` 末尾後，
   四條殘留測試**在修復前也全部變綠**（假綠），只有這兩條會紅。
2. **`assertNotIn` / `assertNotEqual` 前先證明讀得到東西**——`result.get('error')
   or ''` 在 error 缺席或空字串時會讓 `assertNotIn(x, '')` 恆真（#529 D 組：
   Odoo Text 欄位寫入 `''` 讀回也是 `''`，連 TypeError 都不會幫忙擋）。所以三條
   訊息面測試都先斷言訊息**是**預期的那一則，再斷言它不含不該有的東西。
"""
from datetime import date, timedelta
from unittest.mock import patch

from odoo.exceptions import AccessError
from odoo.tests.common import HttpCase, tagged

_GENERIC_ERROR = 'An error occurred while skipping the trial period.'


@tagged('post_install', '-at_install')
class TestSkipTrialFailureLeavesNoResidue(HttpCase):
    """skip-trial 失敗時不得留下金流殘留，且使用者要能重試。"""

    def setUp(self):
        super().setUp()
        self.authenticate('admin', 'admin')
        self.admin_user = self.env.ref('base.user_admin')
        self.partner = self.admin_user.partner_id

        # 訂閱發票需要 `company.sh_journal_id`（sale journal）。它由
        # post_init_hook／`<function>` 設定，bare install 的環境不保證已收斂
        # ——fixture 自帶前提，否則「成功路徑走不完」會讓陽性對照失效、配套的
        # 否定性斷言全部退化成假綠。慣例鏡射 test_create_service_validation_
        # rollback_520.setUp。
        company = self.env.company.sudo()
        if not self.env['account.journal'].search([
            ('company_id', '=', company.id), ('type', '=', 'sale'),
        ], limit=1):
            self.env['account.journal'].sudo().create({
                'name': 'Test Sales 525', 'code': 'TS525', 'type': 'sale',
                'company_id': company.id})
        self.env['res.company'].sudo()._ensure_subscription_journal()
        self.assertTrue(
            company.sh_journal_id,
            '本檔的陽性對照需要可用的訂閱 journal；沒有它 skip-trial 根本走不完')

        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'WS 525 A1', 'owner_id': self.admin_user.id})

        # 自建 plan／product：不綁任何 seed 商品的定價與上架狀態（#478/#494
        # 教訓——把當下的商業事實寫死成契約，商品一改動整組測試集體變紅）。
        # 明寫 taxes_id 空集合（#366）讓發票金額 == sh_plan_price，斷言才能對
        # 精確數字而不是「有變就好」。
        self.plan = self.env['sh.subscription.plan'].sudo().create({
            'name': 'Plan 525', 'sh_duration': 1, 'sh_unit': 'month',
            'sh_plan_price': 500.0, 'sh_no_of_billing_cycle': -1,
            'sh_trial': True, 'sh_trial_duration': 7, 'sh_trial_unit': 'day'})
        product_tmpl = self.env['product.template'].sudo().create({
            'name': 'Product 525', 'type': 'service', 'list_price': 500.0,
            'sh_product_subscribe': True,
            'sh_subscription_plan_id': self.plan.id,
            'taxes_id': [(6, 0, [])]})

        today = date.today()
        self.sub = self.env['sh.subscription.subscription'].sudo().create({
            'sh_partner_id': self.partner.id,
            'sh_company_id': company.id,
            'product_id': product_tmpl.product_variant_id.id,
            'sh_subscription_plan_id': self.plan.id,
            'sh_plan_price': 500.0,
            'sh_qty': 1,
            'sh_recurrency': 1,
            'sh_unit': 'month',
            'sh_trial': True,
            'sh_trial_duration': 7,
            'sh_trial_unit': 'day',
            'sh_trial_end_date': today + timedelta(days=6),
            'sh_start_date': today + timedelta(days=7),
            'sh_date_of_next_payment': today + timedelta(days=7),
            'sh_taxes_ids': [(6, 0, [])],
            'state': 'in_progress'})

        template = self.env['woow_paas_platform.cloud_app_template'].sudo().create({
            'name': 'App 525', 'slug': 'app-525',
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example.invalid/app-525',
            'helm_chart_version': '0.0.0',
            'is_active': True, 'is_hidden': False})
        self.service = self.env['woow_paas_platform.cloud_service'].sudo().create({
            'name': 'Svc 525', 'workspace_id': self.workspace.id,
            'template_id': template.id, 'subscription_id': self.sub.id})

        self.partner.sudo().add_wallet_points(
            2000.0, description='525 A1 test credit')
        self.partner.invalidate_recordset(['wallet_balance'])

    # ── 使用者唯一入口（HTTP JSON-RPC）─────────────────────────────────
    def _call_skip_trial(self):
        resp = self.url_open(
            '/api/cloud-services/services/%s/skip-trial' % self.service.id,
            data='{"jsonrpc":"2.0","method":"call","params":{}}',
            headers={'Content-Type': 'application/json'})
        parsed = resp.json()
        self.assertIn('result', parsed, 'resp: %s' % parsed)
        self.env.invalidate_all()
        return parsed['result']

    # ── 四個側效果探針 ─────────────────────────────────────────────────
    def _points(self):
        """錢包餘額的權威載體：loyalty.card.points。"""
        self.env.invalidate_all()
        return self.partner.sudo().get_wallet_loyalty_card().points

    def _wallet_txns(self):
        return self.env['wallet.transaction'].sudo().search([
            ('partner_id', '=', self.partner.id),
            ('transaction_type', '=', 'deduct')])

    def _invoices(self):
        """只看掛在本測試訂閱下的發票，避免踩到環境既有資料。"""
        return self.env['account.move'].sudo().search([
            ('sh_subscription_id', '=', self.sub.id)])

    def _sub_snapshot(self):
        self.sub.invalidate_recordset()
        return {
            'sh_trial': self.sub.sh_trial,
            'sh_trial_end_date': self.sub.sh_trial_end_date,
            'sh_start_date': self.sub.sh_start_date,
            'sh_date_of_next_payment': self.sub.sh_date_of_next_payment,
        }

    # ── 注入：讓失敗發生在**扣點之後** ─────────────────────────────────
    def _break_payment_journal(self):
        """把 `_pay_from_wallet` 的收款 journal 抽掉，回傳復原用的 callable。

        失敗點落在 `action_post()`（發票 posted、序號消耗）與
        `deduct_wallet_points()`（點數已扣、wallet.transaction 已 INSERT）
        **之後**——即 issue #525 A1 表格最下面那一列的殘留形狀。

        刻意不 mock：這是真實會發生的公司設定錯誤，走的是產品程式碼本來就有的
        `raise UserError`，不是為了造紅燈而假造的路徑。
        """
        company = self.env.company.sudo()
        previous = company.sh_paid_subscription_journal
        company.sh_paid_subscription_journal = False
        journals = self.env['account.journal'].sudo().search([
            ('company_id', '=', company.id), ('type', 'in', ('bank', 'cash'))])
        journals.write({'active': False})

        def restore():
            journals.write({'active': True})
            company.sh_paid_subscription_journal = previous
        return restore

    def _fail_after_deduction(self, exception):
        """在扣點之後丟出**指定型別**的例外（給例外分類的兩組測試用）。

        `account.payment.action_post` 位於 `_pay_from_wallet` 的扣點之後，
        殘留形狀與 `_break_payment_journal` 一致，差別只在例外型別。
        """
        return patch.object(
            type(self.env['account.payment']), 'action_post',
            side_effect=exception)

    # ================================================================
    #  陽性對照：成功路徑上，四個探針都看得見側效果
    # ================================================================
    def test_successful_skip_trial_produces_all_four_side_effects(self):
        """四個探針的鑑別力證明——這條不綠，下面四條否定性斷言就是假綠。"""
        before_points = self._points()
        before_txns = len(self._wallet_txns())

        result = self._call_skip_trial()

        self.assertTrue(result.get('success'), 'skip-trial 應成功（回 %r）' % result)
        self.assertEqual(
            self._points(), before_points - 500.0,
            '成功路徑必須從錢包扣掉發票金額——探針讀得到扣點的證明')
        self.assertEqual(
            len(self._wallet_txns()), before_txns + 1,
            '成功路徑必須留下一筆 deduct 交易——探針讀得到 wallet.transaction 的證明')
        self.assertEqual(
            len(self._invoices()), 1,
            '成功路徑必須開出一張發票——探針讀得到 account.move 的證明')
        self.assertFalse(
            self._sub_snapshot()['sh_trial'],
            '成功路徑必須讓訂閱脫離試用——探針讀得到訂閱狀態的證明')

    def _record_wallet_debits(self):
        """把 `deduct_wallet_points` 包成會記錄呼叫的版本（**行為不變**）。

        回傳一個 list：每發生一次真實扣點就 append 一筆。用來把「注入點在扣點
        之後」從檔頭的**宣稱**變成可執行的**斷言**。清單是 Python 物件，不會被
        savepoint 回捲——這正是它在修好之後仍讀得到「扣點曾經發生」的原因。
        """
        Partner = type(self.env['res.partner'])
        original = Partner.deduct_wallet_points
        calls = []

        def recording(partner_self, *args, **kwargs):
            calls.append(args)
            return original(partner_self, *args, **kwargs)

        patcher = patch.object(Partner, 'deduct_wallet_points', recording)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    # ================================================================
    #  注入深度：證明失敗真的發生在「錢已經扣走」之後
    # ================================================================
    def test_journal_injection_fires_after_the_wallet_debit(self):
        """`_break_payment_journal` 的失敗點必須在扣點之後。

        若 `_pay_from_wallet` 哪天被重排（journal 查找搬到 `deduct_wallet_points`
        之前），扣點根本不會發生，下面四條「沒有殘留」就全部退化成恆真、輸出與
        真綠一模一樣。實測：把扣點移到 `_pay_from_wallet` 末尾後，那四條**在修復
        前也全綠**，只有本條會紅。
        """
        debits = self._record_wallet_debits()
        self._break_payment_journal()

        result = self._call_skip_trial()
        self.assertFalse(result.get('success'), '注入後應失敗（回 %r）' % result)

        self.assertEqual(
            len(debits), 1,
            '注入必須落在錢包扣點之後——沒扣過的點數當然「沒被扣走」，'
            '那四條殘留斷言就不是在守什麼東西')

    def test_payment_injection_fires_after_the_wallet_debit(self):
        """`_fail_after_deduction`（patch `account.payment.action_post`）同理。

        例外分類那兩條的 rollback 斷言一樣建立在「錢真的扣過」之上。
        """
        debits = self._record_wallet_debits()
        with self._fail_after_deduction(RuntimeError('boom')):
            result = self._call_skip_trial()
        self.assertFalse(result.get('success'), '注入後應失敗（回 %r）' % result)

        self.assertEqual(
            len(debits), 1,
            'AccessError／未預期例外那兩條的「點數沒被扣」斷言，前提是這個注入點'
            '確實在扣點之後')

    # ================================================================
    #  殘留面 1：錢包點數
    # ================================================================
    def test_failed_skip_trial_does_not_charge_the_wallet(self):
        self._break_payment_journal()
        before_points = self._points()

        result = self._call_skip_trial()
        self.assertFalse(result.get('success'), '注入後應失敗（回 %r）' % result)

        self.assertEqual(
            self._points(), before_points,
            '端點回失敗，錢包點數就不得被扣走——這是 #525 A1 的帳務不一致：'
            '錢已從錢包扣走、發票停在 posted／未付款')

    # ================================================================
    #  殘留面 2：wallet.transaction
    # ================================================================
    def test_failed_skip_trial_leaves_no_wallet_transaction(self):
        self._break_payment_journal()
        before_txns = len(self._wallet_txns())

        result = self._call_skip_trial()
        self.assertFalse(result.get('success'), '注入後應失敗（回 %r）' % result)

        self.assertEqual(
            len(self._wallet_txns()), before_txns,
            '失敗的 skip-trial 不得留下扣點流水——殘留的 deduct 會讓對帳與退點'
            '（#324 的 stamped deduct gate）讀到一筆從未成立的扣款')

    # ================================================================
    #  殘留面 3：account.move
    # ================================================================
    def test_failed_skip_trial_leaves_no_invoice(self):
        self._break_payment_journal()

        result = self._call_skip_trial()
        self.assertFalse(result.get('success'), '注入後應失敗（回 %r）' % result)

        self.assertFalse(
            self._invoices(),
            '失敗的 skip-trial 不得留下發票——殘留的 out_invoice 已 posted、'
            '序號已消耗，卻永遠不會有對應的 payment')

    # ================================================================
    #  殘留面 4：訂閱狀態（＝重試路徑的鑰匙）
    # ================================================================
    def test_failed_skip_trial_leaves_subscription_in_trial(self):
        self._break_payment_journal()
        before = self._sub_snapshot()
        self.assertTrue(
            before['sh_trial'], 'fixture 前提：訂閱一開始在試用期')

        result = self._call_skip_trial()
        self.assertFalse(result.get('success'), '注入後應失敗（回 %r）' % result)

        self.assertEqual(
            self._sub_snapshot(), before,
            '失敗的 skip-trial 不得讓訂閱脫離試用——`_skip_trial` 的前置檢查是'
            '`if not sub.sh_trial: raise`，殘留會把重試路徑鎖死')

    # ================================================================
    #  加重因子 2：使用者要能自我修復（再按一次）
    # ================================================================
    def test_user_can_retry_after_a_failed_skip_trial(self):
        """失敗 → 排除原因 → 再按一次，必須成功。

        殘留把重試鎖死是 #525 A1 比 #520 更嚴重的地方：試用期被燒掉、計費卻沒
        開始，使用者按幾次都只會拿到 'Subscription is not in trial period.'。
        """
        restore = self._break_payment_journal()
        first = self._call_skip_trial()
        self.assertFalse(first.get('success'), '第一次應失敗（回 %r）' % first)

        restore()          # 排除失敗原因（設定回收款 journal）

        retry = self._call_skip_trial()
        self.assertTrue(
            retry.get('success'),
            '排除原因後重試必須成功；失敗代表訂閱被自己上一次的殘留擋在'
            '「已脫離試用」狀態，使用者無法自救（回 %r）' % retry)

    # ================================================================
    #  例外分類：業務規則訊息原文回前端（鎖既有行為，非本次紅燈面）
    # ================================================================
    def test_business_rule_message_reaches_the_tenant(self):
        """UserError 的訊息是寫給使用者看的，不得被統一成泛用字串。

        修前就是綠的——這條鎖的是「修 #525 時別把 UserError 一起吞成泛用訊息」，
        不是本次的缺陷面。真正修前會紅的訊息面在下面兩條（AccessError／未預期）。
        """
        self._break_payment_journal()

        result = self._call_skip_trial()

        self.assertFalse(result.get('success'), result)
        # 先證明訊息**讀得到而且就是那一則**：只寫 assertNotEqual 的話，error
        # 缺席或空字串一樣不等於泛用字串 → 恆真假綠（#529 D 組同構）。綁公司名
        # 這個**資料**而非措辭，重寫訊息不會誤傷（慣例鏡射 #520 的 assertIn
        # template.name）。
        self.assertIn(
            self.env.company.name, result.get('error') or '',
            '應是注入的「無收款 journal」那條業務例外，訊息要指得出是哪家公司'
            '（回 %r）' % result)
        self.assertNotEqual(
            result.get('error'), _GENERIC_ERROR,
            '業務規則例外統一成泛用字串的話，原因只留在 server log，'
            '使用者與第一線支援都無從排錯')

    # ================================================================
    #  例外分類：AccessError 不得把 Odoo 內部技術名吐給租戶
    # ================================================================
    def test_access_error_message_stays_generic(self):
        """`AccessError` 繼承 `UserError`——單寫 `except UserError` 會連它一起
        原文回前端，把 Odoo 內部 model／欄位技術名吐到租戶畫面上。"""
        internal = 'woow_paas_platform.cloud_service'
        with self._fail_after_deduction(
                AccessError('您無權存取 %s 記錄' % internal)):
            result = self._call_skip_trial()

        self.assertFalse(result.get('success'), result)
        # 先釘死訊息**是**泛用那一則，`assertNotIn` 才不會在 error 缺席／為空
        # 字串時恆真（#529 D 組）。
        self.assertEqual(
            result.get('error'), _GENERIC_ERROR,
            'AccessError 不得被當成業務規則訊息原文回傳（回 %r）' % result)
        self.assertNotIn(
            internal, result.get('error') or '',
            'Odoo 內部 model 名不得出現在租戶前台（回 %r）' % result)

    def test_access_error_still_rolls_back_the_wallet_charge(self):
        before_points = self._points()
        with self._fail_after_deduction(AccessError('您無權存取記錄')):
            result = self._call_skip_trial()

        self.assertFalse(result.get('success'), result)
        self.assertEqual(
            self._points(), before_points,
            'AccessError 一樣要撤回已扣的點數')

    # ================================================================
    #  例外分類：未預期例外維持泛用訊息，但資料一樣不得殘留
    # ================================================================
    def test_unexpected_error_message_stays_generic(self):
        secret = 'psycopg2 connection string s3cr3t'
        with self._fail_after_deduction(RuntimeError(secret)):
            result = self._call_skip_trial()

        self.assertFalse(result.get('success'), result)
        # 同上：先證明讀得到泛用訊息本身，`assertNotIn` 才有鑑別力。
        self.assertEqual(
            result.get('error'), _GENERIC_ERROR,
            '未預期例外維持泛用訊息（回 %r）' % result)
        self.assertNotIn(
            secret, result.get('error') or '',
            '未預期例外的內部細節只留在 server log，不得外洩到前端（回 %r）'
            % result)

    def test_unexpected_error_still_rolls_back_the_wallet_charge(self):
        before_points = self._points()
        with self._fail_after_deduction(RuntimeError('boom')):
            result = self._call_skip_trial()

        self.assertFalse(result.get('success'), result)
        self.assertEqual(
            self._points(), before_points,
            '未預期例外一樣要撤回已扣的點數')
