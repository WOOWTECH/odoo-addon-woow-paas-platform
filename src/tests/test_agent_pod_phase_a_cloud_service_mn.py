"""TDD red phase — Phase A: cloud_service M:N reverse + cleanup filter delineation.

Plan: docs/plans/2026-Q2/agent-runtime-pod-plan.md §Phase A Task 3
Design: docs/plans/2026-Q2/agent-runtime-pod-design.md
  - §1.3.1 cloud_service.project_ids 改 Many2many reverse（同一 relation table）
  - §1.3.3 對 refactor branch 已 fix 範圍的明確引用
  - §7.0.4 cloud_service._do_cleanup 從 1:N 延伸到 M:N（URL prefix match）
  - §7.0.7 Step 6: through table 反向 M:N

Decision doc invariants referenced (will be updated by reporter — Phase A
落地時必須 stay aligned):
  - [[mcp-project-decouple]] Inv #4 (cleanup hook 順序 + filter 條件)
  - [[mcp-url-redesign]] Inv #9 (sidecar_backed=True user-scope MCP 必由
    ``_do_cleanup`` + ``unlink()`` 兩條 path 清掉)
  - [[cloud-service-unlink-override-cleanup]] pattern (filter 條件擴維度)

Acceptance criteria covered:

C.1  ``cloud_service.project_ids`` is Many2many reverse on the same
     ``project_cloud_service_rel`` relation table (no longer One2many).
C.2  ``_do_cleanup`` filter on M:N service includes URL-prefix match:
     `lambda s: s.scope == 'user' and s.sidecar_backed and (s.url or '').startswith(<this_sidecar_url_prefix>)`
     — purges only THIS sidecar's MCP, preserves user-scope MCP whose
     URL points to a different (still-alive) cloud_service in the same
     project.
C.3  ``unlink()`` override mirrors the cleanup filter (caller-agnostic
     safety net per [[cloud-service-unlink-override-cleanup]]).
C.4  External user-scope MCP (``sidecar_backed=False``) is preserved
     across cloud_service teardown (Inv #9 leg of pattern doc).
C.5  M:N cross-svc preservation: project P binds svc_A + svc_B; both
     have sidecar-backed user-scope MCP rows; tearing down svc_A
     unlinks ONLY svc_A's MCP — svc_B's MCP stays usable.

Red expectation today (Phase A implementation has NOT started):

* C.1 → RED: today ``cloud_service.project_ids`` is declared as
  ``One2many(comodel_name='project.project', inverse_name='cloud_service_id')``
  on ``src/models/cloud_service.py:184-188``; the type assertion fails.
* C.2 → RED: today ``_do_cleanup`` filter is
  ``lambda s: s.scope == 'user' and s.sidecar_backed`` (no URL prefix
  check). Plan AC requires filter to additionally narrow by
  ``s.url.startswith(this_sidecar_url_prefix)``.
* C.3 → RED: ``unlink()`` override uses the same loose filter — must
  also tighten with URL prefix in Phase A.
* C.5 → RED: today the loose filter wipes BOTH svc_A's and svc_B's
  user-scope MCP when svc_A is torn down (M:N cross-svc collateral).

Boundary（刻意不測）:
* sidecar_backed=False external MCP preservation under 1:N (already
  locked by refactor-branch ``test_mcp_sidecar_backed_lifecycle.py``).
* Helm uninstall path itself — mocked away; we only assert ORM-level
  filter outcomes.
* The exact text of url prefix algorithm (cluster.local format) — we
  let the production code build it; tests assert by-prefix matching
  via the seed URL constructed identically.
"""
import uuid

from odoo.tests.common import TransactionCase, tagged


def _sidecar_url(service):
    """Mirror ``cloud_service._model_build_mcp_endpoint_url`` shape.

    Snapshot URL form used by sidecar-backed user-scope MCP rows; this
    test file relies on the shape being stable between refactor branch
    and Phase A. Any change to the production URL builder must be
    reflected here (see [[mcp-url-redesign]] Inv #7 — snapshot algorithm
    is owned by ``cloud_service._model_build_mcp_endpoint_url``).
    """
    return (
        f'http://{service.helm_release_name}-mcp.'
        f'{service.helm_namespace}.svc.cluster.local:3000/mcp'
    )


@tagged('post_install', '-at_install')
class TestPhaseACloudServiceM2NReverse(TransactionCase):
    """C.1 — cloud_service.project_ids is M2M reverse on shared table."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Workspace = cls.env['woow_paas_platform.workspace']
        cls.Service = cls.env['woow_paas_platform.cloud_service']
        cls.Template = cls.env['woow_paas_platform.cloud_app_template']
        cls.Project = cls.env['project.project']

    def setUp(self):
        super().setUp()
        suffix = uuid.uuid4().hex[:8]
        self.ws = self.Workspace.sudo().create({'name': f'WS {suffix}'})
        self.tmpl = self.Template.sudo().create({
            'name': f'Tmpl {suffix}',
            'slug': f'tmpl-{suffix}',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': f'tmpl-{suffix}',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
        })
        self.svc = self.Service.sudo().create({
            'name': f'Svc {suffix}',
            'workspace_id': self.ws.id,
            'template_id': self.tmpl.id,
            'helm_release_name': f'svc-{suffix}',
            'helm_namespace': f'paas-ws-{suffix}',
        })

    def test_cloud_service_project_ids_is_many2many(self):
        """C.1: ``cloud_service.project_ids`` must be Many2many on the
        ``project_cloud_service_rel`` relation table.

        Red today: One2many declared on cloud_service.py:184.
        """
        fld = self.Service._fields.get('project_ids')
        self.assertIsNotNone(
            fld,
            "C.1 prerequisite: cloud_service.project_ids field missing.",
        )
        self.assertEqual(
            fld.type, 'many2many',
            "C.1: cloud_service.project_ids must be Many2many under M:N "
            "(design §1.3.1 / §7.0.2 step 6). Got %s." % fld.type,
        )
        self.assertEqual(
            fld.comodel_name, 'project.project',
            "C.1: comodel must be project.project.",
        )
        self.assertEqual(
            fld.relation, 'project_cloud_service_rel',
            "C.1: relation table must match project.cloud_service_ids "
            "(shared M2M storage).",
        )

    def test_two_projects_can_bind_same_cloud_service(self):
        """C.1 behaviour: from cloud_service side, .project_ids holds
        multiple projects bound to the same service.

        Red today: One2many with ``inverse_name='cloud_service_id'`` +
        UNIQUE constraint forces at most one project — second create
        raises IntegrityError.
        """
        proj1 = self.Project.sudo().create({
            'name': 'C.1 first project',
            'cloud_service_id': self.svc.id,
        })
        try:
            proj2 = self.Project.sudo().create({
                'name': 'C.1 second project (same svc)',
                'cloud_service_id': self.svc.id,
            })
        except Exception as exc:  # noqa: BLE001
            self.fail(
                "C.1: cloud_service.project_ids must accept two projects "
                "(M:N). Got %s: %s." % (type(exc).__name__, exc)
            )
        self.assertIn(proj1.id, self.svc.project_ids.ids)
        self.assertIn(proj2.id, self.svc.project_ids.ids)


@tagged('post_install', '-at_install')
class TestPhaseACleanupFilterDelineation(TransactionCase):
    """C.2, C.3, C.4, C.5 — _do_cleanup + unlink() URL-prefix filter
    delineation under M:N.

    Layout::

        Project P (M:N)
          ├ bound to svc_A
          │   └ sidecar-backed MCP @ url=<svc_A sidecar URL>
          ├ bound to svc_B (different cloud_service, same workspace)
          │   └ sidecar-backed MCP @ url=<svc_B sidecar URL>
          └ external MCP (sidecar_backed=False, arbitrary URL)

        Teardown svc_A → only svc_A's sidecar-backed MCP must be unlinked.
        svc_B's MCP + external MCP must survive.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Workspace = cls.env['woow_paas_platform.workspace']
        cls.Service = cls.env['woow_paas_platform.cloud_service']
        cls.Template = cls.env['woow_paas_platform.cloud_app_template']
        cls.Project = cls.env['project.project']
        cls.McpServer = cls.env['woow_paas_platform.mcp_server']

        # Refactor-branch invariant guard.
        if 'sidecar_backed' not in cls.McpServer._fields:
            raise RuntimeError(
                "Refactor-branch invariant guard: mcp_server.sidecar_backed "
                "missing — Phase A test assumes PR #54 merged."
            )

    def setUp(self):
        super().setUp()
        suffix = uuid.uuid4().hex[:8]
        self.ws = self.Workspace.sudo().create({'name': f'WS {suffix}'})
        self.tmpl = self.Template.sudo().create({
            'name': f'Tmpl {suffix}',
            'slug': f'tmpl-{suffix}',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': f'tmpl-{suffix}',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
        })
        self.svc_a = self.Service.sudo().create({
            'name': f'Svc A {suffix}',
            'workspace_id': self.ws.id,
            'template_id': self.tmpl.id,
            'helm_release_name': f'svc-a-{suffix}',
            'helm_namespace': f'paas-ws-a-{suffix}',
        })
        self.svc_b = self.Service.sudo().create({
            'name': f'Svc B {suffix}',
            'workspace_id': self.ws.id,
            'template_id': self.tmpl.id,
            'helm_release_name': f'svc-b-{suffix}',
            'helm_namespace': f'paas-ws-b-{suffix}',
        })

        if 'cloud_service_ids' not in self.Project._fields:
            self.skipTest(
                "C.* prerequisite: project.cloud_service_ids missing — "
                "see test_agent_pod_phase_a_project_schema.py:A.1"
            )

        self.project = self.Project.sudo().create({
            'name': f'C project {suffix}',
            'cloud_service_ids': [(6, 0, [self.svc_a.id, self.svc_b.id])],
        })

        # Seed three MCP rows.
        self.mcp_a = self.McpServer.sudo().create({
            'name': f'Sidecar MCP for svc_A {suffix}',
            'url': _sidecar_url(self.svc_a),
            'transport': 'streamable_http',
            'scope': 'user',
            'project_id': self.project.id,
            'sidecar_backed': True,
        })
        self.mcp_b = self.McpServer.sudo().create({
            'name': f'Sidecar MCP for svc_B {suffix}',
            'url': _sidecar_url(self.svc_b),
            'transport': 'streamable_http',
            'scope': 'user',
            'project_id': self.project.id,
            'sidecar_backed': True,
        })
        self.mcp_external = self.McpServer.sudo().create({
            'name': f'External MCP {suffix}',
            'url': 'https://mcp.example.com/sse',
            'transport': 'sse',
            'scope': 'user',
            'project_id': self.project.id,
            'sidecar_backed': False,
        })

    # ------------------------------------------------------------------ #
    # C.2 — _do_cleanup filter narrows by URL prefix                      #
    # ------------------------------------------------------------------ #
    def test_do_cleanup_unlinks_only_own_sidecar_mcp(self):
        """C.2 / C.5: tearing down svc_A unlinks ONLY svc_A's
        sidecar-backed MCP. svc_B's MCP and external MCP survive.

        Red today: filter is ``s.scope == 'user' and s.sidecar_backed``
        only; it wipes BOTH svc_A and svc_B's sidecar MCP because they
        both share ``self.project_ids`` under M:N.

        Mocking helm-uninstall: we don't reach helm RPC because the
        filter step runs first. If helm call still fires (and fails in
        this unit test env), we accept that — only the post-cleanup
        ORM state matters for filter delineation.
        """
        self.assertTrue(
            self.mcp_a.exists() and self.mcp_b.exists() and self.mcp_external.exists(),
            "Fixture sanity: all three MCP rows alive pre-cleanup.",
        )
        try:
            self.svc_a._do_cleanup()
        except Exception:  # noqa: BLE001
            # Helm RPC will fail in unit-test env; we only care about
            # the ORM state set by the filter step. _do_cleanup deletes
            # MCP rows before calling helm — so the filter has already
            # run by the time helm raises. Swallow and continue.
            pass

        # svc_A's sidecar MCP MUST be unlinked.
        self.assertFalse(
            self.mcp_a.exists(),
            "C.2: svc_A's sidecar-backed MCP must be unlinked.",
        )
        # svc_B's sidecar MCP MUST survive (its sidecar still alive).
        self.assertTrue(
            self.mcp_b.exists(),
            "C.2 / C.5: svc_B's sidecar-backed MCP must SURVIVE — "
            "M:N cross-svc cleanup must not wipe other cloud_service's "
            "MCP. Plan §Phase A Task 3.2 — filter must add URL-prefix "
            "match; design §7.0.4.",
        )
        # External MCP MUST survive (Inv #9 carry-over from refactor branch).
        self.assertTrue(
            self.mcp_external.exists(),
            "C.4: external (sidecar_backed=False) MCP must survive "
            "any cloud_service teardown (Inv #9 — refactor branch lock).",
        )

    # ------------------------------------------------------------------ #
    # C.3 — unlink() override mirrors the filter                          #
    # ------------------------------------------------------------------ #
    def test_unlink_override_narrows_same_way(self):
        """C.3: ``cloud_service.unlink()`` override must apply the same
        URL-prefix filter as ``_do_cleanup`` (caller-agnostic safety
        net per [[cloud-service-unlink-override-cleanup]]).

        Red today: ``unlink()`` override uses loose
        ``s.scope == 'user' and s.sidecar_backed`` only.
        """
        try:
            self.svc_a.sudo().unlink()
        except Exception:  # noqa: BLE001
            # Same swallow rationale as test_do_cleanup_*.
            pass

        self.assertFalse(
            self.mcp_a.exists(),
            "C.3: svc_A's sidecar-backed MCP unlinked via unlink() path.",
        )
        self.assertTrue(
            self.mcp_b.exists(),
            "C.3: svc_B's sidecar-backed MCP must SURVIVE under M:N — "
            "unlink() override must use the same URL-prefix filter as "
            "_do_cleanup. Plan §Phase A Task 3.3.",
        )
        self.assertTrue(
            self.mcp_external.exists(),
            "C.3 + C.4: external MCP must survive both cleanup paths.",
        )
