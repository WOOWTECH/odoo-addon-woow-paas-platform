"""ECPay 站內付 2.0（V4 綁卡）上線修正的守線（2026-10-15）。

每一組對應 PR 描述的一項：電話正規化、儲值扣款的 OrderResultURL／Need3D、
自動儲值 3D 與拒絕後不重授權、儲值發票開票模式、最低儲值、綁卡 NT$5 授權取消、
BindCardID unique、create-bind 無 3D 落 token、api_topup 不再假成功、
儲值 3D 回跳 bouncer。

外呼邊界一律 mock 在 ``EcpgV4Client``（平台送出點實際 lookup 的 class）或
vendored ``ECPGClient`` 的方法上；Odoo 測試環境的 ``BlockedRequest`` 會讓漏
patch 的真外呼當場炸，不會靜默通過。
"""
import json
from datetime import timedelta
from unittest.mock import patch

import psycopg2
import requests

from odoo import fields
from odoo.exceptions import ValidationError
from odoo.tests.common import HttpCase, TransactionCase, tagged
from odoo.tools import mute_logger

from odoo.addons.payment_ecpay_ecpg.sdk.aes_crypto import encrypt
from odoo.addons.payment_ecpay_ecpg.sdk.ecpg_client import (
    CreditTradeLookupState,
    ECPGClient,
    ECPGConfigError,
    ECPGCreditCloseRecord,
    ECPGCreditTradeDetail,
    ECPGCreditTradeLookup,
    ECPGError,
)

from ..models.payment_token import ECPG_BINDCARD_UNIQUE_INDEX
from ..services.ecpg_v4 import EcpgV4Client, ecpay_consumer_name, ecpay_consumer_phone

STAGE_MERCHANT = {
    'MerchantID': '3002607',
    'HashKey': 'pwFHCqoQZGmho4w6',
    'HashIV': 'EkRm7iFT261dpevs',
}


class _EcpgFixture:
    """共用：stage 測試商店的 ECPay provider ＋ 綁定卡 token。"""

    def _provider(self):
        provider = self.env['payment.provider'].sudo().search(
            [('code', '=', 'ECPay')], limit=1)
        if not provider:
            self.skipTest('ECPay provider not configured in this DB')
        provider.write(dict(STAGE_MERCHANT, enable_ecpg=True, state='test'))
        return provider

    def _token(self, partner, bindcard_id, provider=None):
        provider = provider or self._provider()
        payment_method = self.env.ref(
            'payment.payment_method_card', raise_if_not_found=False,
        ) or self.env.ref('payment.payment_method_unknown')
        return self.env['payment.token'].sudo().create({
            'provider_id': provider.id,
            'partner_id': partner.id,
            'payment_method_id': payment_method.id,
            'payment_details': '**** **** **** ' + bindcard_id[-4:],
            'provider_ref': bindcard_id,
            'ecpg_bindcard_id': bindcard_id,
            'ecpg_member_id': str(partner.id),
        })


# ====================================================================== #
#  電話正規化（P1：單一 helper）                                          #
# ====================================================================== #

class TestEcpayConsumerPhone(TransactionCase):

    def test_taiwan_mobile_variants_normalise_to_09(self):
        for raw in ('+886912345678', '+886 912-345-678', '886912345678',
                    '+886 0912 345 678', '0912-345-678', '(0912)345678'):
            self.assertEqual(ecpay_consumer_phone(raw), '0912345678', raw)

    def test_everything_else_is_empty(self):
        for raw in (None, '', '   ', '02-2655-1775', '+1 555 555 5555',
                    '091234567', '09123456789', 'ext. 123', '+886 2 2655 1775'):
            self.assertEqual(ecpay_consumer_phone(raw), '', raw)


class TestEcpayConsumerName(TransactionCase):
    """ECPay 對 Name 有字元白名單（stage 實測），不合規整筆拒絕 ⇒ 送空。"""

    def test_accepted_names_pass_through(self):
        for raw in ('王小明', 'John Doe', 'ABC Co., Ltd.', "O'Brien",
                    '王小明(測試)', 'Test-User', 'A/B', '陳㐀', '  Woow   Tech  '):
            self.assertEqual(ecpay_consumer_name(raw), ' '.join(raw.split()), raw)

    def test_rejected_characters_send_empty(self):
        for raw in (None, '', 'Woow E2E', '3M', 'A&B', 'a@b', 'x_y', 'José',
                    'name😀', 'A+B', 'Ｗｏｏｗ', '王小明（測試）', 'A#B', '張𠀋'):
            self.assertEqual(ecpay_consumer_name(raw), '', raw)

    def test_long_names_are_cut_to_50(self):
        self.assertEqual(len(ecpay_consumer_name('A' * 80)), 50)


class TestEcpgV4ParamsRegisteredForUninstall(TransactionCase):
    """「模組常數＋get_param()」寫法 lint 掃不到 ⇒ 這條補上卸載清理守線。"""

    def test_params_in_uninstall_list(self):
        from ..hooks import _CONFIG_PARAMETERS
        for key in ('woow_paas_platform.topup_invoice_issue_mode',
                    'woow_paas_platform.auto_topup_min_amount',
                    'woow_paas_platform.migration_18_0_1_0_240_topup_plan_50_retired',
                    'woow_paas_platform.ecpg_topup_order_result_url_enabled'):
            self.assertIn(key, _CONFIG_PARAMETERS, key)


# ====================================================================== #
#  儲值扣款送出點（P0：OrderResultURL；P1：電話）                          #
# ====================================================================== #

class TestTopupChargeSendV4(_EcpgFixture, TransactionCase):

    def setUp(self):
        super().setUp()
        self.partner = self.env['res.partner'].create({
            'name': 'Woow Charge', 'email': 'v4charge@example.com',
            'phone': '+886 912-345-678',
        })
        self.provider = self._provider()
        self.token = self._token(self.partner, 'V4BIND0001', self.provider)
        self.base = self.env['ir.config_parameter'].sudo().get_param(
            'web.base.url').rstrip('/')

    def _manual_topup(self):
        return self.env['woow.wallet.topup'].sudo().create({
            'partner_id': self.partner.id,
            'amount': 500.0, 'credits': 500.0, 'bonus': 0.0,
            'currency_id': self.env.ref('base.TWD').id,
            'source': 'manual', 'state': 'draft',
        })

    def _charge(self, topup, response):
        captured = {}

        def fake_post(client, path, payload):
            captured['path'] = path
            captured['payload'] = payload
            if isinstance(response, Exception):
                raise response
            return response

        with patch.object(EcpgV4Client, '_post', autospec=True,
                          side_effect=fake_post) as mocked:
            result = self.token._charge(
                amount=topup.amount, reference='TOPUPV4TEST%04d' % topup.id,
                currency_id=topup.currency_id.id,
                purpose_field='woow_topup_id', purpose_id=topup.id,
            )
        return result, captured, mocked

    def test_manual_topup_sends_top_level_order_result_url_without_need3d(self):
        topup = self._manual_topup()
        result, sent, _mocked = self._charge(topup, {
            'RtnCode': 1, 'OrderInfo': {'TradeNo': 'T1'},
            'ThreeDInfo': {'ThreeDURL': 'https://3ds.example/x'},
        })
        payload = sent['payload']
        self.assertEqual(sent['path'], '/Merchant/CreatePaymentWithCardID')
        self.assertEqual(
            payload['OrderResultURL'],
            '%s/api/billing/topup-result/%s' % (self.base, topup.id))
        self.assertNotIn('OrderResultURL', payload['OrderInfo'])
        self.assertNotIn('Need3D', payload)
        self.assertEqual(payload['ConsumerInfo']['Phone'], '0912345678')
        self.assertEqual(payload['ConsumerInfo']['Name'], 'Woow Charge')
        self.assertEqual(payload['OrderInfo']['TotalAmount'], 500)
        self.assertEqual(payload['BindCardID'], 'V4BIND0001')
        # ThreeDURL ⇒ pending + redirect（最終狀態交給 pay_callback）
        self.assertEqual(result['redirect_url'], 'https://3ds.example/x')
        self.assertEqual(result['tx'].state, 'pending')
        self.assertEqual(result['tx'].provider_reference, 'T1')

    def test_order_result_url_kill_switch(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.ecpg_topup_order_result_url_enabled', 'False')
        _result, sent, _mocked = self._charge(self._manual_topup(), {
            'RtnCode': 1, 'OrderInfo': {'TradeNo': 'T2'}})
        self.assertNotIn('OrderResultURL', sent['payload'])

    def test_auto_topup_never_sends_order_result_url(self):
        topup = self.env['woow.wallet.topup'].create_auto_topup(
            partner=self.partner, amount=50.0)
        with patch.object(EcpgV4Client, '_post', autospec=True,
                          return_value={'RtnCode': 1,
                                        'OrderInfo': {'TradeNo': 'T3'}}) as mocked:
            ok, msg = topup.charge_via_token(self.token)
        self.assertTrue(ok, msg)
        payload = mocked.call_args.args[2]
        self.assertNotIn('OrderResultURL', payload)
        self.assertEqual(payload['OrderInfo']['TotalAmount'], 100)

    def test_no_3ds_response_settles_and_credits(self):
        before = self.partner.wallet_balance
        topup = self._manual_topup()
        result, _sent, _mocked = self._charge(topup, {
            'RtnCode': 1, 'OrderInfo': {'TradeNo': 'T4'}})
        self.assertFalse(result['redirect_url'])
        self.assertEqual(result['tx'].state, 'done')
        self.assertEqual(topup.state, 'done')
        self.partner.invalidate_recordset(['wallet_balance'])
        self.assertEqual(self.partner.wallet_balance, before + 500.0)

    def test_ecpay_decline_fails_topup_and_blocks_card(self):
        topup = self._manual_topup()
        with mute_logger('odoo.addons.woow_paas_platform.models.payment_transaction'):
            result, _sent, _mocked = self._charge(
                topup, ECPGError('RtnCode=10100248 msg=declined'))
        self.assertEqual(result['tx'].state, 'error')
        self.assertEqual(topup.state, 'failed')
        self.assertEqual(topup.failure_code, 'card_declined')
        self.assertTrue(self.token._woow_auto_charge_blocked())

    def test_transport_error_does_not_block_card(self):
        topup = self._manual_topup()
        with mute_logger('odoo.addons.woow_paas_platform.models.payment_transaction'):
            self._charge(topup, ECPGConfigError('ECPG HTTP 403 (config/auth)',
                                                http_status=403))
        self.assertEqual(topup.state, 'failed')
        self.assertFalse(self.token._woow_auto_charge_blocked())

    def test_network_error_sets_error_not_draft(self):
        topup = self._manual_topup()
        result, _sent, _mocked = self._charge(
            topup, requests.exceptions.ConnectionError('boom'))
        self.assertEqual(result['tx'].state, 'error')
        self.assertEqual(topup.state, 'failed')

    def test_non_topup_ecpg_tx_still_uses_vendored_path(self):
        """平台只接手儲值；其他 ECPG token 交易仍走 vendored 實作。"""
        tx = self.env['payment.transaction'].sudo().create({
            'provider_id': self.provider.id,
            'payment_method_id': self.token.payment_method_id.id,
            'partner_id': self.partner.id,
            'amount': 300, 'currency_id': self.env.ref('base.TWD').id,
            'reference': 'NONTOPUPV4', 'token_id': self.token.id,
            'operation': 'online_token',
        })
        with patch.object(ECPGClient, 'create_payment_with_card_id',
                          autospec=True,
                          return_value={'RtnCode': 1, 'OrderInfo': {}}) as vendored, \
                patch.object(EcpgV4Client, '_post', autospec=True) as ours:
            tx._send_payment_request()
        vendored.assert_called_once()
        ours.assert_not_called()


# ====================================================================== #
#  自動儲值：3D 立即失敗、拒絕後本週期不再授權同一張卡（P1）                 #
# ====================================================================== #

class TestAutoTopupDeclineBlock(_EcpgFixture, TransactionCase):

    def setUp(self):
        super().setUp()
        self.partner = self.env['res.partner'].create({
            'name': 'V4 Auto', 'email': 'v4auto@example.com'})
        self.token = self._token(self.partner, 'V4AUTO0001')
        if 'property_payment_token_id' in self.partner._fields:
            self.partner.with_company(self.env.company).sudo() \
                .property_payment_token_id = self.token
        self.Sub = self.env['sh.subscription.subscription']

    def test_blocked_card_is_not_reauthorised(self):
        self.token._woow_block_auto_charge('card_declined')
        Topup = self.env['woow.wallet.topup'].sudo()
        before = Topup.search_count([('partner_id', '=', self.partner.id)])
        with patch.object(EcpgV4Client, '_post', autospec=True) as ours:
            ok, msg = self.Sub._attempt_auto_topup(self.partner, 120.0)
        self.assertFalse(ok)
        self.assertEqual(msg, 'card_blocked_after_decline')
        ours.assert_not_called()
        self.assertEqual(
            Topup.search_count([('partner_id', '=', self.partner.id)]), before,
            'a blocked card must not even get a draft top-up')

    def test_cron_3ds_fails_and_next_cycle_attempt_is_blocked(self):
        with patch.object(EcpgV4Client, '_post', autospec=True, return_value={
                'RtnCode': 1, 'ThreeDInfo': {'ThreeDURL': 'https://3ds/x'},
                'OrderInfo': {'MerchantTradeNo': 'X'}}) as ours:
            ok, msg = self.Sub._attempt_auto_topup(self.partner, 120.0)
            self.assertEqual((ok, msg), (False, '3ds_required'))
            ok2, msg2 = self.Sub._attempt_auto_topup(self.partner, 120.0)
        self.assertEqual((ok2, msg2), (False, 'card_blocked_after_decline'))
        self.assertEqual(ours.call_count, 1, 'exactly one authorisation attempt')
        topup = self.env['woow.wallet.topup'].sudo().search(
            [('partner_id', '=', self.partner.id)], limit=1)
        self.assertEqual(topup.state, 'failed')
        self.assertEqual(topup.amount, 200.0, '120 rounds up to NT$200')

    def test_block_expires_after_window(self):
        self.token._woow_block_auto_charge('card_declined')
        self.token.sudo().write({
            'woow_auto_charge_blocked_at':
                fields.Datetime.now() - timedelta(days=31)})
        self.assertFalse(self.token._woow_auto_charge_blocked())

    def test_successful_charge_clears_block(self):
        self.token._woow_block_auto_charge('card_declined')
        topup = self.env['woow.wallet.topup'].create_auto_topup(
            partner=self.partner, amount=10.0)
        with patch.object(EcpgV4Client, '_post', autospec=True,
                          return_value={'RtnCode': 1, 'OrderInfo': {}}):
            ok, _msg = topup.charge_via_token(self.token)
        self.assertTrue(ok)
        self.assertFalse(self.token.woow_auto_charge_blocked_at)


# ====================================================================== #
#  儲值發票開票模式（裁決 1）                                              #
# ====================================================================== #

class TestTopupInvoiceIssueMode(TransactionCase):

    def setUp(self):
        super().setUp()
        self.ICP = self.env['ir.config_parameter'].sudo()
        self.partner = self.env['res.partner'].create({
            'name': 'V4 Invoice', 'email': 'v4inv@example.com'})
        self.company = self.env.company
        self.company.auto_invoice = 'manual'   # 正式站現況

    def _topup_invoice(self):
        topup = self.env['woow.wallet.topup'].sudo().create({
            'partner_id': self.partner.id,
            'amount': 100.0, 'credits': 100.0, 'bonus': 0.0,
            # 公司幣別：測試庫的 TWD 是 inactive，會讓 action_post 失敗而停在
            # draft（cron 只撿 posted）；開票模式與幣別無關。
            'currency_id': self.company.currency_id.id,
            'source': 'manual', 'state': 'pending',
        })
        return topup._ensure_topup_invoice()

    def test_default_automatic_even_when_company_is_manual(self):
        self.ICP.set_param('woow_paas_platform.topup_invoice_issue_mode', False)
        invoice = self._topup_invoice()
        self.assertEqual(invoice.woow_invoice_issue_state, 'pending')
        self.assertTrue(invoice.woow_skip_upstream_hook)
        self.assertEqual(self.company.auto_invoice, 'manual',
                         'the company-level default must not be touched')

    def test_manual_and_company_modes(self):
        self.ICP.set_param('woow_paas_platform.topup_invoice_issue_mode', 'manual')
        self.assertEqual(self._topup_invoice().woow_invoice_issue_state, 'manual')
        self.ICP.set_param('woow_paas_platform.topup_invoice_issue_mode', 'company')
        self.assertEqual(self._topup_invoice().woow_invoice_issue_state, 'manual')
        self.company.auto_invoice = 'automatic'
        self.assertEqual(self._topup_invoice().woow_invoice_issue_state, 'pending')

    def test_unknown_mode_falls_back_to_company(self):
        self.ICP.set_param('woow_paas_platform.topup_invoice_issue_mode', 'yes-please')
        self.assertEqual(self._topup_invoice().woow_invoice_issue_state, 'manual')

    def test_other_invoices_still_follow_company(self):
        """非儲值發票的 resolver 不受儲值參數影響。"""
        self.ICP.set_param('woow_paas_platform.topup_invoice_issue_mode', 'automatic')
        self.assertEqual(
            self.env['account.move']._woow_resolve_invoice_issue_state(
                self.company), 'manual')

    def test_cron_issues_the_automatic_topup_invoice(self):
        invoice = self._topup_invoice()
        self.assertEqual(invoice.state, 'posted')
        Cron = self.env['woow_paas_platform.ecpay_invoice_cron']
        with patch.object(type(Cron), '_issue_one', autospec=True) as issue:
            Cron._cron_issue_pending_invoices()
        issued = [call.args[1] for call in issue.call_args_list]
        self.assertIn(invoice, issued)


# ====================================================================== #
#  最低儲值（裁決 2）                                                     #
# ====================================================================== #

class TestTopupMinimum(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Topup = self.env['woow.wallet.topup']
        self.Plan = self.env['woow_paas_platform.topup_plan']
        self.ICP = self.env['ir.config_parameter'].sudo()
        self.partner = self.env['res.partner'].create({'name': 'V4 Min'})

    def test_auto_amount_rounds_up_to_next_hundred(self):
        for shortfall, expected in ((0.4, 100), (30, 100), (100, 100),
                                    (100.4, 200), (130, 200), (999, 1000)):
            self.assertEqual(self.Topup._auto_topup_charge_amount(shortfall),
                             expected, shortfall)

    def test_minimum_param_and_off_switch(self):
        """下限沿用 #1745 的參數；設 0／填壞＝關閉（連 NT$100 進位一起關）。"""
        key = 'woow_paas_platform.auto_topup_min_amount'
        self.ICP.set_param(key, '200')
        self.assertEqual(self.Topup._auto_topup_charge_amount(30), 200)
        self.assertEqual(self.Topup._auto_topup_charge_amount(250), 300)
        for off in ('0', '-5', 'abc'):
            self.ICP.set_param(key, off)
            self.assertEqual(self.Topup._auto_topup_charge_amount(30.2), 31, off)

    def test_auto_topup_keeps_surplus_as_credits(self):
        topup = self.Topup.create_auto_topup(partner=self.partner, amount=30.0)
        self.assertEqual((topup.amount, topup.credits), (100.0, 100.0))

    def test_active_plan_must_be_whole_and_at_least_minimum(self):
        with self.assertRaises(ValidationError):
            self.Plan.create({'name': 'x50', 'amount': 50, 'credits': 50})
        with self.assertRaises(ValidationError):
            self.Plan.create({'name': 'x100.5', 'amount': 100.5, 'credits': 100})
        ok = self.Plan.create({'name': 'x100', 'amount': 100, 'credits': 100})
        self.assertTrue(ok.active)
        legacy = self.Plan.create({'name': 'legacy50', 'amount': 50,
                                   'credits': 50, 'active': False})
        with self.assertRaises(ValidationError):
            legacy.active = True

    def test_seed_50_plan_cannot_be_relisted_below_minimum(self):
        """NT$50 seed 已下架（#1745 migration 240）；在下限 100 之下不能再上架。"""
        seed = self.env.ref('woow_paas_platform.topup_plan_50')
        self.assertFalse(seed.active)
        with self.assertRaises(ValidationError):
            seed.active = True
        self.ICP.set_param('woow_paas_platform.auto_topup_min_amount', '0')
        seed.active = True   # 下限關閉時只檢查整數
        self.assertTrue(seed.active)


# ====================================================================== #
#  綁卡 NT$5 授權取消帳本（P0）                                           #
# ====================================================================== #

def _bind_data(mtn='BIND0123456789ABCDEF', trade_no='2510150000000001',
               amount=5, rtn=1, member_id=None, bind_card_id='BCV4VOID01'):
    data = {
        'RtnCode': rtn, 'RtnMsg': 'Success', 'MerchantID': '3002607',
        'BindCardID': bind_card_id, 'IsSameCard': False,
        'OrderInfo': {'MerchantTradeNo': mtn, 'TradeNo': trade_no,
                      'TradeAmt': amount},
        'CardInfo': {'Card4No': '2222', 'CardValidYY': '30',
                     'CardValidMM': '12', 'Amount': amount},
    }
    if member_id:
        data['MerchantMemberID'] = member_id
    return data


def _lookup(status, amount=5, close=()):
    return ECPGCreditTradeLookup(
        merchant_trade_no='BIND0123456789ABCDEF',
        state=CreditTradeLookupState.FOUND,
        detail=ECPGCreditTradeDetail(
            trade_id=1, amount=amount, cls_amt=0, auth_time='2026/10/15 10:00:00',
            status=status, close_data=tuple(close)))


class TestBindAuthVoid(_EcpgFixture, TransactionCase):

    def setUp(self):
        super().setUp()
        self.provider = self._provider()
        self.Void = self.env['woow_paas_platform.ecpg_bind_auth_void'].sudo()

    def _row(self, **kw):
        return self.Void._enqueue_from_bind_data(
            self.provider, _bind_data(**kw), 'return_url')

    def test_enqueue_is_idempotent_per_trade(self):
        first = self._row()
        second = self.Void._enqueue_from_bind_data(
            self.provider, _bind_data(), 'order_result')
        self.assertTrue(first)
        self.assertEqual(first, second)
        self.assertEqual(self.Void.search_count(
            [('merchant_trade_no', '=', 'BIND0123456789ABCDEF')]), 1)
        self.assertEqual(first.state, 'pending')
        self.assertEqual(first.amount, 5)

    def test_failed_bind_or_missing_trade_is_not_enqueued(self):
        self.assertFalse(self._row(rtn=10100248))
        self.assertFalse(self._row(trade_no=''))
        # BindCardID 空（卡在通知前被刪）⇒ 授權仍要取消
        self.assertTrue(self._row(mtn='BIND00000000000000B2', bind_card_id=''))

    def _process(self, row, do_action, query=None):
        with patch.object(EcpgV4Client, 'do_action', autospec=True,
                          side_effect=do_action) as act, \
                patch.object(EcpgV4Client, 'query_credit_trade', autospec=True,
                             side_effect=query or AssertionError('no query')) as q:
            outcome = row._process_one()
        return outcome, [c.args[3] for c in act.call_args_list], q

    def test_n_success_voids(self):
        row = self._row()
        outcome, actions, _q = self._process(row, lambda *a: {'RtnCode': 1})
        self.assertEqual(outcome, 'done')
        self.assertEqual(actions, ['N'])
        self.assertEqual(row.state, 'done')
        self.assertIn('N:1', row.action_trail)

    def test_to_be_captured_sends_e_then_n(self):
        row = self._row()
        replies = iter([{'RtnCode': 10200050}, {'RtnCode': 1}, {'RtnCode': 1}])
        outcome, actions, _q = self._process(
            row, lambda *a: next(replies),
            query=lambda *a: _lookup('To be captured'))
        self.assertEqual(outcome, 'done')
        self.assertEqual(actions, ['N', 'E', 'N'])

    def test_captured_is_refunded(self):
        row = self._row()
        replies = iter([{'RtnCode': 0}, {'RtnCode': 1}])
        outcome, actions, _q = self._process(
            row, lambda *a: next(replies),
            query=lambda *a: _lookup('Captured'))
        self.assertEqual((outcome, actions), ('done', ['N', 'R']))

    def test_already_released_needs_nothing(self):
        # 'Cancelled'（雙 L）是 stage 實測 N 之後 CreditDetail 回的拼法。
        for i, status in enumerate(('Canceled', 'Cancelled', 'Unauthorized')):
            row = self._row(mtn='BIND0000000000000RL%d' % i)
            outcome, actions, _q = self._process(
                row, lambda *a: {'RtnCode': 0},
                query=lambda *a, s=status: _lookup(s))
            self.assertEqual((outcome, actions), ('not_needed', ['N']), status)
            self.assertEqual(row.state, 'not_needed')

    def test_stage_doaction_unavailable_is_retried_not_raised(self):
        """stage 沒有 DoAction：要「有嘗試、失敗被吸收、排下一輪」。"""
        row = self._row()

        def unavailable(*a):
            raise ECPGConfigError('ECPG HTTP 404 (config/auth)', http_status=404)
        outcome, actions, _q = self._process(
            row, unavailable, query=lambda *a: _lookup('Authorized'))
        self.assertEqual(outcome, 'retry')
        self.assertEqual(actions, ['N'])
        self.assertEqual(row.state, 'pending')
        self.assertEqual(row.attempt_count, 1)
        self.assertGreater(row.next_attempt_at, fields.Datetime.now())
        self.assertIn('N:exc', row.action_trail)

    def test_gives_up_after_max_attempts(self):
        row = self._row()
        row.write({'attempt_count': 14})
        outcome, _a, _q = self._process(
            row, lambda *a: {'RtnCode': 0},
            query=lambda *a: _lookup('Authorized'))
        self.assertEqual(outcome, 'failed')
        self.assertEqual(row.state, 'failed')

    def test_guard_refuses_non_bind_or_large_amount(self):
        for kw in ({'mtn': 'TOPUP0123456789ABCDE'}, {'amount': 500}):
            row = self._row(**kw)
            with patch.object(EcpgV4Client, 'do_action', autospec=True) as act:
                outcome = row._process_one()
            self.assertEqual(outcome, 'failed', kw)
            act.assert_not_called()

    def test_cron_only_processes_due_rows(self):
        due = self._row()
        later = self._row(mtn='BIND00000000000000C3', trade_no='T9')
        later.write({'next_attempt_at': fields.Datetime.now() + timedelta(hours=1)})
        with patch.object(EcpgV4Client, 'do_action', autospec=True,
                          return_value={'RtnCode': 1}):
            self.Void._cron_process_due()
        self.assertEqual(due.state, 'done')
        self.assertEqual(later.state, 'pending')

    def test_topup_cleanup_cron_runs_the_void_segment(self):
        with patch.object(type(self.Void), '_cron_process_due',
                          autospec=True, return_value={}) as seg:
            self.env['woow.wallet.topup']._cron_cleanup_pending_topups()
        seg.assert_called_once()


# ====================================================================== #
#  BindCardID unique（P1）                                               #
# ====================================================================== #

class TestBindCardUniqueIndex(_EcpgFixture, TransactionCase):

    def test_index_exists_and_rejects_active_duplicates(self):
        self.env.cr.execute('SELECT 1 FROM pg_indexes WHERE indexname = %s',
                            (ECPG_BINDCARD_UNIQUE_INDEX,))
        self.assertTrue(self.env.cr.fetchone())
        partner = self.env['res.partner'].create({'name': 'V4 Uniq'})
        first = self._token(partner, 'V4UNIQ0001')
        with self.assertRaises(psycopg2.errors.UniqueViolation), \
                mute_logger('odoo.sql_db'):
            with self.env.cr.savepoint():
                self._token(partner, 'V4UNIQ0001')
        with patch.object(ECPGClient, 'delete_member_bind_card', autospec=True,
                          return_value={'RtnCode': 1}):
            first.active = False
            first.flush_recordset(['active'])
        # 封存後同一張卡重新綁定（ECPay 回同一個 BindCardID）必須可行。
        again = self._token(partner, 'V4UNIQ0001')
        self.assertTrue(again.active)


# ====================================================================== #
#  HTTP：綁卡回呼（unique＝已存在、登記 void）、create-bind 無 3D、           #
#  api_topup 不再假成功、儲值 3D 回跳 bouncer                              #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestEcpgV4Http(_EcpgFixture, HttpCase):

    def setUp(self):
        super().setUp()
        self.provider = self._provider()
        self.provider.ecpg_member_id_prefix = ''
        self.user = self.env['res.users'].create({
            'name': 'V4 Http', 'login': 'v4_http', 'password': 'v4_http_pw',
            'email': 'v4http@example.com',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.partner = self.user.partner_id
        self.Void = self.env['woow_paas_platform.ecpg_bind_auth_void'].sudo()

    def _envelope(self, data):
        return {
            'MerchantID': STAGE_MERCHANT['MerchantID'],
            'TransCode': 1,
            'Data': encrypt(data, STAGE_MERCHANT['HashKey'], STAGE_MERCHANT['HashIV']),
        }

    def _json_rpc(self, path, params):
        response = self.url_open(
            path, data=json.dumps({'params': params}),
            headers={'Content-Type': 'application/json'}, timeout=30)
        return response.json()['result']

    # --- 綁卡回呼 -------------------------------------------------------
    def test_s2s_bind_callback_persists_token_and_enqueues_void_once(self):
        data = _bind_data(member_id=str(self.partner.id), bind_card_id='BCV4S2S01')
        for _ in range(2):  # ECPay 重送／S2S＋瀏覽器重複
            response = requests.post(
                '%s/payment/ecpg/bind_callback' % self.base_url(),
                data=json.dumps(self._envelope(data)),
                headers={'Content-Type': 'application/json'}, timeout=30)
            self.assertEqual(response.text, '1|OK')
        tokens = self.env['payment.token'].sudo().search(
            [('ecpg_bindcard_id', '=', 'BCV4S2S01')])
        self.assertEqual(len(tokens), 1)
        self.assertEqual(tokens.partner_id, self.partner)
        rows = self.Void.search([('merchant_trade_no', '=', 'BIND0123456789ABCDEF')])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows.source, 'return_url')

    def test_unique_violation_from_concurrent_delivery_is_already_exists(self):
        """另一條回呼路徑已先建 token（index 撞號）⇒ 回 ALREADY_EXISTS，不是失敗。"""
        from odoo.addons.payment_ecpay_ecpg.controllers.ecpg_callback import (
            ECPGCallback,
        )
        payment_method = self.env.ref(
            'payment.payment_method_card', raise_if_not_found=False,
        ) or self.env.ref('payment.payment_method_unknown')
        vals = {
            'provider_id': self.provider.id,
            'partner_id': self.partner.id,
            'payment_method_id': payment_method.id,
            'payment_details': '**** **** **** RACE',
            'provider_ref': 'BCV4RACE01',
            'ecpg_bindcard_id': 'BCV4RACE01',
            'ecpg_member_id': str(self.partner.id),
        }

        def racing_insert(_self, _params):
            # 在 request 自己的 env 上重現「另一條路徑先寫了」：第二筆撞 index。
            from odoo.http import request
            Token = request.env['payment.token'].sudo()
            Token.create(dict(vals))
            Token.create(dict(vals))
            return True, 'OK'

        with patch.object(ECPGCallback, '_handle_bind_callback',
                          autospec=True, side_effect=racing_insert):
            response = requests.post(
                '%s/api/billing/payment-methods/bind-result/BINDRACE' % self.base_url(),
                data={'ResultData': json.dumps(self._envelope(
                    _bind_data(mtn='BIND00000000000RACE1',
                               member_id=str(self.partner.id))))},
                allow_redirects=False, timeout=30)
        self.assertIn('bind_status=ok', response.text)
        rows = self.Void.search([('merchant_trade_no', '=', 'BIND00000000000RACE1')])
        self.assertEqual(rows.source, 'order_result')

    def test_create_bind_without_3ds_persists_token_and_void(self):
        self.authenticate('v4_http', 'v4_http_pw')
        member_id = self.provider._ecpg_build_member_id(self.partner)
        sync = _bind_data(mtn='BIND00000000000SYNC1', member_id=member_id,
                          bind_card_id='BCV4SYNC01')
        with patch.object(ECPGClient, 'create_bind_card', autospec=True,
                          return_value=sync):
            body = self._json_rpc('/api/billing/payment-methods/create-bind', {
                'member_id': member_id, 'trade_no': 'BIND00000000000SYNC1',
                'bind_card_pay_token': 'PAYTOKEN'})
        self.assertTrue(body['success'], body)
        self.assertEqual(body['data']['three_d_url'], '')
        self.assertEqual(body['data']['bind_status'], 'ok')
        token = self.env['payment.token'].sudo().search(
            [('ecpg_bindcard_id', '=', 'BCV4SYNC01')])
        self.assertEqual(token.partner_id, self.partner)
        self.assertEqual(token.payment_details, '**** **** **** 2222')
        row = self.Void.search([('merchant_trade_no', '=', 'BIND00000000000SYNC1')])
        self.assertEqual(row.source, 'create_bind')

    # --- api_topup 結果語意 ---------------------------------------------
    def _topup_setup(self):
        self.token = self._token(self.partner, 'V4HTTPTOPUP1')
        self.plan = self.env['woow_paas_platform.topup_plan'].create({
            'name': 'V4 Plan', 'amount': 500, 'credits': 500})
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.topup_payment_enabled', 'True')
        self.authenticate('v4_http', 'v4_http_pw')

    def test_sync_decline_is_not_success(self):
        self._topup_setup()
        with patch.object(EcpgV4Client, '_post', autospec=True,
                          side_effect=ECPGError('RtnCode=10100248 msg=declined')), \
                mute_logger('odoo.addons.woow_paas_platform.models.payment_transaction'):
            body = self._json_rpc('/api/billing/wallet/topup', {'plan_id': self.plan.id})
        self.assertFalse(body['success'], body)
        self.assertEqual(body['failure_code'], 'card_declined')
        self.assertEqual(body['code'], 'card_declined')
        self.assertEqual(body['data']['state'], 'failed')

    def test_sync_done_is_success(self):
        self._topup_setup()
        with patch.object(EcpgV4Client, '_post', autospec=True,
                          return_value={'RtnCode': 1, 'OrderInfo': {'TradeNo': 'X'}}):
            body = self._json_rpc('/api/billing/wallet/topup', {'plan_id': self.plan.id})
        self.assertTrue(body['success'], body)
        self.assertEqual(body['data']['state'], 'done')

    def test_3ds_returns_redirect_and_pending(self):
        self._topup_setup()
        with patch.object(EcpgV4Client, '_post', autospec=True, return_value={
                'RtnCode': 1, 'ThreeDInfo': {'ThreeDURL': 'https://3ds.example/y'}}):
            body = self._json_rpc('/api/billing/wallet/topup', {'plan_id': self.plan.id})
        self.assertTrue(body['success'], body)
        self.assertEqual(body['data']['state'], 'pending')
        self.assertEqual(body['data']['redirect_url'], 'https://3ds.example/y')

    def test_manual_topup_falls_back_to_saved_invoice_preference(self):
        self._topup_setup()
        self.partner.sudo().write({'woow_invoice_carrier_type': 'mobile',
                                   'woow_invoice_carrier': '/ABC1234'})
        with patch.object(EcpgV4Client, '_post', autospec=True, return_value={
                'RtnCode': 1, 'ThreeDInfo': {'ThreeDURL': 'https://3ds.example/z'}}):
            body = self._json_rpc('/api/billing/wallet/topup', {'plan_id': self.plan.id})
        topup = self.env['woow.wallet.topup'].browse(body['data']['topup_id'])
        self.assertEqual(topup.invoice_carrier_type, 'mobile')
        self.assertEqual(topup.invoice_carrier, '/ABC1234')

    # --- 儲值 3D 回跳 bouncer ------------------------------------------
    def test_topup_result_bouncer_redirects_without_state_change_or_leak(self):
        other = self.env['res.partner'].create({'name': 'Someone Else'})
        topup = self.env['woow.wallet.topup'].sudo().create({
            'partner_id': other.id, 'amount': 98765.0, 'credits': 98765.0,
            'currency_id': self.env.ref('base.TWD').id,
            'source': 'manual', 'state': 'pending',
        })
        response = requests.post(
            '%s/api/billing/topup-result/%s' % (self.base_url(), topup.id),
            data={'ResultData': '{"MerchantID":"3002607","Data":"x"}'},
            allow_redirects=False, timeout=30)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('session_id', response.cookies)
        self.assertIn('/woow#/billing/topup?topup_id=%s' % topup.id, response.text)
        self.assertNotIn('98765', response.text)
        self.assertNotIn('pending', response.text)
        topup.invalidate_recordset(['state'])
        self.assertEqual(topup.state, 'pending')
