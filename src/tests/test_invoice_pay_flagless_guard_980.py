# -*- coding: utf-8 -*-
"""#980 A：/woow 拒付 flagless＋automatic 發票，且在付款前揭露原因。

守衛沿用 #977 的 replay-safe predicate：out_invoice 只有在帶
``woow_skip_upstream_hook``，或逐張發票的 company 不是 ``automatic`` 時才可由
/woow 付款。skip-trial 先用實際 ``_generate_invoice_vals()`` 預演未來發票，再套
同一 predicate；測試以 patched choke point 證明未來若開始 stamp 旗標會自動放行。

所有 automatic 測試都先把上游 ``create_ecpay_invoice`` 換成 recorder 才翻設定，
所以即使守衛退化也不會送出真 ECPay HTTP。
"""
from datetime import date, timedelta
import json
import os
from types import SimpleNamespace
import uuid
from unittest.mock import MagicMock, patch

from odoo.exceptions import AccessError
from odoo.tests.common import HttpCase, TransactionCase, tagged

from ..controllers import paas as paas_controller_module
from ..controllers.billing import _invoice_pay_is_replay_safe
from ..models.cloud_service import _internal_provision

_BLOCKED_CODE = 'flagless_automatic_not_payable'
_BLOCKED_COPY = (
    'This invoice cannot be paid in /woow while automatic e-invoicing is '
    'enabled. Please contact support.')


class _AutomaticInvoiceProbeMixin:
    """先裝上游 recorder 再翻 automatic，避免測試有真 ECPay 出網窗口。"""

    def _install_einvoice_probe(self):
        from odoo.addons.ecpay_invoice_tw.models.account_invoice import (
            AccountMove as UpstreamAccountMove,
        )
        calls = []

        def recording(move_self, *args, **kwargs):
            calls.append(move_self.ids)
            return None

        patcher = patch.object(
            UpstreamAccountMove, 'create_ecpay_invoice', recording)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def _set_invoice_policy(self, policy):
        company = self.env.company.sudo()
        previous = company.auto_invoice
        self.addCleanup(
            lambda: company.write({'auto_invoice': previous}))
        company.write({'auto_invoice': policy})
        company.invalidate_recordset(['auto_invoice'])
        self.assertEqual(company.auto_invoice, policy, 'fixture 前提：company policy 未落地')

    def _use_automatic_invoicing(self):
        calls = self._install_einvoice_probe()
        self._set_invoice_policy('automatic')
        return calls

    def _use_manual_invoicing(self):
        calls = self._install_einvoice_probe()
        self._set_invoice_policy('manual')
        return calls


@tagged('post_install', '-at_install')
class TestInvoicePayFlaglessGuard980(
        _AutomaticInvoiceProbeMixin, HttpCase):
    """invoice pay 三格矩陣＋list/detail 能力 payload。"""

    def setUp(self):
        super().setUp()
        self.authenticate('admin', 'admin')
        self.user = self.env.ref('base.user_admin')
        self.partner = self.user.partner_id
        self.company = self.env.company.sudo()

        if not self.env['account.journal'].sudo().search([
                ('company_id', '=', self.company.id),
                ('type', '=', 'sale')], limit=1):
            self.env['account.journal'].sudo().create({
                'name': 'Test Sales 980',
                'code': 'TS980',
                'type': 'sale',
                'company_id': self.company.id,
            })
        payment_journal = self.env['account.journal'].sudo().search([
            ('company_id', '=', self.company.id),
            ('type', 'in', ('bank', 'cash')),
        ], limit=1)
        if not payment_journal:
            payment_journal = self.env['account.journal'].sudo().create({
                'name': 'Test Bank 980',
                'code': 'TB980',
                'type': 'bank',
                'company_id': self.company.id,
            })
        self.company.sh_paid_subscription_journal = payment_journal

        self.product = self.env['product.product'].sudo().create({
            'name': 'Invoice Guard Product 980',
            'type': 'service',
            'list_price': 500.0,
            'taxes_id': [(6, 0, [])],
        })
        self.invoice = self._make_invoice(flagged=False)
        self.partner.sudo().add_wallet_points(
            3000.0, description='980 invoice guard test credit')
        self.partner.invalidate_recordset(['wallet_balance'])

    def _make_invoice(self, flagged, move_type='out_invoice'):
        return self.env['account.move'].sudo().create({
            'move_type': move_type,
            'partner_id': self.partner.id,
            'company_id': self.company.id,
            'woow_skip_upstream_hook': flagged,
            'invoice_line_ids': [(0, 0, {
                'product_id': self.product.id,
                'quantity': 1,
                'price_unit': 500.0,
                'tax_ids': [(5, 0, 0)],
            })],
        })

    def _post(self, path, params=None):
        response = self.url_open(
            path,
            data=json.dumps({
                'jsonrpc': '2.0',
                'method': 'call',
                'id': 980,
                'params': params or {},
            }),
            headers={'Content-Type': 'application/json'},
        )
        parsed = response.json()
        self.assertIn('result', parsed, '預期 business JSON response，實際 %r' % parsed)
        self.env.invalidate_all()
        return parsed['result']

    def _pay(self, invoice=None):
        invoice = invoice or self.invoice
        return self._post('/api/billing/invoices/%s/pay' % invoice.id)

    def _points(self):
        self.env.invalidate_all()
        return self.partner.sudo().get_wallet_loyalty_card().points

    def test_automatic_flagless_is_rejected_before_every_payment_side_effect(self):
        einvoice_calls = self._use_automatic_invoicing()
        Move = type(self.invoice)
        original_pay = Move._pay_from_wallet
        pay_calls = []

        def recording_pay(move_self, *args, **kwargs):
            pay_calls.append(move_self.ids)
            return original_pay(move_self, *args, **kwargs)

        before_points = self._points()
        before_state = self.invoice.state
        before_payments = set(self.env['account.payment'].sudo().search([
            ('partner_id', '=', self.partner.id),
        ]).ids)
        with patch.object(Move, '_pay_from_wallet', recording_pay):
            result = self._pay()

        self.invoice.invalidate_recordset()
        self.assertFalse(result.get('success'), result)
        self.assertEqual(result.get('code'), _BLOCKED_CODE)
        self.assertEqual(pay_calls, [], '守衛必須在 `_pay_from_wallet` 前拒絕')
        self.assertEqual(einvoice_calls, [], '守衛必須在 action_post／ECPay 外呼前拒絕')
        self.assertEqual(self._points(), before_points, '拒付不得扣 wallet')
        self.assertEqual(self.invoice.state, before_state, '拒付不得改發票 state')
        self.assertEqual(
            set(self.env['account.payment'].sudo().search([
                ('partner_id', '=', self.partner.id),
            ]).ids),
            before_payments,
            '拒付不得建立 payment')

    def test_manual_flagless_keeps_existing_payment_behaviour(self):
        einvoice_calls = self._use_manual_invoicing()
        before_points = self._points()

        result = self._pay()

        self.assertTrue(result.get('success'), result)
        self.assertEqual(einvoice_calls, [], 'manual 下上游 inline hook 必須是 no-op')
        self.assertEqual(self._points(), before_points - 500.0)

    def test_automatic_flagged_keeps_existing_payment_behaviour(self):
        einvoice_calls = self._use_automatic_invoicing()
        invoice = self._make_invoice(flagged=True)
        before_points = self._points()

        result = self._pay(invoice)

        self.assertTrue(result.get('success'), result)
        self.assertEqual(einvoice_calls, [], 'skip flag 必須短路上游 inline hook')
        self.assertEqual(self._points(), before_points - 500.0)

    def test_not_payable_mutation_rejections_return_the_payload_code(self):
        refund = self._make_invoice(flagged=False, move_type='out_refund')
        refund_result = self._pay(refund)
        self.assertFalse(refund_result.get('success'), refund_result)
        self.assertEqual(refund_result.get('code'), 'invoice_not_payable')

        self.invoice.button_cancel()
        self.invoice.invalidate_recordset()
        self.assertEqual(self.invoice.state, 'cancel', 'fixture 前提：發票必須已取消')
        state_result = self._pay()
        self.assertFalse(state_result.get('success'), state_result)
        self.assertEqual(state_result.get('code'), 'invoice_not_payable')

    def test_list_payload_mirrors_payability_for_both_sides_of_the_gate(self):
        self._use_automatic_invoicing()
        flagged = self._make_invoice(flagged=True)

        result = self._post('/api/billing/invoices', {'limit': 50})
        rows = {row['id']: row for row in result['data']}

        self.assertIn(self.invoice.id, rows, 'flagless 發票仍須列出，不做 list filter')
        self.assertEqual(
            (rows[self.invoice.id]['payable_via_woow'],
             rows[self.invoice.id]['pay_blocked_reason']),
            (False, _BLOCKED_CODE))
        self.assertEqual(
            (rows[flagged.id]['payable_via_woow'],
             rows[flagged.id]['pay_blocked_reason']),
            (True, None))

    def test_detail_payload_mirrors_the_same_payability_fields(self):
        self._use_automatic_invoicing()

        result = self._post('/api/billing/invoices/%s' % self.invoice.id)

        self.assertTrue(result.get('success'), result)
        self.assertEqual(result['data']['payable_via_woow'], False)
        self.assertEqual(result['data']['pay_blocked_reason'], _BLOCKED_CODE)


@tagged('post_install', '-at_install')
class TestSkipTrialFlaglessGuard980(
        _AutomaticInvoiceProbeMixin, HttpCase):
    """非 admin workspace owner 的 skip-trial 三格與 estimate HTTP 矩陣。"""

    def setUp(self):
        super().setUp()
        suffix = uuid.uuid4().hex[:8]
        self.login = 'skip_trial_owner_980_%s' % suffix
        self.user = self.env['res.users'].sudo().create({
            'name': 'Skip Trial Owner 980',
            'login': self.login,
            'password': self.login,
            'email': '%s@example.test' % self.login,
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.partner = self.user.partner_id
        self.company = self.env.company.sudo()

        if not self.env['account.journal'].sudo().search([
                ('company_id', '=', self.company.id),
                ('type', '=', 'sale')], limit=1):
            self.env['account.journal'].sudo().create({
                'name': 'Skip Trial Sales 980',
                'code': 'SS980',
                'type': 'sale',
                'company_id': self.company.id,
            })
        self.env['res.company'].sudo()._ensure_subscription_journal()
        payment_journal = self.env['account.journal'].sudo().search([
            ('company_id', '=', self.company.id),
            ('type', 'in', ('bank', 'cash')),
        ], limit=1)
        if not payment_journal:
            payment_journal = self.env['account.journal'].sudo().create({
                'name': 'Skip Trial Bank 980',
                'code': 'SB980',
                'type': 'bank',
                'company_id': self.company.id,
            })
        self.company.sh_paid_subscription_journal = payment_journal

        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'Skip Trial Workspace 980',
            'owner_id': self.user.id,
        })
        self.plan = self.env['sh.subscription.plan'].sudo().create({
            'name': 'Skip Trial Plan 980',
            'sh_duration': 1,
            'sh_unit': 'month',
            'sh_plan_price': 500.0,
            'sh_no_of_billing_cycle': -1,
            'sh_trial': True,
            'sh_trial_duration': 7,
            'sh_trial_unit': 'day',
        })
        product_template = self.env['product.template'].sudo().create({
            'name': 'Skip Trial Product 980',
            'type': 'service',
            'list_price': 500.0,
            'sh_product_subscribe': True,
            'sh_subscription_plan_id': self.plan.id,
            'taxes_id': [(6, 0, [])],
        })
        today = date.today()
        self.sub = self.env['sh.subscription.subscription'].sudo().create({
            'sh_partner_id': self.partner.id,
            'sh_company_id': self.company.id,
            'product_id': product_template.product_variant_id.id,
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
            'state': 'in_progress',
        })
        template = self.env[
            'woow_paas_platform.cloud_app_template'].sudo().create({
                'name': 'Skip Trial App 980',
                'slug': 'skip-trial-app-980',
                'helm_repo_url': '',
                'helm_chart_name': 'oci://example.invalid/skip-trial-980',
                'helm_chart_version': '0.0.0',
                'is_active': True,
                'is_hidden': False,
            })
        self.service = _internal_provision(
            self.env['woow_paas_platform.cloud_service'].sudo()).create({
                'name': 'Skip Trial Service 980',
                'workspace_id': self.workspace.id,
                'template_id': template.id,
                'subscription_id': self.sub.id,
            })
        self.partner.sudo().add_wallet_points(
            3000.0, description='980 skip-trial guard test credit')
        self.partner.invalidate_recordset(['wallet_balance'])
        self.authenticate(self.login, self.login)

    def _post(self, suffix):
        response = self.url_open(
            '/api/cloud-services/services/%s/%s' % (self.service.id, suffix),
            data=json.dumps({
                'jsonrpc': '2.0',
                'method': 'call',
                'id': 980,
                'params': {},
            }),
            headers={'Content-Type': 'application/json'},
        )
        parsed = response.json()
        self.assertIn('result', parsed, '預期 business JSON response，實際 %r' % parsed)
        self.env.invalidate_all()
        return parsed['result']

    def _points(self):
        self.env.invalidate_all()
        return self.partner.sudo().get_wallet_loyalty_card().points

    def _invoices(self):
        return self.env['account.move'].sudo().search([
            ('sh_subscription_id', '=', self.sub.id),
        ])

    def _sub_snapshot(self):
        self.sub.invalidate_recordset()
        return (
            self.sub.sh_trial,
            self.sub.sh_trial_end_date,
            self.sub.sh_start_date,
            self.sub.sh_date_of_next_payment,
        )

    def _flag_generated_invoices(self):
        """在實際建票 choke point 加 skip flag，驗證 guard 不硬編「永遠 flagless」。"""
        Subscription = type(self.sub)
        original = Subscription._generate_invoice_vals

        def flagged(subscription_self, *args, **kwargs):
            values = original(subscription_self, *args, **kwargs)
            values['woow_skip_upstream_hook'] = True
            return values

        return patch.object(Subscription, '_generate_invoice_vals', flagged)

    def test_http_actor_is_non_admin_owner_without_journal_read_access(self):
        response = self.url_open(
            '/web/session/get_session_info',
            data=json.dumps({
                'jsonrpc': '2.0',
                'method': 'call',
                'id': 980,
                'params': {},
            }),
            headers={'Content-Type': 'application/json'},
        )
        payload = response.json()
        self.assertIn('result', payload, 'HTTP session probe 失敗：%r' % payload)
        self.assertEqual(
            payload['result']['uid'], self.user.id,
            'HTTP session 必須真的是非 admin workspace owner，不是 admin fixture',
        )
        self.assertFalse(
            self.user.has_group('base.group_system'),
            'fixture 前提：owner 不得有 system/admin 群組',
        )
        access = self.workspace.with_user(self.user).check_user_access(self.user)
        self.assertTrue(access, 'fixture 前提：HTTP actor 必須是 workspace member')
        self.assertEqual(access.role, 'owner', 'fixture 前提：HTTP actor 必須是 owner')
        with self.assertRaises(AccessError):
            self.env['account.journal'].with_user(self.user).check_access('read')

    def test_automatic_flagless_is_rejected_with_zero_side_effects(self):
        einvoice_calls = self._use_automatic_invoicing()
        before_points = self._points()
        before_sub = self._sub_snapshot()
        before_payments = set(self.env['account.payment'].sudo().search([
            ('partner_id', '=', self.partner.id),
        ]).ids)

        result = self._post('skip-trial')

        self.assertFalse(result.get('success'), result)
        self.assertEqual(result.get('code'), _BLOCKED_CODE)
        self.assertEqual(einvoice_calls, [], '拒付不得抵達上游 ECPay seam')
        self.assertEqual(self._points(), before_points, '拒付不得扣 wallet')
        self.assertEqual(self._sub_snapshot(), before_sub, '拒付不得結束 trial')
        self.assertFalse(self._invoices(), '拒付不得建立 invoice')
        self.assertEqual(
            set(self.env['account.payment'].sudo().search([
                ('partner_id', '=', self.partner.id),
            ]).ids),
            before_payments,
            '拒付不得建立 payment')

    def test_manual_flagless_keeps_existing_skip_trial_behaviour(self):
        einvoice_calls = self._use_manual_invoicing()
        before_points = self._points()

        result = self._post('skip-trial')

        self.assertTrue(result.get('success'), result)
        self.assertEqual(einvoice_calls, [], 'manual 下上游 inline hook 必須是 no-op')
        self.assertEqual(self._points(), before_points - 500.0)
        self.assertEqual(len(self._invoices()), 1)
        self.assertFalse(self._invoices().woow_skip_upstream_hook)

    def test_automatic_flagged_future_invoice_is_allowed(self):
        einvoice_calls = self._use_automatic_invoicing()
        before_points = self._points()

        with self._flag_generated_invoices():
            result = self._post('skip-trial')

        self.assertTrue(result.get('success'), result)
        self.assertEqual(einvoice_calls, [], 'flagged invoice 必須短路上游 ECPay seam')
        self.assertEqual(self._points(), before_points - 500.0)
        self.assertEqual(len(self._invoices()), 1)
        self.assertTrue(self._invoices().woow_skip_upstream_hook)

    def test_estimate_payload_uses_the_same_three_cell_matrix(self):
        einvoice_calls = self._use_automatic_invoicing()
        before_points = self._points()
        before_sub = self._sub_snapshot()
        before_payments = set(self.env['account.payment'].sudo().search([
            ('partner_id', '=', self.partner.id),
        ]).ids)

        estimate_result = self._post('skip-trial-estimate')
        self.assertTrue(estimate_result.get('success'), estimate_result)
        automatic_flagless = estimate_result['data']
        self.assertEqual(
            (automatic_flagless['payable_via_woow'],
             automatic_flagless['pay_blocked_reason']),
            (False, _BLOCKED_CODE))
        self.assertEqual(einvoice_calls, [], 'estimate 不得抵達上游 ECPay seam')
        self.assertEqual(self._points(), before_points, 'estimate 不得扣 wallet')
        self.assertEqual(self._sub_snapshot(), before_sub, 'estimate 不得結束 trial')
        self.assertFalse(self._invoices(), 'transient preview 不得建立 invoice')
        self.assertEqual(
            set(self.env['account.payment'].sudo().search([
                ('partner_id', '=', self.partner.id),
            ]).ids),
            before_payments,
            'estimate 不得建立 payment')

        self.company.write({'auto_invoice': 'manual'})
        manual_flagless = self._post('skip-trial-estimate')['data']
        self.assertEqual(
            (manual_flagless['payable_via_woow'],
             manual_flagless['pay_blocked_reason']),
            (True, None))

        self.company.write({'auto_invoice': 'automatic'})
        with self._flag_generated_invoices():
            automatic_flagged = self._post('skip-trial-estimate')['data']
        self.assertEqual(
            (automatic_flagged['payable_via_woow'],
             automatic_flagged['pay_blocked_reason']),
            (True, None))


@tagged('post_install', '-at_install')
class TestSkipTrialPreviewContract980(TransactionCase):
    """preview 的 company/ACL SSOT 窄 contract，不另造產品 policy。"""

    def test_preview_stamps_subscription_company_then_uses_sudo_new(self):
        controller = paas_controller_module.PaasController()
        request_mock = MagicMock()
        request_mock.env.company.id = 7
        # #991（PR #1300）：controller 的 sudo recordset 一律先 `with_context({})`，
        # 不繼承 request 上可偽造的 `default_*`／身分 context（設計見
        # docs/plans/2026-Q4/2026-10-01-portal-paas-access-design.md）。preview 的
        # 鏈因此是 `env['account.move'].with_context({}).sudo().new(vals)`；
        # 下面「釘 subscription company → sudo().new」的斷言原樣套在清掉 context
        # 之後的那個 model 上，另加一條釘住 context 確實被清空。
        RawMove = request_mock.env.__getitem__.return_value
        Move = RawMove.with_context.return_value
        preview = Move.sudo.return_value.new.return_value
        vals = {'journal_id': 99}
        sub = MagicMock()
        sub.sh_company_id.id = 42
        sub._generate_invoice_vals.return_value = vals
        expected = {
            'payable_via_woow': False,
            'pay_blocked_reason': _BLOCKED_CODE,
        }

        with patch.object(paas_controller_module, 'request', request_mock), \
                patch.object(
                    paas_controller_module,
                    '_invoice_payability_fields',
                    return_value=expected,
                ) as payability:
            result = controller._skip_trial_invoice_payability(
                MagicMock(), sub=sub)

        self.assertEqual(result, expected)
        self.assertEqual(
            vals['company_id'], 42,
            'preview 必須釘 subscription company，不得誤吃 request env company',
        )
        RawMove.with_context.assert_called_once_with({})
        RawMove.sudo.assert_not_called()
        Move.sudo.assert_called_once_with()
        Move.sudo.return_value.new.assert_called_once_with(vals)
        payability.assert_called_once_with(preview)

    def test_replay_safe_predicate_fails_closed_without_invoice_company(self):
        invoice = SimpleNamespace(
            move_type='out_invoice',
            woow_skip_upstream_hook=True,
            company_id=False,
            _llm_is_internal_credit_move=lambda: False,
        )
        self.assertFalse(
            _invoice_pay_is_replay_safe(invoice),
            '缺 company 時即使 skip flag 為真也必須 fail-closed',
        )

        invoice.company_id = SimpleNamespace(auto_invoice='manual')
        invoice.woow_skip_upstream_hook = False
        self.assertTrue(
            _invoice_pay_is_replay_safe(invoice),
            'predicate 必須讀 invoice company 的 manual policy',
        )
        invoice.company_id.auto_invoice = 'automatic'
        self.assertFalse(
            _invoice_pay_is_replay_safe(invoice),
            '同一 invoice company 切 automatic 後必須拒絕',
        )


@tagged('post_install', '-at_install')
class TestFlaglessPaymentFrontendContract980(TransactionCase):
    """前端 fail-closed 讀 payload，並在實際 mutation 前顯示原因。"""

    _FILES = {
        'list_js': ('pages', 'billing', 'InvoiceListPage.js'),
        'list_xml': ('pages', 'billing', 'InvoiceListPage.xml'),
        'detail_js': ('pages', 'billing', 'InvoiceDetailPage.js'),
        'detail_xml': ('pages', 'billing', 'InvoiceDetailPage.xml'),
        'skip_js': ('components', 'modal', 'SkipTrialModal.js'),
        'skip_xml': ('components', 'modal', 'SkipTrialModal.xml'),
        'service_xml': ('pages', 'service', 'ServiceDetailPage.xml'),
    }

    def _source(self, key):
        path = os.path.join(
            os.path.dirname(__file__), '..', 'static', 'src', 'paas',
            *self._FILES[key])
        self.assertTrue(os.path.exists(path), '找不到前端檔案：%s' % path)
        with open(path, encoding='utf-8') as handle:
            return handle.read()

    def test_invoice_pages_fail_closed_on_payable_payload(self):
        for key in ('list_js', 'detail_js'):
            with self.subTest(file=key):
                source = self._source(key)
                self.assertIn('payable_via_woow === true', source)
                self.assertIn('pay_blocked_reason', source)

    def test_invoice_pages_show_the_blocked_reason_before_payment(self):
        expected_disabled = {
            'list_xml': 'disabled="!canPay(invoice)"',
            'detail_xml': 'disabled="!canPay"',
        }
        for key in ('list_xml', 'detail_xml'):
            with self.subTest(file=key):
                source = self._source(key)
                self.assertIn(_BLOCKED_COPY, ' '.join(source.split()))
                self.assertIn(expected_disabled[key], source)

    def test_skip_trial_confirmation_fails_closed_and_shows_reason(self):
        js_source = self._source('skip_js')
        xml_source = self._source('skip_xml')
        self.assertIn('payable_via_woow === true', js_source)
        blocked_getter = js_source.split(
            'get isPaymentBlocked()', 1)[1].split('onBackdropClick', 1)[0]
        self.assertNotIn(
            'pay_blocked_reason', blocked_getter,
            'payable 欄位缺失時仍須顯示 fallback，不可要求 reason 同時存在',
        )
        self.assertIn(_BLOCKED_COPY, ' '.join(xml_source.split()))
        self.assertIn(
            '<t t-else="">Invoice is not payable</t>', xml_source,
            '未知或缺失 reason 必須顯示可翻譯 fallback',
        )
        self.assertIn('disabled="!canConfirm"', xml_source)
        self.assertIn(
            'requireWoowPayable="true"', self._source('service_xml'),
            '只有 Cloud Service 入口啟用 #980；generic modal 不得鎖死其他 surface')
