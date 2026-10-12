"""Actual authenticated HTTP reproduction; own isolated test DB only.

Documents observed source behavior, not a production exploit. Synthetic
identities use example.invalid; no email is sent and no operator/service API
is invoked. HttpCase rollback removes this fixture's records.
"""
import hashlib
import html
import json
import re
import uuid

from odoo.tests.common import HttpCase, tagged


@tagged('post_install', '-at_install', 'ac1011_identity')
class TestInviteProfileIdentityAc1011(HttpCase):
    def setUp(self):
        super().setUp()
        self.env.company.write({'email': 'ac1011-platform-sender@example.invalid'})
        self.synthetic_users = {}
        for label in ('owner', 'attacker', 'victim'):
            login = 'ac1011-identity-%s-%s@example.invalid' % (label, uuid.uuid4().hex)
            user = self.env['res.users'].with_context(
                no_reset_password=True, tracking_disable=True,
                mail_create_nosubscribe=True, mail_create_nolog=True,
            )._create_user_from_template({
                'name': 'Ac1011 Identity %s' % label,
                'login': login, 'email': login, 'password': login,
            })
            self.synthetic_users[label] = user
            self.assertTrue(user.share)
            self.assertTrue(user.has_group('base.group_portal'))
            self.assertFalse(user.has_group('base.group_user'))
            self.assertFalse(user.has_group('base.group_system'))

    def _login(self, label):
        user = self.synthetic_users[label]
        self.authenticate(user.login, user.login)

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
            'action': 'create', 'name': 'Ac1011 synthetic invitation identity workspace',
        })
        self.assertTrue(result['success'], result)
        return result['data']['id']

    def _assert_denied(self, workspace_id, label='attacker'):
        self._login(label)
        result = self._rpc('/api/workspaces/%s' % workspace_id, {'action': 'get'})
        self.assertFalse(result.get('success'), result)
        self.assertFalse(result.get('data'), result)

    def _claim_email(self, email):
        self._login('attacker')
        user = self.synthetic_users['attacker']
        original_login = user.login
        result = self._rpc('/api/me/update', {
            'name': 'Aaa Ac1011 claimed invite recipient', 'email': email, 'phone': '',
        })
        self.assertTrue(result['success'], result)
        self.assertEqual(user.partner_id.email, email)
        self.assertEqual(user.email, email)
        self.assertEqual(user.login, original_login)
        self.assertTrue(user.share)
        self.assertFalse(user.has_group('base.group_user'))
        return user

    def _invite(self, workspace_id, email, role):
        self._login('owner')
        result = self._rpc('/api/workspaces/%s/members' % workspace_id, {
            'action': 'invite', 'email': email, 'role': role,
            'invitation_mode': 'mailbox_challenge_v1',
        })
        return result

    def _accept(self, issue_result, label):
        self.assertTrue(issue_result.get('success'), issue_result)
        invitation = self.env['woow_paas_platform.workspace_invitation'].sudo().browse(
            issue_result['data']['id'])
        self.assertEqual(invitation.state, 'pending')
        self.assertFalse(self._member(invitation.workspace_id.id, invitation.recipient_user_id))
        mail = self.env['mail.mail'].sudo().search([
            ('model', '=', invitation._name), ('res_id', '=', invitation.id),
        ], order='id desc', limit=1)
        self.assertTrue(mail)
        match = re.search(
            r'workspace-invitation/accept/([A-Za-z0-9_-]{43})',
            html.unescape(mail.body_html or ''))
        self.assertTrue(match)
        token = match.group(1)
        self.assertEqual(
            invitation.token_digest,
            hashlib.sha256(token.encode('ascii')).hexdigest())
        self._login(label)
        result = self._rpc('/api/workspace-invitations/accept', {'token': token})
        self.assertTrue(result.get('success'), result)
        return result

    def _member(self, workspace_id, user):
        return self.env['woow_paas_platform.workspace_access'].sudo().search([
            ('workspace_id', '=', workspace_id), ('user_id', '=', user.id),
        ])

    def test_unregistered_recipient_self_claim_gets_real_workspace_admin_access(self):
        workspace_id = self._workspace()
        self._assert_denied(workspace_id)
        target_email = 'ac1011-unregistered-recipient-%s@example.invalid' % uuid.uuid4().hex
        intended_contact = self.env['res.partner'].with_context(
            tracking_disable=True, mail_create_nosubscribe=True,
        ).create({'name': 'Ac1011 intended recipient without account', 'email': target_email})
        self.assertFalse(self.env['res.users'].sudo().search([('email', '=', target_email)]))
        attacker = self._claim_email(target_email)
        result = self._invite(workspace_id, target_email, 'admin')
        self.assertFalse(result.get('success'), result)
        self.assertFalse(self._member(workspace_id, attacker))
        self.assertEqual(intended_contact.email, target_email)
        self._assert_denied(workspace_id)
        self.assertFalse(attacker.has_group('base.group_user'))

    def test_duplicate_portal_email_selects_self_claimed_account_before_intended_portal(self):
        victim = self.synthetic_users['victim']
        victim.partner_id.write({'name': 'Zzz Ac1011 intended registered portal recipient'})
        target_email = victim.email
        workspace_id = self._workspace()
        self._assert_denied(workspace_id)
        attacker = self._claim_email(target_email)
        self.assertNotEqual(attacker.id, victim.id)
        result = self._accept(self._invite(workspace_id, target_email, 'user'), 'victim')
        self.assertEqual(result['data']['user_id'], victim.id)
        self.assertFalse(self._member(workspace_id, attacker))
        self.assertEqual(self._member(workspace_id, victim).role, 'user')
        self._assert_denied(workspace_id)

    def test_existing_internal_recipient_priority_does_not_grant_portal_claimant(self):
        victim = self.synthetic_users['victim']
        victim.write({'groups_id': [(6, 0, [self.env.ref('base.group_user').id])]})
        self.assertFalse(victim.share)
        target_email = victim.email
        workspace_id = self._workspace()
        attacker = self._claim_email(target_email)
        result = self._accept(self._invite(workspace_id, target_email, 'user'), 'victim')
        self.assertEqual(result['data']['user_id'], victim.id)
        self.assertFalse(self._member(workspace_id, attacker))
        self.assertEqual(self._member(workspace_id, victim).role, 'user')
        self._assert_denied(workspace_id)
