"""Tests for Cloud Service model."""
import json
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase
from odoo.exceptions import AccessError, ValidationError
from ..models.cloud_service import _internal_provision


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
        service = _internal_provision(self.Service).create({
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
        service = _internal_provision(self.Service).create({
            'name': 'Pending Service',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
        })
        self.assertEqual(service.state, 'pending')

    def test_service_state_transitions(self):
        """Test valid state transitions."""
        service = _internal_provision(self.Service).create({
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
        service = _internal_provision(self.Service).create({
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
        service = _internal_provision(self.Service).create({
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

    def test_mcp_service_secret_is_stored_separately(self):
        template = self.Template.create({
            'name': 'MCP Template',
            'slug': 'mcp-odoo',
            'category': 'mcp',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'mcp-server',
            'helm_chart_version': '1.0.0',
            # #357 C01：對齊真實 seed 形態——MCP 內建於 body（mcp_enabled=False），
            # admin_password 供給鏈的閘門是 has_admin_gui。
            'mcp_enabled': False,
            'has_admin_gui': True,
        })
        # #357 C19：fixture 必須「先塞入」config.sensitive.admin_password，create()
        # 的 _strip_mcp_sensitive_values 才有東西可剝。原本 fixture 的 helm_values
        # 根本沒有 sensitive 子樹 → assertNotIn 恆真 → 把 strip 實作整段刪掉測試照樣
        # 綠（vacuous assertion＝零鑑別力，正是本輪 WIP 全是紙上綠的典型）。
        service = _internal_provision(self.Service.with_context(
            mcp_admin_password='secret-password',
        )).create({
            'name': 'MCP Service',
            'workspace_id': self.workspace.id,
            'template_id': template.id,
            'helm_values': json.dumps({
                'config': {
                    'sensitive': {'admin_password': 'must-be-stripped-at-create'},
                    'nonSensitive': {'connection': {'url': 'https://tenant.example'}},
                },
            }),
        })

        self.assertNotIn('admin_password', self.Service._fields)
        # 密碼不得留在 helm_values——它是 git / API / helm history 可見面。
        stored = json.loads(service.helm_values)
        self.assertNotIn(
            'sensitive', stored['config'],
            'create() 必須剝掉整個 config.sensitive 子樹（_strip_mcp_sensitive_values）')
        self.assertNotIn('must-be-stripped-at-create', service.helm_values)
        # 反向鎖：strip 不得誤傷 GUI 自填的 nonSensitive（改成整段清空 config 會紅）。
        self.assertEqual(
            stored['config']['nonSensitive']['connection'],
            {'url': 'https://tenant.example'},
            'strip 只能拿掉 sensitive，不得動 GUI 自填的 connection/tools')

        secret = self.env['woow_paas_platform.cloud_service_secret'].search([
            ('service_id', '=', service.id),
        ], limit=1)
        self.assertTrue(secret)
        self.assertEqual(secret.admin_password, 'secret-password')
        self.assertEqual(secret.service_id.id, service.id)

    def test_service_network_configuration(self):
        """Test network configuration fields."""
        service = _internal_provision(self.Service).create({
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
        service = _internal_provision(self.Service).create({
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
        service = _internal_provision(self.Service).create({
            'name': 'Workspace Service',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
        })

        self.assertEqual(service.workspace_id.id, self.workspace.id)
        self.assertIn(service.id, self.workspace.service_ids.ids)

    def test_service_delete_cascade(self):
        """Test that service is deleted when workspace is deleted."""
        service = _internal_provision(self.Service).create({
            'name': 'Cascade Service',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
        })

        service_id = service.id
        self.workspace.unlink()

        # Service should be deleted
        self.assertFalse(self.Service.browse(service_id).exists())

    def test_secret_cascades_with_service(self):
        template = self.Template.create({
            'name': 'MCP Template',
            'slug': 'mcp-odoo',
            'category': 'mcp',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'mcp-server',
            'helm_chart_version': '1.0.0',
            # #357 C01：對齊真實 seed 形態——MCP 內建於 body（mcp_enabled=False），
            # admin_password 供給鏈的閘門是 has_admin_gui。
            'mcp_enabled': False,
            'has_admin_gui': True,
        })
        service = _internal_provision(self.Service.with_context(
            mcp_admin_password='secret-password',
        )).create({
            'name': 'Secret Cascade',
            'workspace_id': self.workspace.id,
            'template_id': template.id,
        })
        secret = self.env['woow_paas_platform.cloud_service_secret'].search([
            ('service_id', '=', service.id),
        ], limit=1)
        self.assertTrue(secret.exists())

        service.unlink()
        self.assertFalse(secret.exists())

    def test_secret_is_not_readable_by_regular_user(self):
        template = self.Template.create({
            'name': 'MCP Template',
            'slug': 'mcp-odoo',
            'category': 'mcp',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'mcp-server',
            'helm_chart_version': '1.0.0',
            # #357 C01：對齊真實 seed 形態——MCP 內建於 body（mcp_enabled=False），
            # admin_password 供給鏈的閘門是 has_admin_gui。
            'mcp_enabled': False,
            'has_admin_gui': True,
        })
        service = _internal_provision(self.Service.with_context(
            mcp_admin_password='secret-password',
        )).create({
            'name': 'Secret ACL',
            'workspace_id': self.workspace.id,
            'template_id': template.id,
        })
        secret_model = self.env['woow_paas_platform.cloud_service_secret']
        # #357 C21：原本這裡直接引用未定義的 `secret` → NameError（而非 AccessError），
        # 測試恆 ERROR＝整波從未真跑。先實證 C01 修好後 create() 真的建了 secret
        # （C01 未修時 create() 用 mcp_enabled 過濾 → 空集合 → 這行就會紅），
        # 再斷言 regular user 讀不到。
        secret = secret_model.sudo().search([
            ('service_id', '=', service.id),
        ], limit=1)
        self.assertTrue(
            secret, '供給鏈接上後 create() 必須為 has_admin_gui template 建 secret')
        self.assertEqual(secret.admin_password, 'secret-password')

        # #357 C22：自建 regular user，不用 self.env.ref('base.user_demo')。
        # demo user 只在「有載入 demo data」的 DB 存在：任何 --without-demo 的 DB
        # （CI 常態）會在 env.ref 當場 ValueError＝整支 ERROR 而非 skip → 這條守著
        # C05 提權洞的 ACL 斷言在那些環境**從未真的執行過**，是 false-green 的溫床。
        # 自建 user 只依賴 base.group_user（base module 的 data、非 demo），讓本測試
        # 在有無 demo data 的 DB 都真的跑得到 deny 分支。
        regular_user = self.env['res.users'].create({
            'name': 'Regular Tenant User',
            'login': 'warden-c22-regular-user',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        # 上面的 sudo().search 已把 admin_password 讀進 cache，且 Odoo 的 cache 以
        # (field, record id) 為 key、跨 env 共用（api.py:1117-1122），故 regular_user
        # 的 env 看得到同一份。本例的 AccessError 其實來自 read() 在 fetch 前跑的
        # model-level ACL 檢查（ir.model.access 只給 base.group_system），與 cache 無關
        # ——invalidate_all 是保險，確保斷言測的是真 ACL 而非快取殘留。
        self.env.invalidate_all()

        with self.assertRaises(AccessError):
            secret_model.with_user(regular_user).browse(secret.id).read(['admin_password'])

    def test_multiple_services_per_workspace(self):
        """Test creating multiple services in same workspace."""
        service1 = _internal_provision(self.Service).create({
            'name': 'Service 1',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
        })
        service2 = _internal_provision(self.Service).create({
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

        service = _internal_provision(self.Service).create({
            'name': 'Timestamp Service',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'deployed_at': datetime.now(),
        })

        self.assertTrue(service.create_date)
        self.assertTrue(service.deployed_at)

    def test_service_upgrade_revision(self):
        """Test incrementing revision on upgrade."""
        service = _internal_provision(self.Service).create({
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
        _internal_provision(self.Service).create({
            'name': 'Running Service',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'state': 'running',
        })
        _internal_provision(self.Service).create({
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

        _internal_provision(self.Service).create({
            'name': 'Service T1',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
        })
        _internal_provision(self.Service).create({
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
        service = _internal_provision(self.Service).create({
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
        service = _internal_provision(self.Service).create({
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
        self.serviceA = _internal_provision(self.Service).create({
            'name': 'Survivor Probe A',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'helm_release_name': 'svc-aaaa',
            'helm_namespace': 'paas-ws-survivor',
            'allocated_vcpu': 10,
            'allocated_ram_gb': 10.0,
            'allocated_storage_gb': 50,
        })
        self.serviceB = _internal_provision(self.Service).create({
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
        self.service = _internal_provision(self.CloudService).create({
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

    def test_trial_to_trial_change_plan_no_longer_restarts_trial(self):
        """#367: trial -> trial change-plan must NOT restart the trial.

        The seeded partner already holds a subscription (the just-closed old
        sub), so per #367 the new sub enters the active (non-trial) state with
        immediate billing rather than a fresh 14-day trial. This supersedes the
        pre-#367 behaviour this test previously locked in — that behaviour was
        the very bug #367 fixes (cf. the #327 refund-window test rewrite that
        likewise flipped a bug-fixated assertion to the corrected semantics).
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
        self.assertFalse(
            new_sub.sh_trial,
            "#367: returning partner change-plan must not restart trial",
        )
        self.assertFalse(new_sub.sh_trial_end_date)
        self.assertEqual(new_sub.sh_subscription_plan_id, self.plan_b_trial14)
        self.assertEqual(res["data"]["refund_amount"], 0)

        # Active date triple is plan-driven (start=today, no trial offset).
        today = date.today()
        expected_end = today + relativedelta(months=1) - timedelta(days=1)
        self.assertEqual(new_sub.sh_start_date, today)
        self.assertEqual(new_sub.sh_end_date, expected_end)
        self.assertGreaterEqual(new_sub.sh_end_date, new_sub.sh_start_date)

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
            # #327：退點視窗改以 sh_date_of_next_payment 反推，週期中點 sub 的
            # 下次扣款日對齊當期結束；明確 day/30 cycle 使半額斷言穩定（不受
            # relativedelta 月份天數浮動）。cycle_start = next_payment − 30d
            # = today−15 = start，remaining 15/30 → 150。
            "sh_date_of_next_payment": today + timedelta(days=15),
            "sh_unit": "day",
            "sh_recurrency": 30,
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


class TestInjectAllowedOrigins(TransactionCase):
    """_inject_allowed_origins pins the service's own origin ONLY for charts that
    gate on the browser Origin header (open-design); no-op for other slugs. The
    open-design daemon 403s a public tenant Origin unless whitelisted verbatim,
    and the chart never learns the tenant host (Cloudflare tunnel, not ingress) —
    so the platform supplies it at deploy time. See #380."""

    def test_inject_open_design_sets_config_allowed_origins(self):
        CloudService = self.env['woow_paas_platform.cloud_service']
        out = CloudService._inject_allowed_origins(
            {}, 'open-design', 'https://paas-cs-abc-def.woowtech.io')
        self.assertEqual(
            out['config']['allowedOrigins'],
            'https://paas-cs-abc-def.woowtech.io')

    def test_inject_noop_for_other_slug(self):
        """Unknown slug (n8n / mcp-*) must NOT gain a config.allowedOrigins key —
        that value path is open-design-specific."""
        CloudService = self.env['woow_paas_platform.cloud_service']
        out = CloudService._inject_allowed_origins(
            {'existing': 1}, 'n8n', 'https://x.woowtech.io')
        self.assertEqual(out, {'existing': 1})

    def test_inject_open_design_preserves_other_config_keys(self):
        CloudService = self.env['woow_paas_platform.cloud_service']
        out = CloudService._inject_allowed_origins(
            {'config': {'bindHost': '127.0.0.1'}},
            'open-design', 'https://x.woowtech.io')
        self.assertEqual(out['config']['bindHost'], '127.0.0.1')
        self.assertEqual(out['config']['allowedOrigins'], 'https://x.woowtech.io')


class TestInjectApiServerCidrs(TransactionCase):
    """_inject_apiserver_cidrs opens the console → kube-apiserver egress hole ONLY
    for charts that run kubectl under a tenant NetworkPolicy (open-design); no-op
    for other slugs. The chart defaults to the kubernetes Service ClusterIP alone,
    which a post-DNAT CNI (woow-k3s) silently blocks — the control-plane node IPs
    are cluster-specific, so ops sets them as a config parameter and the platform
    injects them at deploy time. See #404."""

    def test_inject_open_design_sets_network_policy_cidrs(self):
        CloudService = self.env['woow_paas_platform.cloud_service']
        out = CloudService._inject_apiserver_cidrs(
            {}, 'open-design', ['10.43.0.1/32'])
        self.assertEqual(out['networkPolicy']['apiServerCidrs'], ['10.43.0.1/32'])

    def test_inject_multiple_cidrs_kept_as_list(self):
        """post-DNAT 叢集要同時開 ClusterIP 與三顆 control-plane node /32 —
        chart 端是 list，注入必須保持多值、順序不變。"""
        CloudService = self.env['woow_paas_platform.cloud_service']
        cidrs = [
            '10.43.0.1/32',
            '192.168.10.21/32',
            '192.168.10.22/32',
            '192.168.10.23/32',
        ]
        out = CloudService._inject_apiserver_cidrs({}, 'open-design', cidrs)
        self.assertEqual(out['networkPolicy']['apiServerCidrs'], cidrs)

    def test_inject_noop_for_other_slug(self):
        """Unknown slug (n8n / mcp-*) must NOT gain a networkPolicy key — that
        value path is open-design-specific."""
        CloudService = self.env['woow_paas_platform.cloud_service']
        out = CloudService._inject_apiserver_cidrs(
            {'existing': 1}, 'n8n', ['10.43.0.1/32'])
        self.assertEqual(out, {'existing': 1})

    def test_inject_noop_for_empty_cidrs(self):
        """空清單 → 不寫入，讓 chart 自己的預設留著（chart 語意：空 = console
        完全沒有 apiserver egress，等於把 kubectl 鎖死）。"""
        CloudService = self.env['woow_paas_platform.cloud_service']
        out = CloudService._inject_apiserver_cidrs({}, 'open-design', [])
        self.assertEqual(out, {})

    def test_inject_preserves_other_network_policy_keys(self):
        CloudService = self.env['woow_paas_platform.cloud_service']
        out = CloudService._inject_apiserver_cidrs(
            {'networkPolicy': {'enabled': True}},
            'open-design', ['10.43.0.1/32'])
        self.assertTrue(out['networkPolicy']['enabled'])
        self.assertEqual(out['networkPolicy']['apiServerCidrs'], ['10.43.0.1/32'])

    def test_parse_cidr_list_strips_and_filters(self):
        CloudService = self.env['woow_paas_platform.cloud_service']
        self.assertEqual(
            CloudService._parse_cidr_list(' 10.43.0.1/32 , 192.168.10.21/32 ,, '),
            ['10.43.0.1/32', '192.168.10.21/32'])
        self.assertEqual(CloudService._parse_cidr_list(''), [])
        self.assertEqual(CloudService._parse_cidr_list(False), [])

    def test_get_apiserver_cidrs_default_is_cluster_agnostic(self):
        """未設定 config parameter 時取 chart 一致的預設（k3s kubernetes Service
        ClusterIP）；cluster-specific 的 node /32 由 ops 自行設定，不進 code。"""
        CloudService = self.env['woow_paas_platform.cloud_service']
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.apiserver_cidrs', False)
        self.assertEqual(CloudService._get_apiserver_cidrs(), ['10.43.0.1/32'])

    def test_get_apiserver_cidrs_reads_config_parameter(self):
        CloudService = self.env['woow_paas_platform.cloud_service']
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.apiserver_cidrs',
            '10.43.0.1/32,192.168.10.21/32,192.168.10.22/32')
        self.assertEqual(
            CloudService._get_apiserver_cidrs(),
            ['10.43.0.1/32', '192.168.10.21/32', '192.168.10.22/32'])


class TestAssembleDeployValues411(TransactionCase):
    """#411：全量 values 組裝 helper 與三呼叫點 reset_values 語意。

    契約（docs/decisions/patterns/helm-reset-values-unified-pipeline.md）：
    fresh install 與既存 release 升級走同一條 ``_assemble_deploy_values`` 管線，
    helm 改 reset_values 語意，讓 chart 升版新增預設自然補位 —— 終結 reuse_values
    bug class（既存 release 升級靜默漏 chart 新預設）。
    """

    def setUp(self):
        super().setUp()
        self.Service = self.env['woow_paas_platform.cloud_service']
        self.Template = self.env['woow_paas_platform.cloud_app_template']
        self.Workspace = self.env['woow_paas_platform.workspace']
        # #804：sandbox 會 seed 自己的 paas_domain；本測試主動覆寫成固定、
        # 非 production 預設值，expected 則刻意保留獨立字面量以避免同源假綠。
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.paas_domain', 'issue-804.invalid')
        self.workspace = self.Workspace.create({'name': '411 WS'})
        # open-design template：一次涵蓋全部 4 個 injector（storage_class /
        # image_pull_secrets 對所有 slug 生效；allowed_origins / apiserver_cidrs
        # 只對 open-design slug 有 value-path）。
        self.od_template = self.Template.create({
            'name': 'Open Design 411',
            'slug': 'open-design',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'open-design',
            'helm_chart_version': '0.1.1',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
            'ingress_enabled': True,
        })
        self.od_service = _internal_provision(self.Service).create({
            'name': 'OD Service',
            'workspace_id': self.workspace.id,
            'template_id': self.od_template.id,
            'subdomain': 'sub',
            'helm_release_name': 'svc-od',
            'helm_namespace': 'paas-ws-ododod',
            # base = template 預設 + user config（create 時已 merge 定案）
            'helm_values': json.dumps({
                'config': {'existingUserKey': 'keep'},
                'replicaCount': 3,
            }),
        })
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.apiserver_cidrs', '10.43.0.1/32,192.168.10.21/32')

    # ---- helper 組裝優先序 --------------------------------------------

    def test_assemble_layers_all_injectors_over_user_config(self):
        """全量組裝：base（user config）保留，4 injector 疊在上、彼此不互相覆蓋。"""
        out = self.od_service._assemble_deploy_values()
        values = out['values']
        # 1. base user config 原封保留
        self.assertEqual(values['replicaCount'], 3)
        self.assertEqual(
            values['config']['existingUserKey'], 'keep',
            'injector 不得覆蓋 user config 既有的非同 path 鍵')
        # 2. storage_class injector（預設 path persistence.storageClass）
        self.assertEqual(values['persistence']['storageClass'], 'longhorn-delete')
        # 3. image_pull_secrets injector（global.imagePullSecrets）
        self.assertIn('paas-registry', values['global']['imagePullSecrets'])
        # 4. allowed_origins injector（open-design → config.allowedOrigins），
        #    與 base 的 config.existingUserKey 共存於同一 config 子樹
        self.assertEqual(
            values['config']['allowedOrigins'], 'https://sub.issue-804.invalid')
        self.assertEqual(values['config']['existingUserKey'], 'keep')
        # 5. apiserver_cidrs injector（open-design → networkPolicy.apiServerCidrs）
        self.assertEqual(
            values['networkPolicy']['apiServerCidrs'],
            ['10.43.0.1/32', '192.168.10.21/32'])

    def test_assemble_is_readonly_and_idempotent(self):
        """helper 純函數：不改 svc.helm_values，重複呼叫結果一致（盤點可 dry-run）。"""
        before = json.loads(self.od_service.helm_values)
        first = self.od_service._assemble_deploy_values()['values']
        after = json.loads(self.od_service.helm_values)
        self.assertEqual(before, after, 'helper 不得寫回 svc.helm_values')
        second = self.od_service._assemble_deploy_values()['values']
        self.assertEqual(first, second, 'injector 疊加必須冪等')

    def test_assemble_expose_config_gated_on_ingress_and_subdomain(self):
        """expose_config：ingress 開且有 subdomain 才 expose，否則 None。"""
        self.assertEqual(
            self.od_service._assemble_deploy_values()['expose_config'],
            {'enabled': True, 'subdomain': 'sub'})
        # 關掉 ingress → None
        self.od_template.ingress_enabled = False
        self.assertIsNone(
            self.od_service._assemble_deploy_values()['expose_config'])

    # ---- 呼叫點傳 reset_values --------------------------------------

    def test_redeploy_passes_reset_values_true(self):
        """action_redeploy 既存 release 升級路徑：upgrade_release(reset_values=True)。"""
        mock_client = MagicMock()
        mock_client.get_release.return_value = {'name': 'svc-od', 'revision': 1}
        mock_client.upgrade_release.return_value = {'revision': 2}
        with patch(
            'odoo.addons.woow_paas_platform.services.paas_operator'
            '.get_paas_operator_client',
            return_value=mock_client,
        ):
            self.od_service.action_redeploy()
        kwargs = mock_client.upgrade_release.call_args.kwargs
        self.assertTrue(
            kwargs.get('reset_values'),
            'redeploy 升級路徑必須傳 reset_values=True')
        self.assertNotEqual(
            kwargs.get('reuse_values'), True,
            '不得再傳危險的 reuse_values=True')
        # 全量組裝確實流到 operator（injector 有到）
        self.assertEqual(
            kwargs['values']['persistence']['storageClass'], 'longhorn-delete')

    def test_reset_admin_password_passes_reset_values_true(self):
        """_reset_admin_password：reset_release_config(reset_values=True)。"""
        mcp_template = self.Template.create({
            'name': 'MCP 411', 'slug': 'mcp-odoo', 'category': 'mcp',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'mcp-server', 'helm_chart_version': '1.0.0',
            'mcp_enabled': False, 'has_admin_gui': True,
        })
        service = _internal_provision(self.Service.with_context(
            mcp_admin_password='seed-pw',
        )).create({
            'name': 'MCP Svc 411',
            'workspace_id': self.workspace.id,
            'template_id': mcp_template.id,
            'helm_release_name': 'svc-mcp',
            'helm_namespace': 'paas-ws-mcpmcp',
            'helm_values': json.dumps({'config': {'nonSensitive': {}}}),
        })
        service.write({'state': 'running'})
        mock_client = MagicMock()
        mock_client.reset_release_config.return_value = {'helm_revision': 2}
        with patch(
            'odoo.addons.woow_paas_platform.services.paas_operator'
            '.get_paas_operator_client',
            return_value=mock_client,
        ):
            service._reset_admin_password()
        kwargs = mock_client.reset_release_config.call_args.kwargs
        self.assertTrue(
            kwargs.get('reset_values'),
            'reset admin password 路徑必須傳 reset_values=True')
        self.assertNotEqual(kwargs.get('reuse_values'), True)
