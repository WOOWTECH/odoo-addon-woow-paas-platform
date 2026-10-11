"""Public provision action with a real localhost provider boundary.

Committed process/restart tests live in the audit harness; TransactionCase does
not prove durable commits and is deliberately not used for that claim.
"""
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged

from ..models.smart_home import _internal_identity
from ..models.smart_home_provision_operation import _internal_operation
from .smart_home_fixture import sh_model


@tagged('post_install', '-at_install')
class TestShcDurableProvisionPrototype(TransactionCase):

    def setUp(self):
        super().setUp()
        self.calls = []
        calls = self.calls

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, data, code=200):
                body = json.dumps(data).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                calls.append(('GET', self.path))
                if self.path == '/api/tunnels/provider-binding':
                    return self.send({'provider_account_id': 'synthetic-account'})
                self.send({'detail': 'synthetic unavailable'}, 503)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                calls.append(('POST', self.path))
                if self.path == '/api/tunnels':
                    return self.send({
                        'tunnel_id': str(uuid.uuid4()),
                        'tunnel_name': body['name'],
                        'tunnel_token': 'synthetic-only-token',
                        'dns_failed': False,
                    }, 201)
                self.send({'detail': 'unexpected mutation'}, 503)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        params = self.env['ir.config_parameter'].sudo()
        params.set_param('woow_paas_platform.shc_durable_provision_prototype_enabled', 'True')
        params.set_param('woow_paas_platform.operator_url',
                         'http://127.0.0.1:%s' % self.server.server_port)
        params.set_param('woow_paas_platform.operator_api_key', 'synthetic-only-key')
        params.set_param('woow_paas_platform.paas_domain', 'shc-prototype.invalid')
        plan = self.env['sh.subscription.plan'].create({
            'name': 'Prototype synthetic plan', 'sh_plan_price': 0,
            'sh_duration': 1, 'sh_unit': 'month', 'sh_no_of_billing_cycle': -1,
        })
        product = self.env['product.template'].create({
            'name': 'Prototype synthetic product', 'type': 'service',
            'sh_product_subscribe': True, 'sh_subscription_plan_id': plan.id,
        })
        self.actor = self.env.ref('base.user_admin')
        self.assertTrue(self.actor.active)
        ws = self.env['woow_paas_platform.workspace'].create({
            'name': 'Prototype isolated workspace', 'owner_id': self.actor.id,
        })
        sub = self.env['sh.subscription.subscription'].create({
            'sh_partner_id': self.actor.partner_id.id,
            'product_id': product.product_variant_id.id,
            'sh_subscription_plan_id': plan.id, 'sh_source': 'manual',
            'state': 'in_progress',
        })
        self.home = sh_model(self.env).create({
            'name': 'Prototype queued home', 'workspace_id': ws.id,
            'subscription_id': sub.id,
        })
        self.home = self.home.with_user(self.actor)
        self.assertTrue(self.home._is_subscription_active())

    def test_user_provision_is_accepted_without_remote_mutation(self):
        subscriptions = self.env['sh.subscription.subscription'].search_count([])
        invoices = self.env['account.move'].search_count([])
        result = self.home.action_provision()
        self.assertIsInstance(result, dict)
        self.assertTrue(result['accepted'])
        self.assertFalse(result['success'])
        self.assertEqual(self.home.state, 'provisioning')
        self.assertFalse(self.home.tunnel_id)
        self.assertFalse(any(method != 'GET' for method, path in self.calls))
        self.assertEqual(self.env['sh.subscription.subscription'].search_count([]), subscriptions)
        self.assertEqual(self.env['account.move'].search_count([]), invoices)

    def test_repeated_enqueue_reuses_immutable_nonsecret_operation(self):
        first = self.home.action_provision()
        second = self.home.action_provision()
        self.assertEqual(first['operation']['id'], second['operation']['id'])
        operation = self.env['woow_paas_platform.smart_home_provision_operation']._for_home(self.home)
        self.assertEqual(operation.phase, 'queued')
        self.assertEqual(operation.requester_id, self.actor)
        self.assertNotIn('provider_account_id', first['operation'])
        self.assertNotIn('remote_tunnel_id', first['operation'])
        self.assertNotIn('tunnel_token', first['operation'])
        self.assertFalse(any(method != 'GET' for method, path in self.calls))

    def test_journal_crud_rejects_sudo_and_json_capability_spoofing(self):
        self.home.action_provision()
        operation = self.env['woow_paas_platform.smart_home_provision_operation']._for_home(self.home)
        for value in [True, 'internal', operation.operation_uuid]:
            with self.assertRaises(AccessError):
                operation.sudo().with_context(woow_shc_provision_operation_capability=value).write({'phase': 'queued'})
        with self.assertRaises(AccessError):
            operation.sudo().write({'phase': 'queued'})
        with self.assertRaises(AccessError):
            operation.sudo().unlink()
        with self.assertRaises(AccessError):
            operation.sudo().create({'operation_uuid': str(uuid.uuid4())})

    def test_legacy_error_without_anchor_requires_confirmation(self):
        self.home.write({'state': 'error'})
        with self.assertRaises(UserError):
            self.home.action_provision()
        self.assertFalse(self.env['woow_paas_platform.smart_home_provision_operation']._for_home(self.home))
        self.assertFalse(any(method != 'GET' for method, path in self.calls))

    def test_journal_home_delete_retains_recovery_record(self):
        self.home.action_provision()
        with self.assertRaises(UserError):
            self.home.unlink()
        self.assertTrue(self.home.exists())

    def test_reusing_intent_revalidates_current_workspace_permission(self):
        portal_group = self.env.ref('base.group_portal')
        owner = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'Prototype owner', 'login': str(uuid.uuid4())+'@example.invalid',
            'groups_id': [(6, 0, [portal_group.id])],
        })
        administrator = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'Prototype administrator', 'login': str(uuid.uuid4())+'@example.invalid',
            'groups_id': [(6, 0, [portal_group.id])],
        })
        ws = self.env['woow_paas_platform.workspace'].create({'name': 'Permission fixture', 'owner_id': owner.id})
        access = self.env['woow_paas_platform.workspace_access'].create({
            'workspace_id': ws.id, 'user_id': administrator.id, 'role': 'admin',
        })
        home = sh_model(self.env).create({
            'name': 'Permission fixture', 'workspace_id': ws.id,
            'subscription_id': self.home.subscription_id.id,
        }).with_user(administrator).sudo()
        self.assertTrue(home.action_provision()['accepted'])
        access.write({'role': 'user'})
        with self.assertRaises(AccessError):
            home.action_provision()
        operation = self.env['woow_paas_platform.smart_home_provision_operation']._for_home(home)
        with self.assertRaises(AccessError):
            operation.with_user(administrator).read(['phase'])
        self.assertFalse(any(method != 'GET' for method, path in self.calls))

    def _anchored_failure(self):
        self.home.action_provision()
        operation = self.env['woow_paas_platform.smart_home_provision_operation']._for_home(self.home)
        remote_id = str(uuid.uuid4())
        _internal_operation(operation).write({'phase': 'anchored', 'remote_tunnel_id': remote_id})
        _internal_identity(self.home).write({'state': 'error', 'tunnel_id': remote_id})
        return operation, remote_id

    def test_anchored_retry_queues_persisted_home_without_new_create_or_charge(self):
        operation, remote_id = self._anchored_failure()
        invoices = self.env['account.move'].search_count([])
        subscriptions = self.env['sh.subscription.subscription'].search_count([])
        result = self.home.action_provision()
        self.assertTrue(result['accepted'])
        self.assertFalse(result['success'])
        self.assertEqual(self.home.state, 'provisioning')
        self.assertEqual(operation.phase, 'anchored')
        self.assertEqual(operation.remote_tunnel_id, remote_id)
        self.assertEqual(self.home.tunnel_id, remote_id)
        self.assertFalse(any(method != 'GET' for method, path in self.calls))
        self.assertEqual(self.env['account.move'].search_count([]), invoices)
        self.assertEqual(self.env['sh.subscription.subscription'].search_count([]), subscriptions)

    def test_disabled_worker_does_not_accept_or_requeue_anchored_retry(self):
        operation, remote_id = self._anchored_failure()
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.shc_durable_provision_prototype_enabled', 'False')
        result = self.home.action_provision()
        self.assertFalse(result['accepted'])
        self.assertFalse(result['success'])
        self.assertEqual(result['error_code'], 'provision_worker_disabled')
        self.assertEqual(self.home.state, 'error')
        self.assertEqual(operation.phase, 'anchored')
        self.assertEqual(operation.remote_tunnel_id, remote_id)
        self.assertFalse(any(method != 'GET' for method, path in self.calls))

    def test_completed_journal_returns_active_without_requeue_or_provider_call(self):
        operation, remote_id = self._anchored_failure()
        _internal_operation(operation).write({'phase': 'completed'})
        self.home.write({'state': 'active'})
        result = self.home.action_provision()
        self.assertTrue(result['success'])
        self.assertFalse(result['accepted'])
        self.assertEqual(self.home.state, 'active')
        self.assertEqual(operation.phase, 'completed')
        self.assertEqual(self.home.tunnel_id, remote_id)
        self.assertFalse(any(method != 'GET' for method, path in self.calls))
