"""#279 P5 — one-time tenant-isolation backfill (_backfill_reach_isolation_p5).

Re-applies the baseline NP bundle + managed labels on pre-existing CS namespaces,
and re-stamps env (+ reach-ws) on live agent namespaces, so tenant isolation holds
once #280 removes the chart from-less. Best-effort + idempotent.

NOTE: Odoo model test — runs in-sandbox, not via the operator pytest suite.
"""
import os
import uuid
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase, tagged

_ENV_NAMESPACE_SECRET = 'AGENT_RUNTIME_NAMESPACE_SECRET'
_NS_CLIENT = ("odoo.addons.woow_paas_platform.services."
              "paas_operator.get_paas_operator_client")
_AGENT_CLIENT = ("odoo.addons.woow_paas_platform.services."
                 "agent_runtime_client.get_agent_runtime_client")


@tagged('post_install', '-at_install')
class TestP5Backfill(TransactionCase):

    def setUp(self):
        super().setUp()
        patcher = patch.dict(
            os.environ, {_ENV_NAMESPACE_SECRET: 'unit-test-secret-0123456789'})
        patcher.start()
        self.addCleanup(patcher.stop)
        sfx = uuid.uuid4().hex[:8]
        self.ws = self.env['woow_paas_platform.workspace'].sudo().create(
            {'name': f'W {sfx}'})
        self.tmpl = self.env['woow_paas_platform.cloud_app_template'].sudo().create({
            'name': f'T {sfx}', 'slug': f't-{sfx}', 'helm_repo_url': 'https://x',
            'helm_chart_name': f't-{sfx}', 'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1})
        self.svc = self.env['woow_paas_platform.cloud_service'].sudo().create({
            'name': f'S {sfx}', 'workspace_id': self.ws.id,
            'template_id': self.tmpl.id, 'helm_release_name': f's-{sfx}',
            'helm_namespace': f'paas-ws-{sfx}'})
        self.project = self.env['project.project'].sudo().create(
            {'name': f'P {sfx}', 'use_agent_runtime': True})
        self.project.cloud_service_ids = [(4, self.svc.id)]
        self.session = self.env['woow_paas_platform.agent_session'].sudo().create(
            {'project_id': self.project.id})
        self.session.state = 'active'
        self.hash = self.session.project_namespace_hash
        self.CS = self.env['woow_paas_platform.cloud_service']

    def test_backfill_calls_both_legs(self):
        ns_client, agent_client = MagicMock(), MagicMock()
        with patch(_NS_CLIENT, return_value=ns_client), \
             patch(_AGENT_CLIENT, return_value=agent_client):
            res = self.CS._backfill_reach_isolation_p5()
        ns_client.backfill_baseline_netpol.assert_any_call(
            self.svc.helm_namespace, self.ws.id)
        agent_client.restamp_labels.assert_any_call(
            self.hash, reach_workspace_ids=[self.ws.id])
        self.assertGreaterEqual(res['cs_done'], 1)
        self.assertGreaterEqual(res['ar_done'], 1)

    def test_backfill_best_effort_on_cs_failure(self):
        """A CS-leg failure is logged + counted, and the agent leg still runs."""
        ns_client, agent_client = MagicMock(), MagicMock()
        ns_client.backfill_baseline_netpol.side_effect = Exception("operator 500")
        with patch(_NS_CLIENT, return_value=ns_client), \
             patch(_AGENT_CLIENT, return_value=agent_client):
            res = self.CS._backfill_reach_isolation_p5()
        self.assertGreaterEqual(res['cs_fail'], 1)
        agent_client.restamp_labels.assert_called()

    def test_backfill_no_operator_skips(self):
        with patch(_NS_CLIENT, return_value=None), \
             patch(_AGENT_CLIENT, return_value=None):
            res = self.CS._backfill_reach_isolation_p5()
        self.assertEqual(
            res, {"cs_done": 0, "cs_fail": 0, "ar_done": 0, "ar_fail": 0})
