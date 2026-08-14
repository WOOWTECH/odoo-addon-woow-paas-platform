"""Tests for #318: deleting a CloudService cancels + prorata-refunds its
subscription. Part A — the subscription-level settlement method."""
from unittest.mock import patch

from odoo.tests.common import TransactionCase
from ..models.cloud_service import _internal_provision


class _DeleteFixture(TransactionCase):
    """共用 fixture：建 plan(+product) / template / workspace / service，
    並用 service._create_and_confirm_subscription 造一張綁定的訂閱。"""

    def setUp(self):
        super().setUp()
        self.CloudService = self.env['woow_paas_platform.cloud_service']
        self.CSTemplate = self.env['woow_paas_platform.cloud_app_template']
        self.Workspace = self.env['woow_paas_platform.workspace']
        self.Plan = self.env['sh.subscription.plan']
        self.WalletTx = self.env['wallet.transaction']

        self.plan_paid = self._make_plan('CS Del Paid', 300.0, trial=False)
        self.plan_trial = self._make_plan('CS Del Trial', 200.0, trial=True)

        self.template = self.CSTemplate.create({
            'name': 'CS Del Tmpl', 'slug': 'cs-del',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'cs-del', 'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
        })
        self.owner = self.env['res.users'].create({
            'name': 'CS Del Owner', 'login': 'cs_del_owner',
            'email': 'cs_del_owner@example.test',
        })
        self.owner.groups_id = [(6, 0, [self.env.ref('base.group_user').id])]
        self.workspace = self.Workspace.create({
            'name': 'CS Del WS', 'owner_id': self.owner.id,
        })

    def _make_plan(self, name, price, trial):
        vals = {
            'name': name, 'sh_plan_price': price,
            'sh_duration': 1, 'sh_unit': 'month', 'sh_no_of_billing_cycle': -1,
            'sh_trial': trial,
        }
        if trial:
            vals.update({'sh_trial_duration': 7, 'sh_trial_unit': 'day'})
        plan = self.Plan.create(vals)
        self.env['product.template'].sudo().create({
            'name': name + ' Product', 'type': 'service',
            'sh_product_subscribe': True, 'sh_subscription_plan_id': plan.id,
            'list_price': price,
        })
        return plan

    def _new_service(self, name='CS Del Svc'):
        # 不設 helm_release_name/helm_namespace → unlink() 跳過 helm 清理，
        # 無需 mock operator。
        return _internal_provision(self.CloudService).create({
            'name': name, 'workspace_id': self.workspace.id,
            'template_id': self.template.id,
        })

    def _seed_sub(self, service, plan):
        partner = self.workspace.owner_id.partner_id
        service._create_and_confirm_subscription(partner, plan)
        return service.sudo().subscription_id

    def _refund_txns(self, partner):
        return self.WalletTx.search([
            ('partner_id', '=', partner.id),
            ('transaction_type', '=', 'refund'),
        ])

    def _mark_cycle_paid(self, sub):
        """模擬本週期已真扣款：建一筆 done 的 deduct wallet.transaction，
        讓 _has_paid_current_cycle 為真（#324 gate）。"""
        return self.WalletTx.create({
            'partner_id': sub.sh_partner_id.id,
            'transaction_type': 'deduct',
            'amount': -sub.sh_plan_price,
            'subscription_id': sub.id,
            'state': 'done',
        })


class TestSettleOnEntityDelete(_DeleteFixture):

    def test_paid_sub_cancelled_and_refunded(self):
        svc = self._new_service()
        sub = self._seed_sub(svc, self.plan_paid)
        self.assertEqual(sub.state, 'in_progress')
        partner = sub.sh_partner_id
        self._mark_cycle_paid(sub)  # #324：真已付才退點

        refund = sub._settle_on_entity_delete()

        self.assertEqual(sub.state, 'cancel')
        self.assertEqual(sub.sh_reason, 'Service deleted')
        # 當天刪除 → today == sh_start_date → 全額退（remaining == total）。
        self.assertEqual(refund, 300.0)
        txn = self._refund_txns(partner)
        self.assertEqual(len(txn), 1)
        self.assertEqual(txn.transaction_type, 'refund')
        self.assertEqual(txn.amount, 300.0)
        self.assertEqual(txn.subscription_id.id, sub.id)

    def test_trial_sub_cancelled_without_refund(self):
        svc = self._new_service()
        sub = self._seed_sub(svc, self.plan_trial)
        self.assertTrue(sub.sh_trial)
        partner = sub.sh_partner_id

        refund = sub._settle_on_entity_delete()

        self.assertEqual(sub.state, 'cancel')
        self.assertEqual(refund, 0.0)
        self.assertFalse(self._refund_txns(partner))

    def test_already_cancelled_sub_is_noop(self):
        svc = self._new_service()
        sub = self._seed_sub(svc, self.plan_paid)
        sub.sudo().write({'state': 'close'})
        partner = sub.sh_partner_id

        refund = sub._settle_on_entity_delete()

        self.assertEqual(refund, 0.0)
        self.assertEqual(sub.state, 'close')  # 未被改成 cancel
        self.assertFalse(self._refund_txns(partner))

    def test_partnerless_sub_cancelled_without_refund(self):
        """sh_partner_id 上游非 required；partner-less 的 in_progress sub 仍須
        被取消以止付，但無對象可退點（不得 crash）。用正常 seed 再 null
        partner，避免直接 create 撞上游其他 required 欄位。"""
        svc = self._new_service()
        sub = self._seed_sub(svc, self.plan_paid)
        sub.sudo().write({'sh_partner_id': False})
        self.assertFalse(sub.sh_partner_id)

        credited = sub._settle_on_entity_delete()

        self.assertEqual(sub.state, 'cancel')
        self.assertEqual(credited, 0.0)


class TestCloudServiceUnlinkSettlesSubscription(_DeleteFixture):

    def test_unlink_cancels_and_refunds_paid_sub(self):
        svc = self._new_service()
        sub = self._seed_sub(svc, self.plan_paid)
        partner = sub.sh_partner_id
        self.assertEqual(sub.state, 'in_progress')
        self._mark_cycle_paid(sub)  # #324：真已付才退點

        svc.unlink()

        self.assertFalse(svc.exists())
        self.assertTrue(sub.exists())            # 訂閱 record 存活
        self.assertEqual(sub.state, 'cancel')    # 已停止計費
        txn = self._refund_txns(partner)
        self.assertEqual(len(txn), 1)
        self.assertEqual(txn.amount, 300.0)
        self.assertEqual(txn.subscription_id.id, sub.id)

    def test_unlink_trial_sub_cancelled_no_refund(self):
        svc = self._new_service()
        sub = self._seed_sub(svc, self.plan_trial)
        partner = sub.sh_partner_id

        svc.unlink()

        self.assertEqual(sub.state, 'cancel')
        self.assertFalse(self._refund_txns(partner))

    def test_unlink_service_without_subscription_is_clean(self):
        svc = self._new_service()
        self.assertFalse(svc.subscription_id)
        svc.unlink()
        self.assertFalse(svc.exists())

    def test_helm_failure_aborts_delete_and_preserves_subscription(self):
        """原子性：helm fail-loud（_cleanup_helm→False）時 unlink raise，
        訂閱維持 in_progress、無退點、record 保留。"""
        from odoo.exceptions import UserError
        svc = _internal_provision(self.CloudService).create({
            'name': 'CS Del Helm', 'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'helm_release_name': 'svc-deadbeef',
            'helm_namespace': 'paas-ws-stg-cafef00d',
        })
        sub = self._seed_sub(svc, self.plan_paid)
        partner = sub.sh_partner_id

        with patch.object(type(svc), '_cleanup_helm', return_value=False):
            with self.assertRaises(UserError):
                svc.unlink()

        self.assertTrue(svc.exists())
        self.assertEqual(sub.state, 'in_progress')
        self.assertFalse(self._refund_txns(partner))


class TestSmartHomeUnlinkSettlesSubscription(TransactionCase):
    """#322: 刪 SmartHome 聯動取消訂閱＋按比例退點（wiring 驗證）。"""

    def setUp(self):
        super().setUp()
        self.SmartHome = self.env['woow_paas_platform.smart_home']
        self.Plan = self.env['sh.subscription.plan']
        self.WalletTx = self.env['wallet.transaction']
        self.plan = self.Plan.create({
            'name': 'SH Del Plan', 'sh_plan_price': 300.0, 'sh_duration': 1,
            'sh_unit': 'month', 'sh_no_of_billing_cycle': -1, 'sh_trial': False,
        })
        self.env['product.template'].sudo().create({
            'name': 'SH Del Plan Product', 'type': 'service',
            'sh_product_subscribe': True, 'sh_subscription_plan_id': self.plan.id,
            'list_price': 300.0,
        })
        self.owner = self.env['res.users'].create({
            'name': 'SH Del Owner', 'login': 'sh_del_owner',
            'email': 'sh_del_owner@example.test',
        })
        self.owner.groups_id = [(6, 0, [self.env.ref('base.group_user').id])]
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'SH Del WS', 'owner_id': self.owner.id,
        })
        self.smart_home = self.SmartHome.create({
            'name': 'SH Del Home', 'workspace_id': self.workspace.id,
        })

    def test_unlink_cancels_and_refunds_paid_sub(self):
        partner = self.workspace.owner_id.partner_id
        self.smart_home._create_and_confirm_subscription(partner, self.plan)
        sub = self.smart_home.sudo().subscription_id
        self.assertEqual(sub.state, 'in_progress')
        # #324：真已付才退點 — seed 一筆 done deduct
        self.WalletTx.create({
            'partner_id': partner.id, 'transaction_type': 'deduct',
            'amount': -sub.sh_plan_price, 'subscription_id': sub.id,
            'state': 'done',
        })

        self.smart_home.unlink()

        self.assertTrue(sub.exists())
        self.assertEqual(sub.state, 'cancel')
        txn = self.WalletTx.search([
            ('partner_id', '=', partner.id),
            ('transaction_type', '=', 'refund')])
        self.assertEqual(len(txn), 1)
        self.assertEqual(txn.amount, 300.0)
        self.assertEqual(txn.subscription_id.id, sub.id)


class TestSecurityAccessUnlinkSettlesSubscription(TransactionCase):
    """#322: 刪 SecurityAccess 聯動取消訂閱＋按比例退點（wiring 驗證）。"""

    def setUp(self):
        super().setUp()
        self.SA = self.env['woow_paas_platform.security_access']
        self.Plan = self.env['sh.subscription.plan']
        self.WalletTx = self.env['wallet.transaction']
        self.plan = self.Plan.create({
            'name': 'SA Del Plan', 'sh_plan_price': 300.0, 'sh_duration': 1,
            'sh_unit': 'month', 'sh_no_of_billing_cycle': -1, 'sh_trial': False,
            'sa_max_routes': 3,
        })
        # SA fixture 慣例：用 product.product（非 product.template）
        self.env['product.product'].sudo().create({
            'name': 'SA Del Plan Product', 'type': 'service',
            'sh_product_subscribe': True, 'sh_subscription_plan_id': self.plan.id,
        })
        # journal 前置（_create_and_confirm_subscription 需要；鏡像既有 SA fixture）
        company = self.env.company.sudo()
        if not company.sh_journal_id:
            journal = self.env['account.journal'].sudo().search(
                [('type', '=', 'sale')], limit=1)
            if journal:
                company.sh_journal_id = journal.id
        self.owner = self.env['res.users'].create({
            'name': 'SA Del Owner', 'login': 'sa_del_owner',
            'email': 'sa_del_owner@example.test',
        })
        self.owner.groups_id = [(6, 0, [self.env.ref('base.group_user').id])]
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'SA Del WS', 'owner_id': self.owner.id,
        })
        self.sa = self.SA.create({
            'name': 'SA Del Gateway', 'workspace_id': self.workspace.id,
        })

    def test_unlink_cancels_and_refunds_paid_sub(self):
        partner = self.workspace.owner_id.partner_id
        self.sa._create_and_confirm_subscription(partner, self.plan)
        sub = self.sa.sudo().subscription_id
        self.assertEqual(sub.state, 'in_progress')
        # #324：真已付才退點 — seed 一筆 done deduct
        self.WalletTx.create({
            'partner_id': partner.id, 'transaction_type': 'deduct',
            'amount': -sub.sh_plan_price, 'subscription_id': sub.id,
            'state': 'done',
        })

        self.sa.unlink()

        self.assertTrue(sub.exists())
        self.assertEqual(sub.state, 'cancel')
        txn = self.WalletTx.search([
            ('partner_id', '=', partner.id),
            ('transaction_type', '=', 'refund')])
        self.assertEqual(len(txn), 1)
        self.assertEqual(txn.amount, 300.0)
        self.assertEqual(txn.subscription_id.id, sub.id)
