"""TDD red-phase tests — project-centric detail endpoint.

Plan: ``docs/plans/2026-Q2/2026-05-19-project-detail-page-design.md``
(commit ``9033712`` on branch ``feature/project-setting``).
Issue: `#48 <https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/48>`_.

Scope locked here:

* **AC #1** — ``action=get`` returns the full project shape
  (id/name/description/cloud_service_id/cloud_service_name/workspace_id/
  workspace_name/mcp_server_ids/task_count/open_task_count/created_date)
  and **does NOT** include a top-level ``status`` field (D6 carve-out).
* **AC #2** — ``action=update`` writes name / description /
  cloud_service_id; owner & admin succeed, member is rejected with
  ``Permission denied``.
* **AC #3** — rebind invariants:

    - new ``cloud_service`` must belong to the same workspace (rejected
      otherwise);
    - ``cloud_service_id=False`` (unbind) on a project that still has
      user-scope MCP servers must be blocked by the **endpoint's manual
      query** (D5 / ``mcp_server.py:_check_user_scope_requires_bound_project``
      ``@api.constrains`` does **not** fire on ``project.cloud_service_id``
      writes — see Plan AC #3 third bullet & OR-4);
    - unbind on a project with no user-scope MCP succeeds.
* **AC #6 / AC #11** — non-member of the owning workspace **and**
  requests for a non-existent project id both receive the same
  ``Project not found or access denied`` error (防 id enumeration —
  Plan AC #6 / AC #11 stated wording).
* **D4 / OR-6** — URL pattern is ``/api/support-projects/<int:project_id>``
  (hyphenated, not the existing slash ``/api/support/projects/...``).

Red expectation today (pre-implementation):

The endpoint does **not exist yet**. ``url_open`` against the hyphenated
URL returns Odoo's 404 page (HTML, not JSON-RPC). The ``_post`` helper
below asserts ``status_code == 200`` first, so every test in this file
fails with::

    AssertionError: Endpoint /api/support-projects/<id> did not respond
    (status=404). Body excerpt: <html>...</html>

This is the proper "feature missing" red — each test names the AC it
expects to enforce once the endpoint is wired up.
"""
import json as json_lib
import uuid

from odoo.tests.common import HttpCase, tagged


# --------------------------------------------------------------------------- #
# Shared fixture helpers                                                      #
# --------------------------------------------------------------------------- #


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
class TestProjectDetailFixture(HttpCase):
    """Shared fixture: two workspaces, three roles in W1, bound projects.

    Layout (mirrors ``test_cross_workspace_task_isolation.py`` +
    workspace_access three-role coverage requested by planner in
    ``/tmp/planner-report-issue-48.md``)::

        W1 (Alice = owner)              W2 (Bob = owner)
          ├ Carol (admin)                 └ (no extra members)
          ├ Dave  (user / "member")
          ├ Eve   (non-member, internal user only)
          ├ service_w1_a → project_w1_basic    (bound, no user MCP)
          ├ service_w1_b → (free; rebind target)
          ├ service_w1_c → project_w1_with_mcp (bound, 1 user-scope MCP)
          └ ─                              ├ service_w2_a → project_w2 (bound)
                                           └ service_w2_b → (free; cross-ws rebind target)

    A separate cloud_service per project is required because
    ``project_project.py:32-38`` enforces ``UNIQUE(cloud_service_id)``;
    rebind / cross-workspace rebind tests therefore need free target
    services in both workspaces.
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
        self.alice = _new_user(self.env, 'pd_alice', 'Alice (PD)')
        self.bob = _new_user(self.env, 'pd_bob', 'Bob (PD)')
        self.carol = _new_user(self.env, 'pd_carol', 'Carol (PD)')
        self.dave = _new_user(self.env, 'pd_dave', 'Dave (PD)')
        self.eve = _new_user(self.env, 'pd_eve', 'Eve (PD)')

        # ---- Workspaces (owner access auto-created at workspace.py:73-86)
        self.ws_w1 = self.Workspace.sudo().create({
            'name': 'PD W1', 'owner_id': self.alice.id,
        })
        self.ws_w2 = self.Workspace.sudo().create({
            'name': 'PD W2', 'owner_id': self.bob.id,
        })

        # ---- Extra W1 members --------------------------------------- #
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
            'name': 'PD Template',
            'slug': f'pd-tpl-{uuid.uuid4().hex[:8]}',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'pd-tpl',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
        })

        # ---- Cloud services ----------------------------------------- #
        self.service_w1_a = self.Service.sudo().create({
            'name': 'PD W1 Svc A',
            'workspace_id': self.ws_w1.id,
            'template_id': self.template.id,
            'helm_release_name': 'pd-w1-a',
            'helm_namespace': 'paas-ws-pd-w1-a',
        })
        self.service_w1_b = self.Service.sudo().create({
            'name': 'PD W1 Svc B',
            'workspace_id': self.ws_w1.id,
            'template_id': self.template.id,
            'helm_release_name': 'pd-w1-b',
            'helm_namespace': 'paas-ws-pd-w1-b',
        })
        self.service_w1_c = self.Service.sudo().create({
            'name': 'PD W1 Svc C',
            'workspace_id': self.ws_w1.id,
            'template_id': self.template.id,
            'helm_release_name': 'pd-w1-c',
            'helm_namespace': 'paas-ws-pd-w1-c',
        })
        self.service_w2_a = self.Service.sudo().create({
            'name': 'PD W2 Svc A',
            'workspace_id': self.ws_w2.id,
            'template_id': self.template.id,
            'helm_release_name': 'pd-w2-a',
            'helm_namespace': 'paas-ws-pd-w2-a',
        })
        self.service_w2_b = self.Service.sudo().create({
            'name': 'PD W2 Svc B',
            'workspace_id': self.ws_w2.id,
            'template_id': self.template.id,
            'helm_release_name': 'pd-w2-b',
            'helm_namespace': 'paas-ws-pd-w2-b',
        })

        # ---- Projects ----------------------------------------------- #
        self.project_w1_basic = self.Project.sudo().create({
            'name': 'PD W1 Basic Project',
            'description': 'Initial description.',
            'cloud_service_id': self.service_w1_a.id,
        })
        self.project_w1_with_mcp = self.Project.sudo().create({
            'name': 'PD W1 Project With MCP',
            'description': 'Has one user-scope MCP.',
            'cloud_service_id': self.service_w1_c.id,
        })
        self.project_w2 = self.Project.sudo().create({
            'name': 'PD W2 Project',
            'description': 'In Bob workspace.',
            'cloud_service_id': self.service_w2_a.id,
        })

        # ---- One user-scope MCP under project_w1_with_mcp ----------- #
        self.user_mcp_w1 = self.McpServer.sudo().create({
            'name': 'PD W1 User MCP',
            'url': 'http://pd-w1-c-mcp.paas-ws-pd-w1-c.svc.cluster.local:3001/mcp',
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

        Asserts up-front that the endpoint exists (status 200, JSON body
        with a ``result`` key). In the red phase the endpoint URL does
        not resolve and this helper fails fast with a clear message.
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
                f"(status={response.status_code}). The new project-centric "
                f"endpoint is likely not registered yet. "
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
    def _url_detail(project_id):
        # AC D4 / OR-6: hyphenated resource name.
        return f'/api/support-projects/{project_id}'


# =========================================================================== #
# AC #1 — action=get                                                          #
# =========================================================================== #


@tagged('post_install', '-at_install')
class TestProjectDetailGet(TestProjectDetailFixture):

    def test_get_returns_full_project_data_for_owner(self):
        """AC #1: ``action=get`` returns the documented project shape.

        Locks every key listed in Plan AC #1: id, name, description,
        cloud_service_id, cloud_service_name, workspace_id,
        workspace_name, mcp_server_ids, task_count, open_task_count,
        created_date. ``status`` is intentionally absent (D6 carve-out)
        — see ``test_get_does_not_include_status_field``.
        """
        self.authenticate('pd_alice', 'pd_alice')
        result = self._post(
            self._url_detail(self.project_w1_basic.id),
            {'action': 'get'},
        )
        self.assertTrue(
            result.get('success'),
            f"AC #1: owner get must return success=True. Got: {result!r}",
        )
        data = result.get('data')
        self.assertIsInstance(
            data, dict, "AC #1: result.data must be a dict",
        )
        self.assertEqual(data.get('id'), self.project_w1_basic.id)
        self.assertEqual(data.get('name'), self.project_w1_basic.name)
        # description is fields.Html (std) → Odoo sanitizer wraps plain text
        # into <p>...</p>. Assert substring + <p> tag presence rather than
        # exact string so future sanitizer tweaks don't break this test.
        description_value = str(data.get('description') or '')
        self.assertIn(
            'Initial description.', description_value,
            f"AC #1: description payload must contain the seeded text "
            f"(sanitizer may wrap with <p>). Got: {description_value!r}",
        )
        self.assertIn(
            '<p>', description_value,
            f"AC #1: description is fields.Html (std) → sanitizer must "
            f"wrap plain text seed into <p>...</p>. Got: {description_value!r}",
        )
        self.assertEqual(data.get('cloud_service_id'), self.service_w1_a.id)
        self.assertEqual(data.get('cloud_service_name'), self.service_w1_a.name)
        self.assertEqual(data.get('workspace_id'), self.ws_w1.id)
        self.assertEqual(data.get('workspace_name'), self.ws_w1.name)
        self.assertIn(
            'mcp_server_ids', data,
            "AC #1: payload must include mcp_server_ids list",
        )
        self.assertIsInstance(data['mcp_server_ids'], list)
        self.assertIn('task_count', data, "AC #1: payload must include task_count")
        self.assertIn(
            'open_task_count', data,
            "AC #1: payload must include open_task_count "
            "(explicitly listed in Plan AC #1)",
        )
        self.assertIn(
            'created_date', data,
            "AC #1: payload must include created_date",
        )

    def test_get_does_not_include_status_field(self):
        """AC #1 / D6 carve-out: response payload must NOT include ``status``.

        Plan §Decisions D6 (and AC #1 explicit "**不含 status 欄位**")
        — issue #48's "Status (Active / Completed / 其他既有狀態)" is a
        字面誤會; Odoo std ``project.project`` has no single status
        field. Backend is **not** required to actively reject ``status``
        payload but the GET response must not invent one.
        """
        self.authenticate('pd_alice', 'pd_alice')
        result = self._post(
            self._url_detail(self.project_w1_basic.id),
            {'action': 'get'},
        )
        self.assertTrue(result.get('success'), f"Got: {result!r}")
        data = result.get('data') or {}
        self.assertNotIn(
            'status', data,
            "AC #1 / D6: GET payload must not include a top-level "
            "`status` key. See Plan §D6 carve-out.",
        )

    def test_get_mcp_server_ids_reflects_user_scope_mcp(self):
        """AC #1: mcp_server_ids includes the project's user-scope MCP.

        ``project_w1_with_mcp`` was seeded with one user-scope MCP in
        ``setUp``; the payload's ``mcp_server_ids`` list must surface it
        (the OWL frontend reads from this list to drive McpServersSection
        — Plan §Components / D15 / D16).
        """
        self.authenticate('pd_alice', 'pd_alice')
        result = self._post(
            self._url_detail(self.project_w1_with_mcp.id),
            {'action': 'get'},
        )
        self.assertTrue(result.get('success'), f"Got: {result!r}")
        ids = result['data']['mcp_server_ids']
        # ids may be a list of ints or list of dicts; lock that the
        # MCP we created is referenced one way or the other.
        flat_ids = []
        for entry in ids:
            if isinstance(entry, dict):
                flat_ids.append(entry.get('id'))
            else:
                flat_ids.append(entry)
        self.assertIn(
            self.user_mcp_w1.id, flat_ids,
            "AC #1: project_w1_with_mcp's user-scope MCP must appear in "
            "mcp_server_ids",
        )

    def test_get_admin_can_read(self):
        """AC #6: workspace admin (Carol) can read a project."""
        self.authenticate('pd_carol', 'pd_carol')
        result = self._post(
            self._url_detail(self.project_w1_basic.id),
            {'action': 'get'},
        )
        self.assertTrue(
            result.get('success'),
            f"AC #6: admin must be able to read. Got: {result!r}",
        )

    def test_get_member_can_read(self):
        """AC #6: workspace member with role=user (Dave) can read a project."""
        self.authenticate('pd_dave', 'pd_dave')
        result = self._post(
            self._url_detail(self.project_w1_basic.id),
            {'action': 'get'},
        )
        self.assertTrue(
            result.get('success'),
            f"AC #6: member (role=user) must be able to read. Got: {result!r}",
        )

    def test_get_non_member_returns_unified_error(self):
        """AC #6/#11: non-member returns ``Project not found or access denied``.

        Eve has no workspace access. The endpoint must NOT distinguish
        between "non-member" and "non-existent id" — same error string
        防 id enumeration.
        """
        self.authenticate('pd_eve', 'pd_eve')
        result = self._post(
            self._url_detail(self.project_w1_basic.id),
            {'action': 'get'},
        )
        self.assertFalse(
            result.get('success'),
            f"AC #6: non-member must be denied. Got: {result!r}",
        )
        self.assertEqual(
            result.get('error'), 'Project not found or access denied',
            "AC #6/#11: non-member error string must be exactly "
            "'Project not found or access denied' to match the "
            "non-existent-id path (防 enumeration).",
        )

    def test_get_nonexistent_id_returns_unified_error(self):
        """AC #11: nonexistent project id returns the same error string as non-member case."""
        self.authenticate('pd_alice', 'pd_alice')
        result = self._post(
            self._url_detail(999_999_999),
            {'action': 'get'},
        )
        self.assertFalse(
            result.get('success'),
            f"AC #11: nonexistent id must be rejected. Got: {result!r}",
        )
        self.assertEqual(
            result.get('error'), 'Project not found or access denied',
            "AC #11: nonexistent-id error string must be exactly "
            "'Project not found or access denied' (same as AC #6 "
            "non-member path, 防 enumeration).",
        )

    def test_get_cross_workspace_project_returns_unified_error(self):
        """AC #6/#11: W1-only member reading a W2 project gets unified error.

        Carol is admin in W1, has no access record on W2. Reading
        ``project_w2`` (bound to W2) must return the same error string
        as the non-existent-id and Eve cases.
        """
        self.authenticate('pd_carol', 'pd_carol')
        result = self._post(
            self._url_detail(self.project_w2.id),
            {'action': 'get'},
        )
        self.assertFalse(
            result.get('success'),
            f"Cross-workspace get must be denied. Got: {result!r}",
        )
        self.assertEqual(
            result.get('error'), 'Project not found or access denied',
            "Cross-workspace error string must match the unified "
            "防-enumeration message.",
        )


# =========================================================================== #
# AC #2 — action=update (name / description / cloud_service_id)               #
# =========================================================================== #


@tagged('post_install', '-at_install')
class TestProjectDetailUpdateName(TestProjectDetailFixture):

    def test_update_name_by_owner_succeeds(self):
        """AC #2: owner can update project.name; payload echoes updated data."""
        self.authenticate('pd_alice', 'pd_alice')
        result = self._post(
            self._url_detail(self.project_w1_basic.id),
            {'action': 'update', 'name': 'Renamed By Owner'},
        )
        self.assertTrue(
            result.get('success'),
            f"AC #2: owner update name must succeed. Got: {result!r}",
        )
        self.project_w1_basic.invalidate_recordset()
        self.assertEqual(self.project_w1_basic.name, 'Renamed By Owner')

    def test_update_description_by_admin_succeeds(self):
        """AC #2: admin can update project.description.

        ``description`` is std ``fields.Html`` — the sanitizer wraps plain
        text input with ``<p>...</p>`` on write. Assert substring + tag
        presence rather than exact string so sanitizer tweaks don't break
        this test (Option 2 per the description-html re-loop guidance).
        """
        self.authenticate('pd_carol', 'pd_carol')
        result = self._post(
            self._url_detail(self.project_w1_basic.id),
            {'action': 'update', 'description': 'Edited by admin.'},
        )
        self.assertTrue(
            result.get('success'),
            f"AC #2: admin update description must succeed. Got: {result!r}",
        )
        self.project_w1_basic.invalidate_recordset()
        stored = str(self.project_w1_basic.description or '')
        self.assertIn(
            'Edited by admin.', stored,
            f"AC #2: stored description must contain the submitted text "
            f"(sanitizer may wrap with <p>). Got: {stored!r}",
        )
        self.assertIn(
            '<p>', stored,
            f"AC #2: description is fields.Html (std) → stored value must "
            f"be wrapped with <p>...</p>. Got: {stored!r}",
        )

    def test_update_by_member_returns_permission_denied(self):
        """AC #6: member (role=user) gets exactly ``Permission denied`` on write.

        Member can read (covered by ``test_get_member_can_read``) but
        must be blocked on write with the exact string ``Permission
        denied`` (Plan AC #6 stated wording — distinguishes from the
        ``Project not found or access denied``防-enumeration message).
        """
        self.authenticate('pd_dave', 'pd_dave')
        result = self._post(
            self._url_detail(self.project_w1_basic.id),
            {'action': 'update', 'name': 'Hijack attempt'},
        )
        self.assertFalse(
            result.get('success'),
            f"AC #6: member write must be denied. Got: {result!r}",
        )
        self.assertEqual(
            result.get('error'), 'Permission denied',
            "AC #6: member write must return exactly 'Permission "
            "denied' (distinct from the防-enumeration message for "
            "non-members).",
        )
        # Sanity — record stays untouched.
        self.project_w1_basic.invalidate_recordset()
        self.assertNotEqual(self.project_w1_basic.name, 'Hijack attempt')

    def test_update_by_non_member_returns_unified_error(self):
        """AC #6: non-member write returns the unified防-enumeration string.

        Non-member must not see ``Permission denied`` (which would leak
        the project's existence); they get the same
        ``Project not found or access denied`` as nonexistent ids
        (Plan AC #6 / #11).
        """
        self.authenticate('pd_eve', 'pd_eve')
        result = self._post(
            self._url_detail(self.project_w1_basic.id),
            {'action': 'update', 'name': 'Hijack from outside'},
        )
        self.assertFalse(
            result.get('success'),
            f"AC #6: non-member write must be denied. Got: {result!r}",
        )
        self.assertEqual(
            result.get('error'), 'Project not found or access denied',
            "AC #6/#11: non-member write must use the防-enumeration "
            "string (NOT 'Permission denied') so existence is not "
            "leaked.",
        )


# =========================================================================== #
# AC #3 — action=update on cloud_service_id (rebind / unbind / D5)            #
# =========================================================================== #


@tagged('post_install', '-at_install')
class TestProjectDetailRebind(TestProjectDetailFixture):

    def test_rebind_to_same_workspace_service_succeeds(self):
        """AC #3 (invariant #1): rebind to another service in the same workspace succeeds."""
        self.authenticate('pd_alice', 'pd_alice')
        result = self._post(
            self._url_detail(self.project_w1_basic.id),
            {'action': 'update', 'cloud_service_id': self.service_w1_b.id},
        )
        self.assertTrue(
            result.get('success'),
            f"AC #3: same-workspace rebind must succeed. Got: {result!r}",
        )
        self.project_w1_basic.invalidate_recordset()
        self.assertEqual(
            self.project_w1_basic.cloud_service_id.id, self.service_w1_b.id,
            "AC #3: project.cloud_service_id must point at the new "
            "service after successful rebind.",
        )

    def test_rebind_to_different_workspace_service_rejected(self):
        """AC #3 (invariant #1): rebind to a service in a different workspace is rejected.

        ``service_w2_b`` belongs to Bob's workspace. Alice (owner of W1)
        attempting to rebind ``project_w1_basic`` to a W2 service must
        fail; the original binding must survive.
        """
        self.authenticate('pd_alice', 'pd_alice')
        original_cs = self.project_w1_basic.cloud_service_id.id
        result = self._post(
            self._url_detail(self.project_w1_basic.id),
            {'action': 'update', 'cloud_service_id': self.service_w2_b.id},
        )
        self.assertFalse(
            result.get('success'),
            f"AC #3: cross-workspace rebind must be rejected. Got: {result!r}",
        )
        self.project_w1_basic.invalidate_recordset()
        self.assertEqual(
            self.project_w1_basic.cloud_service_id.id, original_cs,
            "AC #3: rejected rebind must leave project.cloud_service_id "
            "unchanged (no partial write).",
        )

    def test_unbind_without_user_mcp_succeeds(self):
        """AC #3: ``cloud_service_id=False`` succeeds when no user-scope MCP exists.

        ``project_w1_basic`` has no MCP rows in setUp — unbind should
        work and leave ``cloud_service_id`` empty (NULL).
        """
        self.authenticate('pd_alice', 'pd_alice')
        result = self._post(
            self._url_detail(self.project_w1_basic.id),
            {'action': 'update', 'cloud_service_id': False},
        )
        self.assertTrue(
            result.get('success'),
            f"AC #3: unbind without user MCP must succeed. Got: {result!r}",
        )
        self.project_w1_basic.invalidate_recordset()
        self.assertFalse(
            self.project_w1_basic.cloud_service_id,
            "AC #3: after unbind, project.cloud_service_id must be empty/NULL.",
        )

    def test_rebind_with_user_mcp_to_same_workspace_succeeds(self):
        """AC #3: rebind a project_with_mcp to another same-ws service still succeeds.

        Per Plan AC #3 third sub-bullet: rebind does NOT change the
        existing MCP URL (URL is stored, not recomputed); only the
        owning service changes. D5 is still satisfied because the new
        cloud_service_id is not NULL.
        """
        self.authenticate('pd_alice', 'pd_alice')
        original_mcp_url = self.user_mcp_w1.url
        result = self._post(
            self._url_detail(self.project_w1_with_mcp.id),
            {'action': 'update', 'cloud_service_id': self.service_w1_b.id},
        )
        self.assertTrue(
            result.get('success'),
            f"AC #3: rebind with user MCP to same-ws service must "
            f"succeed. Got: {result!r}",
        )
        self.project_w1_with_mcp.invalidate_recordset()
        self.assertEqual(
            self.project_w1_with_mcp.cloud_service_id.id,
            self.service_w1_b.id,
        )
        # MCP URL must NOT be recomputed by the endpoint (AC #3 third
        # sub-bullet: "rebind 不改既有 MCP 的 URL").
        self.user_mcp_w1.invalidate_recordset()
        self.assertEqual(
            self.user_mcp_w1.url, original_mcp_url,
            "AC #3: rebind must not recompute existing MCP URL "
            "(URL is stored, not derived).",
        )


# =========================================================================== #
# AC D4 / OR-6 — URL pattern sanity                                           #
# =========================================================================== #


@tagged('post_install', '-at_install')
class TestProjectDetailUrlPattern(TestProjectDetailFixture):

    def test_hyphenated_url_pattern_is_active(self):
        """D4 / OR-6: the new endpoint lives under ``/api/support-projects/<id>`` (hyphenated).

        The existing slash pattern ``/api/support/projects/<wid>`` (in
        ``controllers/ai_assistant.py:705-757``) lists / creates
        projects scoped to a workspace and conflicts with a per-project
        path segment. The Plan §D4 末段 settles on the hyphenated form
        for the per-project resource; OR-6 tracks a future URL refactor
        to nest under workspace.

        This test posts a minimal ``action=get`` to the hyphenated URL
        and asserts the helper's status-code gate passes. In the green
        phase any valid response (success/failure) confirms the route
        is registered; status 404 → red.
        """
        self.authenticate('pd_alice', 'pd_alice')
        # `_post` already asserts HTTP 200 + has `result` key — that's
        # the URL-pattern check.
        result = self._post(
            self._url_detail(self.project_w1_basic.id),
            {'action': 'get'},
        )
        self.assertIn(
            'success', result,
            "D4 / OR-6: hyphenated URL must reach the handler and "
            "return a `success` flag in the JSON-RPC result envelope.",
        )


# =========================================================================== #
# OR-4 endpoint cleanup — Plan AC #25                                         #
#                                                                             #
# Plan: docs/plans/2026-Q2/2026-05-20-mcp-refactor-url-redesign-plan.md       #
#   AC #25: `_update_support_project` (paas.py:1504-1556) unbind path 的       #
#   D5 manual query (line 1528-1547) **整段刪除**. `vals['cloud_service_id'] #
#   = False` 直接 write，**不檢查** lingering user-scope MCP. R-4 不補.       #
#                                                                             #
# Rationale: D5 拔了，sidecar-backed filter (AC #21/#22) 自然處理 cleanup; 既  #
# 有 endpoint manual query 不再需要 + 與 B 案「unbound project 可掛 user-     #
# scope MCP」(AC #6) 衝突 — 必須刪。                                          #
#                                                                             #
# Red expectation today (pre-implementation, 2026-05-20):                     #
#   * test_unbind_succeeds_with_external_user_mcp_present → RED：今天          #
#     manual query 擋 unbind 帶 user-scope MCP project (PR #53                #
#     test_unbind_with_user_mcp_rejected_via_endpoint_manual_query lock)。    #
#     B 案後此邏輯反而錯 — unbind 必須成功；assertTrue(result.success) 失敗。 #
#   * test_unbind_succeeds_with_sidecar_backed_mcp_present → RED：同上但帶    #
#     sidecar_backed=True fixture。                                            #
#   * test_existing_or4_manual_query_path_removed → RED：grep paas.py        #
#     `_update_support_project` 內仍有「lingering user-scope MCP」query 邏輯。 #
# =========================================================================== #


@tagged('post_install', '-at_install')
class TestProjectDetailUnbindOr4Cleanup(TestProjectDetailFixture):
    """D group — Plan AC #25, OR-4 endpoint cleanup debt 收."""

    def test_unbind_succeeds_with_external_user_mcp_present(self):
        """AC #25: unbind a project with external user-scope MCP must
        SUCCEED (B 案 / OQ-1 — schema 不再 enforce bound-only).

        The fixture seeds ``project_w1_with_mcp`` with one user-scope
        MCP (``auto_created=True`` — would map to ``sidecar_backed=True``
        post-migration; here we test the **external** case by creating
        a sibling external MCP and unbinding).

        After implementation: ``_update_support_project`` writes
        ``cloud_service_id=False`` directly, no manual lingering-MCP
        guard. The external MCP row survives (lifecycle decoupled from
        service per New Inv #9 / cleanup filter AC #21).

        Caveat per plan §AC #25 second bullet: sidecar-backed MCP would
        be in a hybrid weird state (URL is cluster.local but service
        still attached) — that's user-correctable; not a zombie.

        **Red today**：``_update_support_project`` (paas.py:1528-1547)
        currently blocks unbind with a manual query — ``assertTrue
        (result.success)`` fails.
        """
        # Seed an external (sidecar_backed=False) user-scope MCP
        # alongside the fixture's auto_created one. We don't write
        # sidecar_backed explicitly — field may not exist pre-impl;
        # the fixture's pre-existing user_mcp_w1 already covers the
        # "any user-scope MCP present" condition.
        external_mcp = self.McpServer.sudo().create({
            'name': 'PD External MCP (D group)',
            'url': 'https://mcp.example.com/sse',
            'transport': 'sse',
            'scope': 'user',
            'project_id': self.project_w1_with_mcp.id,
        })

        self.authenticate('pd_alice', 'pd_alice')
        result = self._post(
            self._url_detail(self.project_w1_with_mcp.id),
            {'action': 'update', 'cloud_service_id': False},
        )
        self.assertTrue(
            result.get('success'),
            "AC #25: unbind with external user-scope MCP present must "
            "SUCCEED post-refactor. paas.py:_update_support_project "
            "manual query (line 1528-1547) must be deleted. Pre-impl "
            "this test red-locks the current rejection behaviour. "
            f"Got: {result!r}.",
        )

        # Project actually unbound.
        self.project_w1_with_mcp.invalidate_recordset()
        self.assertFalse(
            self.project_w1_with_mcp.cloud_service_id,
            "AC #25: project.cloud_service_id must be NULL after "
            "successful unbind.",
        )

        # External MCP survives unbind — its lifecycle is user-managed.
        external_mcp.invalidate_recordset()
        self.assertTrue(
            external_mcp.exists(),
            "AC #25: external user-scope MCP must survive its project's "
            "unbind (lifecycle decoupled from service per New Inv #9).",
        )

    def test_unbind_succeeds_with_sidecar_backed_mcp_present(self):
        """AC #25: unbind with **sidecar_backed=True** MCP also succeeds.

        Caveat per plan §AC #25 second bullet: sidecar-backed MCP enters
        a "hybrid weird state" (URL is cluster.local pointing at the
        still-running service while project is now unbound). Not a
        zombie — user can re-bind or delete the MCP. Endpoint does NOT
        block this.

        Setup: the fixture's ``self.user_mcp_w1`` was created with
        ``auto_created=True`` (today's pre-rename) — post-implementation
        the migration backfills it to ``sidecar_backed=True``. For the
        test we write the field explicitly so the test is independent
        of migration order.

        **Red today**：same root cause — manual query (lambda s:
        s.scope == 'user') matches the sidecar-backed row → unbind
        blocked → result.success=False.
        """
        # Make sure the fixture's user_mcp_w1 is marked sidecar_backed
        # (this is the state post-AC-#3 migration). Skip cleanly if
        # field doesn't exist — surfaces field-missing as a B-group
        # signal not a D-group false red.
        if 'sidecar_backed' not in self.McpServer._fields:
            self.fail(
                "Fixture-side schema guard: mcp_server.sidecar_backed "
                "missing — Plan AC #2 not yet implemented. D-group tests "
                "depend on the field. (See B-group setUp for the "
                "canonical schema guard.)"
            )
        self.user_mcp_w1.sudo().write({'sidecar_backed': True})

        self.authenticate('pd_alice', 'pd_alice')
        result = self._post(
            self._url_detail(self.project_w1_with_mcp.id),
            {'action': 'update', 'cloud_service_id': False},
        )
        self.assertTrue(
            result.get('success'),
            "AC #25: unbind with sidecar_backed=True user-scope MCP "
            "present must SUCCEED post-refactor. Hybrid weird state is "
            "intentional per Plan §AC #25 second bullet caveat. "
            f"Got: {result!r}.",
        )

        self.project_w1_with_mcp.invalidate_recordset()
        self.assertFalse(
            self.project_w1_with_mcp.cloud_service_id,
            "AC #25: unbind must complete even with sidecar_backed MCP.",
        )

    def test_existing_or4_manual_query_path_removed_from_source(self):
        """AC #25: source grep — manual lingering-MCP query gone.

        Grep ``paas.py`` for the marker substring that the current
        manual query uses; refactor must remove it. More robust than
        attribute reflection (the entire branch is plain Python control
        flow, no method to reflect on).

        Locks the specific pattern from line 1534:
        ``lingering = project.mcp_server_ids.filtered(...)``. The var
        name ``lingering`` is unique enough to be a reliable removal
        marker.

        **Red today**：var name ``lingering`` still in paas.py → assert
        not in source fails.
        """
        import inspect, os
        from odoo.addons.woow_paas_platform.controllers import paas as paas_module
        paas_path = inspect.getsourcefile(paas_module)
        self.assertTrue(
            paas_path and os.path.exists(paas_path),
            f"Test sanity: cannot locate paas.py source.",
        )
        with open(paas_path, encoding='utf-8') as fh:
            paas_source = fh.read()
        self.assertNotIn(
            'lingering = project.mcp_server_ids',
            paas_source,
            "AC #25: `lingering = project.mcp_server_ids` lookup still "
            "in `_update_support_project`. The manual query block at "
            "paas.py:1528-1547 must be deleted per Plan AC #25.",
        )
