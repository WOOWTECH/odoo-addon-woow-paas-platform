"""#864 PR-G — 手動儲值路徑（`/api/billing/wallet/topup`）的外呼前預登記。

裁決 Q1（issue-leader 2026-09-01）：``woow.billing.incident._log()`` 回
``False`` 時 ⛔ 絕不外呼；驗收錨是**外部呼叫 count == 0**，不是只斷言回了
錯誤。錯誤面沿用既有 API envelope 與既有字串，⛔ 不新增產品文案。

⚠ 本檔刻意**不帶** ``-standard``：`.gitea/workflows/odoo-tests.yml` 的
``--test-tags /woow_paas_platform`` 隱含 ``standard`` ⇒ 這一檔是 PR-G 唯一
會被 CI 選中的守線。真跨連線語意（獨立交易活過 rollback、NOWAIT 競態）在
``tests/integration/test_callback_replay_864.py``，那些結構上 CI 跑不到。

⚠ 為什麼 ``_log`` 的失敗用「幣別名稱過不了 snapshot 型別檢查」製造：那是
**真的**走 ``_validate_log_snapshot`` 的 allowlist 回 ``False``，⛔ 沒有 mock
我們自己的 `_log`／route（self-mock 會把「政策有沒有生效」換成「我有沒有
記得改 mock」）。
"""

import json as json_lib
from unittest.mock import patch

from odoo.tests.common import HttpCase, tagged

_INCIDENT_TABLE = 'woow_billing_incident'


@tagged('post_install', '-at_install')
class TestTopupIntentManualRoute864(HttpCase):

    def setUp(self):
        super().setUp()
        self.provider = self.env['payment.provider'].sudo().search(
            [('code', '=', 'ECPay')], limit=1)
        if not self.provider:
            self.skipTest('ECPay provider not installed')
        self.provider.write({'state': 'test', 'enable_ecpg': True})
        self.payment_method = self._ensure_active_payment_method()
        self.user = self.env['res.users'].create({
            'name': 'Intent Tester 864g', 'login': 'intent864g',
            'password': 'intent864g',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.env['payment.token'].sudo().create({
            'provider_id': self.provider.id,
            'payment_method_id': self.payment_method.id,
            'partner_id': self.user.partner_id.id,
            'payment_details': '**** 4242',
            'ecpg_bindcard_id': 'STUB_BIND_864G',
            'provider_ref': 'STUB_BIND_864G',
            'ecpg_card_expire': '2812',
        })
        self.plan = self.env['woow_paas_platform.topup_plan'].create({
            'name': 'IntentPlan864G', 'amount': 500, 'credits': 500,
            'active': True,
        })
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.topup_payment_enabled', 'True',
        )
        self.authenticate('intent864g', 'intent864g')

    # --- helpers ------------------------------------------------------- #

    def _ensure_active_payment_method(self):
        """Odoo 18 refuses ORM activation of a method no provider supports.

        Same shape as `test_topup_controller.py`: prefer the provider's own
        seeded method, fall back to raw SQL activation in the test DB.
        """
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

    def _post_topup(self):
        response = self.url_open(
            '/api/billing/wallet/topup',
            data=json_lib.dumps({'params': {'plan_id': self.plan.id}}),
            headers={'Content-Type': 'application/json'},
            timeout=30,
        )
        return response.json()['result']

    def _patch_ecpay_boundary(self, recorder=None):
        """Patch the one external ECPay boundary and return the mock."""
        tx_model = type(self.env['payment.transaction'])

        def _send(tx_self):
            if recorder is not None:
                recorder(tx_self)
            return {'redirect_url': 'https://ecpay.example/3ds'}

        patcher = patch.object(
            tx_model, '_send_payment_request', autospec=True,
            side_effect=_send,
        )
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        return mock

    def _incident_rows(self, external_ref=None):
        if external_ref:
            self.env.cr.execute(
                'SELECT external_ref, kind, state FROM %s '
                'WHERE external_ref = %%s' % _INCIDENT_TABLE, (external_ref,))
        else:
            self.env.cr.execute(
                'SELECT external_ref, kind, state FROM %s '
                "WHERE kind = 'intent' AND source_model = %%s"
                % _INCIDENT_TABLE, ('woow.wallet.topup',))
        return self.env.cr.fetchall()

    # --- (1) 預登記在外呼之前 --------------------------------------------- #

    def test_intent_is_registered_before_the_outbound_call(self):
        seen = {}

        def _recorder(tx_self):
            self.env.cr.execute(
                'SELECT kind, state FROM %s WHERE external_ref = %%s'
                % _INCIDENT_TABLE, (tx_self.reference,))
            seen['row'] = self.env.cr.fetchone()
            seen['reference'] = tx_self.reference

        mock = self._patch_ecpay_boundary(recorder=_recorder)

        result = self._post_topup()

        self.assertTrue(result['success'], result)
        self.assertEqual(mock.call_count, 1, '前置：外呼必須真的發生一次')
        self.assertEqual(
            seen.get('row'), ('intent', 'open'),
            '外呼那一刻預登記就必須已經在；晚一步寫＝race 窗口內仍然沒有網',
        )
        self.assertEqual(
            self._incident_rows(seen['reference'])[0][0], seen['reference'],
            'external_ref 必須是 reference 原文（＝MTN），對帳才接得上',
        )

    # --- (2) 裁決 Q1：`_log` False ⇒ 外部呼叫 count == 0 ------------------ #

    def test_intent_write_failure_blocks_the_charge_with_zero_calls(self):
        # 幣別名稱含空白 ⇒ 過不了 `_SNAPSHOT_CODE_RE` ⇒ `_log` 走真的
        # validation 回 False。⛔ 沒有 mock 我們自己的任何一段。
        self.env.company.currency_id.sudo().write({'name': 'T W'})
        mock = self._patch_ecpay_boundary()

        result = self._post_topup()

        self.assertEqual(
            mock.call_count, 0,
            '🔴 fail-closed：預登記寫不進去就 ⛔ 不准外呼',
        )
        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 'intent_not_recorded')
        self.assertEqual(
            self._incident_rows(), [],
            '被拒收的預登記 ⛔ 不得留下半筆',
        )
        self.env.cr.execute(
            'SELECT state, failure_code FROM woow_wallet_topup '
            'WHERE partner_id = %s ORDER BY id DESC LIMIT 1',
            (self.user.partner_id.id,),
        )
        self.assertEqual(
            self.env.cr.fetchone(), ('failed', 'intent_not_recorded'),
            'topup 必須留下可查的失敗痕跡，⛔ 不可以卡在 draft 讓人以為沒發生過',
        )

    # --- (3) 錯誤面沿用既有字串，⛔ 不新增產品文案 ------------------------- #

    def test_failure_reuses_the_existing_user_facing_string(self):
        self.env.company.currency_id.sudo().write({'name': 'T W'})
        self._patch_ecpay_boundary()

        result = self._post_topup()

        self.assertEqual(
            result['error'], 'Payment service unavailable',
            '裁決 Q1 明訂不新增產品文案：錯誤訊息沿用 `sdk_error` 分支既有的'
            '那個 msgid，只有技術 code 是新的（前端 translateTopupError 對'
            '未知 code 會 fall back 到 message，所以使用者看到的仍是既有字串）',
        )
