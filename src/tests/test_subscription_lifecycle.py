"""Tests for Subscription Lifecycle Mixin and Cron."""
from datetime import date, timedelta
from unittest.mock import patch, MagicMock

from dateutil.relativedelta import relativedelta

from odoo.tests.common import TransactionCase
from odoo.exceptions import UserError
from ..models.cloud_service import _internal_provision


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
        return _internal_provision(self.CloudService).create(vals)

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

    def test_subscription_info_has_plan_id(self):
        """#368 PR-3：_get_subscription_info 須帶 plan_id，供 ChangePlanModal
        開啟即以 currentPlanId 定位目前方案（cycle×group×seats 由 PlanSelector
        反推）。無 plan_id 前端就無法預選高亮／擋 churn 成同方案。"""
        service = self._create_service()
        sub = self._mock_subscription(service, state='in_progress')
        info = service._get_subscription_info()
        self.assertIn('plan_id', info)
        self.assertEqual(info['plan_id'], sub.sh_subscription_plan_id.id)

    def test_subscription_info_plan_id_none_when_no_plan(self):
        """訂閱存在但無 sh_subscription_plan_id 時，plan_id 為 None（非炸）。"""
        service = self._create_service()
        sub = self._mock_subscription(service, state='in_progress')
        sub.write({'sh_subscription_plan_id': False})
        info = service._get_subscription_info()
        self.assertIn('plan_id', info)
        self.assertIsNone(info['plan_id'])

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

        # #519 提案 B：綁定守衛是 **transition guard**——綁上去的那一刻訂閱必須是
        # in_progress。production 也是這個順序（先綁一筆生效中的訂閱，之後它才過期／
        # 取消），所以這裡先以 in_progress 建立並綁定，最後才寫成受測的目標狀態。
        sub = Subscription.create(dict(sub_vals, state='in_progress'))

        # Set additional fields. 打錯的欄位名必須大聲炸掉：靜默丟棄會讓呼叫端在
        # 「我設過 sh_end_date」的假前提下跑斷言（例如 grace period 測試實際上
        # 從未設過到期日），測試變成恆綠且完全看不出來。
        for key, val in kwargs.items():
            if not hasattr(sub, key):
                self.fail(
                    "_mock_subscription: '%s' 不是 sh.subscription.subscription "
                    "的欄位。fixture 的欄位名打錯若被靜默丟棄，測試會在假前提下"
                    "通過。" % key)
            sub.write({key: val})

        entity.write({'subscription_id': sub.id})
        # 綁好之後才落到受測狀態（含 draft——語意上等同「這筆訂閱後來變成 draft」）。
        if state != 'in_progress':
            sub.write({'state': state})
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

        service = _internal_provision(self.CloudService).create({
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

        service = _internal_provision(self.CloudService).create({
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

        service = _internal_provision(self.CloudService).create({
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


class TestForceTrialDatesFromPlan(TransactionCase):
    """Cover the unconditional plan-driven trial-date write (issue #33).

    Pre-fix, ``_create_and_confirm_subscription`` relied on the onchange
    chain to populate sh_trial_end_date / sh_end_date, then only ran the
    explicit force-trial path on the dedup-collision branch. Two failure
    modes were observed on stg:

    - fresh trial sub (no dedup) → both sh_trial_end_date and sh_end_date
      NULL (subs 38, 39).
    - dedup-collision-only force path set sh_trial_end_date but not
      sh_end_date, which onchange then computed as ``start - 1 day`` (subs
      22, 36, 37).

    The fix removes the dedup gate and writes the full date triple
    (start, trial_end, end) plan-driven. These tests assert the triple is
    coherent for every code path SmartHome / CloudService can reach.
    """

    def setUp(self):
        super().setUp()
        self.SmartHome = self.env['woow_paas_platform.smart_home']
        self.Workspace = self.env['woow_paas_platform.workspace']
        self.Plan = self.env['sh.subscription.plan']

        # 7-day trial / 1-month billing cycle.
        self.plan_trial = self.Plan.create({
            'name': 'Trial 7d / Monthly',
            'sh_plan_price': 100.0,
            'sh_duration': 1,
            'sh_unit': 'month',
            'sh_no_of_billing_cycle': -1,
            'sh_trial': True,
            'sh_trial_duration': 7,
            'sh_trial_unit': 'day',
        })
        # No-trial control plan.
        self.plan_notrial = self.Plan.create({
            'name': 'No Trial / Monthly',
            'sh_plan_price': 200.0,
            'sh_duration': 1,
            'sh_unit': 'month',
            'sh_no_of_billing_cycle': -1,
            'sh_trial': False,
        })
        # _create_and_confirm_subscription resolves the subscription
        # product via product.template.sh_subscription_plan_id.
        for plan, label in [
            (self.plan_trial, 'Trial7'),
            (self.plan_notrial, 'NoTrial'),
        ]:
            self.env['product.template'].sudo().create({
                'name': 'Force-Trial Product %s' % label,
                'type': 'service',
                'sh_product_subscribe': True,
                'sh_subscription_plan_id': plan.id,
                'list_price': plan.sh_plan_price,
            })

        self.owner = self.env['res.users'].create({
            'login': 'force_trial_owner',
            'name': 'Force-Trial Owner',
            'email': 'force_trial_owner@example.test',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.workspace = self.Workspace.create({
            'name': 'Force-Trial WS',
            'owner_id': self.owner.id,
        })
        self.partner = self.owner.partner_id

    def _make_smart_home(self, name):
        return self.SmartHome.create({
            'name': name,
            'workspace_id': self.workspace.id,
        })

    def test_fresh_trial_plan_writes_full_date_triple(self):
        """Fresh trial sub (no dedup collision) gets sh_trial=True and a
        coherent (start, trial_end, end) date triple.

        This is the path that produced stg subs 38 / 39 with NULL dates
        pre-fix — the onchange chain did not populate them and the
        force-trial block was gated behind dedup-collision so it never
        ran.
        """
        smart_home = self._make_smart_home('Fresh-Trial Home')
        smart_home._create_and_confirm_subscription(
            self.partner, self.plan_trial,
        )
        sub = smart_home.sudo().subscription_id

        today = date.today()
        expected_trial_end = today + timedelta(days=7) - timedelta(days=1)
        expected_start = expected_trial_end + timedelta(days=1)
        # plan.sh_duration=1, sh_unit='month' → end = start + 1m − 1d.
        expected_end = (
            expected_start + relativedelta(months=1) - timedelta(days=1)
        )

        self.assertTrue(
            sub.sh_trial, 'fresh trial sub must be in trial state',
        )
        self.assertEqual(sub.sh_trial_duration, 7)
        self.assertEqual(sub.sh_trial_unit, 'day')
        self.assertEqual(
            sub.sh_trial_end_date, expected_trial_end,
            'sh_trial_end_date must be set on fresh trial sub (#33)',
        )
        self.assertEqual(sub.sh_start_date, expected_start)
        self.assertEqual(
            sub.sh_end_date, expected_end,
            'sh_end_date must be set on fresh trial sub (#33)',
        )
        self.assertEqual(sub.sh_date_of_next_payment, expected_start)

    def test_second_entity_same_partner_gets_own_trial(self):
        """#367 (per-entity): a second *entity* for the same partner is a
        brand-new service (its own ``subscription_id`` is empty), so it gets
        its OWN 7-day trial. The trial is a per-service one-time grant, not
        per-partner.

        This supersedes the earlier per-partner reading (a leader over-widening
        during transcription): Eugene's 2026-07-19 clarification on #367 is
        per-entity — every new service gets a trial; only same-service
        change-plan / suspend-resume does not restart it. The per-partner impl
        (which denied a second entity its trial) makes this test red — the
        load-bearing discriminator.
        """
        first = self._make_smart_home('First Home')
        first._create_and_confirm_subscription(self.partner, self.plan_trial)

        second = self._make_smart_home('Second Home')
        second._create_and_confirm_subscription(self.partner, self.plan_trial)
        sub = second.sudo().subscription_id

        today = date.today()
        expected_trial_end = today + timedelta(days=7) - timedelta(days=1)

        self.assertTrue(
            sub.sh_trial,
            '#367 per-entity: a second entity is a new service → own trial',
        )
        self.assertEqual(sub.sh_trial_end_date, expected_trial_end)
        # Trial has not started billing yet → sh_start_date is in the future.
        self.assertEqual(sub.sh_start_date, expected_trial_end + timedelta(days=1))
        self.assertGreaterEqual(sub.sh_end_date, sub.sh_start_date)

    def test_non_trial_plan_leaves_trial_fields_inert(self):
        """``plan.sh_trial=False`` must skip the trial helper entirely so
        sh_trial stays False and sh_trial_end_date stays empty. Removing
        the dedup gate must not accidentally enable trial on non-trial
        subs.

        Issue #39: also assert the active-path date triple is coherent —
        pre-fix the onchange chain produced sh_end_date = start - 32d on
        non-trial subs because cycle_delta was multiplied by
        sh_no_of_billing_cycle (-1, unlimited billing platform-wide).
        """
        smart_home = self._make_smart_home('No-Trial Home')
        smart_home._create_and_confirm_subscription(
            self.partner, self.plan_notrial,
        )
        sub = smart_home.sudo().subscription_id

        self.assertFalse(
            sub.sh_trial, 'non-trial plan must NOT produce trial sub',
        )
        self.assertFalse(sub.sh_trial_end_date)
        # Issue #39 invariants on the active date triple.
        self.assertGreaterEqual(
            sub.sh_end_date, sub.sh_start_date,
            'invariant: sh_end_date must never be earlier than sh_start_date',
        )
        cycle_delta = relativedelta(months=1)
        self.assertEqual(
            sub.sh_date_of_next_payment,
            sub.sh_start_date + cycle_delta,
            'next payment must be plan-driven (#39): start + cycle_delta',
        )


class TestForceActiveDatesFromPlan(TransactionCase):
    """Cover the unconditional plan-driven active-date write (issue #39).

    Pre-fix, ``_create_and_confirm_subscription`` only forced trial dates
    (PR #38, gated behind ``if plan.sh_trial:``). The non-trial path fell
    through to ``sh_subscription``'s onchange chain, which multiplies
    cycle_delta by ``sh_no_of_billing_cycle`` (set to -1 platform-wide for
    unlimited billing), producing sh_end_date = sh_start_date - 32d. UI
    countdown / grace-period readers that consume sh_end_date directly
    (rather than sh_date_of_next_payment) thus saw a negative remaining
    window the moment the sub was created. Refund clamps in
    ``_compute_unused_refund`` masked the financial side but the cosmetic
    side leaked to users.

    The fix adds a sibling helper ``_force_active_dates_from_plan`` that
    writes (start, end, next_payment) plan-driven for every non-trial sub,
    mirroring the PR #38 trial-side correction.
    """

    def setUp(self):
        super().setUp()
        self.SmartHome = self.env['woow_paas_platform.smart_home']
        self.Workspace = self.env['woow_paas_platform.workspace']
        self.Plan = self.env['sh.subscription.plan']

        # Non-trial 1-month plan with unlimited billing cycle, the shape
        # every platform plan ships with — this is the exact configuration
        # that triggers the onchange bug (1 * -1 * month = -1 month).
        self.plan_notrial = self.Plan.create({
            'name': 'No Trial / Monthly',
            'sh_plan_price': 200.0,
            'sh_duration': 1,
            'sh_unit': 'month',
            'sh_no_of_billing_cycle': -1,
            'sh_trial': False,
        })
        # _create_and_confirm_subscription resolves the subscription
        # product via product.template.sh_subscription_plan_id.
        self.env['product.template'].sudo().create({
            'name': 'Force-Active Product NoTrial',
            'type': 'service',
            'sh_product_subscribe': True,
            'sh_subscription_plan_id': self.plan_notrial.id,
            'list_price': self.plan_notrial.sh_plan_price,
        })

        # PR #28 hardening: non-admin owner so no implicit bypass.
        self.owner = self.env['res.users'].create({
            'login': 'force_active_owner',
            'name': 'Force-Active Owner',
            'email': 'force_active_owner@example.test',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.workspace = self.Workspace.create({
            'name': 'Force-Active WS',
            'owner_id': self.owner.id,
        })
        self.partner = self.owner.partner_id

    def _make_smart_home(self, name):
        return self.SmartHome.create({
            'name': name,
            'workspace_id': self.workspace.id,
        })

    def test_fresh_non_trial_plan_writes_active_date_triple(self):
        """Fresh non-trial sub gets a coherent (start, end, next_payment)
        triple from the plan, not the buggy onchange chain.

        Pre-fix:  end = start + 1 * -1 * month - 1d = start - 32d.
        Post-fix: end = start + 1 * month - 1d (no billing-cycle multiplier).
        """
        smart_home = self._make_smart_home('Fresh-NoTrial Home')
        smart_home._create_and_confirm_subscription(
            self.partner, self.plan_notrial,
        )
        sub = smart_home.sudo().subscription_id

        today = date.today()
        cycle_delta = relativedelta(months=1)
        expected_end = today + cycle_delta - timedelta(days=1)

        self.assertFalse(
            sub.sh_trial, 'non-trial plan must NOT produce trial sub',
        )
        self.assertEqual(sub.sh_start_date, today)
        self.assertEqual(
            sub.sh_end_date, expected_end,
            'sh_end_date must be plan-driven (#39): start + cycle_delta - 1d',
        )
        self.assertGreaterEqual(
            sub.sh_end_date, sub.sh_start_date,
            'invariant: sh_end_date must never be earlier than sh_start_date',
        )
        self.assertEqual(
            sub.sh_date_of_next_payment, today + cycle_delta,
            'next payment must be plan-driven (#39): start + cycle_delta',
        )
