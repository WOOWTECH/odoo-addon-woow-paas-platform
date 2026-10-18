"""Native queue/cron transactions in an owned, egress-disabled Odoo fixture.

Run only this tag with cron workers disabled. No SMTP, queue processing,
mail bodies, invitation digests, challenges or authentication secrets are read.
The owner/outsider exercise the issue guard with normal non-superuser envs;
only the trusted fixture inspects outbox/trigger metadata.
"""
from datetime import timedelta
from email.utils import parseaddr
from unittest.mock import patch
import uuid

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

from ..models.workspace_invitation import InvitationRejected
from ..services.paas_capability import _PaaSCapability


class _RollbackIssue(Exception):
    """Roll back a successfully queued invitation within the native transaction."""


@tagged('post_install', '-at_install', 'workspace_invite_mail_dispatch')
class TestWorkspaceInvitationMailDispatch(TransactionCase):
    def setUp(self):
        super().setUp()
        self.env.company.email = 'invitation-dispatch-sender@example.invalid'
        self.env['ir.config_parameter'].set_param('web.base.url', 'https://fixture.invalid')
        self.cron = self.env.ref('mail.ir_cron_mail_scheduler_action')
        # Fixture only: make ordinary cadence insufficient to explain readiness.
        # Runtime must neither rewrite these values nor reactivate this cron.
        self.cron.write({
            'active': True, 'nextcall': fields.Datetime.now() + timedelta(days=1),
        })
        self.users = {}
        for label in ('owner', 'outsider', 'recipient', 'second_recipient'):
            login = 'invite-dispatch-%s-%s@example.invalid' % (label, uuid.uuid4().hex)
            self.users[label] = self.env['res.users'].with_context(
                no_reset_password=True, tracking_disable=True,
                mail_create_nosubscribe=True, mail_create_nolog=True,
            )._create_user_from_template({
                'name': 'Synthetic invitation dispatch %s' % label,
                'login': login, 'email': login,
            })
        owner_env = self.env(user=self.users['owner'].id, su=False, context={})
        self.workspace = _PaaSCapability(owner_env).create_workspace(
            'Synthetic invitation dispatch', '')
        self.issuer = owner_env['woow_paas_platform.workspace_invitation']
        self.assertFalse(self.issuer.env.su)
        self.assertFalse(self.issuer.env.user.has_group('base.group_system'))
        # A protection guard only: template rendering, mail.mail creation and
        # ir.cron._trigger remain native. No transport may be invoked by issue.
        self.smtp_guard = self.enterContext(patch.object(
            type(self.env['mail.mail']), 'send',
            side_effect=AssertionError('SMTP must not run during invitation issue'),
        ))

    def _issue(self, recipient='recipient'):
        return self.issuer._issue(
            self.workspace.id, self.users[recipient].login, 'user')

    def _snapshot(self):
        invitation_model = self.issuer._name
        return {
            'invitation_ids': self.env[invitation_model].search([
                ('workspace_id', '=', self.workspace.id)]).ids,
            'mail_ids': self.env['mail.mail'].search([
                ('model', '=', invitation_model)]).ids,
            'message_ids': self.env['mail.message'].search([
                ('model', '=', invitation_model)]).ids,
            'trigger_ids': self.env['ir.cron.trigger'].search([
                ('cron_id', '=', self.cron.id)]).ids,
        }

    def _cron_config(self):
        return self.cron.read([
            'active', 'nextcall', 'interval_number', 'interval_type', 'user_id',
        ])[0]

    def _assert_queued(self, invitation, recipient='recipient'):
        # Disable prefetch and request metadata explicitly; never load a body.
        rows = self.env['mail.mail'].with_context(prefetch_fields=False).search([
            ('model', '=', self.issuer._name), ('res_id', '=', invitation.id),
        ]).read(['state', 'model', 'res_id'])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['state'], 'outgoing')
        self.assertEqual(rows[0]['res_id'], invitation.id)
        self.assertFalse(self.env['woow_paas_platform.workspace_access'].search([
            ('workspace_id', '=', self.workspace.id),
            ('user_id', '=', self.users[recipient].id),
        ]))
        self.smtp_guard.assert_not_called()

    def test_authorized_issue_queues_mail_and_native_trigger(self):
        before = self._snapshot()
        config = self._cron_config()
        invitation = self._issue()
        self._assert_queued(invitation)
        after = self._snapshot()
        self.assertEqual(len(set(after['trigger_ids']) - set(before['trigger_ids'])), 1)
        self.assertEqual(len(set(after['mail_ids']) - set(before['mail_ids'])), 1)
        self.assertEqual(self._cron_config(), config)

    def test_invitation_sender_excludes_vat_without_renaming_company(self):
        company = self.env.company
        original_name = '渥屋科技股份有限公司  84006751'
        company.with_context(no_vat_validation=True).write({
            'name': original_name, 'vat': '84006751',
        })
        invitation = self._issue()
        self._assert_queued(invitation)
        mail = self.env['mail.mail'].search([
            ('model', '=', self.issuer._name), ('res_id', '=', invitation.id),
        ])
        self.assertEqual(parseaddr(mail.email_from), (
            '渥屋科技股份有限公司', company.email,
        ))
        self.assertEqual(company.name, original_name)
        self.assertEqual(company.partner_id.name, original_name)
        self.assertEqual(company.vat, '84006751')
        self.assertIn('84006751', company.email_formatted)

    def test_unauthorized_issue_has_no_queue_or_trigger(self):
        before = self._snapshot()
        outsider = self.issuer.with_user(self.users['outsider'])
        self.assertFalse(outsider.env.su)
        with self.assertRaises(InvitationRejected) as rejected:
            outsider._issue(self.workspace.id, self.users['recipient'].login, 'user')
        self.assertEqual(rejected.exception.code, 'workspace_access_denied')
        self.assertEqual(self._snapshot(), before)
        self.smtp_guard.assert_not_called()

    def test_invalid_identity_or_role_has_no_queue_or_trigger(self):
        for email, role, code in [
            ('not-a-mailbox', 'user', 'invite_email_invalid'),
            ('absent-%s@example.invalid' % uuid.uuid4().hex, 'user', 'invite_identity_not_found'),
            (self.users['recipient'].login, 'owner', 'invite_role_invalid'),
        ]:
            with self.subTest(code=code):
                before = self._snapshot()
                with self.assertRaises(InvitationRejected) as rejected:
                    self.issuer._issue(self.workspace.id, email, role)
                self.assertEqual(rejected.exception.code, code)
                self.assertEqual(self._snapshot(), before)
        self.smtp_guard.assert_not_called()

    def test_burst_keeps_each_queue_item_but_one_ready_cron_job(self):
        before = self._snapshot()
        config = self._cron_config()
        # Native worker readiness uses PostgreSQL's transaction timestamp.
        # Freeze only the clock to that timestamp, not queue/trigger behavior.
        self.env.cr.execute("SELECT now() AT TIME ZONE 'UTC'")
        transaction_now = self.env.cr.fetchone()[0]
        with patch.object(fields.Datetime, 'now', return_value=transaction_now):
            first = self._issue()
            second = self._issue('second_recipient')
        self._assert_queued(first)
        self._assert_queued(second, 'second_recipient')
        after = self._snapshot()
        self.assertEqual(len(set(after['mail_ids']) - set(before['mail_ids'])), 2)
        self.assertEqual(len(set(after['trigger_ids']) - set(before['trigger_ids'])), 2)
        self.env.flush_all()
        ready = self.cron._get_all_ready_jobs(self.env.cr)
        self.assertEqual(len([job for job in ready if job['id'] == self.cron.id]), 1)
        self.assertEqual(self._cron_config(), config)

    def test_outer_rollback_removes_queue_invitation_and_trigger(self):
        before = self._snapshot()
        config = self._cron_config()
        with self.assertRaises(_RollbackIssue):
            with self.env.cr.savepoint():
                invitation = self._issue()
                self._assert_queued(invitation)
                self.assertEqual(len(set(self._snapshot()['trigger_ids']) - set(before['trigger_ids'])), 1)
                raise _RollbackIssue()
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self._cron_config(), config)
        self.smtp_guard.assert_not_called()

    def test_trigger_failure_rolls_back_rendered_queue_and_invitation(self):
        before = self._snapshot()
        config = self._cron_config()
        trigger_list = type(self.cron)._trigger_list

        def fail_after_native_trigger(cron, at_list):
            trigger_list(cron, at_list)
            raise UserError('Synthetic failure after native trigger creation')

        with patch.object(type(self.cron), '_trigger_list', new=fail_after_native_trigger):
            with self.assertRaises(UserError):
                self._issue()
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self._cron_config(), config)
        self.smtp_guard.assert_not_called()

    def test_inactive_queue_cron_is_not_reactivated(self):
        self.cron.active = False  # fixture only; never a production cron write
        before = self._snapshot()
        config = self._cron_config()
        invitation = self._issue()
        self._assert_queued(invitation)
        self.assertEqual(self._snapshot()['trigger_ids'], before['trigger_ids'])
        self.assertEqual(self._cron_config(), config)
