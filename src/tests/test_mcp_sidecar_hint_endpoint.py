"""TDD red phase — sidecar-hint endpoint + create-with-sidecar_backed flag.

Plan: docs/plans/2026-Q2/2026-05-20-mcp-refactor-url-redesign-plan.md
  - AC #27: ``/api/support-projects/<id>/mcp-servers`` 新增 action=sidecar-hint。
    回 ``{success, data: {url, transport, api_key, suggested_name}}`` 給
    bound + mcp_enabled template；其餘狀態回 ``{success: False, error:
    'No sidecar available for this project'}``.
  - AC #28: ``action=create`` 接受 ``sidecar_backed`` payload field（bool,
    default False），寫入 mcp_server。Endpoint **不** enforce「sidecar_backed
    ↔ URL is cluster.local」一致性（frontend 負責 mental model）.
  - AC #29: sidecar-hint action 套 ``ROLE_OWNER / ROLE_ADMIN``（同 create 層
    級）；non-member 統一回「Project not found or access denied」防 enum
    （與 PR #53 對齊）。

Design context（B 案 OQ-1 / OQ-6）:
  - UI 拆兩按鈕「Add Sidecar MCP」/「Add External MCP」(AC #33)
  - 「Add Sidecar MCP」流程：點擊 → 呼叫 sidecar-hint → 拿 url/transport/
    api_key/suggested_name 預填 form modal → 帶 sidecar_backed=True
    commit
  - Endpoint sidecar-hint 是 read-only computation；不寫 DB

Red expectation today (pre-implementation, 2026-05-20):

* sidecar-hint action 不存在 → endpoint 回 ``{success: False, error:
  'Unknown action: sidecar-hint'}`` （參 ``paas.py:1590`` fallback）→
  ``assertTrue(result.get('success'))`` 失敗 with clean assertion message。
* AC #28 ``sidecar_backed`` payload field — 今天 ``_create_project_mcp_
  server`` (paas.py:1601) 沒讀 ``params.get('sidecar_backed')`` → MCP
  row 落 default ``sidecar_backed=False``（甚至 field 不存在會直接
  AttributeError；fixture-side guard 已在 B group / setUp 處理）→
  assertion 失敗。
* AC #29 ACL — 既有 ``api_support_project_mcp_servers`` (paas.py:1561) 已
  套 ``ROLE_OWNER / ROLE_ADMIN`` 對 write_actions；sidecar-hint 不在
  write_actions tuple 內，今天會走 fallback 'Unknown action'，看不到
  permission 拒絕 → assertion 失敗.

Fixture：reuse pattern from ``test_project_detail_endpoints.py``（HttpCase +
Alice/Bob 雙 workspace + Carol/Dave/Eve 三角色 + mcp_enabled template）.
"""
import json as json_lib
import uuid

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
class TestSidecarHintEndpoint(HttpCase):
    """E group — Plan AC #27, #28, #29."""

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
        # skill-binding-ux：portal settings flag 預設 fail-closed off — 本
        # class 打 MCP write endpoint，需開 flag 保留正向行為守線（flag-off
        # denied 守線在 test_portal_settings_flag.py）。
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.portal_project_settings_editable', 'True')

        # ---- Users -------------------------------------------------- #
        self.alice = _new_user(self.env, 'shp_alice', 'Alice (SHP)')
        self.bob = _new_user(self.env, 'shp_bob', 'Bob (SHP)')
        self.dave = _new_user(self.env, 'shp_dave', 'Dave (SHP)')   # W1 user-role
        self.eve = _new_user(self.env, 'shp_eve', 'Eve (SHP)')      # non-member

        # ---- Workspaces --------------------------------------------- #
        self.ws_w1 = self.Workspace.sudo().create({
            'name': 'SHP W1', 'owner_id': self.alice.id,
        })
        self.ws_w2 = self.Workspace.sudo().create({
            'name': 'SHP W2', 'owner_id': self.bob.id,
        })
        # Dave joins W1 as 'user' role (members not allowed to create
        # MCP / not allowed sidecar-hint).
        self.WorkspaceAccess.sudo().create({
            'workspace_id': self.ws_w1.id, 'user_id': self.dave.id,
            'role': 'user',
        })

        # ---- Templates ---------------------------------------------- #
        # mcp_enabled template — backs the bound + hintable project.
        self.template_mcp = self.Template.sudo().create({
            'name': 'SHP Tmpl MCP',
            'slug': f'shp-tpl-mcp-{uuid.uuid4().hex[:8]}',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'shp-tpl-mcp',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
            'mcp_enabled': True,
            'mcp_sidecar_image': 'ghcr.io/test/mcp:latest',
            'mcp_sidecar_port': 3001,
            'mcp_transport': 'streamable_http',
            'mcp_endpoint_path': '/mcp',
        })
        # mcp_disabled template — for AC #27 third branch.
        self.template_no_mcp = self.Template.sudo().create({
            'name': 'SHP Tmpl NoMCP',
            'slug': f'shp-tpl-no-mcp-{uuid.uuid4().hex[:8]}',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'shp-tpl-no-mcp',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
            'mcp_enabled': False,
        })

        # ---- Services + Projects ----------------------------------- #
        # Bound, mcp_enabled — hint should succeed.
        self.service_bound_mcp = _internal_provision(self.Service.sudo()).create({
            'name': 'SHP Bound Svc MCP',
            'workspace_id': self.ws_w1.id,
            'template_id': self.template_mcp.id,
            'helm_release_name': 'shp-bound-mcp',
            'helm_namespace': 'paas-ws-shp',
            'mcp_auth_token': str(uuid.uuid4()),
        })
        self.project_bound_mcp = self.Project.sudo().create({
            'name': 'SHP Bound Project MCP',
            'cloud_service_id': self.service_bound_mcp.id,
        })

        # Bound, mcp_disabled — hint should fail.
        self.service_bound_no_mcp = _internal_provision(self.Service.sudo()).create({
            'name': 'SHP Bound Svc NoMCP',
            'workspace_id': self.ws_w1.id,
            'template_id': self.template_no_mcp.id,
            'helm_release_name': 'shp-bound-no-mcp',
            'helm_namespace': 'paas-ws-shp',
        })
        self.project_bound_no_mcp = self.Project.sudo().create({
            'name': 'SHP Bound Project NoMCP',
            'cloud_service_id': self.service_bound_no_mcp.id,
        })

        # Unbound (no service) — hint should fail with 'No sidecar
        # available' wording. Alice (the test caller) must be subscribed
        # as project follower so the M-R2 hotfix in
        # ``paas.py:_resolve_project_access`` (HC-2 fix-loop, 2026-05-21)
        # does not short-circuit with 'Project not found or access
        # denied' before the sidecar-hint action branch executes —
        # sudo().create skips the auto-follow hook so we have to
        # subscribe Alice explicitly.
        self.project_unbound = self.Project.sudo().create({
            'name': 'SHP Unbound Project',
        })
        self.project_unbound.message_subscribe(
            partner_ids=[self.alice.partner_id.id],
        )

    # ------------------------------------------------------------------ #
    # JSON-RPC helper (mirrors test_project_detail_endpoints._post)       #
    # ------------------------------------------------------------------ #
    def _post(self, url, params):
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
                f"(status={response.status_code}). "
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
            f"Endpoint {url!r} returned no JSON-RPC `result` key. "
            f"Implementation likely raised inside the handler. "
            f"Body: {body!r}",
        )
        return body['result']

    @staticmethod
    def _url_mcp_servers(project_id):
        return f'/api/support-projects/{project_id}/mcp-servers'

    # ------------------------------------------------------------------ #
    # AC #27: sidecar-hint happy path                                     #
    # ------------------------------------------------------------------ #
    def test_sidecar_hint_returns_correct_payload_for_bound_n8n_project(self):
        """AC #27: bound + mcp_enabled project returns hint payload.

        Asserts the four mandatory fields in ``data``:

          - ``url``      — must be the cluster.local sidecar URL
                           (matches ``cloud_service._model_build_mcp_endpoint_url``
                           algorithm — AC #15: algorithm 不變).
          - ``transport`` — copied from ``template.mcp_transport``.
          - ``api_key``   — copied from ``service.mcp_auth_token``.
          - ``suggested_name`` — ``f"{service.name} MCP"`` (AC #27).

        **Red today**：sidecar-hint action is unknown → falls into the
        ``'Unknown action'`` branch (paas.py:1590) → ``result.success ==
        False``. The first assertion below short-circuits with a clean
        failure naming AC #27.
        """
        self.authenticate('shp_alice', 'shp_alice')
        result = self._post(
            self._url_mcp_servers(self.project_bound_mcp.id),
            {'action': 'sidecar-hint'},
        )
        self.assertTrue(
            result.get('success'),
            "AC #27: sidecar-hint action on bound + mcp_enabled project "
            f"must return success=True. Got: {result!r}. If error is "
            "'Unknown action: sidecar-hint', the endpoint branch in "
            "`paas.py:api_support_project_mcp_servers` is not yet wired.",
        )
        data = result.get('data') or {}
        self.assertIsInstance(
            data, dict,
            "AC #27: result.data must be a dict shape "
            "{url, transport, api_key, suggested_name}.",
        )
        # URL — cluster.local algorithm.
        self.assertIn(
            'url', data,
            "AC #27: data.url missing — sidecar URL hint not computed.",
        )
        self.assertIn(
            'shp-bound-mcp-mcp.paas-ws-shp.svc.cluster.local',
            data['url'] or '',
            "AC #27 / AC #15: data.url must follow the cluster.local "
            "algorithm "
            "`http://{release}-mcp.{namespace}.svc.cluster.local:{port}"
            "{path}`. Got: %r" % (data.get('url'),),
        )
        # transport — copied from template.
        self.assertEqual(
            data.get('transport'), 'streamable_http',
            "AC #27: data.transport must come from template.mcp_transport.",
        )
        # api_key — copied from service.
        self.assertEqual(
            data.get('api_key'),
            self.service_bound_mcp.mcp_auth_token,
            "AC #27: data.api_key must come from service.mcp_auth_token.",
        )
        # suggested_name — f"{service.name} MCP"
        self.assertEqual(
            data.get('suggested_name'),
            f'{self.service_bound_mcp.name} MCP',
            "AC #27: data.suggested_name must be "
            "`f\"{service.name} MCP\"`.",
        )

    def test_sidecar_hint_fails_for_unbound_project(self):
        """AC #27: unbound project (no cloud_service_id) returns failure.

        Per plan §AC #27 first bullet: ``service is None`` → return
        ``{success: False, error: 'No sidecar available for this project'}``.

        **Red today**：no sidecar-hint branch exists; today's controller
        returns ``'Unknown action: sidecar-hint'`` which is a wrong error
        message — substring 'No sidecar available' assertion fails.
        """
        self.authenticate('shp_alice', 'shp_alice')
        result = self._post(
            self._url_mcp_servers(self.project_unbound.id),
            {'action': 'sidecar-hint'},
        )
        self.assertFalse(
            result.get('success'),
            "AC #27: sidecar-hint on unbound project must return "
            f"success=False. Got: {result!r}.",
        )
        self.assertIn(
            'No sidecar available',
            result.get('error') or '',
            "AC #27: unbound project sidecar-hint error must mention "
            "'No sidecar available' so frontend can branch UX. Got: %r"
            % (result.get('error'),),
        )

    def test_sidecar_hint_fails_when_template_mcp_disabled(self):
        """AC #27: bound + template.mcp_enabled=False returns same failure.

        Per plan §AC #27 first bullet second condition.

        **Red today**：same root cause — 'Unknown action: sidecar-hint'
        path masks the correct branch logic.
        """
        self.authenticate('shp_alice', 'shp_alice')
        result = self._post(
            self._url_mcp_servers(self.project_bound_no_mcp.id),
            {'action': 'sidecar-hint'},
        )
        self.assertFalse(
            result.get('success'),
            "AC #27: sidecar-hint on bound but mcp_disabled template "
            f"must return success=False. Got: {result!r}.",
        )
        self.assertIn(
            'No sidecar available',
            result.get('error') or '',
            "AC #27: bound + mcp_disabled error must mention 'No sidecar "
            "available'. Got: %r" % (result.get('error'),),
        )

    # ------------------------------------------------------------------ #
    # AC #29: sidecar-hint ACL — non-member / non-admin                   #
    # ------------------------------------------------------------------ #
    def test_sidecar_hint_acl_non_member_denied_with_project_not_found(self):
        """AC #29: non-member of the project's workspace gets
        ``Project not found or access denied`` (id-enum防).

        Eve is W1-and-W2-non-member; calling sidecar-hint on a W1
        project must return the unified deny error message (matching
        PR #53 AC #6 / #11 wording).

        **Red today**：endpoint route exists (the parent endpoint
        ``api_support_project_mcp_servers`` is live), so 404 won't fire;
        instead the unknown-action fallback returns
        ``'Unknown action: sidecar-hint'`` → the asserted substring
        'Project not found or access denied' fails. After implementation,
        ``_resolve_project_access`` denies Eve before action branching →
        unified error wording.
        """
        self.authenticate('shp_eve', 'shp_eve')
        result = self._post(
            self._url_mcp_servers(self.project_bound_mcp.id),
            {'action': 'sidecar-hint'},
        )
        self.assertFalse(
            result.get('success'),
            "AC #29: non-member sidecar-hint must return success=False. "
            f"Got: {result!r}.",
        )
        self.assertEqual(
            result.get('error'), 'Project not found or access denied',
            "AC #29: non-member must receive the unified "
            "'Project not found or access denied' error (防 id "
            "enumeration). Got: %r." % (result.get('error'),),
        )

    def test_sidecar_hint_acl_member_non_admin_returns_permission_denied(self):
        """AC #29: workspace 'user' role (not owner/admin) → 'Permission denied'.

        Dave has W1 'user' role — he can read project detail but
        sidecar-hint requires ``ROLE_OWNER / ROLE_ADMIN`` (same surface
        as create action, per plan §AC #29).

        **Red today**：falls into 'Unknown action' branch — both because
        the action isn't registered and because permission gate is only
        applied to the existing ``write_actions`` tuple (paas.py:1576).
        Implementation must add 'sidecar-hint' to the gated set.
        """
        self.authenticate('shp_dave', 'shp_dave')
        result = self._post(
            self._url_mcp_servers(self.project_bound_mcp.id),
            {'action': 'sidecar-hint'},
        )
        self.assertFalse(
            result.get('success'),
            "AC #29: workspace 'user' role sidecar-hint must return "
            f"success=False. Got: {result!r}.",
        )
        self.assertEqual(
            result.get('error'), 'Permission denied',
            "AC #29: workspace 'user' role must receive 'Permission "
            "denied' (parity with `action=create`'s gate). Got: %r."
            % (result.get('error'),),
        )

    # ------------------------------------------------------------------ #
    # AC #28: create with sidecar_backed flag                             #
    # ------------------------------------------------------------------ #
    def test_create_with_sidecar_backed_true_persists_flag(self):
        """AC #28: ``action=create`` accepts ``sidecar_backed=True`` and
        writes it to the new mcp_server row.

        **Red today**：``_create_project_mcp_server`` (paas.py:1601)
        doesn't read ``params.get('sidecar_backed')`` from payload, so
        the new row's ``sidecar_backed`` defaults to False — assertion
        below fails. (If ``sidecar_backed`` field itself is missing the
        ORM ``create`` won't accept it either; that's a separate
        ``ValueError`` — implementation must add the field per AC #2 +
        propagate the payload key per AC #28.)
        """
        self.authenticate('shp_alice', 'shp_alice')
        result = self._post(
            self._url_mcp_servers(self.project_bound_mcp.id),
            {
                'action': 'create',
                'name': 'Sidecar-Backed Created MCP',
                'url': (
                    'http://shp-bound-mcp-mcp.paas-ws-shp.svc.cluster.local'
                    ':3001/mcp'
                ),
                'transport': 'streamable_http',
                'sidecar_backed': True,
            },
        )
        self.assertTrue(
            result.get('success'),
            "AC #28: create with sidecar_backed=True must succeed. "
            f"Got: {result!r}.",
        )
        new_id = (result.get('data') or {}).get('id')
        self.assertTrue(
            new_id,
            "AC #28: create response data must surface the new row id.",
        )
        new_mcp = self.McpServer.sudo().browse(new_id)
        self.assertTrue(
            new_mcp.exists(),
            "AC #28: created mcp_server row didn't materialise — "
            "endpoint reported success but DB has no row?",
        )
        self.assertTrue(
            new_mcp.sidecar_backed,
            "AC #28: payload sidecar_backed=True must persist on the "
            "new mcp_server row. Implementation likely didn't read "
            "`params.get('sidecar_backed', False)` in "
            "_create_project_mcp_server (paas.py:1601).",
        )

    def test_create_with_sidecar_backed_false_persists_flag(self):
        """AC #28: ``action=create`` with ``sidecar_backed=False`` (default)
        leaves the flag False.

        Companion to the True-side test. Guards against an implementation
        that hard-codes ``sidecar_backed=True`` regardless of payload.
        """
        self.authenticate('shp_alice', 'shp_alice')
        result = self._post(
            self._url_mcp_servers(self.project_bound_mcp.id),
            {
                'action': 'create',
                'name': 'External Created MCP',
                'url': 'https://mcp.example.com/sse',
                'transport': 'sse',
                'sidecar_backed': False,
            },
        )
        self.assertTrue(
            result.get('success'),
            "AC #28: create with sidecar_backed=False must succeed. "
            f"Got: {result!r}.",
        )
        new_id = (result.get('data') or {}).get('id')
        new_mcp = self.McpServer.sudo().browse(new_id)
        self.assertFalse(
            new_mcp.sidecar_backed,
            "AC #28: sidecar_backed=False payload must remain False — "
            "no implicit upgrade to True (frontend's mental model is "
            "the single source of truth).",
        )

    def test_create_endpoint_enforces_sidecar_url_in_cluster_backed_true_external(self):
        """#271 (reverses AC #28): endpoint now ENFORCES
        ``sidecar_backed=True ⟹ url is in-cluster``.

        Pre-#271 this combination (sidecar_backed=True + external URL) was
        deliberately allowed ("frontend owns the mental model"). #271 reverses
        that: a sidecar-backed row pointing at a public hostname makes the
        in-cluster agent data-plane hit the CF Access login page and makes
        teardown's URL-prefix filter miss the row (orphan). Create must now be
        rejected — controller friendly error + model @api.constrains.
        """
        self.authenticate('shp_alice', 'shp_alice')
        result = self._post(
            self._url_mcp_servers(self.project_bound_mcp.id),
            {
                'action': 'create',
                'name': 'Inconsistent External-but-Backed MCP',
                'url': 'https://external.example.com/sse',
                'transport': 'sse',
                'sidecar_backed': True,
            },
        )
        self.assertFalse(
            result.get('success'),
            "#271: sidecar_backed=True + external URL must be REJECTED. "
            f"Got: {result!r}.",
        )
        self.assertIn(
            'cluster.local', (result.get('error') or '').lower(),
            "#271: rejection error must explain the in-cluster requirement. "
            f"Got: {result!r}.",
        )

    def test_create_endpoint_allows_backed_false_cluster_local(self):
        """#271: the invariant is one-directional
        (``sidecar_backed=True ⟹ in-cluster``). The reverse is NOT enforced:
        ``sidecar_backed=False`` + a cluster.local URL must still succeed
        (a user may hand-copy an existing sidecar URL into the External form;
        we don't auto-upgrade the flag or reject the URL). Orthogonality is
        preserved on the False side only.
        """
        self.authenticate('shp_alice', 'shp_alice')
        result = self._post(
            self._url_mcp_servers(self.project_bound_mcp.id),
            {
                'action': 'create',
                'name': 'Inconsistent Internal-but-Unbacked MCP',
                'url': (
                    'http://shp-bound-mcp-mcp.paas-ws-shp.svc.cluster.local'
                    ':3001/mcp'
                ),
                'transport': 'streamable_http',
                'sidecar_backed': False,
            },
        )
        self.assertTrue(
            result.get('success'),
            "AC #28: sidecar_backed=False + cluster.local URL must NOT "
            "be auto-upgraded or rejected. Got: %r." % (result,),
        )
