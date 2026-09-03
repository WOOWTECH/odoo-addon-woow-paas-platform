"""#864 PR-G — `pay_callback` route override：只收 facts，⛔ 對外回應零漂移。

工單／裁決界線逐字：override **只**把 provider 事實寫進 incident inbox，
所有對外 ``status`` ＋ body 必須與 vendored base 版 **byte-identical**；
「查無 tx 改回可重送」是 PR-H 的事，G ⛔ 不碰。

🔴 **為什麼要有一條直接量回應的測試**（裁決明訂）：「我沒改那段」是推論不是
證據。本檔把 vendored parent 的 ``pay_callback`` 原始碼裡**每一個**
``http.Response(...)`` 字面值抽出來當第二把尺，再用真的 HTTP 請求量現場回應，
兩者對不上就紅——vendored addon 之後升版改了回應，也會在這裡出聲。

⚠ 本檔刻意**不帶** ``-standard`` ⇒ 會被 CI 選中（PR-G 的 CI 盲區補償）。
真跨連線語意（callback 早於主交易 commit）在
``tests/integration/test_callback_replay_864.py``。
"""

import ast
import inspect
import json as json_lib
import uuid

from odoo.addons.payment_ecpay_ecpg.controllers.ecpg_callback import (
    ECPGCallback,
)
from odoo.addons.payment_ecpay_ecpg.sdk.aes_crypto import encrypt
from odoo.tests.common import HttpCase, tagged

_INCIDENT_TABLE = 'woow_billing_incident'
_CALLBACK_URL = '/payment/ecpg/pay_callback'

# Luhn-valid test PAN (Visa test number).  Never a real card.
_LUHN_PAN = '4111111111111111'

# vendored `18.0.0.3.5` 的完整回應值域。**⛔ 不是**期望值的唯一來源：下面
# `test_vendored_response_vocabulary_is_unchanged` 會從 parent 原始碼重新抽
# 一次做集合等值，任何一邊漂了都紅。
_EXPECTED_RESPONSE_VOCABULARY = {
    ('0|INVALID_RESULT_DATA', 400),
    ('1|OK', 200),
}


def _vendored_response_literals():
    """從 parent 的 `pay_callback` 原始碼抽出 (body, status) 集合。"""
    source = inspect.getsource(ECPGCallback.pay_callback)
    tree = ast.parse(inspect.cleandoc(source))
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, 'attr', None) or getattr(func, 'id', None)
        if name != 'Response' or not node.args:
            continue
        body = node.args[0]
        if not isinstance(body, ast.Constant):
            continue
        status = 200
        for keyword in node.keywords:
            if keyword.arg == 'status' and isinstance(
                    keyword.value, ast.Constant):
                status = keyword.value.value
        found.add((body.value, status))
    return found


@tagged('post_install', '-at_install')
class TestEcpgCallbackInbox864(HttpCase):

    def setUp(self):
        super().setUp()
        self.provider = self.env['payment.provider'].sudo().search(
            [('code', '=', 'ECPay')], limit=1)
        if not self.provider:
            self.skipTest('ECPay provider not installed')
        self.hash_key = 'pwFHCqoQZGmho4w6'
        self.hash_iv = 'EkRm7iFT261dpevs'
        self.merchant_id = '3002607'
        self.provider.write({
            'state': 'test', 'enable_ecpg': True,
            'MerchantID': self.merchant_id,
            'HashKey': self.hash_key, 'HashIV': self.hash_iv,
        })
        self.payment_method = self._ensure_active_payment_method()
        self.partner = self.env['res.partner'].create(
            {'name': 'ECPG inbox 864g'})
        self.marker = 'CB864%s' % uuid.uuid4().hex[:9].upper()

    def _ensure_active_payment_method(self):
        method = self.provider.payment_method_ids[:1]
        if not method:
            method = self.env['payment.method'].search(
                [('code', '=', 'ecpay')], limit=1)
        if not method:
            method = self.env.ref(
                'payment.payment_method_unknown', raise_if_not_found=False,
            ) or self.env['payment.method'].search([], limit=1)
        if method and not method.active:
            self.env.cr.execute(
                'UPDATE payment_method SET active = TRUE WHERE id = %s',
                [method.id])
            method.invalidate_recordset(['active'])
        return method

    # --- helpers ------------------------------------------------------- #

    def _envelope(self, payload, merchant_id=None, raw_data=None):
        return {
            'MerchantID': merchant_id or self.merchant_id,
            'RpHeader': {'Timestamp': 1700000000},
            'TransCode': 1,
            'TransMsg': '',
            'RtnCode': 1,
            'RtnMsg': 'Success',
            'Data': raw_data if raw_data is not None else encrypt(
                payload, self.hash_key, self.hash_iv),
        }

    def _post(self, envelope_or_raw):
        body = (envelope_or_raw if isinstance(envelope_or_raw, str)
                else json_lib.dumps(envelope_or_raw))
        response = self.url_open(
            _CALLBACK_URL, data=body,
            headers={'Content-Type': 'application/json'}, timeout=30,
        )
        return response.status_code, response.text

    def _pay_payload(self, mtn, trade_no='TN0001', rtn_code=1,
                     rtn_msg='Succeeded'):
        order = {'MerchantTradeNo': mtn} if mtn is not None else {}
        if trade_no is not None:
            order['TradeNo'] = trade_no
        return {'RtnCode': rtn_code, 'RtnMsg': rtn_msg, 'OrderInfo': order}

    def _row(self, external_ref):
        self.env.cr.execute(
            'SELECT kind, state, rtn_code, trade_no, detail, occurrences '
            'FROM %s WHERE external_ref = %%s' % _INCIDENT_TABLE,
            (external_ref,))
        row = self.env.cr.fetchone()
        return row and dict(zip(
            ('kind', 'state', 'rtn_code', 'trade_no', 'detail',
             'occurrences'), row))

    # --- (1) vendored 回應值域沒有漂 -------------------------------------- #

    def test_vendored_response_vocabulary_is_unchanged(self):
        self.assertEqual(
            _vendored_response_literals(), _EXPECTED_RESPONSE_VOCABULARY,
            'vendored payment_ecpay_ecpg 的 pay_callback 回應值域變了：'
            '本 PR 的「對外零漂移」保證是相對於這組字面值定義的，'
            '⇒ 先確認新值域，再決定 override 要不要跟著改',
        )

    # --- (2) 五個可構造分支的現場回應 byte-identical ---------------------- #

    def test_invalid_result_data_response_is_byte_identical(self):
        self.assertEqual(self._post('not-json-at-all'),
                         (400, '0|INVALID_RESULT_DATA'))

    def test_provider_not_found_response_is_byte_identical(self):
        envelope = self._envelope(
            self._pay_payload('%sX1' % self.marker), merchant_id='9999999')
        self.assertEqual(self._post(envelope), (200, '1|OK'))

    def test_decrypt_failure_response_is_byte_identical(self):
        envelope = self._envelope(None, raw_data='!!!not-base64!!!')
        self.assertEqual(self._post(envelope), (200, '1|OK'))

    def test_missing_merchant_trade_no_response_is_byte_identical(self):
        envelope = self._envelope(self._pay_payload(None))
        self.assertEqual(self._post(envelope), (200, '1|OK'))

    def test_transaction_missing_response_is_byte_identical(self):
        """🔴 本題標的分支：查無 tx 仍必須 200 + `1|OK`（G ⛔ 不碰 H 的政策）。"""
        mtn = '%sM1' % self.marker
        envelope = self._envelope(self._pay_payload(mtn))

        self.assertEqual(
            self._post(envelope), (200, '1|OK'),
            '⛔ G 不得改「查無 tx」的對外回應——那是 PR-H 的射程；'
            '本 PR 的收斂只靠自己的預登記＋inbox＋replay',
        )

    def test_settled_transaction_response_is_byte_identical(self):
        mtn = '%sM2' % self.marker
        tx = self.env['payment.transaction'].sudo().create({
            'provider_id': self.provider.id,
            'payment_method_id': self.payment_method.id,
            'partner_id': self.partner.id,
            'amount': 500.0,
            'currency_id': self.env.company.currency_id.id,
            'reference': mtn,
        })
        tx.write({'state': 'cancel'})
        self.env.cr.flush()

        self.assertEqual(self._post(self._envelope(self._pay_payload(mtn))),
                         (200, '1|OK'))

    def test_unsuccessful_rtn_code_response_is_byte_identical(self):
        mtn = '%sM3' % self.marker
        self.env['payment.transaction'].sudo().create({
            'provider_id': self.provider.id,
            'payment_method_id': self.payment_method.id,
            'partner_id': self.partner.id,
            'amount': 500.0,
            'currency_id': self.env.company.currency_id.id,
            'reference': mtn,
        })
        self.env.cr.flush()

        self.assertEqual(
            self._post(self._envelope(
                self._pay_payload(mtn, rtn_code=10100058,
                                  rtn_msg='card declined'))),
            (200, '1|OK'))

    # --- (3) inbox：真孤兒 → callback_tx_missing -------------------------- #

    def test_callback_without_preregistration_lands_as_orphan(self):
        mtn = '%sO1' % self.marker

        self._post(self._envelope(self._pay_payload(mtn, trade_no='TN-O1')))

        row = self._row(mtn)
        self.assertIsNotNone(
            row, 'callback 事實必須落地，否則 replay 沒有入帳依據')
        self.assertEqual(row['kind'], 'callback_tx_missing')
        self.assertEqual(
            row['rtn_code'], '1',
            'RtnCode 是 int，⛔ 不轉 str 會被 Char 型別檢查整筆靜默拒收',
        )
        self.assertEqual(row['trade_no'], 'TN-O1',
                         'trade_no 必須是 provider 真的給的 TradeNo')

    # --- (4) inbox：已有預登記 → 同列更新，kind 不變 ---------------------- #

    def test_callback_with_preregistration_updates_the_intent_row(self):
        mtn = '%sI1' % self.marker
        intent_id = self.env['woow.billing.incident']._log({
            'external_ref': mtn, 'kind': 'intent',
            'partner_ref': self.partner.id, 'amount': 500.0,
            'source_model': 'woow.wallet.topup', 'source_ref': 1,
            'detail': '付款意圖預登記（測試 fixture）。',
        })
        self.assertTrue(intent_id)

        self._post(self._envelope(self._pay_payload(mtn, trade_no='TN-I1')))

        row = self._row(mtn)
        self.assertEqual(
            row['kind'], 'intent',
            '已有預登記時 ⛔ 不得另開一列、也不得改 kind（裁決 G1）',
        )
        self.assertEqual(row['rtn_code'], '1')
        self.assertEqual(row['trade_no'], 'TN-I1')
        self.assertEqual(row['occurrences'], 2)

    # --- (5) PCI：provider 原始訊息 ⛔ 不進 snapshot，但 facts 仍落地 ------- #

    def test_provider_message_never_reaches_the_snapshot(self):
        mtn = '%sP1' % self.marker

        self._post(self._envelope(self._pay_payload(
            mtn, trade_no='TN-P1',
            rtn_msg='card %s <b>declined</b>' % _LUHN_PAN)))

        row = self._row(mtn)
        self.assertIsNotNone(
            row,
            '🔴 帶 PAN-like/HTML 的 RtnMsg ⛔ 不可以讓整筆 snapshot 被拒收 —— '
            '那等於「越危險的 callback 越沒有紀錄」；detail 必須寫死通用句子',
        )
        self.assertEqual(row['rtn_code'], '1')
        self.assertEqual(row['trade_no'], 'TN-P1')
        self.assertNotIn(_LUHN_PAN, row['detail'] or '')
        self.assertNotIn('declined', row['detail'] or '')
        self.assertNotIn('<b>', row['detail'] or '')
