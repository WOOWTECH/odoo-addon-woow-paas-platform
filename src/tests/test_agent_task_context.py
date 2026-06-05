"""Minimal task-context hook (will be absorbed by feat-enrich-agent-context)."""
import uuid

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestAgentTaskContext(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        suffix = uuid.uuid4().hex[:8]
        cls.project = cls.env['project.project'].create(
            {'name': f'Ctx Proj {suffix}'})
        cls.task = cls.env['project.task'].create({
            'name': f'Ctx Task {suffix}',
            'project_id': cls.project.id,
            # NOTE: plain inline text inside <p>. Odoo's html2plaintext renders
            # <b>/<strong> as markdown *emphasis* (e.g. "*billing*"), which would
            # break the substring assertion below while the impl is correct —
            # the load-bearing invariant here is HTML-stripping (assertNotIn '<p>'),
            # not inline-emphasis rendering. See plan Task 4 deviation note.
            'description': '<p>Fix the billing bug</p>',
        })
        cls.channel = cls.task._create_chat_channel()

    def test_context_contains_name_and_plain_description(self):
        ctx = self.env['project.task']._agent_task_context_for_channel(
            self.channel)
        self.assertIn(self.task.name, ctx)
        self.assertIn('Fix the billing bug', ctx)
        self.assertNotIn('<p>', ctx, 'description must be plain text')

    def test_context_empty_for_non_task_channel(self):
        ch = self.env['discuss.channel'].sudo().create({
            'name': 'free', 'channel_type': 'channel'})
        self.assertEqual(
            self.env['project.task']._agent_task_context_for_channel(ch), '')

    def test_context_truncates_long_description(self):
        self.task.description = '<p>' + ('x' * 5000) + '</p>'
        ctx = self.env['project.task']._agent_task_context_for_channel(
            self.channel)
        self.assertLessEqual(len(ctx), 2200)
