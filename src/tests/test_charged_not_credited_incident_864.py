"""#864 PR-E — CHARGED_NOT_CREDITED 改走 `woow.billing.incident` 獨立交易。

`wallet.topup.charge_via_token` 有兩個既有分支會在「ECPay 已經接受扣款
（``tx.state == 'done'``）但後續入帳/記帳處理失敗」時把 topup 標成
``state='failed'`` + ``failure_code='wallet_credit_failed'``。這兩個分支
以前只寫這個標記——它住在跟 cron 同一顆 savepoint 裡，savepoint 若因為別的
原因（如 dunning bookkeeping 炸掉）整個回捲，這個「已經真的扣款」的證據會
被連坐捲掉。#864 PR-D 的 ``woow.billing.incident._log()`` 用獨立 cursor
commit，不受外層 savepoint 影響；本檔驗證兩個分支都：

1. 真的呼叫 ``_log()``，且 ``kind='charged_not_credited'``。
2. 呼叫順序在 ``self.write({'state': 'failed', ...})`` **之前**（🔴 順序
   陷阱——如果寫反，incident 寫失敗時就沒有任何東西保底）。
3. snapshot 不帶原始例外文字／PAN-like／HTML——只放 allowlisted 純量。
4. 非 CHARGED_NOT_CREDITED 的失敗分支（真 SDK error／tx 非 done）完全不觸發
   incident（call_count==0 對照組）。

讀回用**獨立 cursor**（沿 ``test_billing_incident_864.py`` 的 ``_read`` 慣例）
——``_log()`` 的 commit 走另一條連線，TransactionCase 自己的 ``self.env.cr``
是 REPEATABLE READ，同一個 test 內用 ORM search 讀不到剛剛才被別的 cursor
commit 的列。
"""
from unittest.mock import patch

from odoo.tests.common import TransactionCase


class TestChargedNotCreditedIncident864(TransactionCase):

    def setUp(self):
        super().setUp()
        self.partner = self.env['res.partner'].create({
            'name': '864-E Test',
            'email': 'test-864e@example.com',
        })
        self.partner.sudo().add_wallet_points(100.0, description='Test seed')
        self.partner.invalidate_recordset(['wallet_balance'])
        self._external_refs_to_purge = []
        self.addCleanup(self._purge)

    def _purge(self):
        if not self._external_refs_to_purge:
            return
        with self.env.registry.cursor() as cr:
            cr.execute(
                'DELETE FROM woow_billing_incident WHERE external_ref = ANY(%s)',
                (self._external_refs_to_purge,),
            )
            cr.commit()

    def _setup_ecpg_token(self, bindcard_id):
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

    def _patch_send_payment_request(self, side_effect=None, return_value=None):
        TxModel = type(self.env['payment.transaction'])
        kwargs = {}
        if side_effect is not None:
            kwargs['side_effect'] = side_effect
        if return_value is not None:
            kwargs['return_value'] = return_value
        return patch.object(TxModel, '_send_payment_request', autospec=True, **kwargs)

    def _read_incident_by_ref(self, external_ref):
        """Fresh-cursor read — 見檔頭 docstring：TransactionCase 的 self.env.cr 是 RR。"""
        self._external_refs_to_purge.append(external_ref)
        with self.env.registry.cursor() as cr:
            cr.execute(
                "SELECT kind, state, partner_ref, payment_token_ref, amount, "
                "credits, bonus, currency, trade_no, failure_code, "
                "source_model, source_ref, detail "
                "FROM woow_billing_incident WHERE external_ref = %s",
                (external_ref,),
            )
            row = cr.fetchone()
        if not row:
            return None
        names = (
            'kind', 'state', 'partner_ref', 'payment_token_ref', 'amount',
            'credits', 'bonus', 'currency', 'trade_no', 'failure_code',
            'source_model', 'source_ref', 'detail',
        )
        return dict(zip(names, row))

    # --- Branch 1: _send_payment_request raises AFTER _set_done ran ------ #

    def test_send_payment_request_raises_after_set_done_logs_incident(self):
        """ECPay 已扣款（_set_done 跑過）但 inner callback 炸掉：incident 必須落地。"""
        provider, token = self._setup_ecpg_token('AB12340001')
        topup = self.env['woow.wallet.topup'].create_auto_topup(
            partner=self.partner, amount=50.0,
        )

        def _set_done_then_raise(tx_self):
            tx_self.state = 'done'
            raise RuntimeError('inner callback boom')

        with self._patch_send_payment_request(side_effect=_set_done_then_raise):
            ok, msg = topup.charge_via_token(token)

        self.assertFalse(ok)
        self.assertEqual(msg, 'wallet_credit_failed')
        self.assertEqual(topup.state, 'failed')
        self.assertEqual(topup.failure_code, 'wallet_credit_failed')

        ref = topup.payment_transaction_id.reference
        incident = self._read_incident_by_ref(ref)
        self.assertIsNotNone(
            incident, '分支1（_set_done 後炸例外）必須落一筆 incident')
        self.assertEqual(incident['kind'], 'charged_not_credited')
        self.assertEqual(incident['state'], 'open')
        self.assertEqual(incident['partner_ref'], self.partner.id)
        self.assertEqual(incident['payment_token_ref'], token.id)
        self.assertEqual(incident['trade_no'], ref)
        self.assertEqual(incident['failure_code'], 'wallet_credit_failed')
        self.assertEqual(incident['source_model'], 'woow.wallet.topup')
        self.assertEqual(incident['source_ref'], topup.id)
        self.assertEqual(incident['credits'], topup.credits)
        self.assertEqual(incident['bonus'], topup.bonus)

    # --- Branch 2: _on_payment_done raises AFTER tx confirmed done -------- #

    def test_on_payment_done_raises_after_charge_confirmed_logs_incident(self):
        """ECPay 已扣款、tx.state=='done' 確認過，但 _on_payment_done 炸掉。"""
        provider, token = self._setup_ecpg_token('AB12340002')
        topup = self.env['woow.wallet.topup'].create_auto_topup(
            partner=self.partner, amount=75.0,
        )

        def _set_done(tx_self):
            tx_self.state = 'done'
            return {}

        TopupModel = type(topup)
        with self._patch_send_payment_request(side_effect=_set_done), \
                patch.object(TopupModel, '_on_payment_done', autospec=True,
                             side_effect=RuntimeError('bookkeeping boom')):
            ok, msg = topup.charge_via_token(token)

        self.assertFalse(ok)
        self.assertEqual(msg, 'wallet_credit_failed')
        self.assertEqual(topup.state, 'failed')

        ref = topup.payment_transaction_id.reference
        incident = self._read_incident_by_ref(ref)
        self.assertIsNotNone(
            incident, '分支2（_on_payment_done 炸例外）必須落一筆 incident')
        self.assertEqual(incident['kind'], 'charged_not_credited')

    # --- 順序：incident 必須先於 state='failed' 落地 ----------------------- #

    def test_incident_is_logged_before_failed_state_write(self):
        """🔴 順序陷阱：incident 若寫在 write 之後，incident 失敗時就沒有保底。"""
        provider, token = self._setup_ecpg_token('AB12340003')
        topup = self.env['woow.wallet.topup'].create_auto_topup(
            partner=self.partner, amount=50.0,
        )

        def _set_done_then_raise(tx_self):
            tx_self.state = 'done'
            raise RuntimeError('inner callback boom')

        call_order = []
        IncidentModel = type(self.env['woow.billing.incident'])
        original_log = IncidentModel._log

        def _log_recorder(incident_self, snapshot):
            # #864 PR-G 之後這條路徑上有兩筆 `_log`（外呼前的 intent 預登記、
            # 以及 CHARGED_NOT_CREDITED），記下 kind 才分得出誰是誰。
            call_order.append('log:%s' % snapshot.get('kind'))
            return original_log(incident_self, snapshot)

        TopupModel = type(topup)
        original_write = TopupModel.write

        def _write_recorder(topup_self, vals):
            if vals.get('state') == 'failed':
                call_order.append('write_failed')
            return original_write(topup_self, vals)

        with self._patch_send_payment_request(side_effect=_set_done_then_raise), \
                patch.object(IncidentModel, '_log', autospec=True,
                             side_effect=_log_recorder), \
                patch.object(TopupModel, 'write', autospec=True,
                             side_effect=_write_recorder):
            topup.charge_via_token(token)

        self.assertEqual(
            call_order,
            ['log:intent', 'log:charged_not_credited', 'write_failed'],
            'incident 必須先於 failed 標記寫入——寫反的話 incident 失敗時 '
            '這筆 charge 結果就沒有任何東西保底。'
            '（#864 PR-G：最前面那筆 intent 是外呼前的預登記）',
        )
        self._external_refs_to_purge.append(topup.payment_transaction_id.reference)

    # --- incident 失敗（fail-soft）不得擋住 failed 標記 -------------------- #

    def test_log_failure_does_not_block_failed_marker(self):
        """`_log()` 本身若失敗（fail-soft），原本的 failed 標記仍要落地。"""
        provider, token = self._setup_ecpg_token('AB12340004')
        topup = self.env['woow.wallet.topup'].create_auto_topup(
            partner=self.partner, amount=50.0,
        )

        def _set_done_then_raise(tx_self):
            tx_self.state = 'done'
            raise RuntimeError('inner callback boom')

        IncidentModel = type(self.env['woow.billing.incident'])
        original_log = IncidentModel._log

        def _boom_only_for_the_failure_incident(incident_self, snapshot):
            # #864 PR-G：同一條路徑上現在有兩筆 `_log`。本測要守的是
            # 「**CHARGED_NOT_CREDITED 那一筆**炸掉不得連累 failed 標記」；
            # 外呼前的 intent 預登記是另一回事（它失敗是 fail-closed、根本
            # 不會走到這裡），⛔ 不可以一起炸，否則測到的是別的分支。
            if snapshot.get('kind') == 'charged_not_credited':
                raise RuntimeError('incident write boom')
            return original_log(incident_self, snapshot)

        with self._patch_send_payment_request(side_effect=_set_done_then_raise), \
                patch.object(IncidentModel, '_log', autospec=True,
                             side_effect=_boom_only_for_the_failure_incident):
            ok, msg = topup.charge_via_token(token)

        self.assertFalse(ok)
        self.assertEqual(msg, 'wallet_credit_failed')
        self.assertEqual(
            topup.state, 'failed',
            'incident 寫入本身炸掉，也不能連累原本的 failed 保底標記',
        )

    # --- 對照組：非 CHARGED_NOT_CREDITED 分支完全不動作 --------------------- #

    def test_generic_sdk_error_does_not_log_incident(self):
        """真 SDK error（ECPay 從未接受扣款）不是 CHARGED_NOT_CREDITED，不落 incident。"""
        provider, token = self._setup_ecpg_token('AB12340005')
        topup = self.env['woow.wallet.topup'].create_auto_topup(
            partner=self.partner, amount=50.0,
        )

        IncidentModel = type(self.env['woow.billing.incident'])
        with self._patch_send_payment_request(
                side_effect=RuntimeError('network timeout')), \
                patch.object(IncidentModel, '_log', autospec=True) as mock_log:
            ok, msg = topup.charge_via_token(token)

        self.assertFalse(ok)
        self.assertEqual(msg, 'sdk_error')
        self.assertEqual(topup.state, 'failed')
        # #864 PR-G 之後 `charge_via_token` 在外呼**之前**會多寫一筆
        # `kind='intent'` 的預登記 ⇒ 判準從「`_log` 完全沒被呼叫」收斂成
        # 「沒有 `charged_not_credited` 那一筆」，那才是本對照組真正在守的東西。
        self.assertEqual(
            [call.args[1].get('kind') for call in mock_log.call_args_list],
            ['intent'],
            '真 SDK error ⛔ 不得落 charged_not_credited；'
            '只該有 PR-G 的外呼前預登記那一筆',
        )

    def test_tx_not_done_does_not_log_incident(self):
        """tx 結束在非 done 狀態（ECPay 明確拒絕）不是 CHARGED_NOT_CREDITED。"""
        provider, token = self._setup_ecpg_token('AB12340006')
        topup = self.env['woow.wallet.topup'].create_auto_topup(
            partner=self.partner, amount=50.0,
        )

        def _set_error(tx_self):
            tx_self.state = 'error'
            return {}

        IncidentModel = type(self.env['woow.billing.incident'])
        with self._patch_send_payment_request(side_effect=_set_error), \
                patch.object(IncidentModel, '_log', autospec=True) as mock_log:
            ok, msg = topup.charge_via_token(token)

        self.assertFalse(ok)
        self.assertEqual(topup.state, 'failed')
        # 同上（#864 PR-G）：判準是「沒有 charged_not_credited 那一筆」。
        self.assertEqual(
            [call.args[1].get('kind') for call in mock_log.call_args_list],
            ['intent'],
            'tx 結束在非 done ⛔ 不得落 charged_not_credited；'
            '只該有 PR-G 的外呼前預登記那一筆',
        )

    # --- PCI/sanitize：raw 例外文字不得流進 detail -------------------------- #

    def test_detail_never_contains_raw_exception_text(self):
        """例外訊息可能夾帶 PAN-like／HTML；incident 的 detail 只放固定通用句。"""
        provider, token = self._setup_ecpg_token('AB12340007')
        topup = self.env['woow.wallet.topup'].create_auto_topup(
            partner=self.partner, amount=50.0,
        )
        dangerous_message = (
            'card 4111111111111111 <script>alert(1)</script> leaked'
        )

        def _set_done_then_raise(tx_self):
            tx_self.state = 'done'
            raise RuntimeError(dangerous_message)

        with self._patch_send_payment_request(side_effect=_set_done_then_raise):
            ok, msg = topup.charge_via_token(token)

        self.assertFalse(ok)
        ref = topup.payment_transaction_id.reference
        incident = self._read_incident_by_ref(ref)
        self.assertIsNotNone(
            incident,
            'detail 必須是固定通用句、不含原始例外內容，才不會被 PCI/HTML '
            'sanitize gate 整筆拒收',
        )
        self.assertNotIn('4111111111111111', incident['detail'])
        self.assertNotIn('<script>', incident['detail'])
        self.assertNotIn(dangerous_message, incident['detail'])
        # 原始例外文字仍完整留在 topup 自己的 failure_message（未被本次改動動到）。
        self.assertIn(dangerous_message[:50], topup.failure_message)
