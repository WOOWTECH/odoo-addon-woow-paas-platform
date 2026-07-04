"""#279 O5c — reach sync triggers.

project.write (Path A, cloud_service_ids) + cloud_service.write/unlink (Path B,
workspace_id / project_ids) push the project's reachable-workspace set to its agent
pod via the operator /sync-workspaces contract. GRANT is fail-loud (raise → binding
rolls back); REVOKE-only is best-effort (enforcing cron is authoritative).

NOTE: Odoo model tests — run in-sandbox (./odoo-bin --test-tags /woow_paas_platform),
not via the operator pytest suite.
"""
import os
import uuid
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.woow_paas_platform.services.agent_runtime_client import (
    AgentRuntimeOperatorError,
)

_ENV_NAMESPACE_SECRET = 'AGENT_RUNTIME_NAMESPACE_SECRET'
_CLIENT_PATH = (
    "odoo.addons.woow_paas_platform.services."
    "agent_runtime_client.get_agent_runtime_client"
)


@tagged('post_install', '-at_install')
class TestReachSyncTriggers(TransactionCase):

    def setUp(self):
        super().setUp()
        os.environ[_ENV_NAMESPACE_SECRET] = 'unit-test-secret-0123456789'
        self.addCleanup(os.environ.pop, _ENV_NAMESPACE_SECRET, None)
        sfx = uuid.uuid4().hex[:8]
        self.ws_a = self.env['woow_paas_platform.workspace'].sudo().create(
            {'name': f'A {sfx}'})
        self.ws_b = self.env['woow_paas_platform.workspace'].sudo().create(
            {'name': f'B {sfx}'})
        self.tmpl = self.env['woow_paas_platform.cloud_app_template'].sudo().create({
            'name': f'T {sfx}', 'slug': f't-{sfx}',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': f't-{sfx}', 'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
        })
        # CS without helm_release_name/helm_namespace → unlink skips _cleanup_helm.
        self.svc_a = self.env['woow_paas_platform.cloud_service'].sudo().create({
            'name': f'A {sfx}', 'workspace_id': self.ws_a.id,
            'template_id': self.tmpl.id,
        })
        self.svc_b = self.env['woow_paas_platform.cloud_service'].sudo().create({
            'name': f'B {sfx}', 'workspace_id': self.ws_b.id,
            'template_id': self.tmpl.id,
        })
        self.project = self.env['project.project'].sudo().create(
            {'name': f'P {sfx}', 'use_agent_runtime': True})
        self.session = self.env['woow_paas_platform.agent_session'].sudo().create(
            {'project_id': self.project.id})
        self.session.state = 'active'          # reach-bearing state
        self.hash = self.session.project_namespace_hash

    # --- Path A (project.write) ------------------------------------------- #

    def test_bind_grant_calls_sync_require_release_true(self):
        """Bind a CS (workspace newly reachable) → GRANT → require_release=True."""
        client = MagicMock()
        with patch(_CLIENT_PATH, return_value=client):
            self.project.cloud_service_ids = [(4, self.svc_a.id)]
        client.sync_workspaces.assert_called_once()
        args, kwargs = client.sync_workspaces.call_args
        self.assertEqual(args[0], self.hash)
        self.assertEqual(sorted(args[1]), [self.ws_a.id])
        self.assertIs(kwargs.get('require_release'), True)

    def test_unbind_revoke_best_effort_require_release_false(self):
        """Unbind → REVOKE-only → require_release=False, and a client error is
        swallowed (best-effort; the enforcing cron converges)."""
        # bind first (no operator configured in setUp → reconcile is a no-op)
        self.project.cloud_service_ids = [(4, self.svc_a.id)]
        client = MagicMock()
        client.sync_workspaces.side_effect = AgentRuntimeOperatorError("operator down")
        with patch(_CLIENT_PATH, return_value=client):
            self.project.cloud_service_ids = [(3, self.svc_a.id)]  # unlink the bind
        client.sync_workspaces.assert_called_once()
        self.assertIs(
            client.sync_workspaces.call_args.kwargs.get('require_release'), False)

    def test_grant_failure_is_fail_loud(self):
        """A GRANT whose operator call fails re-raises → caller txn rolls back."""
        client = MagicMock()
        client.sync_workspaces.side_effect = AgentRuntimeOperatorError("5xx")
        with patch(_CLIENT_PATH, return_value=client):
            with self.assertRaises(AgentRuntimeOperatorError):
                self.project.cloud_service_ids = [(4, self.svc_a.id)]

    def test_no_session_no_client_call(self):
        """A project with no reach-bearing session → cold start owns provisioning."""
        self.session.state = 'absent'
        client = MagicMock()
        with patch(_CLIENT_PATH, return_value=client):
            self.project.cloud_service_ids = [(4, self.svc_a.id)]
        client.sync_workspaces.assert_not_called()

    def test_use_agent_runtime_off_no_call(self):
        self.project.use_agent_runtime = False
        client = MagicMock()
        with patch(_CLIENT_PATH, return_value=client):
            self.project.cloud_service_ids = [(4, self.svc_a.id)]
        client.sync_workspaces.assert_not_called()

    def test_untracked_write_no_call(self):
        """A project write NOT touching cloud_service_ids never reconciles."""
        self.project.cloud_service_ids = [(4, self.svc_a.id)]
        client = MagicMock()
        with patch(_CLIENT_PATH, return_value=client):
            self.project.name = 'renamed'
        client.sync_workspaces.assert_not_called()

    # --- Path B (cloud_service.write / unlink) ---------------------------- #

    def test_cs_unlink_triggers_revoke(self):
        """Deleting a bound CS drops its workspace → REVOKE on the project."""
        self.project.cloud_service_ids = [(4, self.svc_a.id)]
        client = MagicMock()
        with patch(_CLIENT_PATH, return_value=client):
            self.svc_a.unlink()
        client.sync_workspaces.assert_called_once()
        self.assertIs(
            client.sync_workspaces.call_args.kwargs.get('require_release'), False)

    def test_cs_workspace_reassign_on_live_ns_rejected(self):
        """Retargeting a deployed CS's workspace is rejected (ns identity fixed)."""
        from odoo.exceptions import UserError
        live = self.env['woow_paas_platform.cloud_service'].sudo().create({
            'name': 'live', 'workspace_id': self.ws_a.id, 'template_id': self.tmpl.id,
            'helm_release_name': 'live-rel', 'helm_namespace': 'paas-ws-live',
        })
        with self.assertRaises(UserError):
            live.workspace_id = self.ws_b.id
