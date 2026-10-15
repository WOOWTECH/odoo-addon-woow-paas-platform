"""Tests for wallet.topup.create_auto_topup factory method (Path B Phase 4)."""
from unittest.mock import patch

from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase


class TestCreateAutoTopup(TransactionCase):

    def setUp(self):
        super().setUp()
        self.partner = self.env['res.partner'].create({
            'name': 'Test',
            'email': 'test@example.com',
        })

    def test_create_auto_topup_returns_topup_record(self):
        """create_auto_topup creates a draft topup with source=auto, no plan_id."""
        # 200 已是 NT$100 的倍數且 ＞ 下限 ⇒ 這裡驗的是「照缺口刷」，不碰下限／進位。
        topup = self.env['woow.wallet.topup'].create_auto_topup(
            partner=self.partner, amount=200.0,
        )
        self.assertEqual(topup.source, 'auto')
        self.assertEqual(topup.state, 'draft')
        self.assertEqual(topup.amount, 200.0)
        self.assertEqual(topup.credits, 200.0)
        self.assertEqual(topup.bonus, 0.0)
        self.assertFalse(topup.plan_id, "auto-topup must NOT pin a plan")
        self.assertEqual(topup.partner_id, self.partner)

    def test_create_auto_topup_inherits_partner_invoice_carrier(self):
        """The new topup carries the partner's invoice carrier preference."""
        # Use the same field names as res_partner_invoice.py
        # (woow_invoice_carrier_type / woow_invoice_carrier are the partner-side fields)
        if 'woow_invoice_carrier_type' in self.partner._fields:
            self.partner.write({
                'woow_invoice_carrier_type': 'mobile',
                'woow_invoice_carrier': '/ABC1234',
            })
        else:
            self.skipTest("Partner invoice carrier fields not present in this environment")

        topup = self.env['woow.wallet.topup'].create_auto_topup(
            partner=self.partner, amount=50.0,
        )
        self.assertEqual(topup.invoice_carrier_type, 'mobile')
        self.assertEqual(topup.invoice_carrier, '/ABC1234')

    def test_create_auto_topup_zero_amount_raises(self):
        """Negative or zero shortfall must be rejected."""
        with self.assertRaises(ValidationError):
            self.env['woow.wallet.topup'].create_auto_topup(
                partner=self.partner, amount=0,
            )

    def test_create_auto_topup_negative_amount_raises(self):
        """Negative amount must be rejected (defensive)."""
        with self.assertRaises(ValidationError):
            self.env['woow.wallet.topup'].create_auto_topup(
                partner=self.partner, amount=-10,
            )

    def test_create_auto_topup_ceils_fractional_amount(self):
        """Shortfall rounds UP to a whole multiple of the minimum top-up (NT$100).

        Integer TWD keeps wallet/invoice/charge reconcilable; the NT$100 step
        (ECPay V4 go-live decision 2026-10-15) keeps auto top-ups from
        authorising the card for tiny amounts. The surplus stays in the wallet.
        """
        topup = self.env['woow.wallet.topup'].create_auto_topup(
            partner=self.partner, amount=300.40,
        )
        self.assertEqual(topup.amount, 400.0,
                         "amount must round up to the next NT$100 and match the ECPay charge")
        self.assertEqual(topup.credits, 400.0,
                         "credits must equal amount (1 credit = 1 TWD)")


class TestAutoTopupMinimumCharge(TransactionCase):
    """自動補值的最低扣款金額（ECPay 每筆最低手續費 NT$5）。"""

    def setUp(self):
        super().setUp()
        self.partner = self.env['res.partner'].create({
            'name': 'Min charge',
            'email': 'min@example.com',
        })
        self.Topup = self.env['woow.wallet.topup']
        self.ICP = self.env['ir.config_parameter'].sudo()

    def _set_minimum(self, value):
        self.ICP.set_param(self.Topup._AUTO_TOPUP_MIN_PARAM, value)

    def test_default_minimum_is_100(self):
        """未設定參數時，下限＝預設的 NT$100。"""
        self.ICP.search([('key', '=', self.Topup._AUTO_TOPUP_MIN_PARAM)]).unlink()
        self.assertEqual(self.Topup._auto_topup_min_amount(), 100.0)

    def test_shortfall_below_minimum_is_raised(self):
        """缺口低於下限 ⇒ 刷下限金額，credits 一起抬（差額留在錢包）。"""
        topup = self.Topup.create_auto_topup(partner=self.partner, amount=12.0)
        self.assertEqual(topup.amount, 100.0,
                         "小額缺口要抬到下限，否則最低手續費吃掉四成")
        self.assertEqual(topup.credits, 100.0,
                         "credits 必須等於實際扣款，三邊才對得起來")

    def test_shortfall_above_minimum_rounds_to_next_hundred(self):
        """缺口高於下限 ⇒ 進位到下一個 NT$100（ECPay V4 裁決 2，2026-10-15）。

        #1745 原本是「照缺口刷」；裁決 2 改為進位到 NT$100，減少同一張卡的
        授權次數，多收的留在錢包。已是 100 的倍數就不再多抬。
        """
        topup = self.Topup.create_auto_topup(partner=self.partner, amount=450.0)
        self.assertEqual(topup.amount, 500.0)
        self.assertEqual(topup.credits, 500.0)
        topup = self.Topup.create_auto_topup(partner=self.partner, amount=400.0)
        self.assertEqual(topup.amount, 400.0)

    def test_shortfall_equal_to_minimum_is_untouched(self):
        """邊界：等於下限時不應該被再抬一次。"""
        topup = self.Topup.create_auto_topup(partner=self.partner, amount=100.0)
        self.assertEqual(topup.amount, 100.0)

    def test_fractional_shortfall_below_minimum_uses_minimum(self):
        """先 ceil 再比下限，兩段不互相吃掉。"""
        topup = self.Topup.create_auto_topup(partner=self.partner, amount=12.4)
        self.assertEqual(topup.amount, 100.0)

    def test_minimum_is_configurable(self):
        self._set_minimum('500')
        topup = self.Topup.create_auto_topup(partner=self.partner, amount=120.0)
        self.assertEqual(topup.amount, 500.0)

    def test_minimum_zero_disables_the_floor(self):
        """設 0 ＝ 回到「缺多少刷多少」。"""
        self._set_minimum('0')
        topup = self.Topup.create_auto_topup(partner=self.partner, amount=12.0)
        self.assertEqual(topup.amount, 12.0)

    def test_non_numeric_minimum_falls_back_to_no_floor(self):
        """參數被填壞時往「不多刷」倒，不是用猜的數字去刷使用者的卡。"""
        self._set_minimum('一百')
        topup = self.Topup.create_auto_topup(partner=self.partner, amount=12.0)
        self.assertEqual(topup.amount, 12.0)

    def test_negative_minimum_is_treated_as_no_floor(self):
        self._set_minimum('-50')
        self.assertEqual(self.Topup._auto_topup_min_amount(), 0.0)

    def test_nt50_topup_plan_is_retired(self):
        """NT$50 方案已下架：前台只拿得到 NT$100 以上的方案。"""
        plan = self.env.ref('woow_paas_platform.topup_plan_50')
        self.assertFalse(plan.active, "NT$50 方案應已停用")
        active = self.env['woow_paas_platform.topup_plan'].search(
            [('active', '=', True)])
        self.assertTrue(active, "至少要留一個可用方案")
        self.assertFalse(
            active.filtered(lambda p: p.amount < 100),
            "不應該還有低於 NT$100 的上架方案",
        )


class TestChargeViaToken(TransactionCase):
    """Tests for wallet.topup.charge_via_token (Path B Phase 4.2)."""

    def setUp(self):
        super().setUp()
        self.partner = self.env['res.partner'].create({
            'name': 'Test',
            'email': 'test@example.com',
        })
        # wallet_balance is a non-stored compute on res.partner backed by
        # loyalty.card.points (sh_subscription_wallet). Writing it on create
        # is a no-op; credits must flow through add_wallet_points.
        self.partner.sudo().add_wallet_points(100.0, description='Test seed')
        self.partner.invalidate_recordset(['wallet_balance'])

    def _setup_ecpg_token(self, bindcard_id):
        """Provision an ECPG-ready provider + token for self.partner."""
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
        return provider, token

    def test_charge_via_token_no_token_returns_failure(self):
        """Calling charge_via_token without a valid token returns ('no_token', False)."""
        topup = self.env['woow.wallet.topup'].create_auto_topup(
            partner=self.partner, amount=50.0,
        )
        ok, msg = topup.charge_via_token(token=None)
        self.assertFalse(ok)
        self.assertEqual(msg, 'no_token')

    def test_charge_via_token_inactive_token_returns_failure(self):
        """Inactive token must be rejected."""
        provider, token = self._setup_ecpg_token('AB12345678')
        token.active = False
        topup = self.env['woow.wallet.topup'].create_auto_topup(
            partner=self.partner, amount=50.0,
        )
        ok, msg = topup.charge_via_token(token)
        self.assertFalse(ok)
        self.assertEqual(msg, 'no_token')

    def _patch_send_payment_request(self, side_effect=None, return_value=None):
        """Patch `_send_payment_request` on the live registry class.

        Odoo composes the model class at registry load via _inherit, so
        patching the upstream module path (odoo.addons.payment...) does
        nothing — the ECPG override at payment_ecpay_ecpg shadows it. The
        only reliable target is the actual class object in the registry.
        """
        TxModel = type(self.env['payment.transaction'])
        kwargs = {}
        if side_effect is not None:
            kwargs['side_effect'] = side_effect
        if return_value is not None:
            kwargs['return_value'] = return_value
        return patch.object(TxModel, '_send_payment_request', autospec=True, **kwargs)

    def test_charge_via_token_successful_charge_credits_wallet_and_creates_invoice(self):
        """End-to-end happy path: charge -> wallet credited -> invoice created with skip flag."""
        # #864 PR-C：儲值發票的開票狀態改由 res.company.auto_invoice 決定
        # （fail-closed resolver），不再寫死 'pending'。本測試斷言的是「自動開票
        # 政策下 cron 撿得到」，故顯式把政策設成 automatic。
        self.env.company.auto_invoice = 'automatic'
        provider, token = self._setup_ecpg_token('AB12345678')
        topup = self.env['woow.wallet.topup'].create_auto_topup(
            partner=self.partner, amount=150.0,
        )

        # Mock _send_payment_request to (a) flip tx.state to 'done' and
        # (b) return an empty dict (no 3DS redirect).
        def _set_done(tx_self):
            tx_self.state = 'done'
            return {}

        with self._patch_send_payment_request(side_effect=_set_done):
            ok, msg = topup.charge_via_token(token)

        self.assertTrue(ok, f"Expected success, got: {msg}")
        self.assertEqual(msg, 'ok')
        self.assertEqual(topup.state, 'done')
        # Wallet credited
        self.partner.invalidate_recordset(['wallet_balance'])
        # 100 seed + 200（短缺 150 進位到下一個 NT$100，ECPay V4 裁決 2）
        self.assertEqual(self.partner.wallet_balance, 300.0)
        # Invoice created with skip flag
        self.assertTrue(topup.invoice_account_move_id)
        self.assertTrue(topup.invoice_account_move_id.woow_skip_upstream_hook)
        # manual 政策下的對應行為由 test_invoice_issue_policy_864.py 蓋。
        self.assertEqual(
            topup.invoice_account_move_id.woow_invoice_issue_state, 'pending')

    def test_charge_via_token_3ds_required_fails_immediately(self):
        """3DS redirect on a cron charge: fail now, never park in 'pending'.

        ECPay V4 go-live: a cron has no browser to finish 3-D Secure, so the
        top-up is failed with ``3ds_required`` at once (no 60-minute pending
        limbo) and the card is blocked from automatic re-authorisation.
        """
        provider, token = self._setup_ecpg_token('AB12345678')
        topup = self.env['woow.wallet.topup'].create_auto_topup(
            partner=self.partner, amount=50.0,
        )
        with self._patch_send_payment_request(
            return_value={'redirect_url': 'https://3ds.example.com/...'},
        ):
            ok, msg = topup.charge_via_token(token)
        self.assertFalse(ok)
        self.assertEqual(msg, '3ds_required')
        self.assertEqual(topup.state, 'failed')
        self.assertEqual(topup.failure_code, '3ds_required')
        self.assertEqual(topup.payment_transaction_id.state, 'error')
        self.assertTrue(token._woow_auto_charge_blocked())

    def test_charge_via_token_tx_failed_marks_topup_failed(self):
        """When tx.state ends as 'error' or similar, topup goes to 'failed'."""
        provider, token = self._setup_ecpg_token('AB12345678')
        topup = self.env['woow.wallet.topup'].create_auto_topup(
            partner=self.partner, amount=50.0,
        )

        def _set_error(tx_self):
            tx_self.state = 'error'
            return {}

        with self._patch_send_payment_request(side_effect=_set_error):
            ok, msg = topup.charge_via_token(token)
        self.assertFalse(ok)
        self.assertEqual(msg, 'error')
        self.assertEqual(topup.state, 'failed')

    def test_charge_via_token_non_draft_state_returns_invalid_state(self):
        """Re-running charge_via_token on a non-draft topup must NOT create a 2nd transaction."""
        provider, token = self._setup_ecpg_token('AB12345678')
        topup = self.env['woow.wallet.topup'].create_auto_topup(
            partner=self.partner, amount=50.0,
        )
        topup.write({'state': 'pending'})  # simulate concurrent first caller
        ok, msg = topup.charge_via_token(token)
        self.assertFalse(ok)
        self.assertEqual(msg, 'invalid_state')

    def test_charge_via_token_links_payment_transaction_to_topup(self):
        """The created payment.transaction must have woow_topup_id set for 3DS resume."""
        provider, token = self._setup_ecpg_token('AB12345678')
        topup = self.env['woow.wallet.topup'].create_auto_topup(
            partner=self.partner, amount=50.0,
        )

        def _set_done(tx_self):
            tx_self.state = 'done'
            return {}

        with self._patch_send_payment_request(side_effect=_set_done):
            ok, msg = topup.charge_via_token(token)
        self.assertTrue(ok)
        self.assertTrue(topup.payment_transaction_id)
        self.assertEqual(topup.payment_transaction_id.woow_topup_id.id, topup.id)


class TestLandmineGuards(TransactionCase):
    """Tests for the plan_id-nullable landmines fixed alongside charge_via_token."""

    def setUp(self):
        super().setUp()
        self.partner = self.env['res.partner'].create({'name': 'Test'})

    def test_on_payment_done_handles_nullable_plan_id(self):
        """Wallet ledger description must NOT be 'Top-up: False' for auto-topups."""
        topup = self.env['woow.wallet.topup'].create_auto_topup(
            partner=self.partner, amount=50.0,
        )
        # Build a minimal payment.transaction for the call
        provider = self.env['payment.provider'].sudo().search(
            [('code', '=', 'ECPay')], limit=1,
        )
        if not provider:
            self.skipTest("ECPay provider not configured")
        payment_method = self.env.ref('payment.payment_method_card', raise_if_not_found=False) \
                         or self.env.ref('payment.payment_method_unknown')
        twd = self.env.ref('base.TWD')
        tx = self.env['payment.transaction'].sudo().create({
            'provider_id': provider.id,
            'payment_method_id': payment_method.id,
            'reference': 'TEST-LANDMINE-1',
            'amount': 50.0,
            'currency_id': twd.id,
            'partner_id': self.partner.id,
            'state': 'done',
        })
        topup._on_payment_done(tx)
        # Read the wallet transaction description back
        wtx = self.env['wallet.transaction'].search(
            [('partner_id', '=', self.partner.id)], order='id desc', limit=1,
        )
        self.assertTrue(wtx)
        self.assertNotIn('False', wtx.description,
                          f"Description must not contain 'False'; got: {wtx.description}")
        self.assertIn('Top-up', wtx.description)

    def test_create_topup_invoice_line_name_handles_nullable_plan(self):
        """Auto-topup invoice line name must NOT be 'Wallet Topup: False'.

        The plan landmine guards live in two places: _on_payment_done's wallet
        ledger description (covered by test_on_payment_done_handles_nullable_plan_id)
        AND _create_topup_invoice's line name. This test covers the latter.
        """
        topup = self.env['woow.wallet.topup'].create_auto_topup(
            partner=self.partner, amount=50.0,
        )
        move = topup._create_topup_invoice()
        self.assertTrue(move, "Invoice must be created for auto-topup")
        self.assertTrue(
            move.invoice_line_ids,
            "Invoice must have at least one line",
        )
        line_name = move.invoice_line_ids[0].name
        self.assertNotIn('False', line_name,
                         f"Line name must not contain 'False'; got: {line_name!r}")
        self.assertEqual(
            line_name, 'Wallet Topup',
            "Auto-topup with no plan should use the plain fallback line name",
        )
