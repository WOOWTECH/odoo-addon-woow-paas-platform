"""Interim account-login invite boundary, actual HTTP and membership ORM.

Own dev-sandbox only. Login resolution is an account identifier contract;
it must never be described as mailbox verification. Synthetic credentials
and addresses, no email delivery or operator/service deployment.
"""
import hashlib
import html
import json
import re
import uuid

from odoo.tests.common import HttpCase, tagged


@tagged('post_install', '-at_install', 'ac1011_invite_login')
class TestInviteLoginBoundaryAc1011(HttpCase):
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
        queued_before = self.env['mail.mail'].sudo().search_count([])
        result = self._rpc('/api/workspaces/%s/members' % workspace_id, {
            'action': 'invite', 'email': email, 'role': role,
            'invitation_mode': 'mailbox_challenge_v1',
        })
        if not result.get('success'):
            self.assertEqual(self.env['mail.mail'].sudo().search_count([]), queued_before)
            return result
        invitation = self.env['woow_paas_platform.workspace_invitation'].sudo().browse(
            result['data']['id'])
        self.assertEqual(invitation.state, 'pending')
        self.assertFalse(self._member(workspace_id, invitation.recipient_user_id))
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
        recipient = invitation.recipient_user_id
        label = next(
            key for key, user in self.synthetic_users.items() if user.id == recipient.id)
        self._login(label)
        result = self._rpc('/api/workspace-invitations/accept', {'token': token})
        self.assertTrue(result.get('success'), result)
        return result

    def _assert_denied(self, workspace_id, label='attacker'):
        self._login(label)
        result = self._rpc('/api/workspaces/%s' % workspace_id, {'action': 'get'})
        self.assertFalse(result.get('success'), result)
        self.assertFalse(result.get('data'), result)
        self.assertFalse(self._member(workspace_id, self.synthetic_users[label]))

    def test_profile_claim_of_unregistered_email_never_grants_admin(self):
        workspace_id = self._workspace()
        claimed = 'ac1011-absent-%s@example.invalid' % uuid.uuid4().hex
        self._claim('attacker', claimed)
        result = self._invite(workspace_id, claimed, role='admin')
        self.assertFalse(result.get('success'), result)
        self._assert_denied(workspace_id)

    def test_duplicate_profile_email_cannot_steal_intended_login_uid(self):
        workspace_id = self._workspace()
        victim = self.synthetic_users['victim']
        victim.partner_id.write({'name': 'Zzz Ac1011 intended login account'})
        attacker = self._claim('attacker', victim.login, 'Aaa Ac1011 mutable email claimant')
        result = self._invite(workspace_id, victim.login, role='admin')
        self.assertTrue(result.get('success'), result)
        self.assertEqual(result['data']['user_id'], victim.id)
        self.assertEqual(self._member(workspace_id, victim).role, 'admin')
        self.assertFalse(self._member(workspace_id, attacker))
        self._assert_denied(workspace_id)

    def test_internal_profile_claimant_does_not_override_portal_login_identity(self):
        workspace_id = self._workspace()
        attacker = self.synthetic_users['attacker']
        attacker.write({'groups_id': [(6, 0, [self.env.ref('base.group_user').id])]})
        self.assertFalse(attacker.share)
        victim = self.synthetic_users['victim']
        self._claim('attacker', victim.login)
        result = self._invite(workspace_id, victim.login)
        self.assertTrue(result.get('success'), result)
        self.assertEqual(result['data']['user_id'], victim.id)
        self.assertTrue(self._member(workspace_id, victim))
        self._assert_denied(workspace_id)

    def test_billing_email_drift_does_not_move_account_login_authority(self):
        workspace_id = self._workspace()
        victim = self.synthetic_users['victim']
        original_login = victim.login
        self._claim('victim', 'ac1011-billing-%s@example.invalid' % uuid.uuid4().hex)
        result = self._invite(workspace_id, original_login)
        self.assertTrue(result.get('success'), result)
        self.assertEqual(result['data']['user_id'], victim.id)
        self.assertEqual(self._member(workspace_id, victim).role, 'user')

    def test_casefold_login_collision_refuses_every_candidate(self):
        workspace_id = self._workspace()
        victim = self.synthetic_users['victim']
        attacker = self.synthetic_users['attacker']
        attacker.write({'login': victim.login.upper()})
        self.assertNotEqual(attacker.login, victim.login)
        self._claim('attacker', victim.login)
        result = self._invite(workspace_id, victim.login)
        self.assertFalse(result.get('success'), result)
        self.assertFalse(self._member(workspace_id, victim))
        self._assert_denied(workspace_id)

    def test_profile_only_address_cannot_invite_even_existing_active_account(self):
        workspace_id = self._workspace()
        victim = self.synthetic_users['victim']
        billing = 'ac1011-profile-only-%s@example.invalid' % uuid.uuid4().hex
        self._claim('victim', billing)
        result = self._invite(workspace_id, billing)
        self.assertFalse(result.get('success'), result)
        self.assertFalse(self._member(workspace_id, victim))
        self._assert_denied(workspace_id, label='victim')

    def test_inactive_login_cannot_fall_back_to_active_profile_claimant(self):
        workspace_id = self._workspace()
        victim = self.synthetic_users['victim']
        target = victim.login
        victim.write({'active': False})
        self._claim('attacker', target)
        result = self._invite(workspace_id, target)
        self.assertFalse(result.get('success'), result)
        self.assertFalse(self._member(workspace_id, victim))
        self._assert_denied(workspace_id)

    def test_matching_account_login_can_use_actual_admin_permission_without_group_change(self):
        workspace_id = self._workspace()
        victim = self.synthetic_users['victim']
        result = self._invite(workspace_id, '  ' + victim.login.upper() + '  ', role='admin')
        self.assertTrue(result.get('success'), result)
        self.assertEqual(result['data']['user_id'], victim.id)
        self.assertEqual(self._member(workspace_id, victim).role, 'admin')
        self._login('victim')
        renamed = self._rpc('/api/workspaces/%s' % workspace_id, {
            'action': 'update', 'name': 'Ac1011 explicit login account admin renamed',
        })
        self.assertTrue(renamed.get('success'), renamed)
        self.assertTrue(victim.share)
        self.assertFalse(victim.has_group('base.group_user'))
        self.assertFalse(victim.has_group('base.group_system'))

    def test_literal_sql_wildcards_never_select_other_login(self):
        workspace_id = self._workspace()
        victim = self.synthetic_users['victim']
        target = 'ac1011-literal_%s%%@example.invalid' % uuid.uuid4().hex
        neighbor = target.replace('_', 'x').replace('%', 'suffix')
        victim.write({'login': neighbor, 'email': neighbor})
        result = self._invite(workspace_id, target)
        self.assertFalse(result.get('success'), result)
        self.assertFalse(self._member(workspace_id, victim))

    def test_existing_member_repeat_does_not_change_role(self):
        workspace_id = self._workspace()
        victim = self.synthetic_users['victim']
        first = self._invite(workspace_id, victim.login, role='guest')
        self.assertTrue(first.get('success'), first)
        repeated = self._invite(workspace_id, victim.login, role='admin')
        self.assertFalse(repeated.get('success'), repeated)
        self.assertEqual(self._member(workspace_id, victim).role, 'guest')

    def test_invitation_cannot_assign_owner(self):
        workspace_id = self._workspace()
        victim = self.synthetic_users['victim']
        result = self._invite(workspace_id, victim.login, role='owner')
        self.assertFalse(result.get('success'), result)
        self.assertFalse(self._member(workspace_id, victim))

    def test_nonmember_cannot_invite_account_to_foreign_workspace(self):
        workspace_id = self._workspace()
        victim = self.synthetic_users['victim']
        result = self._invite(workspace_id, victim.login, actor='attacker')
        self.assertFalse(result.get('success'), result)
        self.assertFalse(self._member(workspace_id, victim))

    def test_portal_cannot_rebind_own_login_via_raw_res_users_rpc(self):
        attacker = self.synthetic_users['attacker']
        original_login = attacker.login
        target_login = 'ac1011-raw-login-claim-%s@example.invalid' % uuid.uuid4().hex
        self._login('attacker')
        response = self.url_open(
            '/web/dataset/call_kw/res.users/write',
            data=json.dumps({
                'jsonrpc': '2.0', 'method': 'call', 'params': {
                    'model': 'res.users', 'method': 'write',
                    'args': [[attacker.id], {'login': target_login}], 'kwargs': {},
                },
            }),
            headers={'Content-Type': 'application/json'},
        )
        self.assertEqual(response.status_code, 200)
        self.env.invalidate_all()
        self.assertIn('error', response.json())
        self.assertEqual(attacker.login, original_login)

    def test_profile_payload_cannot_rebind_login_or_other_uid(self):
        attacker = self.synthetic_users['attacker']
        victim = self.synthetic_users['victim']
        original_login = attacker.login
        victim_email = victim.email
        self._login('attacker')
        claimed = 'ac1011-profile-injection-%s@example.invalid' % uuid.uuid4().hex
        result = self._rpc('/api/me/update', {
            'name': 'Ac1011 own mutable notification contact', 'email': claimed,
            'phone': '', 'login': victim.login,
            'user_id': victim.id, 'partner_id': victim.partner_id.id,
        })
        self.assertTrue(result.get('success'), result)
        self.assertEqual(attacker.login, original_login)
        self.assertEqual(attacker.email, claimed)
        self.assertEqual(victim.email, victim_email)
