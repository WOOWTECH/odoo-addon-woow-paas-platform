"""Tests for low balance notification during subscription auto-invoice."""
from datetime import date, timedelta
from unittest.mock import patch

from odoo.tests.common import TransactionCase


class TestLowBalanceNotification(TransactionCase):

    def setUp(self):
        super().setUp()
        self.partner = self.env['res.partner'].create({
            'name': 'Test User',
            'email': 'test@example.com',
            'wallet_balance': 100.0,
        })

    def _setup_ecpg_token(self, bindcard_id):
        """Provision an ECPG-ready provider + token for self.partner and set default.

        Returns: (provider, token) or calls self.skipTest if no ECPay provider exists.
        """
        provider = self.env['payment.provider'].sudo().search(
            [('code', '=', 'ECPay')], limit=1,
        )
        if not provider:
            self.skipTest("ECPay provider not configured in this DB")
        provider.write({
            'enable_ecpg': True,
            'MerchantID': '3002607',
            'HashKey': 'pwFHCqoQZGmho4w6',
            'HashIV': 'EkRm7iFT261dpevs',
            'state': 'test',
        })
        payment_method = self.env.ref(
            'payment.payment_method_card', raise_if_not_found=False,
        ) or self.env.ref('payment.payment_method_unknown')
        token = self.env['payment.token'].sudo().create({
            'provider_id': provider.id,
            'partner_id': self.partner.id,
            'payment_method_id': payment_method.id,
            'payment_details': '**** **** **** ' + bindcard_id[-4:],
            'provider_ref': bindcard_id,
            'ecpg_bindcard_id': bindcard_id,
            'ecpg_member_id': str(self.partner.id),
        })
        if 'property_payment_token_id' in self.partner._fields:
            self.partner.with_company(self.partner.company_id or self.env.company) \
                        .sudo().property_payment_token_id = token
        return provider, token

    def test_notification_sent_when_insufficient_balance(self):
        """When auto-invoice finds insufficient balance, email must be sent."""
        Subscription = self.env['sh.subscription.subscription']
        partner = self.partner
        invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': partner.id,
            'invoice_line_ids': [(0, 0, {
                'name': 'Sub fee',
                'quantity': 1,
                'price_unit': 500.0,
            })],
        })

        template = self.env.ref(
            'woow_paas_platform.mail_template_low_balance',
            raise_if_not_found=False,
        )
        self.assertIsNotNone(
            template,
            "mail_template_low_balance must be defined in data/mail_templates.xml"
        )

        Subscription._notify_low_balance(partner, invoice)

        mails = self.env['mail.mail'].search([
            ('email_to', 'ilike', partner.email),
            ('subject', 'ilike', 'top up'),
        ])
        self.assertTrue(mails, "A top-up notification email should have been queued")

    # 已移除 test_insufficient_balance_triggers_notify_via_auto_invoice：
    # `inspect.getsource(ShSubscriptionFix._on_payment_failure)` ＋
    # `assertIn('_notify_low_balance', source)` 可被註解餵飽（拔掉呼叫、留一行
    # 提到該名字的註解就照綠）。它守的行為已由 test_low_balance_dedup_retry_389
    # 的 TestLowBalanceDedupRetry 覆蓋——2026-08-03 於當前 develop 實測該注入下
    # 那 8 條全紅，屬純冗餘，故不另補替代測試。

    def test_attempt_auto_topup_success_credits_wallet(self):
        """When ECPG charge succeeds, helper credits the wallet and returns (True, 'ok')."""
        from unittest.mock import patch

        provider, token = self._setup_ecpg_token('BC_AUTOTOPUP')

        def _fake_send(self_tx):
            self_tx.state = 'done'
            self_tx.provider_reference = 'FAKE_ECPAY_REF'
            return None

        Subscription = self.env['sh.subscription.subscription']
        before_balance = self.partner.wallet_balance

        with patch.object(
            type(self.env['payment.transaction']),
            '_send_payment_request', _fake_send,
        ):
            ok, msg = Subscription._attempt_auto_topup(self.partner, 400.0)

        self.assertTrue(ok, f"Expected success, got ({ok}, {msg})")
        self.assertEqual(msg, 'ok')
        self.partner.invalidate_recordset()
        self.assertEqual(self.partner.wallet_balance, before_balance + 400.0)

        tx_rows = self.env['wallet.transaction'].sudo().search([
            ('partner_id', '=', self.partner.id),
            ('transaction_type', '=', 'topup'),
        ], order='id desc', limit=1)
        self.assertTrue(tx_rows, "Expected a wallet.transaction row for auto-topup")
        self.assertEqual(tx_rows.amount, 400.0)

    def test_attempt_auto_topup_returns_no_token_when_partner_has_no_default(self):
        """Partner without a default payment token → (False, 'no_token')."""
        Subscription = self.env['sh.subscription.subscription']
        # self.partner has no property_payment_token_id in setUp.
        ok, msg = Subscription._attempt_auto_topup(self.partner, 400.0)
        self.assertFalse(ok)
        self.assertEqual(msg, 'no_token')

    def test_attempt_auto_topup_returns_3ds_required_when_ecpg_redirects(self):
        """ECPG charge that needs 3DS → (False, '3ds_required'), no wallet change."""
        from unittest.mock import patch

        provider, token = self._setup_ecpg_token('BC_3DS')

        def _fake_send_3ds(self_tx):
            # Simulate ECPG override returning a redirect dict for 3DS.
            return {'redirect_url': 'https://3ds.example/verify'}

        before = self.partner.wallet_balance
        with patch.object(
            type(self.env['payment.transaction']),
            '_send_payment_request', _fake_send_3ds,
        ):
            Subscription = self.env['sh.subscription.subscription']
            ok, msg = Subscription._attempt_auto_topup(self.partner, 400.0)

        self.assertFalse(ok)
        self.assertEqual(msg, '3ds_required')
        self.partner.invalidate_recordset()
        self.assertEqual(self.partner.wallet_balance, before, "Wallet must be unchanged on 3DS")

    def test_attempt_auto_topup_returns_failure_state_when_ecpg_errors(self):
        """When _send_payment_request leaves tx.state != 'done', return (False, state)."""
        from unittest.mock import patch

        provider, token = self._setup_ecpg_token('BC_FAIL')

        def _fake_send_error(self_tx):
            self_tx.state = 'error'
            return None

        with patch.object(
            type(self.env['payment.transaction']),
            '_send_payment_request', _fake_send_error,
        ):
            Subscription = self.env['sh.subscription.subscription']
            ok, msg = Subscription._attempt_auto_topup(self.partner, 400.0)

        self.assertFalse(ok)
        self.assertEqual(msg, 'error')

    # 已移除 test_auto_invoice_attempts_auto_topup_before_giving_up：兩條斷言
    # （`assertIn('_attempt_auto_topup', source)` 與
    # `source.count('_handle_invoice_creation') >= 2`）都可被註解餵飽，見
    # TestSettlementRunsAutoTopup 的 docstring。替代守線＝該 class 的三條行為
    # 測試，它們對同樣的注入會紅。

    def test_attempt_auto_topup_returns_no_shortfall_for_zero(self):
        """Pre-condition: shortfall<=0 returns ('no_shortfall', False)
        without invoking the wallet.topup factory."""
        Sub = self.env['sh.subscription.subscription']
        ok, msg = Sub._attempt_auto_topup(self.partner, 0)
        self.assertFalse(ok)
        self.assertEqual(msg, 'no_shortfall')

        ok, msg = Sub._attempt_auto_topup(self.partner, -10)
        self.assertFalse(ok)
        self.assertEqual(msg, 'no_shortfall')

    def test_attempt_auto_topup_handles_currency_missing(self):
        """If create_auto_topup raises ValueError (e.g. base.TWD missing
        or other ref lookup failure), the shim catches and returns
        ('currency_not_configured', False) instead of propagating."""
        from unittest.mock import patch
        self._setup_ecpg_token('AB12345678')  # ensures token resolution succeeds
        with patch.object(
            type(self.env['woow.wallet.topup']),
            'create_auto_topup',
            side_effect=ValueError("External ID not found: base.TWD"),
        ):
            ok, msg = self.env['sh.subscription.subscription']._attempt_auto_topup(
                self.partner, 100,
            )
        self.assertFalse(ok)
        self.assertEqual(msg, 'currency_not_configured')


class TestSettlementRunsAutoTopup(TransactionCase):
    """`_try_settle_invoice` 的行為守線：餘額不足時**真的**去跑自動儲值。

    取代原本的 `inspect.getsource(ShSubscriptionFix._try_settle_invoice)` ＋
    `assertIn('_attempt_auto_topup', source)` ＋
    `count('_handle_invoice_creation') >= 2` 子字串守衛。兩條都可被註解餵飽
    ——把呼叫整段拔掉、只留一行提到那個名字的註解，守衛照樣綠；第二條更鬆，
    註解裡提兩次就滿足 `>= 2`。

    2026-08-02 於 #534 盤點時實測：拔掉 `sh_subscription_fix.py:332` 的
    `_attempt_auto_topup` 呼叫後跑全套 → `0 failed, 0 error(s) of 2193`。
    **自動儲值可以整段消失、全套照樣全綠**——這是金流路徑，且當時是全 repo
    唯一憑據。

    改測行為：造出「餘額不足以支付發票」的真實情境，斷言 settlement 真的扣款
    成功、錢包真的被自動儲值補進來、發票真的被結清。三條測試覆蓋
    `_handle_invoice_creation` 的兩條路徑（餘額夠／儲值成功）與失敗路徑。

    🔴 前置條件一律**斷言**而非假設。特別是 `wallet_balance` 是 loyalty.card
    支撐的 non-stored compute，**直接 write 是 no-op**（本檔上面那個 class 的
    `setUp` 寫 `'wallet_balance': 100.0` 其實沒有生效）。錢包一定要走
    `add_wallet_points()` 灌，然後斷言灌進去了——否則「餘額不足」這個前提會
    因為別的原因成立，測試看似綠、實際上什麼都沒驗到。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Sub = cls.env['sh.subscription.subscription']
        cls.Move = cls.env['account.move']
        # `_pay_from_wallet` 需要一個 bank/cash journal 建 account.payment
        # （慣例鏡射 test_account_move_pay_from_wallet.setUpClass）
        journal = cls.env['account.journal'].sudo().search(
            [('company_id', '=', cls.env.company.id),
             ('type', 'in', ('bank', 'cash'))], limit=1)
        if not journal:
            cls.env['account.journal'].sudo().create({
                'name': 'Test Bank T1', 'type': 'bank', 'code': 'TBT1',
                'company_id': cls.env.company.id})

    # ── fixture helpers ──────────────────────────────────────────────────
    def _make_partner(self, name):
        return self.env['res.partner'].create({
            'name': name, 'email': '%s@example.test' % name.lower()})

    def _credit(self, partner, amount):
        """唯一有效的灌錢包途徑（wallet_balance 是 non-stored compute）。"""
        partner.sudo().add_wallet_points(amount, description='T1 fixture')
        partner.invalidate_recordset(['wallet_balance'])

    def _make_sub(self, partner, price):
        """直接落 in_progress，不走 confirm（避免 eager 開發票）。

        慣例鏡射 test_subscription_failure_handling._make_sub_record。
        """
        today = date.today()
        return self.Sub.sudo().create({
            'sh_partner_id': partner.id, 'sh_unit': 'month',
            'sh_recurrency': 1, 'sh_plan_price': price, 'sh_qty': 1,
            'sh_start_date': today - timedelta(days=1),
            'sh_end_date': today + timedelta(days=29),
            'sh_date_of_next_payment': today,
            'state': 'in_progress', 'sh_trial': False})

    def _make_invoice(self, partner, sub, amount):
        return self.Move.sudo().create({
            'move_type': 'out_invoice', 'partner_id': partner.id,
            'sh_subscription_id': sub.id,
            'invoice_line_ids': [(0, 0, {
                'name': 'Cycle fee T1', 'quantity': 1, 'price_unit': amount,
                'tax_ids': [(5, 0, 0)]})]})   # 無稅：金額好對

    def _setup_token(self, partner, bindcard_id):
        """為 partner 備妥可用的 ECPG token（沒有 provider 就 skip）。"""
        provider = self.env['payment.provider'].sudo().search(
            [('code', '=', 'ECPay')], limit=1)
        if not provider:
            self.skipTest('ECPay provider not configured in this DB')
        provider.write({
            'enable_ecpg': True, 'MerchantID': '3002607',
            'HashKey': 'pwFHCqoQZGmho4w6', 'HashIV': 'EkRm7iFT261dpevs',
            'state': 'test'})
        payment_method = self.env.ref(
            'payment.payment_method_card', raise_if_not_found=False,
        ) or self.env.ref('payment.payment_method_unknown')
        token = self.env['payment.token'].sudo().create({
            'provider_id': provider.id, 'partner_id': partner.id,
            'payment_method_id': payment_method.id,
            'payment_details': '**** **** **** ' + bindcard_id[-4:],
            'provider_ref': bindcard_id, 'ecpg_bindcard_id': bindcard_id,
            'ecpg_member_id': str(partner.id)})
        if 'property_payment_token_id' in partner._fields:
            partner.with_company(partner.company_id or self.env.company) \
                   .sudo().property_payment_token_id = token
        return token

    def _fake_ecpay_ok(self):
        """讓 ECPay 授權「成功」——tx 落 done，wallet.topup 於是入帳。"""
        def _send(self_tx):
            self_tx.state = 'done'
            self_tx.provider_reference = 'FAKE_T1_REF'
        return patch.object(
            type(self.env['payment.transaction']), '_send_payment_request',
            _send)

    def _fake_ecpay_declined(self):
        def _send(self_tx):
            self_tx.state = 'error'
        return patch.object(
            type(self.env['payment.transaction']), '_send_payment_request',
            _send)

    # ── 路徑 B：餘額不足 → 自動儲值補足 → 發票結清 ──────────────────────
    def test_shortfall_triggers_auto_topup_and_settles_invoice(self):
        """核心：餘額不足時 settlement 必須真的跑自動儲值，並把發票結清。

        注入 `_attempt_auto_topup` 呼叫消失時，這條會紅在 `assertTrue(result)`
        ／錢包沒被補／發票沒被結清——而不是只有某個字串沒出現。
        """
        partner = self._make_partner('T1 Shortfall')
        self._credit(partner, 100.0)
        self._setup_token(partner, 'BC_T1_OK')
        sub = self._make_sub(partner, 500.0)
        invoice = self._make_invoice(partner, sub, 500.0)

        # ── 陽性對照：情境真的成立（缺一則後面的斷言全部失去鑑別力）──
        self.assertEqual(
            invoice.amount_total, 500.0,
            '發票金額不是預期值 ⇒ fixture 沒建對（稅／幣別介入？）')
        self.assertEqual(
            partner.wallet_balance, 100.0,
            'add_wallet_points 沒把錢灌進去 ⇒ 「餘額不足」可能只是因為錢包是 0，'
            '不是因為我造出了缺口')
        self.assertLess(
            partner.wallet_balance, invoice.amount_total,
            '前置：餘額必須真的不足，否則走的是 balance-sufficient 那條路，'
            '根本碰不到自動儲值')
        self.assertEqual(invoice.state, 'draft', '前置：發票應仍為未結清的草稿')

        with self._fake_ecpay_ok():
            result = self.Sub._try_settle_invoice(sub, invoice)

        # ── 核心斷言 ──
        self.assertTrue(
            result,
            'settlement 應回報成功——餘額不足但自動儲值可以補足時，'
            '_try_settle_invoice 必須先試自動儲值再放棄')
        partner.invalidate_recordset(['wallet_balance'])
        topups = self.env['woow.wallet.topup'].sudo().search([
            ('partner_id', '=', partner.id)])
        self.assertTrue(
            topups,
            '沒有任何 wallet.topup 被建出來 ⇒ 自動儲值根本沒被觸發'
            '（正是 #534 盤點時注入的那個缺陷）')
        self.assertEqual(
            sum(topups.mapped('credits')), 400.0,
            '自動儲值金額應等於缺口 500-100（實得 %s）'
            % sum(topups.mapped('credits')))
        invoice.invalidate_recordset()
        self.assertEqual(
            invoice.payment_state, 'paid',
            '儲值成功後發票必須被結清——這條打的是 auto-topup-success 分支上的 '
            '_handle_invoice_creation 呼叫（舊守衛的 count>=2 想守、但守不住的那個）')

    # ── 路徑 A：餘額本來就夠 → 直接結清（不動用自動儲值）────────────────
    def test_sufficient_balance_settles_without_topup(self):
        """另一條 `_handle_invoice_creation` 路徑：餘額夠時直接從錢包扣。"""
        partner = self._make_partner('T1 Sufficient')
        self._credit(partner, 800.0)
        sub = self._make_sub(partner, 500.0)
        invoice = self._make_invoice(partner, sub, 500.0)

        # 陽性對照：錢包真的有錢、且真的蓋得過發票
        self.assertEqual(
            partner.wallet_balance, 800.0,
            'add_wallet_points 沒生效 ⇒ 這條會退化成在測「0 元錢包也能過」')
        self.assertGreaterEqual(partner.wallet_balance, invoice.amount_total)
        self.assertEqual(invoice.state, 'draft')

        result = self.Sub._try_settle_invoice(sub, invoice)

        self.assertTrue(result, '餘額足夠時 settlement 應成功')
        invoice.invalidate_recordset()
        self.assertEqual(
            invoice.payment_state, 'paid',
            '餘額足夠路徑上的 _handle_invoice_creation 沒把發票結清')
        partner.invalidate_recordset(['wallet_balance'])
        self.assertEqual(
            partner.wallet_balance, 300.0,
            '錢包應被扣掉發票金額 800-500（實得 %s）' % partner.wallet_balance)
        self.assertFalse(
            self.env['woow.wallet.topup'].sudo().search(
                [('partner_id', '=', partner.id)]),
            '餘額足夠時不得觸發自動儲值（會對使用者的卡多刷一筆）')

    # ── 失敗路徑：自動儲值被拒 → 不得謊報成功、不得結清發票 ──────────────
    def test_declined_topup_leaves_invoice_unpaid(self):
        """儲值被拒時 settlement 必須回報失敗，且不得把發票當成已付。"""
        partner = self._make_partner('T1 Declined')
        self._credit(partner, 100.0)
        self._setup_token(partner, 'BC_T1_NG')
        sub = self._make_sub(partner, 500.0)
        invoice = self._make_invoice(partner, sub, 500.0)

        # 陽性對照：缺口真的存在（否則「回報失敗」可能只是因為別的原因）
        self.assertEqual(partner.wallet_balance, 100.0)
        self.assertLess(partner.wallet_balance, invoice.amount_total)

        with self._fake_ecpay_declined():
            result = self.Sub._try_settle_invoice(sub, invoice)

        self.assertFalse(
            result, '儲值被拒時 settlement 不得回報成功（會讓 dunning 不啟動）')
        invoice.invalidate_recordset()
        self.assertNotEqual(
            invoice.payment_state, 'paid',
            '儲值失敗卻把發票標成已付 ⇒ 平台白送一個週期')
        partner.invalidate_recordset(['wallet_balance'])
        self.assertEqual(
            partner.wallet_balance, 100.0,
            '儲值失敗不得動到錢包餘額（實得 %s）' % partner.wallet_balance)
