"""Portal PaaS product-path contract, using real authenticated HTTP requests.

These tests intentionally do not prescribe sudo versus ORM implementation:
legitimate Portal actions must succeed, foreign resources must stay hidden,
and the caller must never acquire an internal/ERP identity. Provisioning and
payment are NOT exercised here; their separate tests must cover those gates.
Run only in an isolated Odoo test database, never on production.
"""
import json
import uuid

from odoo.tests.common import HttpCase, tagged
from .invitation_mailbox_fixture import accept_queued_invitation


@tagged('post_install', '-at_install', 'portal_paas')
class TestPortalPaasWorkspaceJourney(HttpCase):
    def setUp(self):
        super().setUp()
        self.env.company.email = 'isolated-invite-sender@example.invalid'
        self.users = {}
        for label in ('owner', 'other', 'admin', 'member', 'guest'):
            login = 'portal-paas-%s-%s@example.invalid' % (
                label, uuid.uuid4().hex,
            )
            user = self.env['res.users'].with_context(
                no_reset_password=True,
                tracking_disable=True,
                mail_create_nosubscribe=True,
            )._create_user_from_template({
                'name': 'Portal PaaS %s' % label,
                'login': login,
                'email': login,
                'password': login,
            })
            self.users[label] = user
            self._assert_portal_identity(user)

    def _assert_portal_identity(self, user):
        self.assertTrue(user.has_group('base.group_portal'))
        self.assertFalse(user.has_group('base.group_user'))
        self.assertFalse(user.has_group('base.group_system'))
        self.assertFalse(user.has_group('account.group_account_manager'))

    def _login(self, label):
        user = self.users[label]
        self.authenticate(user.login, user.login)

    def _rpc(self, path, params):
        response = self.url_open(
            path,
            data=json.dumps({
                'jsonrpc': '2.0', 'method': 'call', 'params': params,
            }),
            headers={'Content-Type': 'application/json'},
        )
        self.env.invalidate_all()
        self.assertEqual(response.status_code, 200)
        return response.json()

    def _success(self, path, params):
        response = self._rpc(path, params)
        self.assertNotIn('error', response, response)
        result = response.get('result')
        self.assertIsInstance(result, dict)
        self.assertTrue(result.get('success'), result)
        return result

    def _assert_denied(self, response):
        if 'error' in response:
            # Do not count unrelated crashes as a successful security denial.
            error_name = response['error'].get('data', {}).get('name', '')
            self.assertIn(error_name, (
                'odoo.exceptions.AccessError', 'odoo.exceptions.AccessDenied',
                'odoo.exceptions.MissingError',
            ), response)
        else:
            result = response.get('result', {})
            self.assertIs(result.get('success'), False, response)
            self.assertFalse(result.get('data'), response)

    def _create_workspace(self):
        return self._success('/api/workspaces', {
            'action': 'create',
            'name': 'Portal workspace %s' % uuid.uuid4().hex,
        })['data']['id']

    def test_signup_member_can_create_and_revisit_own_workspace(self):
        self._login('owner')
        result = self._success('/api/workspaces', {'action': 'list'})
        self.assertEqual(result['data'], [])
        workspace_id = self._create_workspace()
        result = self._success('/api/workspaces', {'action': 'list'})
        self.assertEqual({row['id'] for row in result['data']}, {workspace_id})
        workspace = self.env['woow_paas_platform.workspace'].browse(workspace_id)
        self.assertEqual(workspace.owner_id, self.users['owner'])
        self.assertEqual(
            workspace.access_ids.filtered(
                lambda row: row.user_id == self.users['owner']
            ).role,
            'owner',
        )
        self._success('/api/workspaces/%s' % workspace_id, {
            'action': 'update', 'name': 'My renamed Portal workspace',
        })
        # Exercise an existing member with a new authenticated session too.
        self._login('other')
        self._login('owner')
        result = self._success('/api/workspaces', {'action': 'list'})
        self.assertEqual({row['id'] for row in result['data']}, {workspace_id})
        self._assert_portal_identity(self.users['owner'])

    def test_foreign_workspace_hidden_in_product_api_and_direct_orm(self):
        self._login('owner')
        workspace_id = self._create_workspace()
        self._login('other')
        self.assertEqual(
            self._success('/api/workspaces', {'action': 'list'})['data'], [],
        )
        for action in ('get', 'update', 'delete'):
            with self.subTest(action=action):
                self._assert_denied(self._rpc(
                    '/api/workspaces/%s' % workspace_id,
                    {'action': action, 'name': 'Forbidden foreign rename'},
                ))
        response = self._rpc('/web/dataset/call_kw', {
            'model': 'woow_paas_platform.workspace',
            'method': 'search_read',
            'args': [[('id', '=', workspace_id)]],
            'kwargs': {'fields': ['id', 'name']},
        })
        if 'error' in response:
            self._assert_denied(response)
        else:
            self.assertEqual(response['result'], [])
        workspace = self.env['woow_paas_platform.workspace'].browse(workspace_id)
        self.assertEqual(workspace.owner_id, self.users['owner'])
        self.assertEqual(workspace.state, 'active')
        self.assertNotEqual(workspace.name, 'Forbidden foreign rename')

    def test_workspace_roles_do_not_turn_portal_members_into_admins(self):
        self._login('owner')
        workspace_id = self._create_workspace()
        for label, role in (('admin', 'admin'), ('member', 'user'), ('guest', 'guest')):
            self.env['woow_paas_platform.workspace_access'].create({
                'workspace_id': workspace_id,
                'user_id': self.users[label].id,
                'role': role,
            })
        self.env.flush_all()
        for label in ('member', 'guest'):
            with self.subTest(member=label):
                self._login(label)
                result = self._success('/api/workspaces', {'action': 'list'})
                self.assertEqual({row['id'] for row in result['data']}, {workspace_id})
                self._assert_denied(self._rpc(
                    '/api/workspaces/%s' % workspace_id,
                    {'action': 'update', 'name': 'Forbidden member rename'},
                ))
                self._assert_portal_identity(self.users[label])
        self._login('admin')
        self._success('/api/workspaces/%s' % workspace_id, {
            'action': 'update', 'name': 'Permitted workspace admin rename',
        })
        self._assert_portal_identity(self.users['admin'])

    def test_new_member_can_load_public_paas_surfaces(self):
        self._login('owner')
        for path in (
            '/api/config', '/api/me', '/api/dashboard/summary',
            '/api/cloud/templates', '/api/billing/wallet',
            '/api/billing/subscriptions', '/api/billing/invoices',
            '/api/billing/payment-methods',
        ):
            with self.subTest(path=path):
                self._success(path, {})
        self._assert_portal_identity(self.users['owner'])

    def test_authenticated_external_account_without_portal_group_can_use_paas(self):
        # The product contract is authenticated/non-public/non-internal, not
        # the incidental presence of one group on older external accounts.
        user = self.users['owner']
        user.write({'groups_id': [(5, 0, 0)]})
        self.assertTrue(user.share)
        self.assertFalse(user._is_public())
        self.assertFalse(user.has_group('base.group_portal'))
        self.assertFalse(user.has_group('base.group_user'))
        self._login('owner')
        self.assertEqual(
            self._success('/api/workspaces', {'action': 'list'})['data'], [],
        )
        workspace_id = self._create_workspace()
        self.assertIn(workspace_id, {
            row['id'] for row in self._success(
                '/api/workspaces', {'action': 'list'},
            )['data']
        })
        self._assert_denied(self._rpc('/web/dataset/call_kw', {
            'model': 'woow_paas_platform.workspace', 'method': 'read',
            'args': [[workspace_id]], 'kwargs': {'fields': ['id']},
        }))
        self.assertFalse(user.has_group('base.group_user'))

    def test_member_email_lookup_does_not_expand_sql_wildcards(self):
        login = 'invite-target-%s@example.invalid' % uuid.uuid4().hex
        target = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'Invitation target', 'login': login, 'email': login, 'password': login,
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self._login('owner')
        workspace_id = self._create_workspace()
        result = self._rpc('/api/workspaces/%s/members' % workspace_id, {
            'action': 'invite', 'email': '%@example.invalid', 'role': 'guest',
            'invitation_mode': 'mailbox_challenge_v1',
        })
        self._assert_denied(result)
        self.assertFalse(self.env['woow_paas_platform.workspace_access'].search([
            ('workspace_id', '=', workspace_id), ('user_id', '=', target.id),
        ]))
        # Exact case-insensitive email matching must still work.
        invited = self._success('/api/workspaces/%s/members' % workspace_id, {
            'action': 'invite', 'email': login.upper(), 'role': 'guest',
            'invitation_mode': 'mailbox_challenge_v1',
        })
        invited = accept_queued_invitation(self, invited, target, login, self._success)
        self._login('owner')
        self.assertEqual(invited['data']['user_id'], target.id)

    def test_literal_underscore_email_never_invites_a_different_account(self):
        address = 'invite_%s_member@example.invalid' % uuid.uuid4().hex
        User = self.env['res.users'].with_context(no_reset_password=True)
        decoy = User._create_user_from_template({
            'name': 'A wildcard decoy', 'login': address.replace('_', 'X'),
            'email': address.replace('_', 'X'),
        })
        intended = User._create_user_from_template({
            'name': 'Z literal intended', 'login': address, 'email': address,
            'password': address,
        })
        self._login('owner')
        workspace_id = self._create_workspace()
        invited = self._success('/api/workspaces/%s/members' % workspace_id, {
            'action': 'invite', 'email': address, 'role': 'guest',
            'invitation_mode': 'mailbox_challenge_v1',
        })
        invited = accept_queued_invitation(self, invited, intended, address, self._success)
        self._login('owner')
        self.assertEqual(invited['data']['user_id'], intended.id)
        self.assertFalse(self.env['woow_paas_platform.workspace_access'].search([
            ('workspace_id', '=', workspace_id), ('user_id', '=', decoy.id),
        ]))

    def test_portal_cannot_create_erp_users_or_companies(self):
        self._login('owner')
        for model, values in (
            ('res.users', {'login': 'forbidden-portal-user@example.invalid',
                           'name': 'Forbidden internal user'}),
            ('res.company', {'name': 'Forbidden Portal company'}),
            ('ir.config_parameter', {'key': 'portal_paas.forbidden',
                                     'value': 'must-not-be-created'}),
        ):
            with self.subTest(model=model):
                self._assert_denied(self._rpc('/web/dataset/call_kw', {
                    'model': model, 'method': 'create',
                    'args': [values], 'kwargs': {},
                }))
