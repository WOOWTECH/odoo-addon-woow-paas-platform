# -*- coding: utf-8 -*-
"""#864 格1（PR-A）：`api_invoice_pay` 半成交 —— 畫面說失敗，錢卻已經扣走。

`account_move._pay_from_wallet` 的寫入是**分段**的，而 `api_invoice_pay`
（`src/controllers/billing.py`）用 `except UserError` / `except Exception` 把整條
鏈上的例外吃掉、以 HTTP 200 ＋ `success: False` 返回。例外沒有傳播到框架層 →
request 正常結束 → **transaction 照常 commit** → 例外噴在哪一步，殘留就累積到哪：

    account_move.py `_pay_from_wallet`
      `action_post()`            發票 posted、**發票號碼（序號）已消耗**
      `deduct_wallet_points()`   **錢包點數已扣**＋wallet.transaction 已 INSERT
      journal 查找               ← 公司沒設收款 journal ⇒ `raise UserError`
      `account.payment` create   （更深的注入點才走得到）
      `reconcile()`              （更深的注入點才走得到）

三個加重因子：

1. **帳務不一致**——錢從錢包扣走了，發票停在 posted／未付款，沒有 payment、
   沒有 reconcile。這不是「少建一筆資料」，是使用者的錢對不上帳。
2. **重試會再扣一次**——與 #525 A1 的 skip-trial 不同，這裡的殘留**不會**把重試
   路徑鎖死：`api_invoice_pay` 允許 `state == 'posted'` 的發票再付一次，於是
   使用者看到「失敗」後再按一次，錢包被**扣第二次**。所以本檔的重試面斷言不是
   「還能不能重試」而是「重試成功之後總共扣了幾次」。
3. **發票號碼跳號**——被回捲不掉的 `action_post()` 燒掉一個序號，下一張真發票
   接在跳號之後。對電子發票是可稽核的缺口。

本檔的驗收面不是回應字串，而是**側效果**：錢包點數、wallet.transaction、
account.move 狀態、account.payment、reconcile 五個實體，以及發票序號。

## 注入方式

| helper | 失敗點 | 能證明的殘留 |
| --- | --- | --- |
| `_break_payment_journal()` | journal 查找（扣點**之後**、建 payment 之前） | 點數／wallet.transaction／發票 posted／序號 |
| `_fail_after_payment_create()` | `account.payment.action_post`（payment 已建） | 上列 ＋ **孤兒 account.payment** |
| `_fail_after_reconcile()` | `account.move.line.reconcile` 執行**之後** | 上列 ＋ **已成立的 reconcile** |

`_break_payment_journal` 刻意零 mock：那是真實會發生的公司設定錯誤，走的是產品
程式碼本來就有的 `raise UserError`，不是為了造紅燈而假造的路徑。另外兩個注入點
只為了把失敗推到更深的位置——`_break_payment_journal` 那條路上 payment 與
reconcile **根本沒被走到**，拿它去斷言「沒有 payment 殘留」會是恆真的假綠。

## 兩道防空真值的閘

1. **注入深度不是宣稱，是斷言**——三個 `test_*_fires_after_*` 把「失敗點在那一步
   之後」釘成可執行的斷言。若哪天 `_pay_from_wallet` 被重排（journal 查找搬到
   `deduct_wallet_points` 之前），殘留根本不會產生，下面所有「沒有殘留」的斷言
   就全部退化成恆真，而輸出與真綠一模一樣。
2. **陽性對照**——`test_successful_pay_produces_all_five_side_effects` 走同一條
   HTTP 路徑成功付款，斷言五個探針都讀得到側效果。這條不綠，下面每一條否定性
   斷言都是假綠。

## 為什麼一律走真 HTTP、不直呼 controller 方法

本檔每一條斷言的結論都取決於「例外**逸出被測碼之後**會發生什麼」。#572 已實測
記載：例外逸出時，直呼與真 HTTP 的交易語意會分歧，直呼寫的斷言在這種形狀下
系統性失去鑑別力。所以 fixture 建好之後一律用 `self.url_open()` 打 JSON-RPC。

## 前端那一半（`PayInvoiceModal.js`）

付款失敗時前端只設 `state.error`，餘額仍是打 API 之前的舊快取 ⇒ 使用者看著一個
不存在的餘額決定要不要再按一次。本 repo **沒有可執行 JS 的測試基礎設施**（無
hoot／QUnit，`HttpCase.browser_js` 在 paas-odoo image 內只會 SkipTest），所以那
一半走**靜態原始碼掃描**，形態與 `test_locale_formatting_guard_107.py` 一致：
`TestPayInvoiceModalRefetchesWalletOnFailure`。它守得住「失敗分支裡有沒有重抓
錢包」，守不住執行期真實渲染結果——後者本 PR 以人工 UI 驗證交付，不假裝涵蓋。
"""
import json
import os
import re
from unittest.mock import patch

from psycopg2.errorcodes import SERIALIZATION_FAILURE
from psycopg2.errors import SerializationFailure

from odoo.tests.common import HttpCase, TransactionCase, tagged

_AMOUNT = 500.0
_CREDIT = 2000.0
_GENERIC_ERROR = 'Payment processing failed.'

# 🔴 `HttpCase.url_open` 的預設 `timeout=12` 對「永遠衝突」那幾條不夠：修好之後
# 框架會真的跑滿 `MAX_TRIES_ON_CONCURRENCY_FAILURE = 5` 次，而每次重試前
# `time.sleep(random.uniform(0.0, 2 ** tryno))` ⇒ 期望 15 秒、最壞 30 秒。
# 症狀是 `requests.exceptions.ReadTimeout` ⇒ 測試報 error 而不是 fail，看起來像
# 測試寫壞了。慣例鏡射 `test_concurrency_retry_533._RETRY_SAFE_TIMEOUT`。
_RETRY_SAFE_TIMEOUT = 90


def _serialization_failure():
    """建一個 **pgcode 正確** 的 40001，否則驅動不到 `retrying()` 的重試分支。

    直接建構的 `SerializationFailure` `pgcode` 是 `None`（且唯讀），而
    `retrying()` 在重試分支會 `errorcodes.lookup(exc.pgcode)` ⇒ 合成例外會讓它
    在第一輪就 `KeyError`，重試路徑根本走不到。手法取自 Odoo 自己的
    `odoo/addons/test_http/controllers.py::upload_file_retry`。
    """
    exc = SerializationFailure(
        'could not serialize access due to concurrent update')
    exc.__setstate__({'pgcode': SERIALIZATION_FAILURE})
    return exc


class _InvoicePayFixtureMixin:
    """建一張可用錢包付掉的 draft 發票，並提供五個側效果探針。

    ⚠ 只放 helper：Odoo 的 test loader（`odoo/tests/loader.py::
    get_module_test_cases`）只看 `test_case_class.__dict__`，寫在 mixin 裡的
    `test_*` 一條都不會被執行，而輸出與「跑過且全綠」不可區分。
    """

    def _setup_invoice_fixture(self, tag):
        self.authenticate('admin', 'admin')
        self.admin_user = self.env.ref('base.user_admin')
        self.partner = self.admin_user.partner_id

        # out_invoice 需要公司有 sale journal。bare install 不保證已收斂 ⇒
        # fixture 自帶前提，否則「成功路徑走不完」會讓陽性對照失效、配套的
        # 否定性斷言全部退化成假綠。慣例鏡射 test_skip_trial_rollback_525.setUp。
        company = self.env.company.sudo()
        if not self.env['account.journal'].search([
            ('company_id', '=', company.id), ('type', '=', 'sale'),
        ], limit=1):
            self.env['account.journal'].sudo().create({
                'name': 'Test Sales %s' % tag, 'code': 'TS%s' % tag,
                'type': 'sale', 'company_id': company.id})
        self.assertTrue(
            self.env['account.journal'].sudo().search([
                ('company_id', '=', company.id),
                ('type', 'in', ('bank', 'cash')),
            ], limit=1),
            '本檔的陽性對照需要可用的收款 journal；沒有它 _pay_from_wallet 走不完')

        # 自建 product：不綁任何 seed 商品的定價與上架狀態（#478/#494 教訓）。
        # 明寫 taxes_id 空集合（#366）讓發票金額 == _AMOUNT，斷言才能對精確
        # 數字而不是「有變就好」。
        self.product = self.env['product.product'].sudo().create({
            'name': 'Product 864 %s' % tag,
            'type': 'service',
            'list_price': _AMOUNT,
            'taxes_id': [(6, 0, [])]})

        self.invoice = self._make_invoice()
        self.partner.sudo().add_wallet_points(
            _CREDIT, description='864 PR-A test credit')
        self.partner.invalidate_recordset(['wallet_balance'])

    def _make_invoice(self, amount=_AMOUNT):
        return self.env['account.move'].sudo().create({
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'invoice_line_ids': [(0, 0, {
                'product_id': self.product.id,
                'quantity': 1,
                'price_unit': amount,
                'tax_ids': [(5, 0, 0)],
            })]})

    # ── 使用者唯一入口（HTTP JSON-RPC）─────────────────────────────────
    def _post_pay(self, invoice_id=None, timeout=_RETRY_SAFE_TIMEOUT):
        """打付款端點，回傳**整包** parsed body。

        刻意不在這裡斷言 `result` 存在——「有沒有 result 鍵」是併發那組的判別式。
        """
        resp = self.url_open(
            '/api/billing/invoices/%s/pay'
            % (self.invoice.id if invoice_id is None else invoice_id),
            data=json.dumps({
                'jsonrpc': '2.0', 'method': 'call', 'id': 864, 'params': {}}),
            headers={'Content-Type': 'application/json'},
            timeout=timeout)
        self.env.invalidate_all()
        return resp.json()

    def _call_pay(self, invoice_id=None):
        parsed = self._post_pay(invoice_id)
        self.assertIn('result', parsed, 'resp: %s' % parsed)
        return parsed['result']

    # ── 五個側效果探針 ─────────────────────────────────────────────────
    def _points(self):
        """錢包餘額的權威載體：loyalty.card.points。"""
        self.env.invalidate_all()
        return self.partner.sudo().get_wallet_loyalty_card().points

    def _wallet_txns(self):
        return self.env['wallet.transaction'].sudo().search([
            ('partner_id', '=', self.partner.id),
            ('transaction_type', '=', 'deduct')])

    def _invoice_state(self):
        self.invoice.invalidate_recordset()
        return self.invoice.state

    def _payments(self):
        return self.env['account.payment'].sudo().search([
            ('partner_id', '=', self.partner.id)])

    def _partials(self):
        """本張發票上已成立的 reconcile（partial reconcile 列）。"""
        self.invoice.invalidate_recordset()
        lines = self.invoice.line_ids
        return self.env['account.partial.reconcile'].sudo().search([
            '|', ('debit_move_id', 'in', lines.ids),
            ('credit_move_id', 'in', lines.ids)])

    @staticmethod
    def _sequence_number(name):
        """從 `INV/2026/00007` 這類發票號碼抽出尾端流水號。"""
        match = re.search(r'(\d+)\s*$', name or '')
        return int(match.group(1)) if match else None


@tagged('post_install', '-at_install')
class TestInvoicePayFailureLeavesNoResidue(_InvoicePayFixtureMixin, HttpCase):
    """收款 journal 缺失（真實設定錯誤，零 mock）⇒ 不得留下任何金流殘留。"""

    def setUp(self):
        super().setUp()
        self._setup_invoice_fixture('864')

    # ── 注入：讓失敗發生在**扣點之後** ─────────────────────────────────
    def _break_payment_journal(self):
        """把 `_pay_from_wallet` 的收款 journal 抽掉，回傳復原用的 callable。

        失敗點落在 `action_post()`（發票 posted、序號消耗）與
        `deduct_wallet_points()`（點數已扣、wallet.transaction 已 INSERT）
        **之後**、建 `account.payment` 之前。刻意不 mock：這是真實會發生的公司
        設定錯誤，走的是產品程式碼本來就有的 `raise UserError`。
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

    def _record_wallet_debits(self):
        """把 `deduct_wallet_points` 包成會記錄呼叫的版本（**行為不變**）。

        回傳的 list 是 Python 物件，不會被 savepoint 回捲——這正是它在修好之後
        仍讀得到「扣點曾經發生」的原因。
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
    #  陽性對照：成功路徑上，五個探針都看得見側效果
    # ================================================================
    def test_successful_pay_produces_all_five_side_effects(self):
        """這條不綠，下面每一條否定性斷言都是假綠。"""
        before_points = self._points()
        before_txns = len(self._wallet_txns())
        before_payments = len(self._payments())

        result = self._call_pay()

        self.assertTrue(result.get('success'), '付款應成功（回 %r）' % result)
        self.assertEqual(
            self._points(), before_points - _AMOUNT,
            '成功路徑必須從錢包扣掉發票金額——探針讀得到扣點的證明')
        self.assertEqual(
            len(self._wallet_txns()), before_txns + 1,
            '成功路徑必須留下一筆 deduct 交易——探針讀得到 wallet.transaction 的證明')
        self.assertEqual(
            self._invoice_state(), 'posted',
            '成功路徑必須把發票 post 掉——探針讀得到 account.move 狀態的證明')
        self.assertEqual(
            len(self._payments()), before_payments + 1,
            '成功路徑必須建出 account.payment——探針讀得到 payment 的證明')
        self.assertTrue(
            self._partials(),
            '成功路徑必須把發票與 payment reconcile——探針讀得到 reconcile 的證明')

    # ================================================================
    #  注入深度：證明失敗真的發生在「錢已經扣走」之後
    # ================================================================
    def test_journal_injection_fires_after_the_wallet_debit(self):
        """`_break_payment_journal` 的失敗點必須在扣點之後。

        若 `_pay_from_wallet` 哪天被重排（journal 查找搬到 `deduct_wallet_points`
        之前），扣點根本不會發生，下面那些「沒有殘留」就全部退化成恆真、輸出與
        真綠一模一樣。
        """
        debits = self._record_wallet_debits()
        self._break_payment_journal()

        result = self._call_pay()
        self.assertFalse(result.get('success'), '注入後應失敗（回 %r）' % result)

        self.assertEqual(
            len(debits), 1,
            '注入必須落在錢包扣點之後——沒扣過的點數當然「沒被扣走」，'
            '那幾條殘留斷言就不是在守什麼東西')

    # ================================================================
    #  殘留面 1：錢包點數
    # ================================================================
    def test_failed_pay_does_not_charge_the_wallet(self):
        self._break_payment_journal()
        before_points = self._points()

        result = self._call_pay()
        self.assertFalse(result.get('success'), '注入後應失敗（回 %r）' % result)

        self.assertEqual(
            self._points(), before_points,
            '端點回失敗，錢包點數就不得被扣走——這是 #864 格1 的帳務不一致：'
            '錢已從錢包扣走、發票停在 posted／未付款')

    # ================================================================
    #  殘留面 2：wallet.transaction
    # ================================================================
    def test_failed_pay_leaves_no_wallet_transaction(self):
        self._break_payment_journal()
        before_txns = len(self._wallet_txns())

        result = self._call_pay()
        self.assertFalse(result.get('success'), '注入後應失敗（回 %r）' % result)

        self.assertEqual(
            len(self._wallet_txns()), before_txns,
            '失敗的付款不得留下扣點流水——殘留的 deduct 會讓對帳與退點'
            '（#324 的 stamped deduct gate）讀到一筆從未成立的扣款')

    # ================================================================
    #  殘留面 3：發票狀態（posted 是不可逆的對外事實）
    # ================================================================
    def test_failed_pay_leaves_the_invoice_unposted(self):
        self._break_payment_journal()
        self.assertEqual(
            self._invoice_state(), 'draft', 'fixture 前提：發票一開始是 draft')

        result = self._call_pay()
        self.assertFalse(result.get('success'), '注入後應失敗（回 %r）' % result)

        self.assertEqual(
            self._invoice_state(), 'draft',
            '失敗的付款不得把發票 post 掉——posted 的 out_invoice 已消耗序號、'
            '在下游（電子發票開立、對帳）是對外事實，卻永遠不會有對應的 payment')

    # ================================================================
    #  殘留面 4：發票序號（跳號是可稽核的缺口）
    # ================================================================
    def test_failed_pay_does_not_consume_the_invoice_sequence(self):
        """回捲之後，下一張發票的號碼不得跳號。

        判準刻意不是「本張發票沒有名字」——那與上一條重複。這裡量的是**外部
        可觀測的缺口**：先 post 一張錨點發票拿到號碼 n，注入失敗一次，再 post
        一張跟隨發票，它必須拿到 n+1。修前那次失敗會燒掉 n+1，跟隨發票變成
        n+2 ⇒ 帳上永遠少一號。
        """
        anchor = self._make_invoice(1.0)
        anchor.action_post()
        anchor_no = self._sequence_number(anchor.name)
        self.assertIsNotNone(
            anchor_no,
            '前提：發票號碼要抽得出流水號，否則本條的比較沒有意義（name=%r）'
            % anchor.name)

        self._break_payment_journal()
        result = self._call_pay()
        self.assertFalse(result.get('success'), '注入後應失敗（回 %r）' % result)

        follower = self._make_invoice(1.0)
        follower.action_post()

        self.assertEqual(
            self._sequence_number(follower.name), anchor_no + 1,
            '失敗的付款不得消耗發票序號：錨點 %s、跟隨 %s ⇒ 中間那一號被回捲不掉的'
            'action_post() 燒掉了' % (anchor.name, follower.name))

    # ================================================================
    #  重試：排除原因後要成功，而且**只扣一次**
    # ================================================================
    def test_user_can_retry_after_a_failed_pay(self):
        """失敗 → 排除原因 → 再按一次，必須成功（鎖住新 savepoint 不擋重試）。"""
        restore = self._break_payment_journal()
        first = self._call_pay()
        self.assertFalse(first.get('success'), '第一次應失敗（回 %r）' % first)

        restore()          # 排除失敗原因（設定回收款 journal）

        retry = self._call_pay()
        self.assertTrue(
            retry.get('success'),
            '排除原因後重試必須成功（回 %r）' % retry)

    def test_a_successful_retry_charges_the_wallet_only_once(self):
        """#525 A1 的殘留把重試**鎖死**；這裡相反——殘留讓重試**再扣一次**。

        `api_invoice_pay` 允許 `state == 'posted'` 的發票再付一次（`amount` 改
        取 `amount_residual`），所以修前使用者看到「失敗」再按一次會成功，代價是
        同一張發票的錢被扣了兩遍。所以重試面真正的判準是**總共扣了多少**。
        """
        before_points = self._points()
        restore = self._break_payment_journal()
        first = self._call_pay()
        self.assertFalse(first.get('success'), '第一次應失敗（回 %r）' % first)

        restore()
        retry = self._call_pay()
        self.assertTrue(retry.get('success'), '重試應成功（回 %r）' % retry)

        self.assertEqual(
            self._points(), before_points - _AMOUNT,
            '一張發票只能扣一次：失敗那次的扣點必須已被回捲，否則使用者為同一張'
            '發票付了兩遍，而畫面上只承認一次')

    # ================================================================
    #  例外分類：業務規則訊息原文回前端（鎖既有行為，非本次紅燈面）
    # ================================================================
    def test_business_rule_message_reaches_the_tenant(self):
        """`UserError` 的訊息是寫給使用者看的，不得被新增的 except 分支蓋掉。

        修前就是綠的——這條鎖的是「修 #864 格1 時別把 UserError 一起吞成泛用
        訊息」，不是本次的缺陷面。
        """
        self._break_payment_journal()

        result = self._call_pay()

        self.assertFalse(result.get('success'), result)
        # 先證明訊息**讀得到而且就是那一則**：只寫 assertNotEqual 的話，error
        # 缺席或空字串一樣不等於泛用字串 → 恆真假綠（#529 D 組同構）。綁公司名
        # 這個**資料**而非措辭，重寫訊息不會誤傷。
        self.assertIn(
            self.env.company.name, result.get('error') or '',
            '應是注入的「無收款 journal」那條業務例外，訊息要指得出是哪家公司'
            '（回 %r）' % result)
        self.assertNotEqual(
            result.get('error'), _GENERIC_ERROR,
            '業務規則例外統一成泛用字串的話，原因只留在 server log，'
            '使用者與第一線支援都無從排錯')


@tagged('post_install', '-at_install')
class TestInvoicePayDeepFailureLeavesNoResidue(
        _InvoicePayFixtureMixin, HttpCase):
    """失敗點推到 payment／reconcile **之後** ⇒ 那兩層殘留也必須撤乾淨。

    ⛔ 這兩面不能掛在 `_break_payment_journal` 上：那條路上 `account.payment`
    與 `reconcile()` **根本沒被走到**，「沒有 payment 殘留」在修前修後都成立
    ⇒ 恆真的假綠。所以本 class 自帶更深的注入點，並各配一條深度斷言。
    """

    def setUp(self):
        super().setUp()
        self._setup_invoice_fixture('864D')

    def _fail_after_payment_create(self, exception):
        """在 `account.payment` **建好之後**丟例外。

        `account.payment.action_post` 位於 `_pay_from_wallet` 的 payment
        `create()` 之後、`reconcile()` 之前。回傳 (patcher, calls)。
        """
        Payment = type(self.env['account.payment'])
        calls = []

        def exploding(payment_self, *args, **kwargs):
            calls.append(payment_self.ids)
            raise exception

        return patch.object(Payment, 'action_post', exploding), calls

    def _fail_after_reconcile(self, exception):
        """在 `reconcile()` **執行完之後**丟例外。

        `calls` 每筆記的是「那一刻該批 move line 上已成立的 partial reconcile
        筆數」——深度斷言要的不是「reconcile 被呼叫了」而是「reconcile 真的
        產生了東西」，否則「沒有 reconcile 殘留」仍可能是恆真。
        """
        Line = type(self.env['account.move.line'])
        original = Line.reconcile
        calls = []

        def failing(line_self, *args, **kwargs):
            result = original(line_self, *args, **kwargs)
            calls.append(line_self.env['account.partial.reconcile'].sudo(
            ).search_count([
                '|', ('debit_move_id', 'in', line_self.ids),
                ('credit_move_id', 'in', line_self.ids)]))
            raise exception

        return patch.object(Line, 'reconcile', failing), calls

    # ================================================================
    #  殘留面 5：account.payment
    # ================================================================
    def test_payment_injection_fires_after_the_payment_is_created(self):
        """深度斷言：注入點必須在 `account.payment` 建好之後。"""
        patcher, calls = self._fail_after_payment_create(RuntimeError('boom'))
        with patcher:
            result = self._call_pay()
        self.assertFalse(result.get('success'), '注入後應失敗（回 %r）' % result)

        self.assertEqual(
            len(calls), 1,
            '注入必須落在 account.payment 建立之後——沒建過的 payment 當然'
            '「沒有殘留」，下一條就不是在守什麼東西')

    def test_failed_pay_leaves_no_account_payment(self):
        before_payments = len(self._payments())

        patcher, _calls = self._fail_after_payment_create(RuntimeError('boom'))
        with patcher:
            result = self._call_pay()
        self.assertFalse(result.get('success'), '注入後應失敗（回 %r）' % result)

        self.assertEqual(
            len(self._payments()), before_payments,
            '失敗的付款不得留下 account.payment——孤兒 payment 會掛在收款 journal '
            '上、進銀行對帳，卻沒有任何發票與它 reconcile')

    # ================================================================
    #  殘留面 6：reconcile
    # ================================================================
    def test_reconcile_injection_fires_after_the_reconciliation(self):
        """深度斷言：注入點必須在 reconcile **真的產生 partial 列**之後。"""
        patcher, partials_seen = self._fail_after_reconcile(RuntimeError('boom'))
        with patcher:
            result = self._call_pay()
        self.assertFalse(result.get('success'), '注入後應失敗（回 %r）' % result)

        self.assertTrue(
            partials_seen and partials_seen[0] >= 1,
            '注入必須落在 reconcile 之後，而且那次 reconcile 真的產生了 partial '
            '列（實測到 %r）——否則下一條的「沒有 reconcile 殘留」是恆真'
            % (partials_seen,))

    def test_failed_pay_leaves_no_reconciliation(self):
        patcher, _seen = self._fail_after_reconcile(RuntimeError('boom'))
        with patcher:
            result = self._call_pay()
        self.assertFalse(result.get('success'), '注入後應失敗（回 %r）' % result)

        self.assertFalse(
            self._partials(),
            '失敗的付款不得留下已成立的 reconcile——那會讓發票在報表上看起來'
            '已核銷，而端點回報的是失敗')

    def test_deep_failure_still_rolls_back_the_wallet_charge(self):
        """更深的失敗點一樣要撤回已扣的點數（不只 journal 那一條路）。"""
        before_points = self._points()

        patcher, _calls = self._fail_after_payment_create(RuntimeError('boom'))
        with patcher:
            result = self._call_pay()
        self.assertFalse(result.get('success'), '注入後應失敗（回 %r）' % result)

        self.assertEqual(
            self._points(), before_points,
            'payment 之後才失敗的路徑，一樣要撤回已扣的點數')

    def test_unexpected_error_message_stays_generic(self):
        """未預期例外維持泛用訊息——內部細節只留在 server log。"""
        secret = 'psycopg2 connection string s3cr3t'
        patcher, _calls = self._fail_after_payment_create(RuntimeError(secret))
        with patcher:
            result = self._call_pay()

        self.assertFalse(result.get('success'), result)
        # 先證明讀得到泛用訊息本身，`assertNotIn` 才有鑑別力（#529 D 組）。
        self.assertEqual(
            result.get('error'), _GENERIC_ERROR,
            '未預期例外維持泛用訊息（回 %r）' % result)
        self.assertNotIn(
            secret, result.get('error') or '',
            '未預期例外的內部細節不得外洩到前端（回 %r）' % result)


@tagged('post_install', '-at_install')
class TestInvoicePayConcurrencyReachesRetrying(
        _InvoicePayFixtureMixin, HttpCase):
    """並發類 DB 例外必須傳到框架的 `retrying()`，不得被 controller 吞掉。

    `psycopg2.errors.SerializationFailure`（SQLSTATE 40001）的 MRO 是
    `SerializationFailure → TransactionRollbackError → OperationalError →
    DatabaseError → Error → Exception`——**它是 `Exception` 子類**，而
    `retrying()`（`odoo/service/model.py`）的攔截點在 controller 的 except 鏈
    **外層**。controller 一旦用裸 `except Exception` 收掉並正常 return，重試機制
    就整個失效：本來會被自動重試而成功的操作，變成使用者看到的失敗。

    這條路徑安全的理由（PR-A 卡片的 ⛔ 陷阱那格）：`_pay_from_wallet` 純 DB
    操作、無外部呼叫 ⇒ 框架重播整個 handler 不會產生重複的對外副作用。
    ⛔ **同一招不可套用到 `api_topup`**——那支 try 內是 ECPay 外呼，重播＝重複扣款。

    🔴 **本 class 量不到的那一格（綠燈不代表它成立）**：這裡注入的是 Python 層
    `raise`，PG 那側的交易**沒有**進入 aborted 狀態。因此「交易真的 aborted 之後，
    `cr.savepoint()` 的 `__exit__` 去 `ROLLBACK TO SAVEPOINT` 會不會二次爆炸」
    這格**沒有被走到**。要坐實需要兩條連線對同一列真的撞出 40001（#518 的先例
    是用 PR 內的外部真並發腳本補的）。本 PR 的依據是同形狀的
    `api_service_skip_trial`（#525 A1 ＋ #533）已在 develop 上跑著，不是本檔測到的。
    ⚠ 慣例鏡射 `test_concurrency_retry_533.py` 檔頭「不在本檔涵蓋範圍」那一節。
    """

    def setUp(self):
        super().setUp()
        self._setup_invoice_fixture('864C')
        self.Move = type(self.env['account.move'])

    def _patch_always_failing(self):
        """讓 `_pay_from_wallet` **每次**都拋 40001。回傳呼叫次數 list。"""
        calls = []

        def exploding(_self, *args, **kwargs):
            calls.append(1)
            raise _serialization_failure()

        patcher = patch.object(self.Move, '_pay_from_wallet', exploding)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def _patch_failing_once(self):
        """第一次拋 40001、之後照原樣執行——模擬「重試一次就會成功」的衝突。"""
        original = self.Move._pay_from_wallet
        calls = []

        def flaky(_self, *args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise _serialization_failure()
            return original(_self, *args, **kwargs)

        patcher = patch.object(self.Move, '_pay_from_wallet', flaky)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    # ── 探針自驗 ───────────────────────────────────────────────────────
    def test_the_injected_exception_carries_a_real_pgcode(self):
        """這條不綠，底下所有「有沒有重試」的斷言都在測一條走不到的路徑。"""
        exc = _serialization_failure()
        self.assertEqual(
            exc.pgcode, SERIALIZATION_FAILURE,
            '注入的例外必須帶真正的 40001 pgcode，否則 retrying() 會在 '
            'errorcodes.lookup() 就炸掉')

    def test_injection_fires_inside_the_endpoint(self):
        """若 `_pay_from_wallet` 哪天不再被這支端點呼叫，注入就完全沒發生。"""
        calls = self._patch_always_failing()

        self._post_pay()

        self.assertGreaterEqual(
            len(calls), 1,
            '注入的方法必須真的被端點呼叫到，否則本 class 其餘斷言都是恆真')

    # ── 缺陷探針①：例外必須傳到框架 ───────────────────────────────────
    def test_serialization_failure_is_not_swallowed(self):
        """修前：`except Exception` 收掉 → HTTP 200 ＋ JSON-RPC `result`。

        修後：例外逸出 controller → 框架回 JSON-RPC `error`。兩者可區分。
        """
        self._patch_always_failing()

        parsed = self._post_pay()

        self.assertNotIn(
            'result', parsed,
            '並發例外被 controller 吞掉並轉成一般業務失敗 ⇒ retrying() 沒有機會'
            '以新 snapshot 重試，本來會自動成功的付款變成使用者看到的失敗。'
            '回應：%r' % parsed)

    # ── 缺陷探針②：只衝突一次 ⇒ 重試應該把操作救回來 ─────────────────
    def test_a_transient_conflict_is_retried_and_the_payment_succeeds(self):
        calls = self._patch_failing_once()

        parsed = self._post_pay()

        self.assertEqual(
            parsed.get('result', {}).get('success'), True,
            '一次性的並發衝突應該被 retrying() 以新 snapshot 重試並成功；'
            '_pay_from_wallet 被呼叫 %d 次，回應：%r' % (len(calls), parsed))

    def test_a_transient_conflict_triggers_a_second_attempt(self):
        """把上一條的機制面單獨釘住：真的有第二次嘗試，不是第一次就矇對。"""
        calls = self._patch_failing_once()

        self._post_pay()

        self.assertEqual(
            len(calls), 2,
            '一次性衝突下 _pay_from_wallet 應被呼叫兩次（第一次拋 40001、第二次'
            '成功）；只被呼叫 %d 次代表 retrying() 從來沒有拿到那個例外' % len(calls))


# ---------------------------------------------------------------------------
# 前端那一半：失敗分支要重抓錢包餘額
# ---------------------------------------------------------------------------
_MODAL_REL_PATH = (
    '..', 'static', 'src', 'paas', 'components', 'modal', 'PayInvoiceModal.js')

# 合成對照樣本：讓「抽取器真的分得出兩者」成為可執行的斷言，而不是宣稱。
# ⚠ 真實檔案在**修好之後**兩個分支都會有 `fetchWallet` ⇒ 只掃「檔案裡有沒有
# 這個字」零鑑別力（成功分支本來就有一個），判準必須是**失敗分支裡有沒有**。
_SYNTHETIC_WITH_REFETCH = '''
  async onConfirm() {
    const result = await billingService.payInvoice(1);
    if (result.success) {
      await billingService.fetchWallet();
      this.props.onPaid();
    } else {
      this.state.error = result.error;
      await billingService.fetchWallet();
      this.state.paying = false;
    }
  }
'''
_SYNTHETIC_WITHOUT_REFETCH = '''
  async onConfirm() {
    const result = await billingService.payInvoice(1);
    if (result.success) {
      await billingService.fetchWallet();
      this.props.onPaid();
    } else {
      this.state.error = result.error;
      this.state.paying = false;
    }
  }
'''


def _failure_branch(source):
    """抽出 `onConfirm()` 裡 `} else {` 之後那一段（含大括號配對）。

    抽不到回 `None`——呼叫端必須先斷言它不是 `None`，否則「失敗分支裡沒有 X」
    在抽取器失效時會恆真（探針壞掉與陰性結果不可區分）。
    """
    entry = source.find('onConfirm(')
    if entry < 0:
        return None
    marker = source.find('} else {', entry)
    if marker < 0:
        return None
    start = marker + len('} else {')
    depth = 1
    for index in range(start, len(source)):
        char = source[index]
        if char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                return source[start:index]
    return None


@tagged('post_install', '-at_install')
class TestPayInvoiceModalRefetchesWalletOnFailure(TransactionCase):
    """`PayInvoiceModal.onConfirm` 的失敗分支必須重抓錢包餘額（靜態掃描）。

    本 repo 沒有可執行 JS 的測試基礎設施（無 hoot／QUnit，`HttpCase.browser_js`
    在 paas-odoo image 內只會 SkipTest），所以這條走靜態原始碼掃描，形態鏡射
    `test_locale_formatting_guard_107.py` 與 `test_agent_pod_phase_c_sse_gateway.py`
    的 XSS guard。

    守得住：失敗分支又被改回「只設 error、不重抓餘額」。
    守不住：`billingService.fetchWallet()` 內部實作被改壞、以及執行期真實渲染
    結果——後者靠 PR 的人工 UI 驗證，本檔刻意不假裝涵蓋。
    """

    def _modal_source(self):
        path = os.path.join(os.path.dirname(__file__), *_MODAL_REL_PATH)
        self.assertTrue(
            os.path.exists(path), '找不到 PayInvoiceModal.js（%s）' % path)
        with open(path, encoding='utf-8') as handle:
            return handle.read()

    def test_the_failure_branch_extractor_discriminates(self):
        """探針自檢：抽取器對兩份合成樣本必須給出相反結論。

        這條不綠，下面兩條的陰性／陽性結果都不可採信。
        """
        with_branch = _failure_branch(_SYNTHETIC_WITH_REFETCH)
        without_branch = _failure_branch(_SYNTHETIC_WITHOUT_REFETCH)

        self.assertIsNotNone(with_branch, '抽取器在合成陽性樣本上就抽不到失敗分支')
        self.assertIsNotNone(without_branch, '抽取器在合成陰性樣本上就抽不到失敗分支')
        self.assertIn(
            'fetchWallet', with_branch, '合成陽性樣本的失敗分支應被判為「有重抓」')
        self.assertNotIn(
            'fetchWallet', without_branch,
            '合成陰性樣本的失敗分支應被判為「沒重抓」——抽取器若把整個 onConfirm '
            '都收進來，成功分支的 fetchWallet 會讓它恆真')

    def test_the_extractor_finds_the_real_failure_branch(self):
        """前提釘死：真實檔案抽得到失敗分支，下一條才不是恆真。"""
        branch = _failure_branch(self._modal_source())

        self.assertTrue(
            branch and branch.strip(),
            'PayInvoiceModal.onConfirm 抽不到 `} else {` 失敗分支——元件結構若被'
            '重寫（例如改用 early return），本檔的守線要跟著改，不能靜默失效')

    def test_the_failure_branch_refetches_the_wallet(self):
        """#864 格1：付款失敗後餘額可能已被後端回捲，舊快取會誤導使用者。"""
        branch = _failure_branch(self._modal_source())

        self.assertIn(
            'fetchWallet', branch or '',
            '付款失敗分支必須重抓錢包餘額：後端回捲之後（或別的頁籤／自動扣款'
            '動過錢包）`this.billing.wallet` 仍是打 API 之前的舊快取，使用者會'
            '看著一個不存在的餘額決定要不要再按一次')
