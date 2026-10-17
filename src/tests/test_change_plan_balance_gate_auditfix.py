"""商用驗收 AUDITFIX change-plan-insufficient-balance（#1677 A1）。

## 缺陷

錢包不足（含 0 點）時換方案仍會成功：三個 change-plan 入口（Cloud Service／Security
Access／Smart Home）都是「退點 → 關舊訂閱 → `_create_and_confirm_subscription`」，沒有任何
餘額檢查；新訂閱 confirm 時開出首期發票、錢包扣不到就留成未付。而每日扣款排程只撈
`sh_date_of_next_payment <= today`——新訂閱的下次扣款日已經是一期之後（年繳＝一年後），
這張未付的首期發票一整年都不會被追款。

## 修法契約（#1677 owner 拍板 A1）

- 三個入口在第一個寫入（退點）之前檢查：錢包餘額＋本次退點 < 新方案首期應付（含稅，與
  skip-trial／續訂同一把尺 `_sh_get_period_billing_amount`）⇒ 回 `insufficient_balance`，
  不退點、不關舊訂閱、不建新訂閱、不開發票。錯誤碼與 `/api/billing/invoices/<id>/pay` 一致。
- `dry_run=True`：走完同一組閘之後回估算（`total`／`balance`／`refund`／`sufficient`／
  `shortfall`），不寫任何東西——前端據此停用「確認變更」並顯示差額。
- 每日扣款排程另外追「下次扣款日還沒到、但名下有已過帳未付發票」的進行中訂閱：用錢包結清，
  失敗照 dunning 規則累計；結清時**不**推進下次扣款日（那張發票付的是已經開始的這一期）。
"""
from datetime import timedelta
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.woow_paas_platform.controllers.paas import PaasController
from odoo.addons.woow_paas_platform.controllers.security_access import (
    SecurityAccessController,
)
from odoo.addons.woow_paas_platform.controllers.smart_home import SmartHomeController
from ..models.cloud_service import _internal_provision
from .smart_home_fixture import sh_model
from .test_change_plan_ends_trial_36 import _ensure_journal, _make_owner, _make_plan

_CTRL = 'odoo.addons.woow_paas_platform.controllers.%s.request'


def _wallet(partner, target):
    """把 partner 的錢包設成剛好 ``target`` 點。"""
    loyalty = partner.sudo().get_wallet_loyalty_card()
    if loyalty:
        loyalty.points = 0
    if target > 0:
        partner.sudo().add_wallet_points(target, description='balance gate fixture')
    partner.invalidate_recordset(['wallet_balance'])


class _GateCase(TransactionCase):

    def setUp(self):
        super().setUp()
        _ensure_journal(self.env)
        self.Move = self.env['account.move'].sudo()

    def _state(self, entity):
        sub = entity.sudo().subscription_id
        return sub, sub.state, self.Move.search_count([]), \
            sub.sh_partner_id.wallet_balance

    def _assert_blocked(self, res, entity, before, price):
        old_sub, old_state, moves, balance = before
        self.assertFalse(res.get('success'), res)
        self.assertEqual(res.get('code'), 'insufficient_balance', res)
        self.assertEqual(res['data']['code'], 'insufficient_balance', res)
        # 首期應付是含稅金額（測試 DB 的預設銷項稅也算進去），至少是方案標價。
        self.assertGreaterEqual(res['data']['required'], price, res)
        self.assertAlmostEqual(
            res['data']['shortfall'], res['data']['required'] - res['data']['available'], places=2)
        self.env.invalidate_all()
        self.assertEqual(entity.sudo().subscription_id, old_sub, '不得建新訂閱')
        self.assertEqual(old_sub.state, old_state, '舊訂閱不得被關閉')
        self.assertEqual(self.Move.search_count([]), moves, '不得開任何發票／貸項')
        self.assertAlmostEqual(
            old_sub.sh_partner_id.wallet_balance, balance, places=2, msg='錢包不得變動')


@tagged('post_install', '-at_install')
class TestCloudServiceChangePlanBalanceGateAuditfix(_GateCase):

    def setUp(self):
        super().setUp()
        self.controller = PaasController()
        self.plan_a = _make_plan(self.env, 'AF CS A', 300.0, 0)
        self.plan_b = _make_plan(self.env, 'AF CS B', 1000.0, 0)
        template = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'AF Balance Tmpl', 'slug': 'af-balance-gate',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'af-balance', 'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
            'subscription_plan_ids': [(6, 0, [self.plan_a.id, self.plan_b.id])],
        })
        self.owner = _make_owner(self.env, 'af_cs')
        ws = self.env['woow_paas_platform.workspace'].create({
            'name': 'AF Balance WS', 'owner_id': self.owner.id})
        self.service = _internal_provision(
            self.env['woow_paas_platform.cloud_service']).create({
                'name': 'AF Balance Svc', 'workspace_id': ws.id,
                'template_id': template.id})
        self.partner = ws.owner_id.partner_id
        self.service._create_and_confirm_subscription(self.partner, self.plan_a)

    def _change(self, **kw):
        with patch(_CTRL % 'paas', new=MagicMock(env=self.env(user=self.owner))):
            return self.controller.api_service_change_plan(
                service_id=self.service.id, plan_id=self.plan_b.id, **kw)

    def _required(self):
        res = self._change(dry_run=True)
        self.assertTrue(res.get('success'), res)
        return res['data']['required']

    def test_zero_balance_is_refused_before_any_write(self):
        _wallet(self.partner, 0)
        before = self._state(self.service)

        res = self._change()

        self._assert_blocked(res, self.service, before, 1000.0)
        self.assertAlmostEqual(res['data']['shortfall'], res['data']['required'], places=2)
        self.assertIn('{:,.2f}'.format(res['data']['shortfall']), res['error'])

    def test_short_balance_is_refused(self):
        _wallet(self.partner, self._required() - 1.0)
        before = self._state(self.service)

        res = self._change()

        self._assert_blocked(res, self.service, before, 1000.0)
        self.assertAlmostEqual(res['data']['shortfall'], 1.0, places=2)

    def test_dry_run_reports_the_quote_without_writing(self):
        required = self._required()
        _wallet(self.partner, required + 200.0)
        before = self._state(self.service)

        res = self._change(dry_run=True)

        self.assertTrue(res.get('success'), res)
        data = res['data']
        self.assertTrue(data['dry_run'])
        self.assertTrue(data['sufficient'])
        self.assertAlmostEqual(data['required'], required, places=2)
        self.assertAlmostEqual(data['balance'], required + 200.0, places=2)
        self.assertAlmostEqual(data['shortfall'], 0.0, places=2)
        self.env.invalidate_all()
        self.assertEqual(self.service.sudo().subscription_id, before[0])
        self.assertEqual(before[0].state, 'in_progress')
        self.assertEqual(self.Move.search_count([]), before[2])

    def test_dry_run_with_short_balance_is_a_successful_estimate(self):
        required = self._required()
        _wallet(self.partner, 100.0)

        res = self._change(dry_run=True)

        self.assertTrue(res.get('success'), res)
        self.assertFalse(res['data']['sufficient'])
        self.assertAlmostEqual(res['data']['shortfall'], required - 100.0, places=2)

    def test_enough_balance_changes_the_plan_and_pays_the_first_invoice(self):
        _wallet(self.partner, self._required())
        old_sub = self.service.sudo().subscription_id

        res = self._change()

        self.assertTrue(res.get('success'), res)
        new_sub = self.service.sudo().subscription_id
        self.assertNotEqual(new_sub, old_sub)
        invoices = self.Move.search([('sh_subscription_id', '=', new_sub.id)])
        self.assertTrue(invoices)
        self.assertTrue(all(inv.payment_state in ('paid', 'in_payment') for inv in invoices),
                        [(inv.id, inv.payment_state) for inv in invoices])


@tagged('post_install', '-at_install')
class TestSaShChangePlanBalanceGateAuditfix(_GateCase):

    def setUp(self):
        super().setUp()
        self.sa_plan_a = _make_plan(self.env, 'AF SA A', 100.0, 0, sa_max_routes=3)
        self.sa_plan_b = _make_plan(self.env, 'AF SA B', 800.0, 0, sa_max_routes=5)
        self.sa_template = self.env[
            'woow_paas_platform.security_access_template'].sudo().create({
                'name': 'AF SA Tmpl',
                'subscription_plan_ids': [(6, 0, [self.sa_plan_a.id, self.sa_plan_b.id])]})

    def test_security_access_zero_balance_is_refused(self):
        owner = _make_owner(self.env, 'af_sa')
        ws = self.env['woow_paas_platform.workspace'].sudo().create({
            'name': 'AF SA WS', 'owner_id': owner.id})
        sa = self.env['woow_paas_platform.security_access'].sudo().create({
            'name': 'AF SA GW', 'workspace_id': ws.id, 'template_id': self.sa_template.id})
        sa._create_and_confirm_subscription(ws.owner_id.partner_id, self.sa_plan_a)
        _wallet(ws.owner_id.partner_id, 0)
        before = self._state(sa)

        with patch(_CTRL % 'security_access', new=MagicMock(env=self.env(user=owner))):
            res = SecurityAccessController().api_sa_change_plan(
                workspace_id=ws.id, sa_id=sa.id, plan_id=self.sa_plan_b.id)

        self._assert_blocked(res, sa, before, 800.0)

    def test_smart_home_zero_balance_is_refused(self):
        plan_a = _make_plan(self.env, 'AF SH A', 100.0, 0)
        plan_b = _make_plan(self.env, 'AF SH B', 700.0, 0)
        self.env['woow_paas_platform.smart_home_template'].sudo().create({
            'name': 'AF SH Tmpl', 'is_active': True,
            'subscription_plan_ids': [(6, 0, [plan_a.id, plan_b.id])]})
        owner = _make_owner(self.env, 'af_sh')
        ws = self.env['woow_paas_platform.workspace'].sudo().create({
            'name': 'AF SH WS', 'owner_id': owner.id})
        home = sh_model(self.env).sudo().create({'name': 'AF SH Home', 'workspace_id': ws.id})
        home._create_and_confirm_subscription(ws.owner_id.partner_id, plan_a)
        _wallet(ws.owner_id.partner_id, 0)
        before = self._state(home)

        with patch(_CTRL % 'smart_home', new=MagicMock(env=self.env(user=owner))):
            res = SmartHomeController().api_smarthome_change_plan(
                workspace_id=ws.id, smarthome_id=home.id, plan_id=plan_b.id)

        self._assert_blocked(res, home, before, 700.0)


@tagged('post_install', '-at_install')
class TestUnpaidFirstInvoiceDunningAuditfix(TransactionCase):
    """dunning 缺口：下次扣款日還沒到、但名下有已過帳未付發票的進行中訂閱也要追款。"""

    def setUp(self):
        super().setUp()
        self.Sub = self.env['sh.subscription.subscription'].sudo()
        self.today = fields.Date.today()
        self.partner = self.env['res.partner'].create({
            'name': 'AF Dunning Partner', 'email': 'af_dunning@example.test'})
        _wallet(self.partner, 0)
        plan = self.env['sh.subscription.plan'].sudo().create({
            'name': 'AF Dunning Yearly', 'sh_plan_price': 1200.0, 'sh_duration': 1,
            'sh_unit': 'year', 'sh_no_of_billing_cycle': -1})
        self.next_payment = self.today + timedelta(days=360)
        self.sub = self.Sub.create({
            'sh_partner_id': self.partner.id, 'sh_subscription_plan_id': plan.id,
            'sh_plan_price': 1200.0, 'sh_recurrency': 1, 'sh_unit': 'year',
            'sh_start_date': self.today - timedelta(days=5),
            'sh_date_of_next_payment': self.next_payment,
            'state': 'in_progress'})
        self.invoice = self.env['account.move'].sudo().create({
            'move_type': 'out_invoice', 'partner_id': self.partner.id,
            'sh_subscription_id': self.sub.id,
            'invoice_date': self.today - timedelta(days=5),
            'invoice_line_ids': [(0, 0, {
                'name': 'AF first period', 'quantity': 1, 'price_unit': 1200.0})]})
        self.invoice.action_post()
        self.assertEqual(self.invoice.payment_state, 'not_paid', '前提：首期發票未付')

    def test_unpaid_first_invoice_enters_dunning(self):
        self.Sub._sh_create_auto_invoice()

        self.assertEqual(self.sub.woow_consecutive_payment_failures, 1,
                         '未付的首期發票要進 dunning，不能等到一年後')
        self.assertEqual(self.sub.sh_date_of_next_payment, self.next_payment)

    def test_topped_up_wallet_settles_it_without_moving_the_next_payment(self):
        self.sub.write({'woow_consecutive_payment_failures': 2})
        _wallet(self.partner, 1500.0)

        self.Sub._sh_create_auto_invoice()

        self.assertIn(self.invoice.payment_state, ('paid', 'in_payment'))
        self.assertEqual(self.sub.woow_consecutive_payment_failures, 0)
        self.assertEqual(self.sub.sh_date_of_next_payment, self.next_payment,
                         '這張發票付的是已經開始的這一期，不得再推一期')

    def test_draft_or_paid_invoices_are_not_chased(self):
        self.invoice.button_draft()
        self.Sub._sh_create_auto_invoice()
        self.assertEqual(self.sub.woow_consecutive_payment_failures, 0)
