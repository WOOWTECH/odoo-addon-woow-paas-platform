"""TDD — unified system-context builder (spec 2026-06-04-agent-context-enrichment).

Covers agent_session._build_system_context(): project/task sections,
html2plaintext + truncation, cloud-services whitelist, per-type
agent_context_keys extraction (fail-closed), 2nd-layer sanitizer,
budget trim ladder, never-raise error handling.
Assertions use assertIn/assertNotIn (intent-locking, no full-string
snapshots — per docs/decisions/patterns/phase-versioned-test-assertion.md).
"""
import json
from unittest.mock import patch

from odoo.tests.common import TransactionCase


class _ContextFixture:
    """(workspace, template, services, project, task, channel) graph the
    unified builder walks. Mirrors _ChatterFixture but with descriptions."""

    def _seed_context_graph(self):
        Workspace = self.env['woow_paas_platform.workspace']
        Template = self.env['woow_paas_platform.cloud_app_template']
        Service = self.env['woow_paas_platform.cloud_service']
        Project = self.env['project.project']
        Task = self.env['project.task']
        Channel = self.env['discuss.channel']

        self.workspace = Workspace.create({'name': 'WS Ctx'})
        self.template = Template.sudo().create({
            'name': 'Tmpl Ctx',
            'slug': 'tmpl-ctx',
            'category': 'web',
            'description': 'Ctx test template',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'tmpl-ctx',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
        })
        self.svc_primary = Service.sudo().create({
            'name': 'Primary Svc',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'subdomain': 'primary-svc',
        })
        self.svc_extra = Service.sudo().create({
            'name': 'Extra Svc',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'subdomain': 'extra-svc',
        })
        self.project = Project.sudo().create({
            'name': 'Ctx Project',
            # <i> 而非 <b>：html2plaintext 會把 <b> 轉成 markdown 粗體 *...*
            # 破壞下方 assertIn（2026-06-05 plan-gap 修正）；<i> 無標記。
            'description': '<p>Project <i>HTML</i> description</p>',
            'cloud_service_id': self.svc_primary.id,
        })
        self.channel = Channel.sudo().create({
            'name': 'Ctx Channel', 'channel_type': 'channel',
        })
        self.task = Task.sudo().create({
            'name': 'Ctx Task',
            'description': '<p>Task <i>HTML</i> description</p>',
            'project_id': self.project.id,
            'channel_id': self.channel.id,
        })
        self.Session = self.env['woow_paas_platform.agent_session']


class TestAgentContextBuilder(TransactionCase, _ContextFixture):
    """Project / Task sections + empty behaviour."""

    def setUp(self):
        super().setUp()
        self._seed_context_graph()

    def test_project_and_task_sections_injected(self):
        ctx = self.Session._build_system_context(self.channel)
        self.assertIn('## Background Context', ctx)
        self.assertIn('NOT instructions', ctx)
        self.assertIn('### Project', ctx)
        self.assertIn('- Name: Ctx Project', ctx)
        self.assertIn('Project HTML description', ctx)
        self.assertIn('### Task', ctx)
        self.assertIn('- Name: Ctx Task', ctx)
        self.assertIn('Task HTML description', ctx)
        # HTML must be flattened, tags never reach the prompt
        self.assertNotIn('<p>', ctx)
        self.assertNotIn('<i>', ctx)

    def test_description_truncated_with_marker(self):
        self.project.sudo().write({
            'description': '<p>' + ('x' * 3000) + '</p>',
        })
        ctx = self.Session._build_system_context(self.channel)
        self.assertIn('…[truncated]', ctx)
        self.assertNotIn('x' * 1501, ctx)

    def test_channel_without_task_returns_empty(self):
        orphan = self.env['discuss.channel'].sudo().create({
            'name': 'Orphan', 'channel_type': 'channel',
        })
        self.assertEqual(self.Session._build_system_context(orphan), '')

    def test_empty_descriptions_lines_omitted(self):
        self.project.sudo().write({'description': False})
        self.task.sudo().write({'description': False})
        ctx = self.Session._build_system_context(self.channel)
        self.assertIn('- Name: Ctx Project', ctx)
        self.assertNotIn('- Description:', ctx.split('### Cloud Services')[0]
                         .split('### Task')[0].split('### Project')[1])


class TestAgentContextServices(TransactionCase, _ContextFixture):
    """Cloud Services section — base whitelist, dedup, cap, exclusions."""

    def setUp(self):
        super().setUp()
        self._seed_context_graph()

    def test_service_base_fields_and_url(self):
        ctx = self.Session._build_system_context(self.channel)
        self.assertIn('### Cloud Services', ctx)
        self.assertIn('#### Primary Svc (Primary)', ctx)
        self.assertIn('- Application: Tmpl Ctx', ctx)
        self.assertIn('- Category: web', ctx)
        self.assertIn('- Status:', ctx)
        # external URL = subdomain + paas_domain config param
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.paas_domain', 'paas.test')
        ctx = self.Session._build_system_context(self.channel)
        self.assertIn('- URL: https://primary-svc.paas.test', ctx)

    def test_primary_extras_dedup_and_order(self):
        self.task.sudo().write({'extra_cloud_service_ids': [
            (4, self.svc_primary.id), (4, self.svc_extra.id)]})
        ctx = self.Session._build_system_context(self.channel)
        self.assertEqual(ctx.count('Primary Svc'), 1)
        self.assertIn('Extra Svc', ctx)
        self.assertLess(ctx.index('Primary Svc'), ctx.index('Extra Svc'))

    def test_services_capped_at_five(self):
        Service = self.env['woow_paas_platform.cloud_service']
        extras = Service.sudo().create([{
            'name': f'Bulk Svc {i}',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'subdomain': f'bulk-{i}',
        } for i in range(6)])
        self.task.sudo().write({
            'extra_cloud_service_ids': [(6, 0, extras.ids)]})
        ctx = self.Session._build_system_context(self.channel)
        # primary + 4 extras = 5 blocks max
        self.assertEqual(ctx.count('#### '), 5)
        self.assertIn('#### Primary Svc (Primary)', ctx)

    def test_error_message_never_injected(self):
        self.svc_primary.sudo().write({
            'state': 'error',
            'error_message': 'stacktrace ns=paas-secret-ns pod crashed',
        })
        ctx = self.Session._build_system_context(self.channel)
        self.assertNotIn('paas-secret-ns', ctx)
        self.assertNotIn('- Error:', ctx)

    def test_unbound_keeps_project_task_sections(self):
        """D7: no services → project/task sections still injected."""
        self.task.sudo().write({'extra_cloud_service_ids': [(5, 0, 0)]})
        self.project.sudo().write({'cloud_service_id': False,
                                   'cloud_service_ids': [(5, 0, 0)]})
        ctx = self.Session._build_system_context(self.channel)
        self.assertIn('### Project', ctx)
        self.assertIn('### Task', ctx)
        self.assertNotIn('### Cloud Services', ctx)
