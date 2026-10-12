"""Portal product capabilities do not imply raw ORM or ERP permissions.

HTTP probes exercise the real principal and JSON-RPC context. Only operator
I/O is mocked; membership, role gates, ORM ACLs and payloads stay real.
"""
import json
import uuid
from contextlib import ExitStack
from unittest.mock import patch

from odoo.tests.common import HttpCase, tagged

from ..models.cloud_service import _internal_provision
from ..models.smart_home import _platform_order
from ..models.security_access import _skip_route_sync
from ..services.paas_capability import _PaaSCapability
from .invitation_mailbox_fixture import accept_queued_invitation

_PREFIX = 'odoo.addons.woow_paas_platform.'
_OPERATOR_FACTORIES = (
    'controllers.paas.get_paas_operator_client',
    'models.smart_home.get_paas_operator_client',
    'models.security_access.get_paas_operator_client',
    'services.paas_operator.get_paas_operator_client',
)


@tagged('post_install', '-at_install', 'portal_paas')
class TestPortalPaasCapability20261001(HttpCase):
    def setUp(self):
        super().setUp()
        self.suffix = uuid.uuid4().hex[:10]
        self.env.company.email = 'isolated-invite-sender@example.invalid'
        # Fresh-install tests must not depend on an upgrade or another test
        # having populated the subscription journal configuration.
        if not self.env.company.sh_journal_id:
            journal = self.env['account.journal'].search([
                ('company_id', '=', self.env.company.id), ('type', '=', 'sale'),
            ], limit=1)
            if not journal:
                journal = self.env['account.journal'].create({
                    'name': 'Portal test sales', 'type': 'sale',
                    'code': 'PC' + self.suffix[:3].upper(),
                    'company_id': self.env.company.id,
                })
            self.env.company.sh_journal_id = journal
        self.users = {}
        for label in ('owner', 'admin', 'user', 'guest', 'other', 'invitee'):
            login = 'cap-%s-%s@example.invalid' % (label, self.suffix)
            user = self.env['res.users'].with_context(no_reset_password=True).create({
                'name': login, 'login': login, 'email': login, 'password': login,
                'groups_id': [(6, 0, [self.env.ref('base.group_portal').id])],
            })
            self.assertTrue(user.share)
            self.assertFalse(user.has_group('base.group_user'))
            self.users[label] = user
        self.ws = self.env['woow_paas_platform.workspace'].create({
            'name': 'Capabilities ' + self.suffix, 'owner_id': self.users['owner'].id,
        })
        self.other_ws = self.env['woow_paas_platform.workspace'].create({
            'name': 'Foreign ' + self.suffix, 'owner_id': self.users['other'].id,
        })
        for role in ('admin', 'user', 'guest'):
            self.env['woow_paas_platform.workspace_access'].create({
                'workspace_id': self.ws.id, 'user_id': self.users[role].id,
                'role': role,
            })
        self.template = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'Capability template', 'slug': 'cap-' + self.suffix,
            'helm_repo_url': 'oci://example.invalid',
            'helm_chart_name': 'capability',
            'helm_chart_version': '1.0.0', 'workload_type': 'container',
            'post_deploy_init_type': 'none', 'mcp_enabled': False,
            'helm_value_specs': json.dumps({'optional': [
                {'key': 'display', 'type': 'string'},
                {'key': 'auth.apiKey', 'type': 'password'},
            ]}),
            'helm_default_values': json.dumps({
                'display': 'safe', 'platformOnly': 'NEVER-DISCLOSE-DEFAULT',
            }),
        })
        self.resources = {}
        for label, ws in (('own', self.ws), ('foreign', self.other_ws)):
            service = _internal_provision(self.env['woow_paas_platform.cloud_service']).create({
                'name': label + ' service', 'workspace_id': ws.id,
                'template_id': self.template.id, 'state': 'running',
                'helm_namespace': 'paas-ws-' + ws.ws_hash,
                'helm_release_name': 'cap-' + label + '-' + self.suffix,
                'helm_values': json.dumps({'display': 'safe',
                                          'platformOnly': 'NEVER-DISCLOSE-SERVICE',
                                          'auth': {'apiKey': 'NEVER-DISCLOSE-KEY'}}),
            }).with_env(self.env)
            home = _platform_order(self.env['woow_paas_platform.smart_home']).create({
                'name': label + ' home', 'workspace_id': ws.id,
            }).with_env(self.env)
            sa = self.env['woow_paas_platform.security_access'].create({
                'name': label + ' access', 'workspace_id': ws.id,
            })
            route = _skip_route_sync(self.env['woow_paas_platform.security_access_route']).create({
                'security_access_id': sa.id,
                'subdomain_prefix': 'cap-' + label + '-' + self.suffix,
                'service_url': 'http://localhost:8123',
            }).with_env(self.env)
            self.resources[label] = {'services': service, 'smarthomes': home,
                                     'security-accesses': sa, 'route': route}
        self.env.flush_all()

    def _login(self, label):
        user = self.users[label]
        self.authenticate(user.login, user.login)

    def _raw(self, path, params):
        response = self.url_open(path, data=json.dumps({
            'jsonrpc': '2.0', 'method': 'call', 'params': params,
        }), headers={'Content-Type': 'application/json'})
        self.assertEqual(response.status_code, 200)
        self.env.invalidate_all()
        return response.json()

    def _rpc(self, path, params=None, success=True):
        body = self._raw(path, params or {})
        self.assertIn('result', body, 'Product endpoint must not leak ORM errors: %s' % body)
        result = body['result']
        self.assertIs(result.get('success'), success, result)
        if not success:
            self.assertFalse(result.get('data'), result)
        return result

    def _url(self, family, record=None, ws=None):
        url = '/api/workspaces/%s/%s' % ((ws or self.ws).id, family)
        return url + ('/%s' % record.id if record else '')

    def _no_operator(self, stack):
        return [stack.enter_context(patch(_PREFIX + target))
                for target in _OPERATOR_FACTORIES]

    def test_every_role_reads_own_three_service_families_not_foreign(self):
        with ExitStack() as stack:
            # No tunnel is provisioned; overlay behavior is tested separately.
            stack.enter_context(patch.object(type(self.resources['own']['smarthomes']),
                                             '_live_tunnel_status', return_value={}))
            stack.enter_context(patch.object(type(self.resources['own']['security-accesses']),
                                             '_live_tunnel_status', return_value={}))
            for role in ('owner', 'admin', 'user', 'guest'):
                self._login(role)
                for family in ('services', 'smarthomes', 'security-accesses'):
                    with self.subTest(role=role, family=family):
                        own, foreign = self.resources['own'][family], self.resources['foreign'][family]
                        listed = self._rpc(self._url(family), {'action': 'list'})
                        self.assertIn(own.id, [r['id'] for r in listed['data']])
                        self.assertNotIn(foreign.id, [r['id'] for r in listed['data']])
                        detail = self._rpc(self._url(family, own), {'action': 'get'})
                        encoded = json.dumps(detail)
                        for secret in ('NEVER-DISCLOSE', 'tunnel_token', 'deploy_params',
                                       'n8n_owner_password', 'gateway_api_key'):
                            self.assertNotIn(secret, encoded)
                        self._rpc(self._url(family, foreign), {'action': 'get'}, success=False)
                        self._rpc(self._url(family, ws=self.other_ws), {'action': 'list'}, success=False)

    def test_non_managers_and_foreign_mutations_never_reach_operator(self):
        with ExitStack() as stack:
            operators = self._no_operator(stack)
            for role in ('user', 'guest', 'other'):
                self._login(role)
                for family in ('services', 'smarthomes', 'security-accesses'):
                    own = self.resources['own'][family]
                    for action in ('delete', 'update' if family == 'services' else 'provision'):
                        self._rpc(self._url(family, own), {'action': action}, success=False)
                    self._rpc(self._url(family), {'action': 'create', 'name': 'Forbidden',
                              'template_id': self.template.id, 'plan_id': 1}, success=False)
                self._rpc(self._url('services', self.resources['own']['services']) +
                          '/reset-admin-password', success=False)
                self._rpc(self._url('security-accesses', self.resources['own']['security-accesses']) +
                          '/tunnel-token', success=False)
            self._login('owner')
            for family in ('services', 'smarthomes', 'security-accesses'):
                self._rpc(self._url(family, self.resources['foreign'][family]),
                          {'action': 'delete'}, success=False)
            for operator in operators:
                operator.assert_not_called()
        for family in ('services', 'smarthomes', 'security-accesses'):
            self.assertTrue(self.resources['own'][family].exists())
            self.assertTrue(self.resources['foreign'][family].exists())

    def test_membership_ids_cannot_cross_workspaces_or_modify_owner(self):
        self._login('admin')
        foreign = self.other_ws.access_ids[:1]
        own_owner = self.ws.access_ids.filtered(lambda row: row.role == 'owner')
        for target in (foreign, own_owner):
            for action in ('update_role', 'remove'):
                self._rpc('/api/workspaces/%s/members/%s' % (self.ws.id, target.id),
                          {'action': action, 'role': 'admin'}, success=False)
        self._rpc('/api/workspaces/%s' % self.ws.id, {'action': 'delete'}, success=False)
        self.assertEqual(self.ws.state, 'active')
        self.assertEqual(own_owner.role, 'owner')
        self.assertTrue(foreign.exists())
        invited = self._rpc('/api/workspaces/%s/members' % self.ws.id, {
            'action': 'invite', 'email': self.users['invitee'].email, 'role': 'user',
            'invitation_mode': 'mailbox_challenge_v1',
        })
        invited = accept_queued_invitation(
            self, invited, self.users['invitee'], self.users['invitee'].login, self._rpc)
        self._login('admin')
        member_url = '/api/workspaces/%s/members/%s' % (self.ws.id, invited['data']['id'])
        self._rpc(member_url, {'action': 'update_role', 'role': 'admin'})
        self._rpc(member_url, {'action': 'remove'})
        self.assertTrue(self.users['invitee'].share)

    def test_request_context_cannot_forge_workspace_or_membership_defaults(self):
        self._login('owner')
        result = self._rpc('/api/workspaces', {
            'action': 'create', 'name': 'Clean context ' + self.suffix,
            'owner_id': self.users['other'].id, 'state': 'archived',
            'context': {
                'default_owner_id': self.users['other'].id,
                'default_state': 'archived', 'default_ws_hash': 'deadbeef',
                'default_access_ids': [(0, 0, {'user_id': self.users['other'].id, 'role': 'admin'})],
                'allowed_company_ids': [987654321], 'active_test': False,
            },
        })
        workspace = self.env['woow_paas_platform.workspace'].browse(result['data']['id'])
        self.assertEqual(workspace.owner_id, self.users['owner'])
        self.assertEqual(workspace.state, 'active')
        self.assertNotEqual(workspace.ws_hash, 'deadbeef')
        self.assertEqual(workspace.access_ids.user_id, self.users['owner'])
        result = self._rpc('/api/workspaces/%s/members' % self.ws.id, {
            'action': 'invite', 'email': self.users['invitee'].email, 'role': 'guest',
            'invitation_mode': 'mailbox_challenge_v1',
            'context': {'default_workspace_id': self.other_ws.id, 'default_role': 'owner',
                        'default_invited_by_id': self.users['other'].id},
        })
        result = accept_queued_invitation(
            self, result, self.users['invitee'], self.users['invitee'].login, self._rpc)
        self._login('owner')
        member = self.env['woow_paas_platform.workspace_access'].browse(result['data']['id'])
        self.assertEqual(member.workspace_id, self.ws)
        self.assertEqual(member.role, 'guest')
        self.assertEqual(member.invited_by_id, self.users['owner'])

    def test_raw_rpc_own_records_and_public_mutators_stay_denied(self):
        self._login('owner')
        with ExitStack() as stack:
            operators = self._no_operator(stack)
            records = [self.ws, self.ws.access_ids[:1]] + [
                self.resources['own'][f] for f in ('services', 'smarthomes', 'security-accesses', 'route')
            ]
            for record in records:
                for method, args, kwargs in (
                    ('read', [record.ids], {'fields': ['id'],
                                           'context': {'sudo': True, 'uid': 1}}),
                    ('write', [record.ids, {'name': 'Forbidden raw RPC'}], {}),
                    ('unlink', [record.ids], {}),
                ):
                    if record._name.endswith('_route') and method == 'write':
                        args = [record.ids, {'service_url': 'http://forbidden.invalid'}]
                    if record._name.endswith('workspace_access') and method == 'write':
                        args = [record.ids, {'role': 'admin'}]
                    with self.subTest(model=record._name, method=method):
                        body = self._raw('/web/dataset/call_kw', {
                            'model': record._name, 'method': method, 'args': args, 'kwargs': kwargs,
                        })
                        self.assertIn('error', body, body)
                        self.assertEqual(body['error']['data']['name'], 'odoo.exceptions.AccessError', body)
            for family, methods in (
                ('services', ('action_redeploy', '_redeploy', '_reset_admin_password')),
                ('smarthomes', ('action_provision', 'action_delete', 'action_refresh_status')),
                ('security-accesses', ('action_provision', 'action_delete', 'action_refresh_status')),
            ):
                record = self.resources['own'][family]
                for method in methods:
                    body = self._raw('/web/dataset/call_kw', {
                        'model': record._name, 'method': method, 'args': [record.ids], 'kwargs': {},
                    })
                    self.assertIn('error', body, (family, method, body))
                    self.assertEqual(body['error']['data']['name'], 'odoo.exceptions.AccessError', body)
            for operator in operators:
                operator.assert_not_called()

    def test_raw_paas_creation_remains_denied_without_operator_calls(self):
        self._login('owner')
        cases = (
            ('workspace', {'name': 'Forbidden raw workspace'}),
            ('workspace_access', {'workspace_id': self.ws.id,
                                  'user_id': self.users['invitee'].id, 'role': 'admin'}),
            ('cloud_service', {'name': 'Forbidden raw service',
                               'workspace_id': self.ws.id, 'template_id': self.template.id}),
            ('smart_home', {'name': 'Forbidden raw home', 'workspace_id': self.ws.id}),
            ('security_access', {'name': 'Forbidden raw access', 'workspace_id': self.ws.id}),
        )
        with ExitStack() as stack:
            operators = self._no_operator(stack)
            for model, values in cases:
                with self.subTest(model=model):
                    result = self._raw('/web/dataset/call_kw', {
                        'model': 'woow_paas_platform.' + model, 'method': 'create',
                        'args': [values], 'kwargs': {'context': {'sudo': True, 'uid': 1}},
                    })
                    self.assertIn('error', result, result)
                    # These models deliberately reject raw creation via
                    # their dedicated platform-order gates before ORM ACLs.
                    expected_error = {
                        'cloud_service': _PREFIX + 'models.cloud_service.ProvisionGateError',
                        'smart_home': _PREFIX + 'models.smart_home.SmartHomeOrderGateError',
                        'security_access': _PREFIX + 'models.security_access.SecurityAccessOrderGateError',
                    }.get(model, 'odoo.exceptions.AccessError')
                    self.assertEqual(result['error']['data']['name'], expected_error, result)
            for operator in operators:
                operator.assert_not_called()

    def test_policy_route_substitution_denied_before_operator(self):
        self._login('owner')
        own_sa = self.resources['own']['security-accesses']
        foreign_route = self.resources['foreign']['route']
        with ExitStack() as stack:
            operators = self._no_operator(stack)
            self._rpc(self._url('security-accesses', own_sa) + '/access-policies', {
                'action': 'create', 'name': 'Forbidden route', 'route_id': foreign_route.id,
                'include': [{'rule_type': 'email', 'rule_value': 'a@example.invalid'}],
            }, success=False)
            self._rpc(self._url('security-accesses', own_sa) +
                      '/routes/%s/access-config' % foreign_route.id,
                      {'is_protected': True}, success=False)
            for operator in operators:
                operator.assert_not_called()
        self.assertFalse(foreign_route.is_protected)

    def test_access_policy_crud_pins_typed_anchors_and_rule_commands(self):
        self._login('owner')
        with ExitStack() as stack:
            operators = self._no_operator(stack)
            for family in ('services', 'smarthomes', 'security-accesses'):
                own, foreign = self.resources['own'][family], self.resources['foreign'][family]
                route_key = {}
                if family == 'smarthomes':
                    route_key['route_id'] = own._ensure_primary_route().id
                    foreign_anchor = {'smart_home_route_id': foreign._ensure_primary_route().id}
                elif family == 'security-accesses':
                    route_key['route_id'] = self.resources['own']['route'].id
                    foreign_anchor = {'route_id': self.resources['foreign']['route'].id}
                else:
                    foreign_anchor = {'cloud_service_id': foreign.id}
                foreign_policy = _skip_route_sync(
                    self.env['woow_paas_platform.security_access_access_policy']
                ).create({
                    **foreign_anchor, 'name': 'Foreign policy', 'decision': 'deny',
                    'rule_ids': [(0, 0, {'group': 'include', 'rule_type': 'everyone', 'rule_value': ''})],
                }).with_env(self.env)
                self.env.flush_all()
                url = self._url(family, own) + '/access-policies'
                result = self._rpc(url, {
                    'action': 'create', 'name': 'Own policy', 'decision': 'deny',
                    **route_key,
                    'cloud_service_id': self.resources['foreign']['services'].id,
                    'security_access_id': self.resources['foreign']['security-accesses'].id,
                    'smart_home_id': self.resources['foreign']['smarthomes'].id,
                    'cf_policy_id': 'FORGED-CF-ID',
                    'include': [{'rule_type': 'everyone', 'rule_value': '',
                                 'policy_id': foreign_policy.id, 'cf_policy_id': 'FORGED-RULE-ID'}],
                })
                policy = self.env['woow_paas_platform.security_access_access_policy'].browse(result['data']['id'])
                self.assertFalse(policy.cf_policy_id)
                self.assertEqual(policy.rule_ids.policy_id, policy)
                anchor_field = {'services': 'cloud_service_id',
                                'smarthomes': 'smart_home_id',
                                'security-accesses': 'security_access_id'}[family]
                self.assertEqual(policy[anchor_field], own)
                for role in ('owner', 'admin', 'user', 'guest'):
                    self._login(role)
                    listed = self._rpc(url, {'action': 'list'})
                    self.assertIn(policy.id, [p['id'] for p in listed['data']])
                    self.assertNotIn(foreign_policy.id, [p['id'] for p in listed['data']])
                    if role in ('user', 'guest'):
                        self._rpc(url, {'action': 'update', 'policy_id': policy.id,
                                        'name': 'Forbidden'}, success=False)
                        self._rpc(url, {'action': 'delete', 'policy_id': policy.id}, success=False)
                self._login('owner')
                for record in (policy, policy.rule_ids):
                    values = {'name': 'Forbidden raw policy'} if record == policy else {'sequence': 99}
                    for method, args, kwargs in (
                        ('read', [record.ids], {'fields': ['id']}),
                        ('write', [record.ids, values], {}),
                        ('unlink', [record.ids], {}),
                    ):
                        before_calls = [list(operator.mock_calls) for operator in operators]
                        raw = self._raw('/web/dataset/call_kw', {
                            'model': record._name, 'method': method,
                            'args': args, 'kwargs': kwargs,
                        })
                        self.assertIn('error', raw, raw)
                        self.assertIn(raw['error']['data']['name'], (
                            'odoo.exceptions.AccessError', 'odoo.exceptions.UserError',
                            'odoo.exceptions.ValidationError',
                        ), raw)
                        self.assertEqual(before_calls, [list(operator.mock_calls) for operator in operators])
                self._rpc(url, {'action': 'update', 'policy_id': foreign_policy.id,
                                'name': 'Forbidden'}, success=False)
                self._rpc(url, {'action': 'delete', 'policy_id': foreign_policy.id}, success=False)
                self._rpc(url, {'action': 'update', 'policy_id': policy.id, 'name': 'Renamed'})
                self.assertEqual(policy.name, 'Renamed')
                self._rpc(url, {'action': 'delete', 'policy_id': policy.id})
                self.assertFalse(policy.exists())
                self.assertTrue(foreign_policy.exists())

    def test_anonymous_product_requests_refused_before_operator(self):
        self.authenticate(None, None)
        with ExitStack() as stack:
            operators = self._no_operator(stack)
            for url in ('/api/workspaces', self._url('services'),
                        self._url('smarthomes'), self._url('security-accesses')):
                body = self._raw(url, {'action': 'list'})
                self.assertIn('error', body, body)
                self.assertNotIn('result', body, body)
            for operator in operators:
                operator.assert_not_called()

    def _order_catalog(self, family):
        plan = self.env['sh.subscription.plan'].create({
            'name': 'Portal trial ' + family, 'sh_plan_price': 100,
            'sh_duration': 1, 'sh_unit': 'month', 'sh_no_of_billing_cycle': -1,
            'sh_trial': True, 'sh_trial_duration': 7, 'sh_trial_unit': 'day',
        })
        self.env['product.template'].create({
            'name': 'Portal trial product ' + family, 'type': 'service',
            'sh_product_subscribe': True, 'sh_subscription_plan_id': plan.id,
            'list_price': 100, 'taxes_id': [(5, 0, 0)],
        })
        if family == 'services':
            template = self.template
            template.subscription_plan_ids = [(6, 0, [plan.id])]
        else:
            model = ('smart_home_template' if family == 'smarthomes'
                     else 'security_access_template')
            template = self.env['woow_paas_platform.' + model].create({
                'name': 'Portal ' + family, 'slug': family + '-' + self.suffix,
                'subscription_plan_ids': [(6, 0, [plan.id])],
            })
        self.env.flush_all()
        return template, plan

    def test_portal_orders_preserve_owner_billing_and_paid_gates(self):
        """Real order/subscription workflow, no authorization or billing mocks."""
        catalogs = {f: self._order_catalog(f)
                    for f in ('services', 'smarthomes', 'security-accesses')}
        self._login('admin')
        with ExitStack() as stack:
            operators = self._no_operator(stack)
            # Scheduling only: never let a test start deployment work.
            stack.enter_context(patch.object(type(self.env['ir.cron']), '_trigger'))
            for family, (template, plan) in catalogs.items():
                with self.subTest(family=family):
                    result = self._rpc(self._url(family), {
                        'action': 'create', 'name': 'Portal ordered ' + family,
                        'template_id': template.id, 'plan_id': plan.id, 'values': {},
                        'context': {'default_workspace_id': self.other_ws.id,
                                    'default_state': 'error', 'default_project_ids': [(6, 0, [])]},
                    })
                    model = self.resources['own'][family]._name
                    record = self.env[model].browse(result['data']['id'])
                    self.assertEqual(record.workspace_id, self.ws)
                    self.assertTrue(record.subscription_id)
                    self.assertEqual(record.subscription_id.sh_partner_id,
                                     self.users['owner'].partner_id)
                    self.assertEqual(record.subscription_id.sh_subscription_plan_id, plan)
                    # The private seam keeps env.uid; sudo must not turn a
                    # Portal workspace admin into the system paid-gate bypass.
                    record.subscription_id.write({'state': 'cancel'})
                    self.env.flush_all()
                    self._rpc(self._url(family, record), {'action': 'delete'}, success=False)
                    self.assertTrue(record.exists())
            for operator in operators:
                operator.assert_not_called()
        self.assertTrue(self.users['admin'].share)

    def test_raw_workspace_unlink_must_authorize_before_sudo_child_teardown(self):
        """Security contract: even a denied RPC must not start remote cleanup.

        Regression for workspace.unlink authorization before its privileged
        child cascade, independent of the product controller's checks.
        """
        self._login('other')
        SA = type(self.resources['own']['security-accesses'])
        with patch.object(SA, '_cleanup_cloudflare') as teardown:
            body = self._raw('/web/dataset/call_kw', {
                'model': self.ws._name, 'method': 'unlink',
                'args': [self.ws.ids], 'kwargs': {},
            })
            self.assertIn('error', body, body)
            self.assertEqual(body['error']['data']['name'], 'odoo.exceptions.AccessError', body)
            teardown.assert_not_called()

    def _mark_legacy_workspace_archived(self):
        # #1295 deliberately forbids creating this state even under sudo.
        # Seed only the historical DB shape for the read-side denial probe;
        # do not add a production escape hatch or weaken the new guard.
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE woow_paas_platform_workspace SET state='archived' WHERE id=%s",
            (self.ws.id,),
        )
        self.env.invalidate_all()

    def test_portal_owner_cannot_archive_a_workspace_with_live_resources(self):
        self._login('owner')
        with ExitStack() as stack:
            operators = self._no_operator(stack)
            body = self._raw('/api/workspaces/%s' % self.ws.id, {'action': 'delete'})
            self.assertNotIn('error', body, body)
            result = body['result']
            self.assertIs(result.get('success'), False)
            self.assertEqual(result.get('code'), 'workspace_has_services')
            self.assertEqual(self.ws.state, 'active')
            self.assertEqual({row['name'] for row in result['data']['services']},
                             {'own service', 'own home', 'own access'})
            for operator in operators:
                operator.assert_not_called()

    def test_public_inactive_archived_and_revoked_membership_fail_closed(self):
        public = self.env.ref('base.public_user')
        self.assertFalse(_PaaSCapability(self.env(user=public.id)).workspace(self.ws.id))
        inactive = self.users['invitee']
        inactive.active = False
        self.assertFalse(_PaaSCapability(self.env(user=inactive.id)).workspace(self.ws.id))
        self._login('guest')
        membership = self.ws.access_ids.filtered(lambda row: row.user_id == self.users['guest'])
        membership.unlink()
        self.env.flush_all()
        self._rpc(self._url('services'), {'action': 'list'}, success=False)
        self._mark_legacy_workspace_archived()
        self._login('owner')
        self._rpc(self._url('services'), {'action': 'list'}, success=False)
        self._rpc('/api/workspaces/%s' % self.ws.id, {'action': 'get'}, success=False)

    def test_workspace_membership_does_not_grant_private_project_or_mcp_access(self):
        project = self.env['project.project'].create({
            'name': 'PRIVATE_PROJECT_' + self.suffix,
            'privacy_visibility': 'employees',
            'cloud_service_id': self.resources['own']['services'].id,
            'use_agent_runtime': False,
        })
        server = self.env['woow_paas_platform.mcp_server'].create({
            'name': 'PRIVATE_MCP_' + self.suffix,
            'url': 'https://private-project.example.invalid/mcp',
            'transport': 'sse', 'scope': 'user', 'project_id': project.id,
        })
        self.env['ir.config_parameter'].set_param(
            'woow_paas_platform.portal_project_settings_editable', 'True',
        )
        self.env.flush_all()
        service_path = self._url('services', self.resources['own']['services']) + '/mcp-servers'
        project_path = '/api/support-projects/%s' % project.id
        with ExitStack() as stack:
            operators = self._no_operator(stack)
            sync = stack.enter_context(patch.object(type(server), 'action_sync_tools'))
            connection = stack.enter_context(patch.object(type(server), 'action_test_connection'))
            for role in ('owner', 'admin', 'user', 'guest'):
                self._login(role)
                for path, actions in (
                    (project_path, ('get', 'update')),
                    (project_path + '/mcp-servers', ('list', 'create', 'update', 'delete', 'sync', 'test')),
                    (service_path, ('list', 'create', 'update', 'delete', 'sync', 'test')),
                ):
                    for action in actions:
                        with self.subTest(role=role, path=path, action=action):
                            result = self._rpc(path, {
                                'action': action, 'server_id': server.id,
                                'name': 'Forbidden project edit',
                                'url': 'https://forbidden.example.invalid/mcp',
                            }, success=False)
                            self.assertNotIn('PRIVATE_', json.dumps(result))
                            self.assertNotIn('private-project.example.invalid', json.dumps(result))
            for operator in operators:
                operator.assert_not_called()
            sync.assert_not_called()
            connection.assert_not_called()
        self.assertTrue(server.exists())
        self.assertEqual(project.name, 'PRIVATE_PROJECT_' + self.suffix)
        self.assertEqual(server.name, 'PRIVATE_MCP_' + self.suffix)
        # Preserve the pre-existing legitimate internal workspace-member path.
        login = 'project-internal-%s@example.invalid' % self.suffix
        internal = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': login, 'login': login, 'password': login,
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.env['woow_paas_platform.workspace_access'].create({
            'workspace_id': self.ws.id, 'user_id': internal.id, 'role': 'admin',
        })
        self.env.flush_all()
        self.authenticate(login, login)
        self._rpc(project_path, {'action': 'get'})
        listed = self._rpc(service_path, {'action': 'list'})
        self.assertIn(server.id, [row['id'] for row in listed['data']])

    def test_internal_smart_home_and_access_still_reject_archived_workspace(self):
        login = 'internal-cap-%s@example.invalid' % self.suffix
        user = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': login, 'login': login, 'password': login,
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.env['woow_paas_platform.workspace_access'].create({
            'workspace_id': self.ws.id, 'user_id': user.id, 'role': 'admin',
        })
        self.env.flush_all()
        self.authenticate(login, login)
        for family in ('smarthomes', 'security-accesses'):
            self._rpc(self._url(family), {'action': 'list'})
        self._mark_legacy_workspace_archived()
        for family in ('smarthomes', 'security-accesses'):
            with self.subTest(family=family):
                self._rpc(self._url(family), {'action': 'list'}, success=False)

    def test_capability_preserves_trusted_locale_not_rpc_context(self):
        user = self.users['owner']
        user.tz = 'Asia/Taipei'
        caller_env = self.env(user=user.id)(context={
            'lang': 'en_US', 'tz': 'UTC', 'uid': 1,
            'default_owner_id': self.users['other'].id,
            'default_access_ids': [(0, 0, {'user_id': self.users['other'].id,
                                          'role': 'owner'})],
        })
        workspace = _PaaSCapability(caller_env).workspace(self.ws.id)
        self.assertEqual(workspace.env.uid, user.id)
        self.assertEqual(workspace.env.context.get('lang'), user.lang or 'en_US')
        self.assertEqual(workspace.env.context.get('tz'), 'Asia/Taipei')
        self.assertNotIn('uid', workspace.env.context)
        self.assertNotIn('default_owner_id', workspace.env.context)
        self.assertNotIn('default_access_ids', workspace.env.context)

    def test_catalog_hides_unlisted_templates_and_platform_defaults(self):
        self._login('owner')
        result = self._rpc('/api/cloud/templates/%s' % self.template.id)
        self.assertEqual(result['data']['helm_default_values'], {'display': 'safe'})
        self.assertNotIn('NEVER-DISCLOSE', json.dumps(result))
        self.template.is_hidden = True
        self.env.flush_all()
        self._rpc('/api/cloud/templates/%s' % self.template.id, success=False)
        self._rpc('/api/cloud-services/templates/%s/plans' % self.template.id, success=False)
        listed = self._rpc('/api/cloud/templates')
        self.assertNotIn(self.template.id, [row['id'] for row in listed['data']])
        # Hidden-only does NOT conceal already purchased instances.
        self._rpc(self._url('services', self.resources['own']['services']))
        self.template.conceal_instances = True
        self.env.flush_all()
        self._rpc(self._url('services', self.resources['own']['services']), success=False)
