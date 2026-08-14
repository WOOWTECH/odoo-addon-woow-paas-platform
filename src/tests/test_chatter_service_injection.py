"""TDD red phase tests — AI prompt cloud_service injection (AC #14-19).

Plan: docs/refactor/plan-cloud-service-project-decouple.md (D7=A, D9=A→B)

Tests exercise ``DiscussChannel._get_cloud_service_context()`` directly,
which is what the AI reply path consumes (see ``_post_ai_reply``).  Red
mode (current code in src/models/discuss_channel.py:216-274) lacks:

  * primary + extras dedup merge (returns single section only)
  * sanitizer / blacklist (raw ``helm_values`` JSON is dumped)
  * unbound-with-extras path (early-returns '' on unbound project)

Implementation will refactor ``_get_cloud_service_context`` to take the
task into account (or new helper) and to gate every secret-bearing
field by ``_sanitize_helm_values``.

2026-06-05 update: _get_cloud_service_context now delegates to the unified
agent_session._build_system_context (spec 2026-06-04). AC #19 rewritten per
D7 (unbound still injects project/task); AC #15/16 fixtures opt-in via
template.agent_context_keys per D4 (fail-closed helm_values whitelist).
"""
from odoo.tests.common import TransactionCase
from ..models.cloud_service import _internal_provision


class _ChatterFixture:
    """Helper to build the (task, channel, service) graph the chatter
    AI flow walks.  Used by every chatter-injection scenario below.
    """

    def _seed_chatter_graph(self):
        Workspace = self.env['woow_paas_platform.workspace']
        Template = self.env['woow_paas_platform.cloud_app_template']
        Service = self.env['woow_paas_platform.cloud_service']
        Project = self.env['project.project']
        Task = self.env['project.task']
        Channel = self.env['discuss.channel']

        self.workspace = Workspace.create({'name': 'WS Chatter'})
        self.template = Template.sudo().create({
            'name': 'Tmpl Chatter',
            'slug': 'tmpl-chatter',
            'category': 'web',
            'description': 'Chatter test template',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'tmpl-chatter',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
        })
        self.svc_primary = _internal_provision(Service.sudo()).create({
            'name': 'Primary Svc',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'subdomain': 'primary-svc',
        })
        self.svc_extra = _internal_provision(Service.sudo()).create({
            'name': 'Extra Svc',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'subdomain': 'extra-svc',
        })
        self.project_bound = Project.sudo().create({
            'name': 'Project Bound',
            'cloud_service_id': self.svc_primary.id,
        })
        self.project_unbound = Project.sudo().create({
            'name': 'Project Unbound',
        })

        self.channel_bound = Channel.sudo().create({
            'name': 'Chatter Bound', 'channel_type': 'channel',
        })
        self.channel_unbound = Channel.sudo().create({
            'name': 'Chatter Unbound', 'channel_type': 'channel',
        })

        self.task_bound = Task.sudo().create({
            'name': 'Task Bound',
            'project_id': self.project_bound.id,
            'channel_id': self.channel_bound.id,
        })
        self.task_unbound = Task.sudo().create({
            'name': 'Task Unbound',
            'project_id': self.project_unbound.id,
            'channel_id': self.channel_unbound.id,
        })


class TestPrimaryAndExtrasMerge(TransactionCase, _ChatterFixture):
    """AC #14, #17, #19 — merge / unbound / empty paths."""

    def setUp(self):
        super().setUp()
        self._seed_chatter_graph()

    # ---------------- AC #14 ---------------- #
    def test_ac14_primary_then_extras_merged_with_dedup(self):
        """AC #14: primary precedes extras; duplicates de-duplicated."""
        # Link extras: [primary (dup), extra] — primary must not appear twice.
        self.task_bound.sudo().write({
            'extra_cloud_service_ids': [
                (4, self.svc_primary.id),
                (4, self.svc_extra.id),
            ],
        })

        context = self.channel_bound._get_cloud_service_context()
        self.assertTrue(context, "AC #14: context must not be empty for bound task")

        # Primary section appears once
        primary_count = context.count('Primary Svc')
        self.assertEqual(
            primary_count, 1,
            f"AC #14: Primary Svc must appear exactly once (dedup); "
            f"got {primary_count} occurrences",
        )

        # Extra section appears
        self.assertIn(
            'Extra Svc', context,
            "AC #14: extras must be injected when set",
        )

        # Primary appears before extras
        self.assertLess(
            context.index('Primary Svc'), context.index('Extra Svc'),
            "AC #14: primary section must precede extras section",
        )

    # ---------------- AC #17 ---------------- #
    def test_ac17_unbound_with_extras_injects_extras_only(self):
        """AC #17: unbound project + extras → extras-only injection."""
        self.task_unbound.sudo().write({
            'extra_cloud_service_ids': [(4, self.svc_extra.id)],
        })

        context = self.channel_unbound._get_cloud_service_context()
        self.assertTrue(
            context,
            "AC #17: unbound task with extras must still inject context "
            "(current code returns '' early — that's the red signal)",
        )
        self.assertIn(
            'Extra Svc', context,
            "AC #17: extras must be visible in unbound chatter prompt",
        )
        self.assertNotIn(
            'Primary Svc', context,
            "AC #17: no primary service present, prompt must not mention one",
        )

    # ---------------- AC #19 (rewritten — spec 2026-06-04 D7) -------- #
    def test_ac19_unbound_no_extras_no_service_section(self):
        """AC #19 (D7 rewrite): unbound + no extras → NO Cloud Services
        block, but project/task background sections ARE injected."""
        context = self.channel_unbound._get_cloud_service_context()
        self.assertNotIn(
            '### Cloud Services', context,
            "AC #19: no service bound → no Cloud Services section",
        )
        self.assertNotIn('#### ', context,
                         "AC #19: no service blocks at all")
        self.assertIn(
            'Project Unbound', context,
            "AC #19/D7: project section must be injected even unbound",
        )
        self.assertIn(
            'Task Unbound', context,
            "AC #19/D7: task section must be injected even unbound",
        )


class TestBoundNoExtrasRegression(TransactionCase, _ChatterFixture):
    """AC #18 — bound task without extras keeps current behavior."""

    def setUp(self):
        super().setUp()
        self._seed_chatter_graph()

    def test_ac18_bound_no_extras_only_primary(self):
        """AC #18: bound task w/o extras — only primary section, no Extras label."""
        context = self.channel_bound._get_cloud_service_context()
        self.assertTrue(context, "Bound task must inject context")
        self.assertIn(
            'Primary Svc', context,
            "AC #18: bound task's primary service must appear",
        )
        self.assertNotIn(
            'Extra Svc', context,
            "AC #18: extras section must NOT appear when extras is empty",
        )


class TestHelmValuesSanitizer(TransactionCase, _ChatterFixture):
    """AC #15-16 — secret redaction in helm_values + blacklist fields."""

    def setUp(self):
        super().setUp()
        self._seed_chatter_graph()

    # ---------------- AC #15 ---------------- #
    def test_ac15_helm_values_password_redacted(self):
        """AC #15: pattern (?i)(password|secret|token|api[_-]?key|credential)
        value replaced with ***REDACTED***.
        """
        secret_value = 'super-secret-pw-123'
        self.template.sudo().write({'agent_context_keys': '["db", "app"]'})
        self.svc_primary.sudo().write({
            'helm_values': (
                '{"db": {"password": "%s"}, '
                '"app": {"replicas": 3}}'
            ) % secret_value,
        })

        context = self.channel_bound._get_cloud_service_context()
        self.assertNotIn(
            secret_value, context,
            "AC #15: raw password value leaked into AI prompt",
        )
        self.assertIn(
            '***REDACTED***', context,
            "AC #15: sanitizer must replace password value with ***REDACTED***",
        )
        # Non-sensitive value preserved
        self.assertIn(
            '"replicas"', context,
            "AC #15: non-sensitive keys must be retained (sanity)",
        )

    def test_ac15_token_and_api_key_patterns_redacted(self):
        """AC #15: additional patterns (token, api_key, credential)."""
        self.template.sudo().write({'agent_context_keys': '["auth"]'})
        self.svc_primary.sudo().write({
            'helm_values': (
                '{"auth": {"token": "tk-9999", '
                '"api_key": "ak-8888", '
                '"client_secret": "cs-7777", '
                '"credential": "cr-6666"}}'
            ),
        })
        context = self.channel_bound._get_cloud_service_context()
        for leaked in ('tk-9999', 'ak-8888', 'cs-7777', 'cr-6666'):
            self.assertNotIn(
                leaked, context,
                f"AC #15: sensitive value {leaked!r} leaked into prompt",
            )

    # ---------------- AC #16 ---------------- #
    def test_ac16_blacklisted_fields_never_injected(self):
        """AC #16: ``n8n_api_key`` / ``n8n_owner_password`` / ``mcp_auth_token``
        even when present in ``helm_values`` keys, are blacklisted.
        """
        # Put the *literal* blacklisted field names inside helm_values to
        # simulate the case where a future chart leaks them into values.
        self.template.sudo().write({'agent_context_keys': (
            '["n8n_api_key", "n8n_owner_password", "mcp_auth_token"]')})
        self.svc_primary.sudo().write({
            'helm_values': (
                '{"n8n_api_key": "n8n-leak-aaaa", '
                '"n8n_owner_password": "n8n-leak-bbbb", '
                '"mcp_auth_token": "mcp-leak-cccc"}'
            ),
        })

        context = self.channel_bound._get_cloud_service_context()
        for leaked in (
            'n8n-leak-aaaa', 'n8n-leak-bbbb', 'mcp-leak-cccc',
        ):
            self.assertNotIn(
                leaked, context,
                f"AC #16: blacklisted value {leaked!r} leaked into prompt",
            )
