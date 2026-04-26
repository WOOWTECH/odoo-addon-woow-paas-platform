"""Tests for Subscription Lifecycle Mixin and Cron."""
from datetime import date, timedelta
from unittest.mock import patch, MagicMock

from odoo.tests.common import TransactionCase
from odoo.exceptions import UserError


class TestSubscriptionLifecycleMixin(TransactionCase):
    """Test subscription lifecycle methods on CloudService and SmartHome."""

    def setUp(self):
        super().setUp()
        self.Workspace = self.env['woow_paas_platform.workspace']
        self.CloudService = self.env['woow_paas_platform.cloud_service']
        self.SmartHome = self.env['woow_paas_platform.smart_home']
        self.Template = self.env['woow_paas_platform.cloud_app_template']

        self.workspace = self.Workspace.create({'name': 'Test WS'})

        # Non-admin internal user for tests that exercise _check_operation_allowed
        # (admins bypass all subscription restrictions, so blocked-operation tests
        # need a regular user to actually trigger the UserError).
        self.regular_user = self.env['res.users'].create({
            'login': 'sub_lifecycle_regular',
            'name': 'Regular Lifecycle User',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })

        self.template = self.Template.create({
            'name': 'Test App',
            'slug': 'test-app',
            'helm_repo_url': '',
            'helm_chart_name': 'oci://test/chart',
            'helm_chart_version': '1.0.0',
        })

    def _create_service(self, **kwargs):
        vals = {
            'name': 'Test Service',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            **kwargs,
        }
        return self.CloudService.create(vals)

    def _create_smarthome(self, **kwargs):
        vals = {
            'name': 'Test Home',
            'workspace_id': self.workspace.id,
            **kwargs,
        }
        return self.SmartHome.create(vals)

    # ----------------------------------------------------------------
    # _is_subscription_active
    # ----------------------------------------------------------------

    def test_no_subscription_is_active(self):
        """Entity without subscription should be considered active (legacy)."""
        service = self._create_service()
        self.assertTrue(service._is_subscription_active())

    def test_active_subscription_is_active(self):
        """Entity with in_progress subscription should be active."""
        service = self._create_service()
        sub = self._mock_subscription(service, state='in_progress')
        self.assertTrue(service._is_subscription_active())

    def test_expired_subscription_not_active(self):
        """Entity with expired subscription should not be active."""
        service = self._create_service()
        sub = self._mock_subscription(service, state='expire')
        self.assertFalse(service._is_subscription_active())

    # ----------------------------------------------------------------
    # _is_in_grace_period
    # ----------------------------------------------------------------

    def test_grace_period_within(self):
        """Expired 3 days ago with 7-day grace = in grace period."""
        service = self._create_service()
        sub = self._mock_subscription(
            service, state='expire',
            sh_end_date=date.today() - timedelta(days=3),
        )
        self.assertTrue(service._is_in_grace_period())

    def test_grace_period_expired(self):
        """Expired 10 days ago with 7-day grace = past grace period."""
        service = self._create_service()
        sub = self._mock_subscription(
            service, state='expire',
            sh_end_date=date.today() - timedelta(days=10),
        )
        self.assertFalse(service._is_in_grace_period())

    def test_grace_period_active_sub(self):
        """Active subscription = not in grace period."""
        service = self._create_service()
        sub = self._mock_subscription(service, state='in_progress')
        self.assertFalse(service._is_in_grace_period())

    # ----------------------------------------------------------------
    # _is_past_grace_period
    # ----------------------------------------------------------------

    def test_past_grace_cancelled(self):
        """Cancelled subscription = always past grace."""
        service = self._create_service()
        sub = self._mock_subscription(service, state='cancel')
        self.assertTrue(service._is_past_grace_period())

    def test_past_grace_expired_beyond(self):
        """Expired beyond grace days = past grace."""
        service = self._create_service()
        sub = self._mock_subscription(
            service, state='expire',
            sh_end_date=date.today() - timedelta(days=10),
        )
        self.assertTrue(service._is_past_grace_period())

    # ----------------------------------------------------------------
    # _check_operation_allowed
    # ----------------------------------------------------------------

    def test_operation_allowed_no_sub(self):
        """No subscription = operations allowed."""
        service = self._create_service()
        service._check_operation_allowed(operation='write')  # Should not raise

    def test_operation_allowed_active(self):
        """Active subscription = write allowed."""
        service = self._create_service()
        self._mock_subscription(service, state='in_progress')
        service._check_operation_allowed(operation='write')  # Should not raise

    def test_operation_blocked_grace_write(self):
        """Grace period = write blocked."""
        service = self._create_service()
        self._mock_subscription(
            service, state='expire',
            sh_end_date=date.today() - timedelta(days=3),
        )
        service = service.with_user(self.regular_user)
        with self.assertRaises(UserError):
            service._check_operation_allowed(operation='write')

    def test_operation_allowed_grace_read(self):
        """Grace period = read allowed."""
        service = self._create_service()
        self._mock_subscription(
            service, state='expire',
            sh_end_date=date.today() - timedelta(days=3),
        )
        service._check_operation_allowed(operation='read')  # Should not raise

    def test_operation_blocked_past_grace(self):
        """Past grace = all operations blocked."""
        service = self._create_service()
        self._mock_subscription(
            service, state='expire',
            sh_end_date=date.today() - timedelta(days=10),
        )
        service = service.with_user(self.regular_user)
        with self.assertRaises(UserError):
            service._check_operation_allowed(operation='read')

    def test_admin_bypasses_restriction(self):
        """Admin user should bypass all subscription restrictions."""
        service = self._create_service()
        self._mock_subscription(
            service, state='expire',
            sh_end_date=date.today() - timedelta(days=10),
        )
        # Switch to admin user
        service = service.with_user(self.env.ref('base.user_admin'))
        service._check_operation_allowed(operation='write')  # Should not raise

    # ----------------------------------------------------------------
    # _do_suspend / _do_resume (CloudService)
    # ----------------------------------------------------------------

    @patch('odoo.addons.woow_paas_platform.services.paas_operator.get_paas_operator_client')
    def test_service_suspend(self, mock_get_client):
        """Suspend should set state to suspended and call scale(0)."""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        service = self._create_service(
            state='running',
            helm_release_name='svc-test',
            helm_namespace='paas-ws-test',
        )
        service._do_suspend()

        self.assertEqual(service.state, 'suspended')
        self.assertEqual(service.pre_suspend_state, 'running')
        mock_client.scale_release.assert_called_once_with(
            'svc-test', 'paas-ws-test', replicas=0,
        )

    @patch('odoo.addons.woow_paas_platform.services.paas_operator.get_paas_operator_client')
    def test_service_resume(self, mock_get_client):
        """Resume should restore pre-suspend state and call scale(1)."""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        service = self._create_service(
            state='suspended',
            pre_suspend_state='running',
            helm_release_name='svc-test',
            helm_namespace='paas-ws-test',
        )
        service._do_resume()

        self.assertEqual(service.state, 'running')
        self.assertFalse(service.pre_suspend_state)
        mock_client.scale_release.assert_called_once_with(
            'svc-test', 'paas-ws-test', replicas=1,
        )

    def test_service_suspend_already_suspended(self):
        """Suspending an already suspended service should be a no-op."""
        service = self._create_service(state='suspended')
        service._do_suspend()
        self.assertEqual(service.state, 'suspended')

    # ----------------------------------------------------------------
    # _do_suspend / _do_resume (SmartHome)
    # ----------------------------------------------------------------

    @patch('odoo.addons.woow_paas_platform.models.smart_home.get_paas_operator_client')
    def test_smarthome_suspend(self, mock_get_client):
        """Suspend should set state to suspended and configure tunnel to 503."""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        home = self._create_smarthome(
            state='active',
            tunnel_id='tun-123',
            subdomain='test-home',
        )
        home._do_suspend()

        self.assertEqual(home.state, 'suspended')
        self.assertEqual(home.pre_suspend_state, 'active')
        mock_client.configure_tunnel.assert_called_once()
        # Suspend routes the tunnel to a 503 sentinel so user traffic is blocked.
        call_args = mock_client.configure_tunnel.call_args
        self.assertEqual(call_args.kwargs.get('service_url'), 'http_status:503')

    @patch('odoo.addons.woow_paas_platform.models.smart_home.get_paas_operator_client')
    def test_smarthome_resume(self, mock_get_client):
        """Resume should restore state and configure tunnel back."""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        home = self._create_smarthome(
            state='suspended',
            pre_suspend_state='active',
            tunnel_id='tun-123',
            subdomain='test-home',
            original_service_url='http://localhost:8123',
        )
        home._do_resume()

        self.assertEqual(home.state, 'active')
        self.assertFalse(home.pre_suspend_state)
        self.assertTrue(mock_client.configure_tunnel.called)

    # ----------------------------------------------------------------
    # _get_subscription_info
    # ----------------------------------------------------------------

    def test_subscription_info_none(self):
        """No subscription should return None."""
        service = self._create_service()
        self.assertIsNone(service._get_subscription_info())

    def test_subscription_info_present(self):
        """Active subscription should return info dict."""
        service = self._create_service()
        self._mock_subscription(service, state='in_progress')
        info = service._get_subscription_info()
        self.assertIsNotNone(info)
        self.assertEqual(info['state'], 'in_progress')

    # ----------------------------------------------------------------
    # Helper
    # ----------------------------------------------------------------

    def _mock_subscription(self, entity, state='in_progress', **kwargs):
        """Create a minimal mock subscription record on the entity.

        We create a real sh.subscription.subscription record if the model
        is available, otherwise mock it.
        """
        Subscription = self.env['sh.subscription.subscription']

        sub_vals = {
            'sh_partner_id': self.env.user.partner_id.id,
            'state': state,
        }

        # Set dates for grace period tests
        if 'sh_end_date' in kwargs:
            sub_vals['sh_end_date'] = kwargs['sh_end_date']
        if 'sh_start_date' in kwargs:
            sub_vals['sh_start_date'] = kwargs['sh_start_date']

        # Try to create a real subscription; need product + plan
        Plan = self.env['sh.subscription.plan']
        plan = Plan.search([], limit=1)
        if not plan:
            plan = Plan.create({
                'name': 'Test Plan',
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
                'name': 'Test Subscription Product',
                'type': 'service',
                'sh_product_subscribe': True,
                'sh_subscription_plan_id': plan.id,
                'list_price': 100,
            })

        product = product_tmpl.product_variant_id

        sub_vals.update({
            'product_id': product.id,
            'sh_subscription_plan_id': plan.id,
        })

        sub = Subscription.create(sub_vals)

        # Force state (bypass normal workflow for testing)
        if state != 'draft':
            sub.write({'state': state})

        # Set additional fields
        for key, val in kwargs.items():
            if hasattr(sub, key):
                sub.write({key: val})

        entity.write({'subscription_id': sub.id})
        return sub


class TestSubscriptionLifecycleCron(TransactionCase):
    """Test the subscription lifecycle enforcement cron."""

    def setUp(self):
        super().setUp()
        self.Workspace = self.env['woow_paas_platform.workspace']
        self.CloudService = self.env['woow_paas_platform.cloud_service']
        self.Template = self.env['woow_paas_platform.cloud_app_template']
        self.CronModel = self.env['woow_paas_platform.subscription_lifecycle_cron']

        self.workspace = self.Workspace.create({'name': 'Cron Test WS'})
        self.template = self.Template.create({
            'name': 'Cron Test App',
            'slug': 'cron-test',
            'helm_repo_url': '',
            'helm_chart_name': 'oci://test/chart',
            'helm_chart_version': '1.0.0',
        })

    @patch('odoo.addons.woow_paas_platform.services.paas_operator.get_paas_operator_client')
    def test_cron_suspends_past_grace(self, mock_get_client):
        """Cron should suspend services expired beyond grace period."""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        service = self.CloudService.create({
            'name': 'Expired Service',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'state': 'running',
            'helm_release_name': 'svc-test',
            'helm_namespace': 'paas-ws-test',
        })

        # Create expired subscription (10 days ago)
        helper = TestSubscriptionLifecycleMixin()
        helper.env = self.env
        sub = helper._mock_subscription(
            service, state='expire',
            sh_end_date=date.today() - timedelta(days=10),
            sh_start_date=date.today() - timedelta(days=40),
        )

        # Run cron
        self.CronModel._cron_enforce_subscription_lifecycle()

        service.invalidate_recordset()
        self.assertEqual(service.state, 'suspended')

    @patch('odoo.addons.woow_paas_platform.services.paas_operator.get_paas_operator_client')
    def test_cron_does_not_suspend_in_grace(self, mock_get_client):
        """Cron should NOT suspend services within grace period."""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        service = self.CloudService.create({
            'name': 'Grace Period Service',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'state': 'running',
            'helm_release_name': 'svc-grace',
            'helm_namespace': 'paas-ws-test',
        })

        helper = TestSubscriptionLifecycleMixin()
        helper.env = self.env
        sub = helper._mock_subscription(
            service, state='expire',
            sh_end_date=date.today() - timedelta(days=3),
            sh_start_date=date.today() - timedelta(days=33),
        )

        self.CronModel._cron_enforce_subscription_lifecycle()

        service.invalidate_recordset()
        self.assertEqual(service.state, 'running')  # Still running, within grace

    @patch('odoo.addons.woow_paas_platform.services.paas_operator.get_paas_operator_client')
    def test_cron_resumes_renewed(self, mock_get_client):
        """Cron should resume suspended services when subscription is renewed."""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        service = self.CloudService.create({
            'name': 'Renewed Service',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'state': 'suspended',
            'pre_suspend_state': 'running',
            'helm_release_name': 'svc-renew',
            'helm_namespace': 'paas-ws-test',
        })

        helper = TestSubscriptionLifecycleMixin()
        helper.env = self.env
        sub = helper._mock_subscription(
            service, state='in_progress',
            sh_start_date=date.today(),
            sh_end_date=date.today() + timedelta(days=30),
        )

        self.CronModel._cron_enforce_subscription_lifecycle()

        service.invalidate_recordset()
        self.assertEqual(service.state, 'running')
