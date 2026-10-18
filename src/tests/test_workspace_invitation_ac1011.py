"""Actual HTTP pending-invitation/mail-queue boundary.

The committed two-transaction PG replay race is a separate shell runner after
HttpCase teardown, avoiding the held global billing-roster fixture lock.

Only root's isolated own database. Mail is rendered into its native outbox;
cron and network egress are disabled, so no real recipient is contacted.
The test's trusted fixture reads that outbox as the local mailbox sink;
normal principals are tested against RPC access to the same secrets.
"""
import hashlib
import html
import json
import re
import secrets
import uuid
from datetime import timedelta
from unittest.mock import patch

from odoo import fields, SUPERUSER_ID
from odoo.exceptions import ValidationError
from odoo.tests.common import HttpCase, tagged


@tagged('post_install', '-at_install', 'ac1011_invite_pending')
class TestWorkspaceInvitationAc1011(HttpCase):
    def setUp(self):
        super().setUp()
        self.env.company.write({'email': 'ac1011-platform-sender@example.invalid'})
        self.synthetic_users = {}
        self.passwords = {}
        for label in ('owner', 'attacker', 'victim'):
            login = 'ac1011-invite-%s-%s@example.invalid' % (label, uuid.uuid4().hex)
            password = uuid.uuid4().hex
            user = self.env['res.users'].with_context(
                no_reset_password=True, tracking_disable=True,
                mail_create_nosubscribe=True, mail_create_nolog=True,
            )._create_user_from_template({
                'name': 'Ac1011 Invite %s' % label,
                'login': login, 'email': login, 'password': password,
            })
            self.synthetic_users[label] = user
            self.passwords[label] = password
            self.assertTrue(user.share)
            self.assertTrue(user.has_group('base.group_portal'))
            self.assertFalse(user.has_group('base.group_user'))
            self.assertFalse(user.has_group('base.group_system'))

    def _login(self, label):
        self.authenticate(self.synthetic_users[label].login, self.passwords[label])

    def _rpc(self, url, params):
        response = self.url_open(
            url, data=json.dumps({'jsonrpc': '2.0', 'method': 'call', 'params': params}),
            headers={'Content-Type': 'application/json'},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertNotIn('error', body, body)
        self.env.invalidate_all()
        self.assertIsInstance(body.get('result'), dict)
        return body['result']

    def _workspace(self):
        self._login('owner')
        result = self._rpc('/api/workspaces', {
            'action': 'create', 'name': 'Ac1011 synthetic account invite boundary',
        })
        self.assertTrue(result['success'], result)
        return result['data']['id']

    def _member(self, workspace_id, user):
        return self.env['woow_paas_platform.workspace_access'].sudo().search([
            ('workspace_id', '=', workspace_id), ('user_id', '=', user.id),
        ])

    def _claim(self, label, email, name=None):
        self._login(label)
        user = self.synthetic_users[label]
        original_login = user.login
        result = self._rpc('/api/me/update', {
            'name': name or user.name, 'email': email, 'phone': '',
        })
        self.assertTrue(result['success'], result)
        self.assertEqual(user.email, email)
        self.assertEqual(user.login, original_login)
        return user

    def _invite(self, workspace_id, email, role='user', actor='owner'):
        self._login(actor)
        result = self._rpc('/api/workspaces/%s/members' % workspace_id, {
            'action': 'invite', 'email': email, 'role': role,
            'invitation_mode': 'mailbox_challenge_v1',
        })
        return result

    def _assert_denied(self, workspace_id, label='attacker'):
        self._login(label)
        result = self._rpc('/api/workspaces/%s' % workspace_id, {'action': 'get'})
        self.assertFalse(result.get('success'), result)
        self.assertFalse(result.get('data'), result)
        self.assertFalse(self._member(workspace_id, self.synthetic_users[label]))

    def _ledger(self, invitation_id):
        return self.env['woow_paas_platform.workspace_invitation'].sudo().browse(invitation_id)

    def _issue(self, workspace_id, label='victim', role='user', actor='owner'):
        recipient = self.synthetic_users[label]
        result = self._invite(workspace_id, recipient.login, role=role, actor=actor)
        self.assertTrue(result.get('success'), result)
        self.assertEqual(result['data'].get('state'), 'pending')
        self.assertFalse(self._member(workspace_id, recipient))
        invitation = self._ledger(result['data']['id'])
        mail = self.env['mail.mail'].sudo().search([
            ('model', '=', invitation._name), ('res_id', '=', invitation.id),
        ], order='id desc', limit=1)
        self.assertTrue(mail, 'Expected a native sandbox outbox record')
        self.assertEqual(mail.state, 'outgoing')
        self.assertEqual(mail.email_to, recipient.login.lower())
        self.assertFalse(mail.email_cc)
        self.assertFalse(mail.recipient_ids)
        match = re.search(r'workspace-invitation/accept/([A-Za-z0-9_-]{43})', html.unescape(mail.body_html or ''))
        self.assertTrue(match, 'Expected a challenge in the sandbox generated outbox')
        token = match.group(1)
        self.assertEqual(invitation.token_digest, hashlib.sha256(token.encode('ascii')).hexdigest())
        self.assertFalse(token in json.dumps(result), 'A nonce must never appear in the issue API response')
        return invitation, token

    def _accept(self, token, label='victim', **payload):
        self._login(label)
        return self._rpc('/api/workspace-invitations/accept', {'token': token, **payload})

    def _grant_member(self, workspace_id, label, role):
        invitation, token = self._issue(workspace_id, label=label, role=role)
        result = self._accept(token, label=label)
        self.assertTrue(result.get('success'), result)
        self.assertEqual(self._member(workspace_id, self.synthetic_users[label]).role, role)
        self.assertEqual(invitation.state, 'accepted')
        return self._member(workspace_id, self.synthetic_users[label])

    def _raw_rpc(self, model, method, args, kwargs=None):
        response = self.url_open('/web/dataset/call_kw/%s/%s' % (model, method),
            data=json.dumps({'jsonrpc': '2.0', 'method': 'call', 'params': {
                'model': model, 'method': method, 'args': args, 'kwargs': kwargs or {},
            }}), headers={'Content-Type': 'application/json'})
        self.assertEqual(response.status_code, 200)
        self.env.invalidate_all()
        return response.json()


    def _invitation_boundary_snapshot(self, workspace_id):
        invitations = self.env['woow_paas_platform.workspace_invitation'].sudo().search([
            ('workspace_id', '=', workspace_id)], order='id')
        members = self.env['woow_paas_platform.workspace_access'].sudo().search([
            ('workspace_id', '=', workspace_id)], order='id')
        return {
            'invitations': [(row.id, row.state, row.token_digest, row.expires_at)
                            for row in invitations],
            'members': [(row.id, row.user_id.id, row.role) for row in members],
            'mail_ids': self.env['mail.mail'].sudo().search([
                ('model', '=', invitations._name),
                ('res_id', 'in', invitations.ids)], order='id').ids,
            'message_ids': self.env['mail.message'].sudo().search([
                ('model', '=', invitations._name),
                ('res_id', 'in', invitations.ids)], order='id').ids,
            'mail_count': self.env['mail.mail'].sudo().search_count([]),
            'message_count': self.env['mail.message'].sudo().search_count([]),
        }

    def test_legacy_invite_without_protocol_has_no_membership_or_outbox_side_effect(self):
        workspace_id = self._workspace()
        before = self._invitation_boundary_snapshot(workspace_id)
        result = self._rpc('/api/workspaces/%s/members' % workspace_id, {
            'action': 'invite', 'email': self.synthetic_users['victim'].login,
            'role': 'admin',
        })
        self.assertFalse(result.get('success'), result)
        self.assertEqual(result.get('code'), 'invite_client_update_required')
        self.assertEqual(self._invitation_boundary_snapshot(workspace_id), before)
        self.assertFalse(self._member(workspace_id, self.synthetic_users['victim']))
        self._login('attacker')
        denied = self._rpc('/api/workspaces/%s/members' % workspace_id, {
            'action': 'invite', 'email': self.synthetic_users['victim'].login,
            'role': 'admin',
        })
        self.assertFalse(denied.get('success'), denied)
        self.assertEqual(denied.get('code'), 'workspace_access_denied')
        self.assertEqual(self._invitation_boundary_snapshot(workspace_id), before)

    def test_unknown_invitation_protocol_preserves_pending_challenge_and_existing_member(self):
        workspace_id = self._workspace()
        member = self._grant_member(workspace_id, 'attacker', 'user')
        invitation, token = self._issue(workspace_id, role='guest')
        before = self._invitation_boundary_snapshot(workspace_id)
        self._login('owner')
        for mode in (None, '', 'mailbox_challenge_v2', False, 1, [], {}):
            with self.subTest(mode=mode):
                result = self._rpc('/api/workspaces/%s/members' % workspace_id, {
                    'action': 'invite', 'email': self.synthetic_users['victim'].login,
                    'role': 'admin', 'invitation_mode': mode,
                })
                self.assertFalse(result.get('success'), result)
                self.assertEqual(result.get('code'), 'invite_client_update_required')
                self.assertEqual(self._invitation_boundary_snapshot(workspace_id), before)
                self.assertEqual(member.role, 'user')
                self.assertEqual(invitation.state, 'pending')
        self._login('attacker')
        denied = self._rpc('/api/workspaces/%s/members' % workspace_id, {
            'action': 'invite', 'email': self.synthetic_users['victim'].login,
            'role': 'admin', 'invitation_mode': 'mailbox_challenge_v2',
        })
        self.assertFalse(denied.get('success'), denied)
        self.assertEqual(denied.get('code'), 'workspace_access_denied')
        self.assertEqual(self._invitation_boundary_snapshot(workspace_id), before)
        accepted = self._accept(token)
        self.assertTrue(accepted.get('success'), accepted)
        self.assertEqual(self._member(workspace_id, self.synthetic_users['victim']).role, 'guest')
        self.assertEqual(member.role, 'user')

    def test_issue_is_pending_and_does_not_grant_until_authenticated_acceptance(self):
        workspace_id = self._workspace()
        invitation, token = self._issue(workspace_id, role='admin')
        self._assert_denied(workspace_id, label='victim')
        self._login('owner')
        listed = self._rpc('/api/workspaces/%s/invitations' % workspace_id, {'action': 'list'})
        self.assertTrue(listed.get('success'), listed)
        self.assertEqual([row['id'] for row in listed['data']], [invitation.id])
        self.assertFalse(token in json.dumps(listed), 'A nonce must never appear in list responses')
        self.assertFalse('token_digest' in json.dumps(listed))
        accepted = self._accept(token)
        self.assertTrue(accepted.get('success'), accepted)
        self.assertEqual(self._member(workspace_id, self.synthetic_users['victim']).role, 'admin')
        renamed = self._rpc('/api/workspaces/%s' % workspace_id, {
            'action': 'update', 'name': 'Ac1011 admin accepted delivered invitation',
        })
        self.assertTrue(renamed.get('success'), renamed)
        self.assertTrue(self.synthetic_users['victim'].share)
        self.assertFalse(self.synthetic_users['victim'].has_group('base.group_user'))

    def test_unregistered_profile_email_claim_cannot_get_a_pending_grant(self):
        workspace_id = self._workspace()
        claimed = 'ac1011-pending-absent-%s@example.invalid' % uuid.uuid4().hex
        self._claim('attacker', claimed)
        result = self._invite(workspace_id, claimed, role='admin')
        self.assertFalse(result.get('success'), result)
        self._assert_denied(workspace_id)
        self.assertFalse(self.env['woow_paas_platform.workspace_invitation'].sudo().search([
            ('workspace_id', '=', workspace_id)]))

    def test_duplicate_profile_emails_never_select_claimant_uid(self):
        workspace_id = self._workspace()
        victim = self.synthetic_users['victim']
        victim.partner_id.write({'name': 'Zzz Ac1011 intended challenge account'})
        self._claim('attacker', victim.login, 'Aaa Ac1011 false email claimant')
        invitation, token = self._issue(workspace_id, role='admin')
        self.assertEqual(invitation.recipient_user_id, victim)
        denied = self._accept(token, label='attacker')
        self.assertFalse(denied.get('success'), denied)
        self._assert_denied(workspace_id)
        accepted = self._accept(token)
        self.assertTrue(accepted.get('success'), accepted)
        self.assertEqual(self._member(workspace_id, victim).role, 'admin')

    def test_internal_profile_claimant_does_not_override_portal_login(self):
        workspace_id = self._workspace()
        attacker = self.synthetic_users['attacker']
        attacker.write({'groups_id': [(6, 0, [self.env.ref('base.group_user').id])]})
        self._claim('attacker', self.synthetic_users['victim'].login)
        invitation, token = self._issue(workspace_id)
        self.assertEqual(invitation.recipient_user_id, self.synthetic_users['victim'])
        self.assertFalse(self._member(workspace_id, attacker))
        self.assertTrue(self._accept(token).get('success'))

    def test_billing_email_can_change_without_changing_delivery_or_uid(self):
        workspace_id = self._workspace()
        victim = self.synthetic_users['victim']
        original_login = victim.login
        self._claim('victim', 'ac1011-other-billing-%s@example.invalid' % uuid.uuid4().hex)
        invitation, token = self._issue(workspace_id)
        self.assertEqual(invitation.recipient_email, original_login)
        accepted = self._accept(token)
        self.assertTrue(accepted.get('success'), accepted)
        self.assertEqual(accepted['data']['user_id'], victim.id)

    def test_casefold_login_ambiguity_fails_before_queue_or_ledger(self):
        workspace_id = self._workspace()
        victim = self.synthetic_users['victim']
        # X-SET-007 起 ORM 拒絕只差大小寫的重複 login；這種碰撞只剩修補前的資料
        # （正式站 uid 227／236），直接改表模擬，邀請端仍必須 fail-closed。
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE res_users SET login = %s WHERE id = %s",
            [victim.login.upper(), self.synthetic_users['attacker'].id])
        self.env.invalidate_all()
        result = self._invite(workspace_id, victim.login)
        self.assertFalse(result.get('success'), result)
        self.assertEqual(result.get('code'), 'invite_identity_ambiguous')
        self.assertFalse(self.env['woow_paas_platform.workspace_invitation'].sudo().search([
            ('workspace_id', '=', workspace_id)]))
        self.assertFalse(self._member(workspace_id, victim))

    def test_wrong_uid_cannot_consume_nonce_and_intended_uid_can_accept_later(self):
        workspace_id = self._workspace()
        invitation, token = self._issue(workspace_id)
        denied = self._accept(token, label='attacker')
        self.assertFalse(denied.get('success'), denied)
        self.assertEqual(invitation.state, 'pending')
        self._assert_denied(workspace_id)
        self.assertTrue(self._accept(token).get('success'))

    def test_expired_nonce_never_grants_membership(self):
        workspace_id = self._workspace()
        invitation, token = self._issue(workspace_id)
        invitation.write({'expires_at': fields.Datetime.now() - timedelta(seconds=1)})
        denied = self._accept(token)
        self.assertFalse(denied.get('success'), denied)
        self._assert_denied(workspace_id, label='victim')

    def test_unknown_or_malformed_nonce_never_grants_membership(self):
        workspace_id = self._workspace()
        invitation, _token = self._issue(workspace_id)
        for token in ('invalid', secrets.token_urlsafe(32), None, {'unexpected': True}):
            with self.subTest(token_type=type(token).__name__):
                denied = self._accept(token)
                self.assertFalse(denied.get('success'), denied)
        self.assertEqual(invitation.state, 'pending')
        self._assert_denied(workspace_id, label='victim')

    def test_consumed_nonce_cannot_regrant_after_membership_removal(self):
        workspace_id = self._workspace()
        invitation, token = self._issue(workspace_id)
        accepted = self._accept(token)
        self.assertTrue(accepted.get('success'), accepted)
        membership = self._member(workspace_id, self.synthetic_users['victim'])
        self._login('owner')
        removed = self._rpc('/api/workspaces/%s/members/%s' % (workspace_id, membership.id), {'action': 'remove'})
        self.assertTrue(removed.get('success'), removed)
        replay = self._accept(token)
        self.assertFalse(replay.get('success'), replay)
        self.assertEqual(invitation.state, 'accepted')
        self._assert_denied(workspace_id, label='victim')

    def test_accept_payload_cannot_upgrade_role_or_retarget_workspace_or_uid(self):
        workspace_id = self._workspace()
        invitation, token = self._issue(workspace_id, role='guest')
        accepted = self._accept(token, role='admin', workspace_id=workspace_id + 123456,
                                user_id=self.synthetic_users['attacker'].id, invited_by_id=self.synthetic_users['attacker'].id)
        self.assertTrue(accepted.get('success'), accepted)
        self.assertEqual(self._member(workspace_id, self.synthetic_users['victim']).role, 'guest')
        self.assertFalse(self._member(workspace_id, self.synthetic_users['attacker']))
        self.assertEqual(invitation.accepted_by_id, self.synthetic_users['victim'])

    def test_grantor_demotion_revokes_pending_token_even_if_promoted_again(self):
        workspace_id = self._workspace()
        admin = self._grant_member(workspace_id, 'attacker', 'admin')
        invitation, token = self._issue(workspace_id, actor='attacker')
        self._login('owner')
        for role in ('user', 'admin'):
            updated = self._rpc('/api/workspaces/%s/members/%s' % (workspace_id, admin.id), {'action': 'update_role', 'role': role})
            self.assertTrue(updated.get('success'), updated)
        denied = self._accept(token)
        self.assertFalse(denied.get('success'), denied)
        self.assertEqual(invitation.state, 'revoked')
        self._assert_denied(workspace_id, label='victim')

    def test_removed_grantor_cannot_leave_an_active_pending_token(self):
        workspace_id = self._workspace()
        admin = self._grant_member(workspace_id, 'attacker', 'admin')
        invitation, token = self._issue(workspace_id, actor='attacker')
        self._login('owner')
        removed = self._rpc('/api/workspaces/%s/members/%s' % (workspace_id, admin.id), {'action': 'remove'})
        self.assertTrue(removed.get('success'), removed)
        denied = self._accept(token)
        self.assertFalse(denied.get('success'), denied)
        self.assertEqual(invitation.state, 'revoked')

    def test_archive_revokes_pending_and_reactivation_does_not_restore_it(self):
        workspace_id = self._workspace()
        invitation, token = self._issue(workspace_id)
        self._login('owner')
        archived = self._rpc('/api/workspaces/%s' % workspace_id, {'action': 'delete'})
        self.assertTrue(archived.get('success'), archived)
        denied = self._accept(token)
        self.assertFalse(denied.get('success'), denied)
        self.assertEqual(invitation.state, 'revoked')
        self.env['woow_paas_platform.workspace'].sudo().browse(workspace_id).write({'state': 'active'})
        replay = self._accept(token)
        self.assertFalse(replay.get('success'), replay)

    def test_owner_revocation_invalidates_nonce_without_grant(self):
        workspace_id = self._workspace()
        invitation, token = self._issue(workspace_id)
        self._login('owner')
        revoked = self._rpc('/api/workspaces/%s/invitations' % workspace_id, {'action': 'revoke', 'invitation_id': invitation.id})
        self.assertTrue(revoked.get('success'), revoked)
        self.assertEqual(invitation.state, 'revoked')
        denied = self._accept(token)
        self.assertFalse(denied.get('success'), denied)
        self._assert_denied(workspace_id, label='victim')

    def test_resend_rotates_nonce_and_uses_latest_server_role(self):
        workspace_id = self._workspace()
        old, old_token = self._issue(workspace_id, role='admin')
        newest, newest_token = self._issue(workspace_id, role='user')
        self.assertEqual(old.state, 'revoked')
        self.assertNotEqual(old.id, newest.id)
        rejected = self._accept(old_token)
        self.assertFalse(rejected.get('success'), rejected)
        accepted = self._accept(newest_token)
        self.assertTrue(accepted.get('success'), accepted)
        self.assertEqual(self._member(workspace_id, self.synthetic_users['victim']).role, 'user')

    def test_recipient_cannot_read_nonce_digest_or_outbox_body_via_rpc(self):
        workspace_id = self._workspace()
        invitation, token = self._issue(workspace_id)
        mail = self.env['mail.mail'].sudo().search([('model', '=', invitation._name), ('res_id', '=', invitation.id)], limit=1)
        self._login('victim')
        for model, record_id, columns in (
                (invitation._name, invitation.id, ['token_digest']), ('mail.mail', mail.id, ['body_html'])):
            body = self._raw_rpc(model, 'read', [[record_id], columns])
            self.assertTrue('error' in body, 'A normal recipient must not read challenge secrets by ORM RPC')
            self.assertFalse(token in json.dumps(body), 'RPC errors must not contain a plaintext nonce')
        body = self._raw_rpc('mail.message', 'search_read', [[('model', '=', invitation._name), ('res_id', '=', invitation.id)], ['body']])
        self.assertFalse(token in json.dumps(body), 'Recipient chatter must not expose the outbound invitation')
        self.assertEqual(invitation.state, 'pending')

    def test_owner_cannot_read_plaintext_challenge_as_mail_author_or_creator(self):
        workspace_id = self._workspace()
        invitation, token = self._issue(workspace_id)
        mail = self.env['mail.mail'].sudo().search([('model', '=', invitation._name), ('res_id', '=', invitation.id)], limit=1)
        self.assertEqual(mail.mail_message_id.create_uid.id, SUPERUSER_ID)
        self.assertNotEqual(mail.mail_message_id.author_id, self.synthetic_users['owner'].partner_id)
        # Both Portal and ERP/internal workspace owners must use the same
        # sanitized BFF, with no ability to retrieve a delivery challenge.
        for internal in (False, True):
            if internal:
                self.synthetic_users['owner'].write({'groups_id': [(6, 0, [self.env.ref('base.group_user').id])]})
            self._login('owner')
            for model, record_id, columns in (
                    (invitation._name, invitation.id, ['token_digest']),
                    ('mail.mail', mail.id, ['body_html']),
                    ('mail.message', mail.mail_message_id.id, ['body'])):
                body = self._raw_rpc(model, 'read', [[record_id], columns])
                self.assertTrue('error' in body, 'Inviter must not read the delivery challenge by ORM RPC')
                self.assertFalse(token in json.dumps(body), 'An inviter RPC must not expose a nonce')
            body = self._raw_rpc('mail.message', 'search_read', [[('model', '=', invitation._name), ('res_id', '=', invitation.id)], ['body']])
            self.assertFalse(token in json.dumps(body), 'Inviter chatter search must not expose a nonce')

    def test_insert_failure_rolls_back_grant_and_preserves_nonce_for_retry(self):
        workspace_id = self._workspace()
        invitation, token = self._issue(workspace_id)
        victim_id = self.synthetic_users['victim'].id
        Access = type(self.env['woow_paas_platform.workspace_access'])
        original = Access._validate_fields
        def fail_after_real_insert(records, *args, **kwargs):
            result = original(records, *args, **kwargs)
            if any(row.workspace_id.id == workspace_id and row.user_id.id == victim_id for row in records):
                raise ValidationError('Synthetic post-insert grant validation failure')
            return result
        with patch.object(Access, '_validate_fields', fail_after_real_insert):
            failed = self._accept(token)
        self.assertFalse(failed.get('success'), failed)
        self.assertFalse(self._member(workspace_id, self.synthetic_users['victim']))
        self.assertEqual(invitation.state, 'pending')
        self.assertTrue(self._accept(token).get('success'))

    def test_outbox_creation_failure_rolls_back_resend_and_preserves_previous_nonce(self):
        workspace_id = self._workspace()
        original_invitation, original_token = self._issue(workspace_id)
        Template = type(self.env['mail.template'])
        with patch.object(Template, 'send_mail', side_effect=ValidationError('Synthetic mail-queue failure')):
            failed = self._invite(workspace_id, self.synthetic_users['victim'].login)
        self.assertFalse(failed.get('success'), failed)
        self.assertEqual(original_invitation.state, 'pending')
        invitations = self.env['woow_paas_platform.workspace_invitation'].sudo().search([('workspace_id', '=', workspace_id)])
        self.assertEqual(invitations, original_invitation)
        self.assertTrue(self._accept(original_token).get('success'))

    def test_native_outbox_post_insert_failure_rolls_back_resend(self):
        workspace_id = self._workspace()
        original_invitation, original_token = self._issue(workspace_id)
        ledger_name = original_invitation._name
        Mail = type(self.env['mail.mail'])
        original_validate = Mail._validate_fields
        original_mail_ids = self.env['mail.mail'].sudo().search([
            ('model', '=', ledger_name), ('res_id', '=', original_invitation.id),
        ]).ids
        self.assertTrue(original_mail_ids, 'Original native outbox must exist')
        real_insert_seen = []

        def fail_after_native_outbox_insert(records, *args, **kwargs):
            result = original_validate(records, *args, **kwargs)
            for mail in records.sudo():
                if mail.model != ledger_name or not mail.res_id:
                    continue
                invitation = mail.env[ledger_name].sudo().browse(mail.res_id)
                if invitation.id == original_invitation.id or invitation.workspace_id.id != workspace_id:
                    continue
                # Capture only this synthetic resend's actual IDs. The
                # assertions below run after the HTTP request rolls back;
                # they must prove that real SQL rows existed before failure.
                real_insert_seen.append({
                    'mail_id': mail.id,
                    'message_id': mail.mail_message_id.id,
                    'invitation_id': invitation.id,
                    'mail_exists': bool(mail.exists()),
                    'message_exists': bool(mail.mail_message_id.exists()),
                    'invitation_exists': bool(invitation.exists()),
                })
                raise ValidationError('Synthetic post-insert native outbox failure')
            return result

        with patch.object(Mail, '_validate_fields', fail_after_native_outbox_insert):
            failed = self._invite(workspace_id, self.synthetic_users['victim'].login)
        self.assertFalse(failed.get('success'), 'Resend must report queue failure')
        self.assertTrue(real_insert_seen, 'A real native mail insert must precede injected failure')
        for inserted in real_insert_seen:
            self.assertTrue(inserted['mail_exists'], 'mail.mail existed before injected failure')
            self.assertTrue(inserted['message_exists'], 'mail.message existed before injected failure')
            self.assertTrue(inserted['invitation_exists'], 'New ledger existed before injected failure')
            for model, key in (('mail.mail', 'mail_id'), ('mail.message', 'message_id'), (ledger_name, 'invitation_id')):
                self.assertFalse(self.env[model].sudo().browse(inserted[key]).exists(),
                                 'The failed resend must roll back every inserted native row')
        invitations = self.env[ledger_name].sudo().search([('workspace_id', '=', workspace_id)])
        self.assertEqual(invitations, original_invitation)
        self.assertEqual(original_invitation.state, 'pending')
        self.assertEqual(self.env['mail.mail'].sudo().browse(original_mail_ids).exists().ids, original_mail_ids)
        self.assertFalse(self._member(workspace_id, self.synthetic_users['victim']))
        self.assertTrue(self._accept(original_token).get('success'), 'Original mailbox challenge must survive failed resend')

    def test_nonmember_cannot_list_or_revoke_another_workspace_invitation(self):
        workspace_id = self._workspace()
        invitation, _token = self._issue(workspace_id)
        self._login('attacker')
        for payload in ({'action': 'list'}, {'action': 'revoke', 'invitation_id': invitation.id}):
            result = self._rpc('/api/workspaces/%s/invitations' % workspace_id, payload)
            self.assertFalse(result.get('success'), result)
        self.assertEqual(invitation.state, 'pending')

    def test_issue_cannot_assign_owner_or_guest_actor_grant_admin(self):
        workspace_id = self._workspace()
        invalid = self._invite(workspace_id, self.synthetic_users['victim'].login, role='owner')
        self.assertFalse(invalid.get('success'), invalid)
        self._grant_member(workspace_id, 'attacker', 'guest')
        unauthorized = self._invite(workspace_id, self.synthetic_users['victim'].login, role='admin', actor='attacker')
        self.assertFalse(unauthorized.get('success'), unauthorized)
        self.assertFalse(self._member(workspace_id, self.synthetic_users['victim']))

    def test_recipient_deactivation_refuses_before_a_profile_claimant_fallback(self):
        workspace_id = self._workspace()
        victim = self.synthetic_users['victim']
        target = victim.login
        victim.write({'active': False})
        self._claim('attacker', target)
        result = self._invite(workspace_id, target)
        self.assertFalse(result.get('success'), result)
        self._assert_denied(workspace_id)

    def test_login_snapshot_change_invalidates_preexisting_nonce(self):
        workspace_id = self._workspace()
        invitation, token = self._issue(workspace_id)
        victim = self.synthetic_users['victim']
        victim.write({'login': 'ac1011-changed-login-%s@example.invalid' % uuid.uuid4().hex})
        denied = self._accept(token)
        self.assertFalse(denied.get('success'), denied)
        self.assertEqual(invitation.state, 'revoked')
        self._assert_denied(workspace_id, label='victim')

    def test_restored_login_never_reactivates_preexisting_nonce(self):
        workspace_id = self._workspace()
        invitation, token = self._issue(workspace_id)
        victim = self.synthetic_users['victim']
        original_login = victim.login
        victim.write({'login': 'ac1011-rotated-login-%s@example.invalid' % uuid.uuid4().hex})
        victim.write({'login': original_login})
        denied = self._accept(token)
        self.assertFalse(denied.get('success'), denied)
        self.assertEqual(invitation.state, 'revoked')
        self._assert_denied(workspace_id, label='victim')

    def test_recipient_reactivation_never_reactivates_preexisting_nonce(self):
        workspace_id = self._workspace()
        invitation, token = self._issue(workspace_id)
        victim = self.synthetic_users['victim']
        victim.write({'active': False})
        victim.write({'active': True})
        denied = self._accept(token)
        self.assertFalse(denied.get('success'), denied)
        self.assertEqual(invitation.state, 'revoked')
        self._assert_denied(workspace_id, label='victim')

    def test_grantor_reactivation_never_reactivates_preexisting_nonce(self):
        workspace_id = self._workspace()
        invitation, token = self._issue(workspace_id)
        owner = self.synthetic_users['owner']
        owner.write({'active': False})
        owner.write({'active': True})
        denied = self._accept(token)
        self.assertFalse(denied.get('success'), denied)
        self.assertEqual(invitation.state, 'revoked')
        self._assert_denied(workspace_id, label='victim')
