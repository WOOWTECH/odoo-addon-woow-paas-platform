"""商用驗收 AUDITFIX：含稅發票 PDF 下載、儀表板「下期帳單」。

## tax-invoice-pdf-403（X-BIL-006）

/woow 發票明細的「下載 PDF」連到原生 ``/report/pdf/account.account_invoices/<id>``，以租戶
（portal）身分渲染；發票行掛了稅（政策 A 起所有新發票都有 5% 稅），報表讀 ``account.tax`` ⇒
AccessError ⇒ 403。修法：平台自己的下載端點，先驗發票屬於目前使用者，再以 sudo 渲染；不給
portal 全域的 ``account.tax`` 讀取權。

## dashboard-next-bill-wrong（FU2-U-plat-ux-3／-4／-5）

``_billing_block`` 拿「最近開始」的那一筆訂閱當 primary：``next_bill_date`` 與帳期來自它，
``next_bill_estimate`` 卻是全部訂閱合計（實測：日期 10-21 對上 40,400，實際 10-18 只扣 30,650）；
試用中的訂閱 ``sh_start_date``＝試用結束次日、``cycle_end``＝next_payment−1 ⇒ 起日大於迄日。
工作區與成員數把已封存的工作區、同一個人在多個工作區都算進去（107 vs 16）。
"""
from datetime import date, timedelta

from odoo.tests.common import HttpCase, new_test_user, tagged


@tagged('post_install', '-at_install')
class TestTenantInvoicePdfAuditfix(HttpCase):

    def setUp(self):
        super().setUp()
        self.owner = new_test_user(
            self.env, login='pdf_owner_auditfix', groups='base.group_portal',
            password='pdf_owner_auditfix', name='PDF Owner')
        self.other = new_test_user(
            self.env, login='pdf_other_auditfix', groups='base.group_portal',
            password='pdf_other_auditfix', name='PDF Other')
        tax = self.env['account.tax'].sudo().create({
            'name': 'Auditfix PDF 5% (included)', 'amount_type': 'percent', 'amount': 5.0,
            'type_tax_use': 'sale', 'price_include_override': 'tax_included'})
        self.invoice = self.env['account.move'].sudo().create({
            'move_type': 'out_invoice',
            'partner_id': self.owner.partner_id.id,
            'invoice_line_ids': [(0, 0, {
                'name': 'Auditfix plan cycle', 'quantity': 1, 'price_unit': 315.0,
                'tax_ids': [(6, 0, tax.ids)]})],
        })
        self.assertTrue(self.invoice.invoice_line_ids.tax_ids, '前提：發票行有稅')

    def _get(self, url):
        return self.url_open(url, allow_redirects=False)

    def test_owner_downloads_a_taxed_invoice(self):
        self.authenticate('pdf_owner_auditfix', 'pdf_owner_auditfix')

        response = self._get('/api/billing/invoices/%s/pdf' % self.invoice.id)

        self.assertEqual(response.status_code, 200, response.text[:500])
        # 測試模式下 Odoo 以 HTML 代替 PDF（沒有 wkhtmltopdf 呼叫）；正式環境是 application/pdf。
        self.assertIn(response.headers['Content-Type'].split(';')[0],
                      ('application/pdf', 'text/html'))
        self.assertIn('attachment', response.headers.get('Content-Disposition', ''))
        self.assertTrue(response.content)

    def test_other_tenant_cannot_download(self):
        self.authenticate('pdf_other_auditfix', 'pdf_other_auditfix')

        response = self._get('/api/billing/invoices/%s/pdf' % self.invoice.id)

        self.assertEqual(response.status_code, 404)

    def test_missing_invoice_is_404(self):
        self.authenticate('pdf_owner_auditfix', 'pdf_owner_auditfix')

        response = self._get('/api/billing/invoices/999999999/pdf')

        self.assertEqual(response.status_code, 404)


@tagged('post_install', '-at_install')
class TestDashboardNextBillAuditfix(HttpCase):

    def setUp(self):
        super().setUp()
        self.authenticate('admin', 'admin')
        self.partner = self.env.ref('base.partner_admin')
        self.admin_user = self.env.ref('base.user_admin')
        self.Sub = self.env['sh.subscription.subscription'].sudo()
        self.env['woow_paas_platform.workspace_access'].sudo().search(
            [('user_id', '=', self.admin_user.id)]).unlink()
        self.Sub.search([
            ('sh_partner_id', '=', self.partner.id), ('state', '=', 'in_progress'),
        ]).write({'state': 'close'})
        self.today = date.today()

    def _plan(self, price):
        return self.env['sh.subscription.plan'].sudo().create({
            'name': 'Auditfix Dashboard Plan %s' % price,
            'sh_duration': 1, 'sh_unit': 'month', 'sh_plan_price': price,
            'sh_no_of_billing_cycle': -1,
        })

    def _sub(self, price, start, next_payment, trial_end=None):
        vals = {
            'sh_partner_id': self.partner.id,
            'sh_subscription_plan_id': self._plan(price).id,
            'sh_plan_price': price, 'sh_recurrency': 1, 'sh_unit': 'month',
            'sh_start_date': start,
            'sh_date_of_next_payment': next_payment,
            'state': 'in_progress',
        }
        if trial_end:
            vals.update({'sh_trial': True, 'sh_trial_end_date': trial_end})
        return self.Sub.create(vals)

    def _billing(self):
        body = self.url_open(
            '/api/dashboard/summary',
            data='{"jsonrpc":"2.0","method":"call","params":{}}',
            headers={'Content-Type': 'application/json'},
        ).json()
        self.assertTrue(body['result']['success'], body)
        return body['result']['data']

    def test_next_bill_is_the_earliest_charge_and_only_its_amount(self):
        """FU2-U-plat-ux-3：日期取最早的扣款日，金額只算那一天到期的訂閱。"""
        soon = self.today + timedelta(days=4)
        # 試用中、最近才開通（sh_start_date 最大）——舊邏輯會拿它當 primary。
        self._sub(1000.0, start=self.today + timedelta(days=8),
                  next_payment=self.today + timedelta(days=8),
                  trial_end=self.today + timedelta(days=7))
        self._sub(300.0, start=self.today - timedelta(days=26), next_payment=soon)
        self._sub(200.0, start=self.today - timedelta(days=40), next_payment=soon)

        billing = self._billing()['billing']

        self.assertEqual(billing['next_bill_date'], soon.isoformat())
        self.assertAlmostEqual(billing['next_bill_estimate'], 500.0, places=2)
        self.assertAlmostEqual(billing['recurring_total_estimate'], 1500.0, places=2)

    def test_trial_cycle_never_starts_after_it_ends(self):
        """FU2-U-plat-ux-5：只有試用中的訂閱時，帳期是試用期，起日不得晚於迄日。"""
        trial_end = self.today + timedelta(days=3)
        self._sub(1000.0, start=trial_end + timedelta(days=1),
                  next_payment=trial_end + timedelta(days=1), trial_end=trial_end)

        billing = self._billing()['billing']

        self.assertLessEqual(billing['current_cycle_start'], billing['current_cycle_end'])
        self.assertEqual(billing['current_cycle_end'], trial_end.isoformat())
        self.assertEqual(billing['next_bill_date'],
                         (trial_end + timedelta(days=1)).isoformat())

    def test_paid_cycle_is_the_current_period_of_the_earliest_charge(self):
        """付費訂閱的帳期＝[next_payment − 一期, next_payment − 1]，不是開通日。"""
        next_payment = self.today + timedelta(days=10)
        self._sub(300.0, start=self.today - timedelta(days=200), next_payment=next_payment)

        billing = self._billing()['billing']

        self.assertEqual(billing['current_cycle_end'],
                         (next_payment - timedelta(days=1)).isoformat())
        self.assertLessEqual(billing['current_cycle_start'], self.today.isoformat())
        self.assertGreater(billing['current_cycle_start'],
                           (self.today - timedelta(days=200)).isoformat())

    def test_archived_workspaces_and_repeat_members_are_not_counted(self):
        """FU2-U-plat-ux-4：封存的工作區不算；成員數是不重複的人數。"""
        Workspace = self.env['woow_paas_platform.workspace'].sudo().with_user(self.admin_user)
        ws_a = Workspace.create({'name': 'Auditfix Dash A'})
        ws_b = Workspace.create({'name': 'Auditfix Dash B'})
        archived = Workspace.create({'name': 'Auditfix Dash Archived'})
        archived.sudo().write({'state': 'archived'})
        member = new_test_user(self.env, login='dash_member_auditfix', name='Dash Member')
        Access = self.env['woow_paas_platform.workspace_access'].sudo()
        for ws in (ws_a, ws_b):
            Access.create({'workspace_id': ws.id, 'user_id': member.id, 'role': 'user'})

        data = self._billing()

        self.assertEqual(data['workspaces']['total'], 2)
        self.assertEqual(data['members']['total'], 2, 'admin＋member，不重複計算')
        self.assertEqual(data['members']['active'], 2)
