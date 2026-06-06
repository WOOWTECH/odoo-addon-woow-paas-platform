"""SecurityAccess must be enforced by the subscription lifecycle cron.

SmartHome historically lacked cron coverage; SecurityAccess must be in the
cron's scan list so expired/cancelled subscriptions suspend the gateway and
renewed subscriptions resume it — same lifecycle CloudService / SmartHome get.
"""
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase

MOCK_GET_CLIENT = (
    'odoo.addons.woow_paas_platform.models.security_access.'
    'get_paas_operator_client'
)


class TestSecurityAccessCron(TransactionCase):
    """SA 必須在 subscription lifecycle cron 的掃描清單內。"""

    def setUp(self):
        super().setUp()
        self.SecurityAccess = self.env['woow_paas_platform.security_access']
        self.CronModel = self.env[
            'woow_paas_platform.subscription_lifecycle_cron']
        self.workspace = self.env['woow_paas_platform.workspace'].create(
            {'name': 'SA Cron WS'})

    def _make_subscription(self, sub_state, **sub_kwargs):
        """Build a valid sh.subscription.subscription record.

        sh.subscription.subscription requires product_id +
        sh_subscription_plan_id (see test_subscription_lifecycle.py's
        _mock_subscription); a bare partner+state create raises. We mirror
        that helper to get a real, persistable subscription, then force the
        target state to bypass the normal billing workflow.
        """
        Plan = self.env['sh.subscription.plan']
        plan = Plan.search([], limit=1)
        if not plan:
            plan = Plan.create({
                'name': 'SA Cron Plan',
                'sh_duration': 1,
                'sh_unit': 'month',
                'sh_plan_price': 100,
                'sh_no_of_billing_cycle': -1,
            })

        ProductTemplate = self.env['product.template']
        product_tmpl = ProductTemplate.search([
            ('sh_product_subscribe', '=', True),
            ('sh_subscription_plan_id', '=', plan.id),
        ], limit=1)
        if not product_tmpl:
            product_tmpl = ProductTemplate.create({
                'name': 'SA Cron Subscription Product',
                'type': 'service',
                'sh_product_subscribe': True,
                'sh_subscription_plan_id': plan.id,
                'list_price': 100,
            })

        sub_vals = {
            'sh_partner_id': self.env.user.partner_id.id,
            'state': sub_state,
            'product_id': product_tmpl.product_variant_id.id,
            'sh_subscription_plan_id': plan.id,
        }
        sub_vals.update(sub_kwargs)
        sub = self.env['sh.subscription.subscription'].create(sub_vals)
        # Force state (bypass normal workflow for testing).
        if sub_state != 'draft':
            sub.write({'state': sub_state})
        return sub

    def _make_sa_with_sub(self, sub_state, **sub_kwargs):
        sa = self.SecurityAccess.create({
            'name': 'Cron Gateway',
            'workspace_id': self.workspace.id,
        })
        # route 先建（sa 還是 pending 不會觸發 operator sync），再轉 active
        self.env['woow_paas_platform.security_access_route'].create({
            'security_access_id': sa.id,
            'subdomain_prefix': sa.canonical_name,
            'is_primary': True,
        })
        sa.write({'state': 'active', 'tunnel_id': 'tun-cron'})
        sub = self._make_subscription(sub_state, **sub_kwargs)
        sa.write({'subscription_id': sub.id})
        return sa

    def test_cron_suspends_sa_past_grace(self):
        sa = self._make_sa_with_sub(
            'expire',
            sh_end_date=date.today() - timedelta(days=10),
        )
        mock_client = MagicMock()
        mock_client.sync_tunnel_routes.return_value = {'routes': []}
        with patch(MOCK_GET_CLIENT, return_value=mock_client):
            self.CronModel._cron_enforce_subscription_lifecycle()
        sa.invalidate_recordset()
        self.assertEqual(sa.state, 'suspended')

    def test_cron_resumes_sa_when_subscription_active(self):
        sa = self._make_sa_with_sub('in_progress')
        sa.write({'state': 'suspended', 'pre_suspend_state': 'active'})
        mock_client = MagicMock()
        mock_client.sync_tunnel_routes.return_value = {'routes': []}
        with patch(MOCK_GET_CLIENT, return_value=mock_client):
            self.CronModel._cron_enforce_subscription_lifecycle()
        sa.invalidate_recordset()
        self.assertEqual(sa.state, 'active')

    def test_cron_skips_grace_period(self):
        sa = self._make_sa_with_sub(
            'expire',
            sh_end_date=date.today() - timedelta(days=3),  # within 7d grace
        )
        mock_client = MagicMock()
        mock_client.sync_tunnel_routes.return_value = {'routes': []}
        with patch(MOCK_GET_CLIENT, return_value=mock_client):
            self.CronModel._cron_enforce_subscription_lifecycle()
        sa.invalidate_recordset()
        self.assertEqual(sa.state, 'active')

    def test_cron_does_not_flip_deleting_back_to_suspended(self):
        """已清理（deleting）的 SA，後續 cron（sub 仍 cancel）不得翻回 suspended。

        branch 4 / _do_suspend 對 'deleting' 須 no-op，避免逆向狀態抖動
        （finding：deleting→suspended state thrash）。
        """
        sa = self._make_sa_with_sub('cancel')
        # 模擬已經跑過 cleanup 的終態：tunnel 已清、state=deleting
        sa.write({'state': 'deleting', 'tunnel_id': False})
        mock_client = MagicMock()
        mock_client.sync_tunnel_routes.return_value = {'routes': []}
        with patch(MOCK_GET_CLIENT, return_value=mock_client):
            self.CronModel._cron_enforce_subscription_lifecycle()
        sa.invalidate_recordset()
        self.assertEqual(sa.state, 'deleting')
        # 不應對 operator 重打 suspend 的 503 sync
        mock_client.sync_tunnel_routes.assert_not_called()
