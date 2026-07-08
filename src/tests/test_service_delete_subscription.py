"""Tests for #318: deleting a CloudService cancels + prorata-refunds its
subscription. Part A — the subscription-level settlement method."""
from unittest.mock import patch

from odoo.tests.common import TransactionCase


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
        return self.CloudService.create({
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


class TestSettleOnEntityDelete(_DeleteFixture):

    def test_paid_sub_cancelled_and_refunded(self):
        svc = self._new_service()
        sub = self._seed_sub(svc, self.plan_paid)
        self.assertEqual(sub.state, 'in_progress')
        partner = sub.sh_partner_id

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
