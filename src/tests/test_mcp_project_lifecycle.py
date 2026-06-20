"""TDD red phase tests — MCP cloud_service ↔ project lifecycle hooks.

Plan: docs/refactor/plan-mcp-project-decouple.md §Lifecycle (AC #9-12)
Decision: docs/refactor/decisions/mcp-project-decouple.md §Invariants 1, 3, 4

This file locks four lifecycle invariants:

* **AC #9 / Invariant #4**: ``cloud_service._do_cleanup`` must call
  ``mcp_server.unlink()`` for the service's user-scope MCP **before** it
  calls ``client.uninstall_release(...)`` and **before** it writes
  ``state='deleting'``.  Out-of-order would leave an ORM-visible mcp_server
  pointing at a torn-down sidecar URL — silent network failures during
  the time window.

* **AC #9 sanity**: cleanup hook must NOT touch ``scope='system'`` MCP rows
  (they have no ``project_id`` and are platform-level resources).

* **AC #11 / Invariant #1**: ``project.unlink()`` cascade-deletes
  ``mcp_server`` rows via ``project_id ondelete='cascade'`` (DB-level).

* **AC #12**: deleting one project must not touch the user-scope MCP of
  another project — guards against an accidental ``unlink()`` walk on
  ``mapped`` recordsets.

Red expectation today (2026-05-18, pre-implementation):

* ``mcp_server`` has no ``project_id`` field → tests guard via
  ``self.fail()`` with a clean ``AssertionError``.
* ``cloud_service._do_cleanup`` currently only does ``helm uninstall`` +
  ``state='deleting'``; it never calls ``mcp_server.unlink()`` for
  user-scope rows.  Order-check captures ``mcp_server`` count at the
  moment of ``uninstall_release`` — today the count equals 1, post-impl
  it must be 0.
"""
import uuid
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestMcpProjectLifecycle(TransactionCase):
    """4-case lifecycle regression suite — order + cascade + cross-contamination."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Workspace = cls.env['woow_paas_platform.workspace']
        cls.Template = cls.env['woow_paas_platform.cloud_app_template']
        cls.Service = cls.env['woow_paas_platform.cloud_service']
        cls.Project = cls.env['project.project']
        cls.McpServer = cls.env['woow_paas_platform.mcp_server']

    def setUp(self):
        super().setUp()
        self.workspace = self.Workspace.sudo().create({'name': 'WS Lifecycle'})
        self.template = self.Template.sudo().create({
            'name': 'Tmpl Lifecycle',
            'slug': 'tmpl-lifecycle',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'tmpl-lc',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
            # Enable MCP so the auto-create path is well-formed in red mode;
            # the actual MCP creation in tests is done explicitly.
            'mcp_enabled': True,
            'mcp_sidecar_image': 'ghcr.io/test/mcp:latest',
            'mcp_sidecar_port': 3001,
            'mcp_transport': 'streamable_http',
            'mcp_endpoint_path': '/mcp',
        })

    def _assert_schema_ready(self):
        """Fail fast if ``project_id`` not yet on mcp_server.

        Every test in this class relies on the new ``project_id`` field
        and project_id-keyed lifecycle hooks.  We surface the missing
        field as a clean ``AssertionError`` instead of a raw
        ``ValueError`` from ORM create — easier to read in red proof
        output.
        """
        if 'project_id' not in self.McpServer._fields:
            self.fail(
                "mcp_server.project_id field missing — lifecycle tests "
                "cannot run pre-implementation. Plan §D1 / AC #1."
            )

    def _make_service_with_project_and_mcp(
        self, name_prefix='LC', release_name='lc-svc',
    ):
        """Build the canonical fixture: service ↔ bound project ↔ user MCP.

        Plan AC #24 (2026-05-20 mcp-refactor-url-redesign) regression lock:
        既有 BLOCKER 2 fixture 必須繼續綠。Fixture now writes BOTH
        ``auto_created=True`` (legacy field) AND ``sidecar_backed=True``
        (new field) so the test stays green throughout the migration
        window:

          - pre-impl: ``sidecar_backed`` field doesn't exist → ORM
            silently drops unknown key when impl不存在的話 raise
            ValueError; fixture-side guard catches missing field via
            ``hasattr`` so legacy auto_created path still works.
          - post-impl: ``sidecar_backed=True`` makes the cleanup filter
            ``lambda s: s.scope == 'user' and s.sidecar_backed`` match
            this row (matches the production fixture invariant — sidecar-
            backed user-scope MCP must be cleaned up by both _do_cleanup
            and unlink override paths per AC #21 / #22).
        """
        service = self.Service.sudo().create({
            'name': f'{name_prefix} Service',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'helm_release_name': release_name,
            'helm_namespace': 'paas-ws-lc',
            'mcp_auth_token': str(uuid.uuid4()),
        })
        project = self.Project.sudo().create({
            'name': f'{name_prefix} Project',
            'cloud_service_id': service.id,
        })
        mcp_vals = {
            'name': f'{name_prefix} User MCP',
            'url': f'http://{release_name}-mcp.paas-ws-lc.svc.cluster.local:3001/mcp',
            'transport': 'streamable_http',
            'scope': 'user',
            'project_id': project.id,
            'auto_created': True,
        }
        # AC #24 regression lock — feed sidecar_backed=True if the new
        # field already exists. Pre-impl this branch is skipped; post-
        # impl the production filter requires it for the row to be
        # cleaned up.
        if 'sidecar_backed' in self.McpServer._fields:
            mcp_vals['sidecar_backed'] = True
        user_mcp = self.McpServer.sudo().create(mcp_vals)
        return service, project, user_mcp

    # ------------------------------------------------------------------ #
    # AC #9 / Invariant #4: order — unlink BEFORE helm uninstall          #
    # ------------------------------------------------------------------ #
    def test_ac9_cleanup_hook_unlinks_user_mcp_before_helm_uninstall(self):
        """Invariant #4: order is unlink → uninstall → state='deleting'.

        Captures ``mcp_server`` count at the *moment* ``uninstall_release``
        is invoked.  If the cleanup hook has unlinked the user-scope MCP
        beforehand (correct), the count is 0.  If it has not (today's
        behaviour or any future re-ordering bug), the count is ≥ 1.

        This test is the regression lock for the long-form comment on
        Invariant #4 in decisions/mcp-project-decouple.md — re-orderings
        of the three-step cleanup procedure open silent time-window bugs
        (sidecar URL stale before ORM row).
        """
        self._assert_schema_ready()
        service, project, user_mcp = self._make_service_with_project_and_mcp(
            name_prefix='Order', release_name='order-svc',
        )
        user_mcp_id = user_mcp.id

        captured = {}

        def fake_uninstall_release(namespace, release_name, subdomain=None):
            # The MCP record must already be gone when helm uninstall runs.
            captured['user_mcp_count_at_uninstall'] = (
                self.McpServer.sudo().with_context(active_test=False)
                .search_count([('id', '=', user_mcp_id)])
            )

        fake_client = MagicMock()
        fake_client.uninstall_release.side_effect = fake_uninstall_release

        # Patch the resolved import inside _do_cleanup (function-scoped
        # import in cloud_service.py:1124).
        with patch(
            'odoo.addons.woow_paas_platform.services.paas_operator'
            '.get_paas_operator_client',
            return_value=fake_client,
        ):
            service._do_cleanup()

        self.assertIn(
            'user_mcp_count_at_uninstall', captured,
            "Invariant #4: cleanup hook did not call client.uninstall_release "
            "— the MCP unlink-then-uninstall sequence is gone.",
        )
        self.assertEqual(
            captured['user_mcp_count_at_uninstall'], 0,
            "Invariant #4 violation: user-scope MCP still existed when "
            "client.uninstall_release was called. Cleanup hook must do "
            "self.project_ids.mapped('mcp_server_ids')"
            ".filtered(lambda s: s.scope=='user').sudo().unlink() FIRST.",
        )

        # Post-cleanup: project still exists (cleanup hook does not touch
        # the project).  This double-checks AC #10 inside the same flow.
        self.assertTrue(
            self.Project.browse(project.id).exists(),
            "AC #10: cleanup hook must not delete the support project.",
        )

    # ------------------------------------------------------------------ #
    # AC #9: system-scope MCP untouched by cleanup hook                   #
    # ------------------------------------------------------------------ #
    def test_ac9_cleanup_hook_does_not_touch_system_mcp(self):
        """AC #9 sanity: cleanup hook only filters scope='user'.

        Setup has *both* a user MCP attached to the service's project and a
        separate scope='system' MCP (no project_id).  After
        ``_do_cleanup``, the system MCP must remain — only user-scope
        rows belong to the cleanup hook's filter.
        """
        self._assert_schema_ready()
        service, project, user_mcp = self._make_service_with_project_and_mcp(
            name_prefix='SysSafe', release_name='syssafe-svc',
        )

        system_mcp = self.McpServer.sudo().create({
            'name': 'Platform-Wide System MCP',
            'url': 'http://platform.mcp/sse',
            'transport': 'sse',
            'scope': 'system',
            # Intentionally no project_id — system MCP is platform-level.
        })
        system_mcp_id = system_mcp.id

        fake_client = MagicMock()  # helm calls are no-ops
        with patch(
            'odoo.addons.woow_paas_platform.services.paas_operator'
            '.get_paas_operator_client',
            return_value=fake_client,
        ):
            service._do_cleanup()

        self.assertTrue(
            self.McpServer.sudo().browse(system_mcp_id).exists(),
            "AC #9 sanity: system-scope MCP was deleted by cleanup hook. "
            "The hook must filter (lambda s: s.scope=='user') before "
            "unlink — system MCPs are platform-wide, not service-bound.",
        )

    # ------------------------------------------------------------------ #
    # AC #11 / Invariant #1: project.unlink() cascades MCP                #
    # ------------------------------------------------------------------ #
    def test_ac11_delete_project_cascades_user_mcp(self):
        """AC #11 / Invariant #1: project.unlink() cascade-deletes mcp_server.

        ``mcp_server.project_id`` carries ``ondelete='cascade'``, so a DB
        FK cascade removes the mcp_server row when its project is
        unlinked.  Without ``ondelete='cascade'`` the row becomes a
        zombie (URL points to torn-down sidecar).
        """
        self._assert_schema_ready()
        service, project, user_mcp = self._make_service_with_project_and_mcp(
            name_prefix='Cascade', release_name='cascade-svc',
        )
        user_mcp_id = user_mcp.id

        project.sudo().unlink()

        self.assertFalse(
            self.McpServer.sudo().with_context(active_test=False)
            .browse(user_mcp_id).exists(),
            "AC #11 / Invariant #1: project.unlink() did not cascade to "
            "mcp_server. project_id must declare ondelete='cascade'.",
        )

    # ------------------------------------------------------------------ #
    # AC #12: deleting one project must not cross-contaminate              #
    # ------------------------------------------------------------------ #
    def test_ac12_delete_project_does_not_cross_contaminate(self):
        """AC #12: cascade is scoped to the deleted project alone.

        Two service / project / MCP triples coexist.  Deleting project A
        must leave project B's MCP intact.  Guards against a careless
        cleanup-walk that maps across all projects of the same workspace.
        """
        self._assert_schema_ready()

        # Two independent triples (different services so support_project
        # uniqueness constraint holds for both).
        service_a, project_a, mcp_a = self._make_service_with_project_and_mcp(
            name_prefix='Aside', release_name='a-svc',
        )
        service_b, project_b, mcp_b = self._make_service_with_project_and_mcp(
            name_prefix='Bside', release_name='b-svc',
        )

        mcp_a_id = mcp_a.id
        mcp_b_id = mcp_b.id

        project_a.sudo().unlink()

        self.assertFalse(
            self.McpServer.sudo().with_context(active_test=False)
            .browse(mcp_a_id).exists(),
            "AC #12: project A's MCP must be cascade-deleted with project A.",
        )
        self.assertTrue(
            self.McpServer.sudo().browse(mcp_b_id).exists(),
            "AC #12 violation: project A.unlink() also wiped project B's "
            "MCP. cascade must remain scoped to the deleted project.",
        )
