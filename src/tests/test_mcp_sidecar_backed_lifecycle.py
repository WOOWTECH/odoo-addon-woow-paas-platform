"""TDD red phase — cleanup hook + unlink override filter must include
``sidecar_backed=True``.

Plan: docs/plans/2026-Q2/2026-05-20-mcp-refactor-url-redesign-plan.md
  - AC #21: ``cloud_service._do_cleanup`` filter from
    ``lambda s: s.scope == 'user'`` → ``lambda s: s.scope == 'user' and
    s.sidecar_backed``
  - AC #22: ``cloud_service.unlink()`` override filter 同 #21
  - AC #23: 順序與 lambda body 不變 — 仍 sudo().unlink()
  - AC #24: PR #47 BLOCKER 2 regression（既有兩 test）必須繼續綠 — 加
    sidecar_backed=True fixture 餵料；system-skip 行為不變

New Invariant #9 (from plan §New invariants):
  - sidecar-backed user-scope MCP 在兩條 cleanup 路徑被清掉，filter 必須
    包含 sidecar_backed=True。**External user-scope MCP**（user 自行
    填的、scope='user' 但 sidecar_backed=False）**不**該被 service 清掉 —
    它與 service 物理 lifecycle 無關，user 自己負責 unlink。

Design context（B 案 OQ-1）:
  - sidecar 物理機制仍在 service 端（template.mcp_enabled / patch_sidecar
    install-time）
  - User-scope MCP 現在分兩種：
    1. sidecar_backed=True — 透過 UI shortcut「Add Sidecar MCP」建立，
       URL 指向 cluster.local sidecar，與 service 同生共死
    2. sidecar_backed=False — 透過「Add External MCP」建立，URL 是
       user-managed external host，與 service lifecycle 解耦
  - 既有 ``auto_created=True`` rows 在 migration 18.0.1.0.25 backfill 成
    ``sidecar_backed=True``（AC #3，由 G group 測）

Red expectation today (pre-implementation, 2026-05-20):

* test_do_cleanup_unlinks_sidecar_backed_only → RED：今天 filter 只看
  ``s.scope == 'user'``，會把 external (sidecar_backed=False) 也一起殺掉 →
  ``assertTrue(external_mcp.exists())`` 失敗.
* test_unlink_override_unlinks_sidecar_backed_only → RED：同上但走
  unlink() override 路徑。
* test_do_cleanup_preserves_external_user_mcp / unlink_override_preserves_
  external_user_mcp → RED：上述 same-fixture 的 external-side 觀察分離成
  兩 method 讓 partial degradation surface（filter 只看 scope='user' 會
  造成此兩 test 同時 RED；implementation 修對後同時 GREEN）.

Cleanup hook test 用 ``patch('...services.paas_operator.get_paas_operator_
client')`` mock（仿 ``test_mcp_project_lifecycle.py:_do_cleanup`` pattern）—
不依賴實 helm，docker-desktop k8s context 即可跑。
"""
import uuid
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestSidecarBackedLifecycleFilter(TransactionCase):
    """B group — Plan AC #21-24, New Invariant #9."""

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
        if 'sidecar_backed' not in self.McpServer._fields:
            self.fail(
                "Fixture-side schema guard: mcp_server.sidecar_backed "
                "field missing — refactor not yet implemented. Plan "
                "AC #2 requires the new boolean field. All B-group tests "
                "depend on it; this is the canonical red message until "
                "the field lands."
            )
        self.workspace = self.Workspace.sudo().create({'name': 'WS SBL'})
        self.template = self.Template.sudo().create({
            'name': 'Tmpl SBL',
            'slug': f'tmpl-sbl-{uuid.uuid4().hex[:8]}',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'tmpl-sbl',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
            'mcp_enabled': True,
            'mcp_sidecar_image': 'ghcr.io/test/mcp:latest',
            'mcp_sidecar_port': 3001,
            'mcp_transport': 'streamable_http',
            'mcp_endpoint_path': '/mcp',
        })

    def _seed_service_with_dual_user_mcps(
        self, name_prefix='SBL', release_name='sbl-svc',
    ):
        """Build the canonical B-group fixture.

        Layout::

            service (helm-backed)
            └─ project (bound to service)
               ├─ user-scope MCP A — sidecar_backed=True  (cluster.local URL)
               └─ user-scope MCP B — sidecar_backed=False (external URL)

        Returns ``(service, project, sidecar_mcp, external_mcp)``.
        """
        service = self.Service.sudo().create({
            'name': f'{name_prefix} Service',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'helm_release_name': release_name,
            'helm_namespace': 'paas-ws-sbl',
            'mcp_auth_token': str(uuid.uuid4()),
        })
        project = self.Project.sudo().create({
            'name': f'{name_prefix} Project',
            'cloud_service_id': service.id,
        })
        sidecar_mcp = self.McpServer.sudo().create({
            'name': f'{name_prefix} Sidecar User MCP',
            'url': (
                f'http://{release_name}-mcp.paas-ws-sbl.svc.cluster.local'
                f':3001/mcp'
            ),
            'transport': 'streamable_http',
            'scope': 'user',
            'project_id': project.id,
            'sidecar_backed': True,
        })
        external_mcp = self.McpServer.sudo().create({
            'name': f'{name_prefix} External User MCP',
            'url': 'https://mcp.example.com/sse',
            'transport': 'sse',
            'scope': 'user',
            'project_id': project.id,
            'sidecar_backed': False,
        })
        return service, project, sidecar_mcp, external_mcp

    # ------------------------------------------------------------------ #
    # AC #21: _do_cleanup filter must include sidecar_backed=True         #
    # ------------------------------------------------------------------ #
    def test_do_cleanup_unlinks_sidecar_backed_only(self):
        """AC #21: ``_do_cleanup`` filter from ``scope == 'user'`` →
        ``scope == 'user' and sidecar_backed``.

        After running ``_do_cleanup`` the sidecar-backed MCP must be
        unlinked (because its URL points at a torn-down sidecar — D1 /
        Invariant #4 ordering still applies), while the external MCP
        must survive (user-managed lifecycle, not service-coupled).

        **Red today**：filter is plain ``s.scope == 'user'`` →
        external_mcp also gets unlinked → ``assertTrue(external.exists())``
        fails. Implementation must add ``and s.sidecar_backed`` to make
        this test green.
        """
        service, project, sidecar_mcp, external_mcp = (
            self._seed_service_with_dual_user_mcps(
                name_prefix='Cleanup', release_name='cleanup-svc',
            )
        )
        sidecar_id = sidecar_mcp.id
        external_id = external_mcp.id

        fake_client = MagicMock()
        with patch(
            'odoo.addons.woow_paas_platform.services.paas_operator'
            '.get_paas_operator_client',
            return_value=fake_client,
        ):
            service._do_cleanup()

        self.assertFalse(
            self.McpServer.sudo().with_context(active_test=False)
            .browse(sidecar_id).exists(),
            "AC #21: sidecar_backed=True user-scope MCP must be removed "
            "by _do_cleanup (the sidecar is being torn down). Current "
            "filter `scope == 'user'` already removes it; this assertion "
            "guards the AC #23 invariant 'sudo().unlink() body 不變'.",
        )

    def test_do_cleanup_preserves_external_user_mcp(self):
        """AC #21 / New Invariant #9: ``_do_cleanup`` must NOT touch
        ``sidecar_backed=False`` (external) user-scope MCP.

        **Red today**：filter is ``scope == 'user'`` (no sidecar_backed
        leg) → external_mcp gets caught by the same `.filtered(...)` →
        unlinked → this assertion fails. After implementation lands the
        new dual-leg filter, the external row survives a sibling
        sidecar-MCP cleanup intact.
        """
        service, project, sidecar_mcp, external_mcp = (
            self._seed_service_with_dual_user_mcps(
                name_prefix='ExtPreserve', release_name='ext-preserve-svc',
            )
        )
        external_id = external_mcp.id

        fake_client = MagicMock()
        with patch(
            'odoo.addons.woow_paas_platform.services.paas_operator'
            '.get_paas_operator_client',
            return_value=fake_client,
        ):
            service._do_cleanup()

        self.assertTrue(
            self.McpServer.sudo().browse(external_id).exists(),
            "AC #21 / New Invariant #9: external (sidecar_backed=False) "
            "user-scope MCP was wiped by _do_cleanup. Filter must be "
            "`lambda s: s.scope == 'user' and s.sidecar_backed` so "
            "external user-managed MCP rows survive a service teardown.",
        )

    # ------------------------------------------------------------------ #
    # AC #22: unlink() override filter must include sidecar_backed=True   #
    # ------------------------------------------------------------------ #
    def test_unlink_override_unlinks_sidecar_backed_only(self):
        """AC #22: ``cloud_service.unlink()`` override filter 同 #21.

        Direct ``service.unlink()`` path (controllers, cron, manual ORM
        ops) — does NOT call ``_do_cleanup`` first. The override at
        ``cloud_service.py:1176-1201`` catches this; its filter must be
        kept in lockstep with ``_do_cleanup`` (otherwise B 案 splits the
        invariant — D5-class zombie returns).

        **Red today**：override filter is also plain ``scope == 'user'``;
        external_mcp gets wiped via the override too. Implementation
        must update BOTH ``_do_cleanup`` AND ``unlink()`` override
        (AC #21 + #22 are paired).
        """
        service, project, sidecar_mcp, external_mcp = (
            self._seed_service_with_dual_user_mcps(
                name_prefix='UnlinkOv', release_name='unlink-ov-svc',
            )
        )
        sidecar_id = sidecar_mcp.id

        # Deliberately do NOT call service._do_cleanup() — exercise the
        # override path directly.
        service.unlink()

        self.assertFalse(
            self.McpServer.sudo().with_context(active_test=False)
            .browse(sidecar_id).exists(),
            "AC #22: sidecar_backed=True user-scope MCP must be removed "
            "by cloud_service.unlink() override (direct delete path — "
            "PR #47 BLOCKER 2 root cause). Filter parity with "
            "_do_cleanup (AC #21).",
        )

    def test_unlink_override_preserves_external_user_mcp(self):
        """AC #22 / New Invariant #9: ``unlink()`` override must NOT touch
        external (sidecar_backed=False) user-scope MCP.

        **Red today**：same root cause as the ``_do_cleanup`` siblings —
        override filter omits sidecar_backed leg, external row gets
        nuked. Lock to prevent partial implementation that fixes only
        ``_do_cleanup`` and forgets the override (filter pair).
        """
        service, project, sidecar_mcp, external_mcp = (
            self._seed_service_with_dual_user_mcps(
                name_prefix='UnlinkExtP', release_name='unlink-extp-svc',
            )
        )
        external_id = external_mcp.id

        service.unlink()

        self.assertTrue(
            self.McpServer.sudo().browse(external_id).exists(),
            "AC #22 / New Invariant #9: external (sidecar_backed=False) "
            "user-scope MCP was wiped by service.unlink() override. "
            "Filter must be `lambda s: s.scope == 'user' and "
            "s.sidecar_backed` — pair with _do_cleanup (AC #21) so "
            "external user-managed MCP rows survive both cleanup paths.",
        )
