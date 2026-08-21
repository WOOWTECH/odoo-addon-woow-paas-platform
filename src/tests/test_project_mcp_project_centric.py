"""TDD red-phase tests — project-centric MCP CRUD endpoint.

Plan: ``docs/plans/2026-Q2/2026-05-19-project-detail-page-design.md``
(commit ``9033712`` on branch ``feature/project-setting``).
Issue: `#48 <https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/48>`_.

Scope locked here:

* **AC #4** — ``action=list`` returns user-scope MCP rows belonging to
  the project, formatted to the same shape as the existing
  service-centric ``_format_mcp_server`` (``id`` / ``name`` / ``url`` /
  ``transport`` / ``description`` / ``state`` / ``state_message`` /
  ``tool_count`` / ``tools`` / ``last_sync``).
* **AC #5** — ``action=create`` / ``update`` / ``delete`` / ``sync`` /
  ``test`` mirror the existing
  ``api_service_mcp_servers`` behaviour, swapping the owning object
  from ``service`` to ``project`` (``project_id`` comes from the path).
* **AC #6** — ACL gate matches endpoint 1: owner/admin can write,
  member (role=user) can read but not write, non-member receives the
  unified ``Project not found or access denied`` (防 id enumeration).
* **AC #25 second file** — creating a ``scope='user'`` MCP under an
  **unbound** project (no ``cloud_service_id``) must be rejected by
  the existing ``mcp_server.py:_check_user_scope_requires_bound_project``
  ``@api.constrains`` (D5 invariant). The endpoint must surface the
  ``ValidationError`` to the JSON-RPC caller as
  ``{success: False, error: ...}``. This path does **not** rely on the
  endpoint's manual D5 query (which only covers the unbind / cloud_service_id
  write path — see Plan AC #3 / AC #25 sub-bullet).

URL pattern (D4 / OR-6):
``POST /api/support-projects/<int:project_id>/mcp-servers``
(hyphenated — same family as the detail endpoint).

Red expectation today (pre-implementation):

The endpoint is not registered yet; ``url_open`` returns 404 / HTML.
The ``_post`` helper asserts HTTP 200 first, so every test fails with
``AssertionError: Endpoint ... did not respond (status=404)``. Once the
green implementation lands, each test enforces the specific AC it names.

External calls (``action_sync_tools`` / ``action_test_connection``) are
patched to no-op so the suite stays hermetic and matches the mocking
style already used in ``test_mcp_integration.py``.
"""
import json as json_lib
import uuid
from unittest.mock import patch

from odoo.tests.common import HttpCase, tagged
from ..models.cloud_service import _internal_provision


def _new_user(env, login, name=None):
    """Create an internal user with password == login (HttpCase auth)."""
    return env['res.users'].sudo().create({
        'name': name or login,
        'login': login,
        'password': login,
        'email': f'{login}@test.example',
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    })


@tagged('post_install', '-at_install')
class TestProjectMcpFixture(HttpCase):
    """Fixture: W1 (3 roles) + W2 (owner only), bound projects, unbound project.

    Layout::

        W1 (Alice = owner)              W2 (Bob = owner)
          ├ Carol (admin)
          ├ Dave  (user / member)
          ├ Eve   (non-member, internal user)
          ├ service_w1_a → project_w1_basic    (bound, 0 user MCP)
          ├ service_w1_b → project_w1_with_mcp (bound, 1 user MCP)
          └ unbound_project_w1                 (cloud_service_id=False)

    ``unbound_project_w1`` is required for the D5 ORM constraint test
    (AC #25 second file). Its ``workspace_id`` is computed as NULL
    because ``project.workspace_id`` is derived from
    ``cloud_service_id.workspace_id`` (``project_project.py:14-20``);
    the planner left ACL semantics for unbound projects to the
    implementer — this test file therefore exercises the D5 negative
    case via Alice's owner session on W1's last bound project that
    Alice can *also* access (the helper falls back to ``sudo`` creation
    of the unbound row).
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Workspace = cls.env['woow_paas_platform.workspace']
        cls.Template = cls.env['woow_paas_platform.cloud_app_template']
        cls.Service = cls.env['woow_paas_platform.cloud_service']
        cls.Project = cls.env['project.project']
        cls.McpServer = cls.env['woow_paas_platform.mcp_server']
        cls.WorkspaceAccess = cls.env['woow_paas_platform.workspace_access']

    def setUp(self):
        super().setUp()

        # skill-binding-ux：flag-on 保留既有正向行為守線（flag-off denied
        # 守線在 test_portal_settings_flag.py）。
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.portal_project_settings_editable', 'True')

        # ---- Users -------------------------------------------------- #
        self.alice = _new_user(self.env, 'pmc_alice', 'Alice (PMC)')
        self.bob = _new_user(self.env, 'pmc_bob', 'Bob (PMC)')
        self.carol = _new_user(self.env, 'pmc_carol', 'Carol (PMC)')
        self.dave = _new_user(self.env, 'pmc_dave', 'Dave (PMC)')
        self.eve = _new_user(self.env, 'pmc_eve', 'Eve (PMC)')

        # ---- Workspaces --------------------------------------------- #
        self.ws_w1 = self.Workspace.sudo().create({
            'name': 'PMC W1', 'owner_id': self.alice.id,
        })
        self.ws_w2 = self.Workspace.sudo().create({
            'name': 'PMC W2', 'owner_id': self.bob.id,
        })
        self.WorkspaceAccess.sudo().create({
            'workspace_id': self.ws_w1.id, 'user_id': self.carol.id,
            'role': 'admin',
        })
        self.WorkspaceAccess.sudo().create({
            'workspace_id': self.ws_w1.id, 'user_id': self.dave.id,
            'role': 'user',
        })

        # ---- Shared template ---------------------------------------- #
        self.template = self.Template.sudo().create({
            'name': 'PMC Template',
            'slug': f'pmc-tpl-{uuid.uuid4().hex[:8]}',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'pmc-tpl',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
        })

        # ---- Cloud services ----------------------------------------- #
        self.service_w1_a = _internal_provision(self.Service.sudo()).create({
            'name': 'PMC W1 Svc A',
            'workspace_id': self.ws_w1.id,
            'template_id': self.template.id,
            'helm_release_name': 'pmc-w1-a',
            'helm_namespace': 'paas-ws-pmc-w1-a',
        })
        self.service_w1_b = _internal_provision(self.Service.sudo()).create({
            'name': 'PMC W1 Svc B',
            'workspace_id': self.ws_w1.id,
            'template_id': self.template.id,
            'helm_release_name': 'pmc-w1-b',
            'helm_namespace': 'paas-ws-pmc-w1-b',
        })

        # ---- Projects (bound + unbound) ----------------------------- #
        self.project_w1_basic = self.Project.sudo().create({
            'name': 'PMC W1 Basic',
            'cloud_service_id': self.service_w1_a.id,
        })
        self.project_w1_with_mcp = self.Project.sudo().create({
            'name': 'PMC W1 With MCP',
            'cloud_service_id': self.service_w1_b.id,
        })
        self.unbound_project_w1 = self.Project.sudo().create({
            'name': 'PMC W1 Unbound',
            # cloud_service_id omitted → unbound (workspace_id will
            # compute to NULL — D5 ORM constraint test relies on this).
        })

        # ---- One user-scope MCP under project_w1_with_mcp ----------- #
        self.user_mcp_w1 = self.McpServer.sudo().create({
            'name': 'PMC W1 User MCP',
            'url': 'http://pmc-w1-b-mcp.paas-ws-pmc-w1-b.svc.cluster.local:3001/mcp',
            'transport': 'streamable_http',
            'scope': 'user',
            'project_id': self.project_w1_with_mcp.id,
            'auto_created': True,
        })

    # ------------------------------------------------------------------ #
    # JSON-RPC helper                                                    #
    # ------------------------------------------------------------------ #
    def _post(self, url, params):
        """POST a JSON-RPC call and return the ``result`` envelope.

        Same contract as the helper in
        ``test_project_detail_endpoints.py`` — fails fast in red phase
        when the endpoint URL does not resolve.
        """
        response = self.url_open(
            url,
            data=json_lib.dumps({
                'jsonrpc': '2.0', 'method': 'call', 'params': params,
            }),
            headers={'Content-Type': 'application/json'},
            timeout=30,
        )
        self.assertEqual(
            response.status_code, 200,
            (
                f"Endpoint {url!r} did not respond with HTTP 200 "
                f"(status={response.status_code}). The project-centric "
                f"MCP endpoint is likely not registered yet. "
                f"Body excerpt: {response.text[:200]!r}"
            ),
        )
        try:
            body = response.json()
        except ValueError as exc:
            self.fail(
                f"Endpoint {url!r} returned non-JSON ({exc}). "
                f"Body excerpt: {response.text[:200]!r}"
            )
        self.assertIn(
            'result', body,
            (
                f"Endpoint {url!r} returned no JSON-RPC `result` key. "
                f"Implementation likely raised inside the handler. "
                f"Body: {body!r}"
            ),
        )
        return body['result']

    @staticmethod
    def _url_mcp(project_id):
        # D4 / OR-6: hyphenated resource name.
        return f'/api/support-projects/{project_id}/mcp-servers'


# =========================================================================== #
# AC #4 — action=list                                                         #
# =========================================================================== #


@tagged('post_install', '-at_install')
class TestProjectMcpList(TestProjectMcpFixture):

    # The field list locked here matches PaasController._format_mcp_server
    # at controllers/paas.py:1400-1420. Keep them in sync.
    _EXPECTED_MCP_KEYS = {
        'id', 'name', 'url', 'transport', 'description',
        'state', 'state_message', 'tool_count', 'tools', 'last_sync',
    }

    def test_list_returns_user_scope_mcp_for_owner(self):
        """AC #4: ``action=list`` returns the project's user-scope MCP entries."""
        self.authenticate('pmc_alice', 'pmc_alice')
        result = self._post(
            self._url_mcp(self.project_w1_with_mcp.id),
            {'action': 'list'},
        )
        self.assertTrue(
            result.get('success'),
            f"AC #4: owner list must succeed. Got: {result!r}",
        )
        data = result.get('data')
        self.assertIsInstance(
            data, list, "AC #4: result.data must be a list",
        )
        ids = [row.get('id') for row in data]
        self.assertIn(
            self.user_mcp_w1.id, ids,
            "AC #4: list must surface the seeded user-scope MCP "
            "belonging to project_w1_with_mcp",
        )

    def test_list_format_matches_existing_format_mcp_server(self):
        """AC #4: each row shape parity with ``_format_mcp_server``.

        Lock the key set so the frontend can reuse the same OWL row
        component for both the legacy service-centric tab and the new
        project-centric McpServersSection (Plan §D16).
        """
        self.authenticate('pmc_alice', 'pmc_alice')
        result = self._post(
            self._url_mcp(self.project_w1_with_mcp.id),
            {'action': 'list'},
        )
        self.assertTrue(result.get('success'), f"Got: {result!r}")
        rows = result.get('data') or []
        self.assertTrue(
            rows,
            "AC #4: list payload must be non-empty for "
            "project_w1_with_mcp (one MCP was seeded).",
        )
        row = rows[0]
        missing = self._EXPECTED_MCP_KEYS - set(row.keys())
        self.assertFalse(
            missing,
            f"AC #4: MCP row payload missing keys {missing!r} "
            f"(should match PaasController._format_mcp_server at "
            f"controllers/paas.py:1400-1420). Got keys: {sorted(row.keys())!r}",
        )

    def test_list_returns_empty_for_project_without_mcp(self):
        """AC #4: project with no user-scope MCP returns an empty list (not an error)."""
        self.authenticate('pmc_alice', 'pmc_alice')
        result = self._post(
            self._url_mcp(self.project_w1_basic.id),
            {'action': 'list'},
        )
        self.assertTrue(result.get('success'), f"Got: {result!r}")
        self.assertEqual(
            result.get('data'), [],
            "AC #4: project without user-scope MCP must return data=[]",
        )

    def test_list_member_can_read(self):
        """AC #6: workspace member (role=user) can list (read parity)."""
        self.authenticate('pmc_dave', 'pmc_dave')
        result = self._post(
            self._url_mcp(self.project_w1_with_mcp.id),
            {'action': 'list'},
        )
        self.assertTrue(
            result.get('success'),
            f"AC #6: member must be able to list. Got: {result!r}",
        )

    def test_list_non_member_returns_unified_error(self):
        """AC #6/#11: non-member gets ``Project not found or access denied``."""
        self.authenticate('pmc_eve', 'pmc_eve')
        result = self._post(
            self._url_mcp(self.project_w1_with_mcp.id),
            {'action': 'list'},
        )
        self.assertFalse(
            result.get('success'),
            f"AC #6: non-member must be denied. Got: {result!r}",
        )
        self.assertEqual(
            result.get('error'), 'Project not found or access denied',
            "AC #6/#11: non-member error must match the unified "
            "防-enumeration message (same as detail endpoint AC #6).",
        )

    def test_list_nonexistent_id_returns_unified_error(self):
        """AC #11: nonexistent project id returns the unified error string."""
        self.authenticate('pmc_alice', 'pmc_alice')
        result = self._post(
            self._url_mcp(999_999_999),
            {'action': 'list'},
        )
        self.assertFalse(
            result.get('success'),
            f"AC #11: nonexistent id must be denied. Got: {result!r}",
        )
        self.assertEqual(
            result.get('error'), 'Project not found or access denied',
            "AC #11: nonexistent-id error must be the unified "
            "防-enumeration message.",
        )


# =========================================================================== #
# AC #5 — action=create / update / delete                                     #
# =========================================================================== #


@tagged('post_install', '-at_install')
class TestProjectMcpCreate(TestProjectMcpFixture):

    def test_create_user_mcp_by_owner_succeeds(self):
        """AC #5: owner can create a user-scope MCP on a bound project.

        Lock the post-conditions: scope='user', project_id == project,
        and the MCP appears via project.mcp_server_ids (the inverse
        used by AI chat consumption — ``discuss_channel.py:322``).
        """
        self.authenticate('pmc_alice', 'pmc_alice')
        result = self._post(
            self._url_mcp(self.project_w1_basic.id),
            {
                'action': 'create',
                'name': 'PMC Created By Owner',
                'url': 'http://custom-mcp.example/mcp',
                'transport': 'streamable_http',
                'description': 'Created via project endpoint.',
            },
        )
        self.assertTrue(
            result.get('success'),
            f"AC #5: owner create must succeed. Got: {result!r}",
        )
        new_id = result['data']['id']
        new_server = self.McpServer.sudo().browse(new_id)
        self.assertTrue(new_server.exists())
        self.assertEqual(new_server.scope, 'user')
        self.assertEqual(new_server.project_id.id, self.project_w1_basic.id)
        self.assertIn(
            new_server.id, self.project_w1_basic.mcp_server_ids.ids,
            "AC #5: created MCP must appear in project.mcp_server_ids "
            "(inverse via mcp_server.project_id).",
        )

    def test_create_user_mcp_by_admin_succeeds(self):
        """AC #5/#6: workspace admin can create MCP."""
        self.authenticate('pmc_carol', 'pmc_carol')
        result = self._post(
            self._url_mcp(self.project_w1_basic.id),
            {
                'action': 'create',
                'name': 'PMC Created By Admin',
                'url': 'http://admin-mcp.example/mcp',
                'transport': 'sse',
            },
        )
        self.assertTrue(
            result.get('success'),
            f"AC #5/#6: admin create must succeed. Got: {result!r}",
        )

    def test_create_by_member_returns_permission_denied(self):
        """AC #6: member (role=user) gets exactly ``Permission denied`` on create."""
        self.authenticate('pmc_dave', 'pmc_dave')
        result = self._post(
            self._url_mcp(self.project_w1_basic.id),
            {
                'action': 'create',
                'name': 'PMC Hijack',
                'url': 'http://hijack-mcp.example/mcp',
                'transport': 'sse',
            },
        )
        self.assertFalse(
            result.get('success'),
            f"AC #6: member create must be denied. Got: {result!r}",
        )
        self.assertEqual(
            result.get('error'), 'Permission denied',
            "AC #6: member write must return exactly 'Permission denied' "
            "(distinct from the防-enumeration message for non-members).",
        )

    def test_create_missing_required_fields_rejected(self):
        """AC #5: ``name`` and ``url`` are required.

        Mirrors ``_create_mcp_server`` at controllers/paas.py:1302-1326:
        empty name or url is rejected with ``Name and URL are required``.
        """
        self.authenticate('pmc_alice', 'pmc_alice')
        result = self._post(
            self._url_mcp(self.project_w1_basic.id),
            {'action': 'create', 'name': '', 'url': ''},
        )
        self.assertFalse(
            result.get('success'),
            f"AC #5: empty name/url must be rejected. Got: {result!r}",
        )


@tagged('post_install', '-at_install')
class TestProjectMcpUpdateDelete(TestProjectMcpFixture):

    def test_update_user_mcp_by_owner_succeeds(self):
        """AC #5: owner can update an MCP attribute (name)."""
        self.authenticate('pmc_alice', 'pmc_alice')
        result = self._post(
            self._url_mcp(self.project_w1_with_mcp.id),
            {
                'action': 'update',
                'server_id': self.user_mcp_w1.id,
                'name': 'PMC Updated Name',
            },
        )
        self.assertTrue(
            result.get('success'),
            f"AC #5: owner update must succeed. Got: {result!r}",
        )
        self.user_mcp_w1.invalidate_recordset()
        self.assertEqual(self.user_mcp_w1.name, 'PMC Updated Name')

    def test_update_cross_project_mcp_rejected(self):
        """AC #5: updating an MCP whose ``project_id`` differs from the path id is rejected.

        Mirrors the service-centric guard at
        ``controllers/paas.py:1334-1336`` which checks
        ``server.project_id.cloud_service_id.id != service.id``. The
        project-centric variant must reject MCPs whose ``project_id``
        does not equal the path project id (防 cross-project tampering).
        """
        self.authenticate('pmc_alice', 'pmc_alice')
        result = self._post(
            self._url_mcp(self.project_w1_basic.id),
            {
                'action': 'update',
                'server_id': self.user_mcp_w1.id,  # belongs to project_w1_with_mcp
                'name': 'Cross-project tamper',
            },
        )
        self.assertFalse(
            result.get('success'),
            f"AC #5: cross-project MCP update must be rejected. "
            f"Got: {result!r}",
        )
        self.user_mcp_w1.invalidate_recordset()
        self.assertNotEqual(
            self.user_mcp_w1.name, 'Cross-project tamper',
            "AC #5: rejected cross-project update must leave the MCP "
            "untouched.",
        )

    def test_update_by_member_returns_permission_denied(self):
        """AC #6: member (role=user) gets ``Permission denied`` on update."""
        self.authenticate('pmc_dave', 'pmc_dave')
        result = self._post(
            self._url_mcp(self.project_w1_with_mcp.id),
            {
                'action': 'update',
                'server_id': self.user_mcp_w1.id,
                'name': 'Member Tamper',
            },
        )
        self.assertFalse(
            result.get('success'),
            f"AC #6: member update must be denied. Got: {result!r}",
        )
        self.assertEqual(result.get('error'), 'Permission denied')

    def test_delete_user_mcp_by_owner_succeeds(self):
        """AC #5: owner can delete an MCP; row is removed from ORM."""
        self.authenticate('pmc_alice', 'pmc_alice')
        mcp_id = self.user_mcp_w1.id
        result = self._post(
            self._url_mcp(self.project_w1_with_mcp.id),
            {'action': 'delete', 'server_id': mcp_id},
        )
        self.assertTrue(
            result.get('success'),
            f"AC #5: owner delete must succeed. Got: {result!r}",
        )
        self.assertFalse(
            self.McpServer.sudo().browse(mcp_id).exists(),
            "AC #5: deleted MCP must be removed from ORM",
        )

    def test_delete_by_member_returns_permission_denied(self):
        """AC #6: member delete must be blocked with ``Permission denied``."""
        self.authenticate('pmc_dave', 'pmc_dave')
        result = self._post(
            self._url_mcp(self.project_w1_with_mcp.id),
            {'action': 'delete', 'server_id': self.user_mcp_w1.id},
        )
        self.assertFalse(
            result.get('success'),
            f"AC #6: member delete must be denied. Got: {result!r}",
        )
        self.assertEqual(result.get('error'), 'Permission denied')
        # Row must still exist.
        self.assertTrue(
            self.McpServer.sudo().browse(self.user_mcp_w1.id).exists(),
            "AC #6: rejected member delete must leave the MCP intact.",
        )


# =========================================================================== #
# AC #5 — action=sync / test (mocked external connection)                     #
# =========================================================================== #


@tagged('post_install', '-at_install')
class TestProjectMcpSyncAndTest(TestProjectMcpFixture):

    def test_sync_user_mcp_invokes_action_sync_tools(self):
        """AC #5: ``action=sync`` triggers ``McpServer.action_sync_tools`` on the row.

        Patches ``action_sync_tools`` so the test does not hit the
        sidecar (mirrors ``test_mcp_integration.py:TestMCPServerCronRetry``
        patch style). Asserts the method was invoked exactly once on
        the MCP row addressed by ``server_id``.
        """
        self.authenticate('pmc_alice', 'pmc_alice')
        patch_target = (
            'odoo.addons.woow_paas_platform.models.mcp_server.'
            'McpServer.action_sync_tools'
        )
        with patch(patch_target, return_value=True) as mock_sync:
            result = self._post(
                self._url_mcp(self.project_w1_with_mcp.id),
                {'action': 'sync', 'server_id': self.user_mcp_w1.id},
            )
        self.assertTrue(
            result.get('success'),
            f"AC #5: sync must succeed when action_sync_tools returns. "
            f"Got: {result!r}",
        )
        self.assertEqual(
            mock_sync.call_count, 1,
            "AC #5: endpoint must call action_sync_tools exactly once "
            "on the addressed MCP row.",
        )

    def test_test_user_mcp_invokes_action_test_connection(self):
        """AC #5: ``action=test`` triggers ``McpServer.action_test_connection``."""
        self.authenticate('pmc_alice', 'pmc_alice')
        patch_target = (
            'odoo.addons.woow_paas_platform.models.mcp_server.'
            'McpServer.action_test_connection'
        )
        # #554：`action_test_connection` 現在回 bool（True＝可連線），controller
        # 判讀它來決定 success。mock 必須忠實反映新契約——留著舊的
        # `return_value=None` 會讓 endpoint 正確地把 None 判成失敗，測試紅在
        # mock 說謊而不是實作有問題。
        with patch(patch_target, return_value=True) as mock_test:
            result = self._post(
                self._url_mcp(self.project_w1_with_mcp.id),
                {'action': 'test', 'server_id': self.user_mcp_w1.id},
            )
        self.assertTrue(
            result.get('success'),
            f"AC #5: test must succeed when action_test_connection "
            f"returns. Got: {result!r}",
        )
        self.assertEqual(
            mock_test.call_count, 1,
            "AC #5: endpoint must call action_test_connection exactly "
            "once on the addressed MCP row.",
        )
