"""Tests for Cloud Service model."""
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase
from odoo.exceptions import ValidationError


class TestCloudService(TransactionCase):
    """Test cases for woow_paas_platform.cloud_service model."""

    def setUp(self):
        """Set up test fixtures."""
        super().setUp()
        self.Service = self.env['woow_paas_platform.cloud_service']
        self.Template = self.env['woow_paas_platform.cloud_app_template']
        self.Workspace = self.env['woow_paas_platform.workspace']

        # Create test workspace
        self.workspace = self.Workspace.create({'name': 'Test Workspace'})

        # Create test template
        self.template = self.Template.create({
            'name': 'Test Template',
            'slug': 'test',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'test',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 2,
            'min_ram_gb': 4.0,
            'min_storage_gb': 10,
        })

    def test_create_service_minimal(self):
        """Test creating a service with minimal fields."""
        service = self.Service.create({
            'name': 'My Service',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
        })
        self.assertEqual(service.name, 'My Service')
        self.assertEqual(service.state, 'pending')
        self.assertTrue(service.reference_id)
        self.assertEqual(service.workspace_id.id, self.workspace.id)
        self.assertEqual(service.template_id.id, self.template.id)

    def test_service_default_state(self):
        """Test that new services start in pending state."""
        service = self.Service.create({
            'name': 'Pending Service',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
        })
        self.assertEqual(service.state, 'pending')

    def test_service_state_transitions(self):
        """Test valid state transitions."""
        service = self.Service.create({
            'name': 'State Test',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
        })

        # pending → deploying
        service.write({'state': 'deploying'})
        self.assertEqual(service.state, 'deploying')

        # deploying → running
        service.write({'state': 'running'})
        self.assertEqual(service.state, 'running')

        # running → upgrading
        service.write({'state': 'upgrading'})
        self.assertEqual(service.state, 'upgrading')

        # upgrading → running
        service.write({'state': 'running'})
        self.assertEqual(service.state, 'running')

    def test_service_error_state(self):
        """Test setting error state with message."""
        service = self.Service.create({
            'name': 'Error Service',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
        })

        service.write({
            'state': 'error',
            'error_message': 'Deployment failed: connection timeout',
        })

        self.assertEqual(service.state, 'error')
        self.assertIn('timeout', service.error_message)

    def test_service_helm_configuration(self):
        """Test setting Helm configuration."""
        service = self.Service.create({
            'name': 'Helm Service',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'helm_release_name': 'test-release',
            'helm_namespace': 'paas-ws-a1b2c3d4',
            'helm_values': '{"replicas": 2}',
            'helm_revision': 1,
        })

        self.assertEqual(service.helm_release_name, 'test-release')
        self.assertEqual(service.helm_namespace, 'paas-ws-a1b2c3d4')
        self.assertEqual(service.helm_revision, 1)
        self.assertIn('replicas', service.helm_values)

    def test_service_network_configuration(self):
        """Test network configuration fields."""
        service = self.Service.create({
            'name': 'Network Service',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'subdomain': 'my-app',
            'custom_domain': 'myapp.example.com',
            'internal_port': 8080,
        })

        self.assertEqual(service.subdomain, 'my-app')
        self.assertEqual(service.custom_domain, 'myapp.example.com')
        self.assertEqual(service.internal_port, 8080)

    def test_service_resource_allocation(self):
        """Test resource allocation from template."""
        service = self.Service.create({
            'name': 'Resource Service',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'allocated_vcpu': self.template.min_vcpu,
            'allocated_ram_gb': self.template.min_ram_gb,
            'allocated_storage_gb': self.template.min_storage_gb,
        })

        self.assertEqual(service.allocated_vcpu, 2)
        self.assertEqual(service.allocated_ram_gb, 4.0)
        self.assertEqual(service.allocated_storage_gb, 10)

    def test_service_belongs_to_workspace(self):
        """Test that service is properly linked to workspace."""
        service = self.Service.create({
            'name': 'Workspace Service',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
        })

        self.assertEqual(service.workspace_id.id, self.workspace.id)
        self.assertIn(service.id, self.workspace.service_ids.ids)

    def test_service_delete_cascade(self):
        """Test that service is deleted when workspace is deleted."""
        service = self.Service.create({
            'name': 'Cascade Service',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
        })

        service_id = service.id
        self.workspace.unlink()

        # Service should be deleted
        self.assertFalse(self.Service.browse(service_id).exists())

    def test_multiple_services_per_workspace(self):
        """Test creating multiple services in same workspace."""
        service1 = self.Service.create({
            'name': 'Service 1',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
        })
        service2 = self.Service.create({
            'name': 'Service 2',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
        })

        self.assertEqual(len(self.workspace.service_ids), 2)
        self.assertIn(service1.id, self.workspace.service_ids.ids)
        self.assertIn(service2.id, self.workspace.service_ids.ids)

    def test_service_timestamp_fields(self):
        """Test timestamp fields."""
        from datetime import datetime

        service = self.Service.create({
            'name': 'Timestamp Service',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'deployed_at': datetime.now(),
        })

        self.assertTrue(service.create_date)
        self.assertTrue(service.deployed_at)

    def test_service_upgrade_revision(self):
        """Test incrementing revision on upgrade."""
        service = self.Service.create({
            'name': 'Upgrade Service',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'helm_revision': 1,
            'state': 'running',
        })

        # Simulate upgrade
        service.write({
            'helm_revision': 2,
            'state': 'upgrading',
        })

        self.assertEqual(service.helm_revision, 2)
        self.assertEqual(service.state, 'upgrading')

    def test_search_services_by_state(self):
        """Test searching services by state."""
        self.Service.create({
            'name': 'Running Service',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'state': 'running',
        })
        self.Service.create({
            'name': 'Pending Service',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'state': 'pending',
        })

        running_services = self.Service.search([
            ('state', '=', 'running'),
            ('workspace_id', '=', self.workspace.id),
        ])
        self.assertGreaterEqual(len(running_services), 1)
        self.assertTrue(all(s.state == 'running' for s in running_services))

    def test_search_services_by_template(self):
        """Test searching services by template."""
        # Create another template
        template2 = self.Template.create({
            'name': 'Template 2',
            'slug': 'template-2',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'test2',
            'helm_chart_version': '1.0.0',
        })

        self.Service.create({
            'name': 'Service T1',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
        })
        self.Service.create({
            'name': 'Service T2',
            'workspace_id': self.workspace.id,
            'template_id': template2.id,
        })

        services_t1 = self.Service.search([
            ('template_id', '=', self.template.id),
        ])
        self.assertGreaterEqual(len(services_t1), 1)
        self.assertTrue(all(s.template_id.id == self.template.id for s in services_t1))

    def test_do_cleanup_calls_uninstall_with_namespace_first(self):
        """Regression: BUG-4.

        paas_operator.PaasOperatorClient.uninstall_release(namespace,
        release_name) takes namespace first; scale_release(release_name,
        namespace) takes release first. _do_cleanup used to call
        uninstall_release with the same (release, namespace) order as the
        sibling scale calls — paas-operator then rejected the request as
        the release name didn't match its namespace prefix, leaving the
        helm release + namespace orphaned in K8s.

        Mock the paas-operator client and assert the exact positional
        argument order, since this is precisely what the bug confused.
        """
        service = self.Service.create({
            'name': 'Cleanup Probe',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'helm_release_name': 'svc-deadbeef',
            'helm_namespace': 'paas-ws-stg-cafef00d',
        })
        mock_client = MagicMock()
        with patch(
            'odoo.addons.woow_paas_platform.services.paas_operator'
            '.get_paas_operator_client',
            return_value=mock_client,
        ):
            service._do_cleanup()

        mock_client.uninstall_release.assert_called_once_with(
            'paas-ws-stg-cafef00d', 'svc-deadbeef',
            subdomain=False,
        )
        self.assertEqual(service.state, 'deleting')

    def test_do_cleanup_passes_subdomain_for_route_teardown(self):
        """Regression: CloudService teardown left its Cloudflare route orphaned.

        ``_do_cleanup`` called ``uninstall_release(namespace, release)`` WITHOUT
        the subdomain. The operator's ``delete_release`` then fell back to
        ``generate_subdomain()`` to *recompute* the route name, which diverges
        from the Odoo-assigned subdomain (``paas-cs-{ws}-{res}``) → the operator
        called ``delete_route`` on a name that never existed → the CF DNS +
        tunnel ingress route outlived the service as a permanent orphan (see
        pitfall ``cf-ns-orphan-no-account-gc``). Pass ``self.subdomain`` so the
        operator deletes the *actual* route.

        Note (scope): this fixes the *main* route for all services. The
        operator's mcp sidecar route stays mismatched — deploy uses
        ``generate_subdomain+"-mcp"`` (releases.py:685), delete uses
        ``(subdomain or generate_subdomain)+"-mcp"`` (:233) — so a service
        WITH a sidecar leaks its mcp route. That is a pre-existing operator
        bug (controller ``_delete_service`` already passed subdomain on
        develop), decoupled from this fix; tracked as issue #207.
        """
        service = self.Service.create({
            'name': 'Route Teardown Probe',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'helm_release_name': 'svc-deadbeef',
            'helm_namespace': 'paas-ws-stg-cafef00d',
            'subdomain': 'paas-cs-cafef00d-deadbeef',
        })
        mock_client = MagicMock()
        with patch(
            'odoo.addons.woow_paas_platform.services.paas_operator'
            '.get_paas_operator_client',
            return_value=mock_client,
        ):
            service._do_cleanup()

        mock_client.uninstall_release.assert_called_once_with(
            'paas-ws-stg-cafef00d', 'svc-deadbeef',
            subdomain='paas-cs-cafef00d-deadbeef',
        )

    def test_delete_recomputes_quota_over_survivors(self):
        """#278 A1：刪除 CS 後以『存活集』重算並 PUT ns quota。

        同 workspace / 同 namespace 兩個 service（各 vcpu=10, ram=10,
        storage=50）。刪掉 serviceA 後，存活集只剩 serviceB → quota 應以
        serviceB 單獨重算：
          cpu = str(max(10*3, 8))       = "30"
          mem = f"{max(int(10*3), 8)}Gi" = "30Gi"
          storage = f"{max(50*3, 100)}Gi" = "150Gi"
        若誤把已刪的 A 也算進去會得到 "60"/"60Gi"/"300Gi"，
        assert_called_once_with 會抓到。
        """
        self.serviceA = self.Service.create({
            'name': 'Survivor Probe A',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'helm_release_name': 'svc-aaaa',
            'helm_namespace': 'paas-ws-survivor',
            'allocated_vcpu': 10,
            'allocated_ram_gb': 10.0,
            'allocated_storage_gb': 50,
        })
        self.serviceB = self.Service.create({
            'name': 'Survivor Probe B',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'helm_release_name': 'svc-bbbb',
            'helm_namespace': 'paas-ws-survivor',
            'allocated_vcpu': 10,
            'allocated_ram_gb': 10.0,
            'allocated_storage_gb': 50,
        })
        with patch(
            'odoo.addons.woow_paas_platform.services.paas_operator'
            '.get_paas_operator_client',
            return_value=MagicMock(),
        ) as gm:
            client = gm.return_value
            client.uninstall_release.return_value = True
            ns = self.serviceA.helm_namespace  # unlink 後就取不到，先捕獲
            self.serviceA.unlink()
            # 存活集只剩 serviceB → survivor-only 重算
            client.update_namespace_quota.assert_called_once_with(
                ns, "30", "30Gi", "150Gi",
            )


class TestCloudServiceChangePlanTrialMode(TransactionCase):
    """Trial-state change-plan on CS (issue #34).

    Mirror of TestSmartHomeChangePlanTrialMode for SH: same 3 product
    paths, plus Q6 — verifies the controller now routes refund through
    _compute_unused_refund (the deleted inline calc would have crashed on
    NULL sh_end_date once the date guard relaxed for trial state).

    Fixture follows the PR #28 hardening (Q8): owner.groups_id is
    explicitly reset to base.group_user only, env cache is dropped before
    the user-scoped call, and the manager-group absence is sanity-asserted.
    """

    def setUp(self):
        from odoo.addons.woow_paas_platform.controllers.paas import PaasController
        super().setUp()
        self.CloudService = self.env["woow_paas_platform.cloud_service"]
        self.CSTemplate = self.env["woow_paas_platform.cloud_app_template"]
        self.Workspace = self.env["woow_paas_platform.workspace"]
        self.Plan = self.env["sh.subscription.plan"]
        self.controller = PaasController()

        self.plan_a_trial7 = self.Plan.create({
            "name": "CS Trial Mode A (7d trial)",
            "sh_plan_price": 100.0, "sh_duration": 1, "sh_unit": "month",
            "sh_no_of_billing_cycle": -1,
            "sh_trial": True, "sh_trial_duration": 7, "sh_trial_unit": "day",
        })
        self.plan_b_trial14 = self.Plan.create({
            "name": "CS Trial Mode B (14d trial)",
            "sh_plan_price": 200.0, "sh_duration": 1, "sh_unit": "month",
            "sh_no_of_billing_cycle": -1,
            "sh_trial": True, "sh_trial_duration": 14, "sh_trial_unit": "day",
        })
        self.plan_c_notrial = self.Plan.create({
            "name": "CS Trial Mode C (no trial)",
            "sh_plan_price": 300.0, "sh_duration": 1, "sh_unit": "month",
            "sh_no_of_billing_cycle": -1,
            "sh_trial": False,
        })
        for plan, label in [
            (self.plan_a_trial7, "A"),
            (self.plan_b_trial14, "B"),
            (self.plan_c_notrial, "C"),
        ]:
            self.env["product.template"].sudo().create({
                "name": "CS Trial Mode Plan %s Product" % label,
                "type": "service",
                "sh_product_subscribe": True,
                "sh_subscription_plan_id": plan.id,
                "list_price": plan.sh_plan_price,
            })

        self.template = self.CSTemplate.create({
            "name": "CS Trial Mode Tmpl",
            "slug": "cs-trial-mode",
            "helm_repo_url": "https://charts.example.com",
            "helm_chart_name": "cs-trial-mode",
            "helm_chart_version": "1.0.0",
            "min_vcpu": 1,
            "min_ram_gb": 1.0,
            "min_storage_gb": 1,
        })

        self.owner = self.env["res.users"].create({
            "name": "CS Trial Mode Owner",
            "login": "cs_trial_mode_owner",
            "email": "cs_trial_mode_owner@example.test",
        })
        # Q8 / PR #28 fixture hygiene.
        base_user = self.env.ref("base.group_user")
        self.owner.groups_id = [(6, 0, [base_user.id])]
        manager_group = self.env.ref(
            "sh_subscription.group_manager_sh_subscription",
            raise_if_not_found=False,
        )
        if manager_group:
            self.assertNotIn(manager_group, self.owner.groups_id)

        self.workspace = self.Workspace.create({
            "name": "CS Trial Mode WS",
            "owner_id": self.owner.id,
        })
        self.service = self.CloudService.create({
            "name": "CS Trial Mode Service",
            "workspace_id": self.workspace.id,
            "template_id": self.template.id,
        })

    def _patch_paas_request(self, env):
        return patch(
            "odoo.addons.woow_paas_platform.controllers.paas.request",
            new=MagicMock(env=env),
        )

    def _seed_subscription(self, plan):
        """Seed an in_progress sub on `plan`, then force NULL sh_end_date
        on trial subs to reproduce historical stg/prod DB residue.

        Post-#33, fresh trial subs get a coherent (start, trial_end, end)
        triple, so this NULL state no longer occurs at create time. But
        legacy rows (stg subs 22/35–39 and any prod equivalents) still
        carry it, and the #34 controller relaxation must continue to
        accept them — hence the explicit NULL here for the regression
        path. See SH counterpart for the full rationale.
        """
        partner = self.workspace.owner_id.partner_id
        self.service._create_and_confirm_subscription(partner, plan)
        sub = self.service.sudo().subscription_id
        if sub.sh_trial:
            sub.write({"sh_end_date": False})
        self.env.invalidate_all()

    def test_trial_to_trial_new_sub_restarts_trial(self):
        """Q1: trial -> trial change-plan creates a new sub in trial state
        with the new plan's trial duration counted from today.

        Post-#33 the date triple (start, trial_end, end) is asserted; the
        plan-driven force in ``_force_trial_dates_from_plan`` runs for
        every trial sub regardless of whether the onchange chain already
        flipped sh_trial=True.
        """
        from datetime import date, timedelta
        from dateutil.relativedelta import relativedelta
        self._seed_subscription(self.plan_a_trial7)
        self.assertTrue(self.service.sudo().subscription_id.sh_trial)

        with self._patch_paas_request(self.env(user=self.owner)):
            res = self.controller.api_service_change_plan(
                service_id=self.service.id,
                plan_id=self.plan_b_trial14.id,
            )
        self.assertTrue(res["success"], res.get("error"))

        new_sub = self.service.sudo().subscription_id
        self.assertTrue(new_sub.sh_trial)
        self.assertEqual(new_sub.sh_subscription_plan_id, self.plan_b_trial14)
        self.assertEqual(res["data"]["refund_amount"], 0)

        # #33: date triple is plan-driven, not onchange-derived.
        today = date.today()
        expected_trial_end = today + timedelta(days=14) - timedelta(days=1)
        expected_start = expected_trial_end + timedelta(days=1)
        expected_end = (
            expected_start + relativedelta(months=1) - timedelta(days=1)
        )
        self.assertEqual(new_sub.sh_trial_end_date, expected_trial_end)
        self.assertEqual(new_sub.sh_start_date, expected_start)
        self.assertEqual(new_sub.sh_end_date, expected_end)

    def test_trial_to_no_trial_enters_active(self):
        """Q2: trial -> no-trial change-plan creates an active sub."""
        from datetime import date
        self._seed_subscription(self.plan_a_trial7)

        with self._patch_paas_request(self.env(user=self.owner)):
            res = self.controller.api_service_change_plan(
                service_id=self.service.id,
                plan_id=self.plan_c_notrial.id,
            )
        self.assertTrue(res["success"], res.get("error"))

        new_sub = self.service.sudo().subscription_id
        self.assertFalse(new_sub.sh_trial)
        self.assertEqual(new_sub.sh_subscription_plan_id, self.plan_c_notrial)
        self.assertEqual(new_sub.sh_start_date, date.today())
        self.assertEqual(res["data"]["refund_amount"], 0)

    def test_active_to_active_regression_proration(self):
        """Regression + Q6: active -> active path still produces a positive
        proration refund, now sourced from _compute_unused_refund instead
        of the deleted inline calc. Asserts the exact half-cycle value to
        catch any drift in how the controller threads refund through."""
        from datetime import date, timedelta
        self._seed_subscription(self.plan_c_notrial)
        today = date.today()
        old_sub = self.service.sudo().subscription_id
        old_sub.write({
            "sh_trial": False,
            "sh_start_date": today - timedelta(days=15),
            "sh_end_date": today + timedelta(days=15),
        })
        # #324：退點前提為本訂閱真已付 — seed 一筆 done deduct
        self.env['wallet.transaction'].sudo().create({
            'partner_id': old_sub.sh_partner_id.id,
            'transaction_type': 'deduct',
            'amount': -old_sub.sh_plan_price,
            'subscription_id': old_sub.id,
            'state': 'done',
        })
        self.env.invalidate_all()

        with self._patch_paas_request(self.env(user=self.owner)):
            res = self.controller.api_service_change_plan(
                service_id=self.service.id,
                plan_id=self.plan_a_trial7.id,
            )
        self.assertTrue(res["success"], res.get("error"))
        # Half of 300 (15 / 30 days remaining) = 150.0
        self.assertEqual(res["data"]["refund_amount"], 150.0)


class TestInjectStorageClass(TransactionCase):
    """_inject_storage_class must map mcp-* slugs to persistence.storageClass."""

    def test_inject_storage_class_mcp_hermes(self):
        CloudService = self.env['woow_paas_platform.cloud_service']
        out = CloudService._inject_storage_class({}, 'mcp-hermes', 'longhorn-delete')
        self.assertEqual(out['persistence']['storageClass'], 'longhorn-delete')

    def test_inject_storage_class_mcp_odoo_and_n8n(self):
        CloudService = self.env['woow_paas_platform.cloud_service']
        for slug in ('mcp-odoo', 'mcp-n8n'):
            out = CloudService._inject_storage_class({}, slug, 'sc-x')
            self.assertEqual(out['persistence']['storageClass'], 'sc-x')

