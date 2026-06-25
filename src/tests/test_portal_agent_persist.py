"""Portal persist regression (HttpCase — "ORM 綠 ≠ endpoint secure" family).

``_persist_ai_response`` must store the agent reply even when the streaming
turn was initiated by a PORTAL (share) user: access was already gated at
stream entry by the portal leg, and the persisted message is a system
artefact authored by OdooBot — not a user-privileged write.

Regression for the 2026-06-05 stg e2e finding (PR #149 verification): the
portal stream rendered live, but the uid-bound persist env hit ir.rule
(discuss.channel read + mail.message create both denied for portal users) →
AccessError swallowed → the reply silently vanished on reload.
"""
import uuid

from odoo.tests.common import tagged

from .test_portal_agent_page import PortalAgentFixture
from ..controllers.ai_assistant import AiAssistantController


@tagged('post_install', '-at_install')
class TestPortalPersistAiResponse(PortalAgentFixture):

    def _persist_as(self, user):
        """Invoke the persist helper exactly as the SSE done-path does
        (ai_assistant.py done / retryable-error branches), with the
        streaming initiator's uid."""
        probe = uuid.uuid4().hex[:8]
        msg_id = AiAssistantController()._persist_ai_response(
            self.env.cr.dbname, user.id, self.channel.id,
            f'<p>persist-probe {probe}</p>', {})
        return msg_id, probe

    def test_persist_for_portal_streaming_user(self):
        """Portal-initiated turn: the reply must land in the channel."""
        msg_id, probe = self._persist_as(self.alice)
        self.assertTrue(
            msg_id,
            'AI reply was not persisted when the stream was initiated by a '
            'portal user — persist must run with system identity, the '
            'initiator uid cannot pass discuss.channel / mail.message '
            'ir.rules')
        msg = self.env['mail.message'].sudo().browse(msg_id)
        self.assertEqual(msg.model, 'discuss.channel')
        self.assertEqual(msg.res_id, self.channel.id)
        self.assertEqual(msg.author_id, self.env.ref('base.partner_root'))
        self.assertIn(probe, msg.body)

    def test_persist_for_internal_user_unchanged(self):
        """Internal-initiated turn keeps persisting (behaviour guard)."""
        msg_id, probe = self._persist_as(self.env.ref('base.user_admin'))
        self.assertTrue(msg_id)
        msg = self.env['mail.message'].sudo().browse(msg_id)
        self.assertEqual(msg.res_id, self.channel.id)
        self.assertEqual(msg.author_id, self.env.ref('base.partner_root'))
        self.assertIn(probe, msg.body)

    def test_persist_empty_response_noop(self):
        """Empty stream output stays a no-op regardless of initiator."""
        msg_id = AiAssistantController()._persist_ai_response(
            self.env.cr.dbname, self.alice.id, self.channel.id, '', {})
        self.assertFalse(msg_id)
