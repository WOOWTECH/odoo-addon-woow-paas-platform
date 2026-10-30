"""Real HTTP acceptance for HA service-first onboarding and trial-only billing."""
import json
import uuid
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests.common import HttpCase, tagged

from .smart_home_fixture import open_gates
from .wallet_fixture import fund_wallet


@tagged('post_install', '-at_install', 'ha_onboarding')
class TestHAOnboarding(HttpCase):
    def setUp(self):
        super().setUp()
        suffix = uuid.uuid4().hex[:8]
        self.user = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'HA acceptance', 'login': 'ha-onboard-' + suffix,
            'email': 'ha-onboard-' + suffix + '@example.test',
            'password': 'Acceptance-only-7day!',
            'groups_id': [(6, 0, [self.env.ref('base.group_portal').id])],
        })
        self.outsider = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'HA onboarding outsider', 'login': 'ha-outsider-' + suffix,
            'email': 'ha-outsider-' + suffix + '@example.test',
            'password': 'Outsider-only-7day!',
            'groups_id': [(6, 0, [self.env.ref('base.group_portal').id])],
        })
        self.client = self.env.ref('woow_paas_platform.oauth_client_ha_smart_home')
        self.scope = 'workspace:read smarthome:read smarthome:tunnel smarthome:provision'
        self.token = self._token(self.scope)
        self.Home = self.env['woow_paas_platform.smart_home'].sudo()
        self.Workspace = self.env['woow_paas_platform.workspace'].sudo()

    def _token(self, scope, user=None):
        user = user or self.user
        return self.env['woow_paas_platform.oauth_token'].sudo().create({
            'access_token': uuid.uuid4().hex, 'scope': scope,
            'expires_at': fields.Datetime.now() + timedelta(hours=1),
            'refresh_expires_at': fields.Datetime.now() + timedelta(days=1),
            'client_id': self.client.id, 'user_id': user.id,
        }).access_token

    def _post(self, payload, path='/api/smarthome/onboarding', token=None):
        return self.url_open(path, data=json.dumps(payload), headers={
            'Authorization': 'Bearer ' + (token or self.token),
            'Content-Type': 'application/json',
        })

    def _options(self, kind='remote_access', workspace=None):
        path = '/api/smarthome/onboarding/options?service_kind=' + kind
        if workspace:
            path += '&workspace_id=' + str(workspace.id)
        return self.url_open(path, headers={'Authorization': 'Bearer ' + self.token})

    def _create(self, kind='remote_access', workspace=None):
        payload = {'service_kind': kind}
        if workspace:
            payload['workspace_id'] = workspace.id
        response = self._post(payload)
        self.assertEqual(response.status_code, 202, response.text)
        data = response.json()
        self.assertTrue(data['created'])
        home = self.Home.browse(data['homes'][0]['id'])
        home.invalidate_recordset()
        return home

    def test_new_user_gets_workspace_and_seven_day_trial(self):
        response = self._options()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['workspaces'], [])
        self.assertFalse(response.json()['has_memberships'])
        home = self._create()
        self.assertEqual(home.workspace_id.owner_id, self.user)
        self.assertEqual(home.workspace_id.name, '我的智慧家庭')
        self.assertEqual(home.ha_port, 8123)
        sub = home.subscription_id
        self.assertTrue(sub.woow_trial_only)
        self.assertTrue(sub.sh_trial)
        self.assertEqual(sub.sh_trial_end_date, fields.Date.today() + timedelta(days=6))
        self.assertEqual(sub.sh_start_date, fields.Date.today() + timedelta(days=7))
        self.assertFalse(sub.sh_date_of_next_payment)
        self.assertEqual(sub.state, 'in_progress')
        self.assertTrue(home.ha_setup_requested)
        self.assertEqual(home.state, 'pending')
        self.assertFalse(home.tunnel_id)
        self.assertTrue(home._get_subscription_info()['requires_paid_activation'])

    def test_mcp_choice_uses_mcp_plan(self):
        home = self._create('remote_access_mcp')
        self.assertTrue(home.subscription_id.sh_subscription_plan_id.mcp_integration)
        self.assertEqual(self._options('remote_access', home.workspace_id).json()['homes'], [])
        self.assertEqual(self._options('remote_access_mcp', home.workspace_id).json()['homes'][0]['id'], home.id)

    def test_options_say_whether_a_service_is_already_used(self):
        """HA issue #37: the component asks before taking over a service in use."""
        home = self._create()
        item = self._options('remote_access', home.workspace_id).json()['homes'][0]
        self.assertEqual(item['binding_state'], 'unbound')
        self.assertEqual((item['connector_count'], item['binding_conflict']), (0, False))
        home._tunnel_binding_claim('a' * 32, 'Living room HA')
        home.write({'connector_count': 2, 'binding_conflict': True})
        item = self._options('remote_access', home.workspace_id).json()['homes'][0]
        self.assertEqual(item['binding_state'], 'claimed')
        self.assertEqual(item['bound_instance_label'], 'Living room HA')
        self.assertEqual((item['connector_count'], item['binding_conflict']), (2, True))

    def test_options_connector_count_comes_from_the_live_tunnel(self):
        """An unbound service can still have a connector (released, not disconnected)."""
        home = self._create()
        open_gates(home).write({'tunnel_id': 'tun-onboard-live'})
        home.write({'connector_count': 2, 'binding_conflict': True})  # stale stored reading
        live = {'connector_count': 1, 'binding_conflict': False, 'tunnel_status': 'connected'}
        with patch.object(type(home), '_live_tunnel_status', return_value=live):
            item = self._options('remote_access', home.workspace_id).json()['homes'][0]
        self.assertEqual((item['connector_count'], item['binding_conflict']), (1, False))

    def test_options_keep_the_stored_reading_when_the_operator_is_silent(self):
        home = self._create()
        open_gates(home).write({'tunnel_id': 'tun-onboard-silent'})
        home.write({'connector_count': 2, 'binding_conflict': True})
        with patch.object(type(home), '_live_tunnel_status', return_value={}):
            response = self._options('remote_access', home.workspace_id)
        self.assertEqual(response.status_code, 200, response.text)
        item = response.json()['homes'][0]
        self.assertEqual((item['connector_count'], item['binding_conflict']), (2, True))
        with patch.object(type(home), '_live_tunnel_status', side_effect=RuntimeError('boom')), \
                self.assertLogs('odoo.addons.woow_paas_platform.controllers.ha_onboarding', level=40):
            response = self._options('remote_access', home.workspace_id)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['homes'][0]['connector_count'], 2)

    def test_new_service_uses_ha_port_without_reconfiguring_existing_service(self):
        response = self._post({'service_kind': 'remote_access', 'ha_port': 18243})
        self.assertEqual(response.status_code, 202, response.text)
        home = self.Home.browse(response.json()['homes'][0]['id'])
        self.assertEqual(home.ha_port, 18243)
        response = self._post({'service_kind': 'remote_access',
                               'workspace_id': home.workspace_id.id, 'ha_port': 80})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()['created'])
        home.invalidate_recordset()
        self.assertEqual(home.ha_port, 18243)

    def test_invalid_ha_ports_leave_no_workspace(self):
        for port in [None, False, 0, -1, 65536, '8123', 3.2, []]:
            response = self._post({'service_kind': 'remote_access', 'ha_port': port})
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(response.json()['error'], 'invalid_ha_port')
        self.assertFalse(self.Workspace.search([('owner_id', '=', self.user.id)]))

    def test_existing_workspace_gets_only_missing_service(self):
        ws = self.Workspace.create({'name': 'existing', 'owner_id': self.user.id})
        home = self._create(workspace=ws)
        other = self._create('remote_access_mcp', ws)
        self.assertNotEqual(home.id, other.id)
        self.assertEqual(home.workspace_id, ws)
        self.assertEqual(other.workspace_id, ws)
        self.assertEqual(self.Workspace.search_count([('owner_id', '=', self.user.id)]), 1)

    def test_repeated_request_reuses_matching_service(self):
        home = self._create()
        response = self._post({'service_kind': 'remote_access', 'workspace_id': home.workspace_id.id})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()['created'])
        self.assertEqual([r['id'] for r in response.json()['homes']], [home.id])
        self.assertEqual(self.Home.search_count([('workspace_id', '=', home.workspace_id.id)]), 1)

    def test_existing_membership_requires_explicit_workspace(self):
        ws = self.Workspace.create({'name': 'existing', 'owner_id': self.user.id})
        response = self._post({'service_kind': 'remote_access'})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error'], 'select_workspace')
        self.assertFalse(self.Home.search([('workspace_id', '=', ws.id)]))

    def test_multiple_workspaces_are_returned_without_auto_creation(self):
        workspaces = self.Workspace.create([
            {'name': '甲', 'owner_id': self.user.id},
            {'name': '乙', 'owner_id': self.user.id},
        ])
        response = self._options()
        self.assertEqual({r['id'] for r in response.json()['workspaces']}, set(workspaces.ids))
        self.assertFalse(self.Home.search([('workspace_id', 'in', workspaces.ids)]))

    def test_read_scope_cannot_provision(self):
        response = self._post({'service_kind': 'remote_access'}, token=self._token('workspace:read smarthome:read'))
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.Workspace.search([('owner_id', '=', self.user.id)]))

    def test_non_member_cannot_provision_in_foreign_workspace(self):
        foreign = self.Workspace.create({'name': 'foreign', 'owner_id': self.env.ref('base.user_admin').id})
        response = self._post({'service_kind': 'remote_access', 'workspace_id': foreign.id})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.Home.search([('workspace_id', '=', foreign.id)]))

    def test_non_member_cannot_reach_onboarding_or_trial_activation_paths(self):
        """Every sudoed onboarding path must reject before operating on its record."""
        home = self._create()
        outsider_token = self._token(self.scope, self.outsider)
        headers = {'Authorization': 'Bearer ' + outsider_token}

        response = self._post(
            {}, f'/api/smarthome/onboarding/homes/{home.id}/prepare', outsider_token,
        )
        self.assertEqual(response.status_code, 403, response.text)

        response = self.url_open(
            '/api/smarthome/onboarding/options?service_kind=remote_access'
            f'&workspace_id={home.workspace_id.id}', headers=headers,
        )
        self.assertEqual(response.status_code, 403, response.text)

        response = self.url_open(
            f'/api/smarthome/homes/{home.id}/tunnel-token', headers=headers,
        )
        self.assertEqual(response.status_code, 403, response.text)

        self.authenticate(self.outsider.login, 'Outsider-only-7day!')
        response = self.url_open(
            f'/api/workspaces/{home.workspace_id.id}/smarthomes/{home.id}/change-plan',
            data=json.dumps({
                'jsonrpc': '2.0', 'method': 'call', 'id': 1,
                'params': {
                    'plan_id': home.subscription_id.sh_subscription_plan_id.id,
                    'dry_run': True,
                },
            }),
            headers={'Content-Type': 'application/json'},
        )
        self.assertNotIn('error', response.json(), response.text)
        self.assertFalse(response.json()['result']['success'], response.text)
        home.invalidate_recordset()
        self.assertTrue(home.subscription_id.woow_trial_only)

    def test_invalid_payloads_leave_no_workspace(self):
        for payload in [[], None, {'service_kind': []}, {'service_kind': 'security_access'},
                        {'service_kind': 'remote_access', 'workspace_id': True},
                        {'service_kind': 'remote_access', 'workspace_id': 2**40}]:
            response = self._post(payload)
            self.assertEqual(response.status_code, 400, response.text)
        self.assertFalse(self.Workspace.search([('owner_id', '=', self.user.id)]))

    def test_trial_cannot_invoice_or_skip_to_payment(self):
        home = self._create()
        with self.assertRaises(UserError):
            home.subscription_id._generate_invoice_vals()
        with self.assertRaises(UserError):
            home._skip_trial()
        home.subscription_id._process_subscription_billing(home.subscription_id, 0)
        self.assertFalse(home.subscription_id.sh_date_of_next_payment)

    def test_expired_trial_pauses_without_invoice_or_automatic_resume(self):
        home = self._create('remote_access_mcp')
        sub = home.subscription_id
        sub.write({'sh_trial_end_date': fields.Date.today() - timedelta(days=1)})
        lifecycle = self.env['woow_paas_platform.subscription_lifecycle_cron']
        lifecycle._enforce_single(home, 14, 6, fields.Date.today())
        self.assertEqual(home.state, 'suspended')
        self.assertEqual(sub.state, 'close')
        self.assertFalse(home._get_mcp_integration())
        self.assertFalse(sub.sh_date_of_next_payment)
        self.assertTrue(home._get_subscription_info()['trial_expired'])
        with self.assertRaises(UserError):
            home._check_operation_allowed()
        lifecycle._enforce_single(home, 14, 6, fields.Date.today(), resume_only=True)
        self.assertEqual(home.state, 'suspended')
        response = self._post({}, f'/api/smarthome/onboarding/homes/{home.id}/prepare')
        self.assertEqual(response.status_code, 409, response.text)

    def test_expired_trial_cannot_get_a_tunnel_token(self):
        home = self._create()
        home.subscription_id.sh_trial_end_date = fields.Date.today() - timedelta(days=1)
        self.env['woow_paas_platform.subscription_lifecycle_cron']._enforce_single(
            home, 14, 6, fields.Date.today())
        response = self.url_open(
            f'/api/smarthome/homes/{home.id}/tunnel-token',
            headers={'Authorization': 'Bearer ' + self.token},
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()['error'], 'trial_expired')

    def test_trial_setup_failure_rolls_back_workspace(self):
        # Factory failure must roll back the workspace written in the same request.
        with patch('odoo.addons.woow_paas_platform.controllers.smart_home.SmartHomeController._create_smarthome',
                   return_value={'success': False, 'error': 'acceptance failure'}):
            response = self._post({'service_kind': 'remote_access'})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.Workspace.search([('owner_id', '=', self.user.id)]))

    def test_existing_oauth_client_is_healed_with_write_scope(self):
        self.assertIn('smarthome:provision', self.client.scopes.split())

    def _activate_same_plan(self, home):
        old_sub = home.subscription_id
        fund_wallet(self.user.partner_id, 1000)
        self.authenticate(self.user.login, 'Acceptance-only-7day!')
        path = f'/api/workspaces/{home.workspace_id.id}/smarthomes/{home.id}/change-plan'
        payload = {'jsonrpc': '2.0', 'method': 'call', 'id': 1,
                   'params': {'plan_id': old_sub.sh_subscription_plan_id.id, 'dry_run': True}}
        response = self.url_open(path, data=json.dumps(payload), headers={'Content-Type': 'application/json'})
        self.assertNotIn('error', response.json(), response.text)
        self.assertTrue(response.json()['result']['success'], response.text)
        home.invalidate_recordset()
        self.assertEqual(home.subscription_id, old_sub, 'quote must not activate payment')
        payload['params']['dry_run'] = False
        response = self.url_open(path, data=json.dumps(payload), headers={'Content-Type': 'application/json'})
        self.assertNotIn('error', response.json(), response.text)
        self.assertTrue(response.json()['result']['success'], response.text)
        home.invalidate_recordset()
        old_sub.invalidate_recordset()
        self.assertEqual(old_sub.state, 'close')
        self.assertNotEqual(home.subscription_id, old_sub)
        self.assertFalse(home.subscription_id.woow_trial_only)
        self.assertFalse(home.subscription_id.sh_trial)
        self.assertEqual(response.json()['result']['data']['refund_amount'], 0)

    def test_active_trial_can_explicitly_activate_same_paid_plan(self):
        self._activate_same_plan(self._create())

    def test_expired_trial_can_explicitly_activate_same_paid_plan(self):
        home = self._create()
        home.subscription_id.sh_trial_end_date = fields.Date.today() - timedelta(days=1)
        self.env['woow_paas_platform.subscription_lifecycle_cron']._enforce_single(
            home, 14, 6, fields.Date.today())
        self.assertEqual(home.state, 'suspended')
        self._activate_same_plan(home)

    def test_login_page_is_chinese_branded_and_keeps_native_form(self):
        response = self.url_open('/web/login?redirect=%2Foauth2%2Fauthorize')
        self.assertEqual(response.status_code, 200, response.text[:400])
        self.assertIn('o_woow_connect_login', response.text)
        self.assertIn('<title>登入並連結智慧平台｜woowtech</title>', response.text)
        self.assertIn('woowtech', response.text)
        self.assertIn('name="csrf_token"', response.text)
        self.assertIn('name="login"', response.text)
        self.assertIn('name="password"', response.text)

    def test_signup_and_password_reset_keep_connect_brand(self):
        for path, heading in [('/web/signup', '建立 woowtech 帳號'),
                              ('/web/reset_password', '重設帳號密碼')]:
            response = self.url_open(path + '?redirect=%2Foauth2%2Fauthorize')
            self.assertEqual(response.status_code, 200)
            self.assertIn('o_woow_connect_login', response.text)
            self.assertIn('<title>' + heading + '｜woowtech</title>', response.text)
