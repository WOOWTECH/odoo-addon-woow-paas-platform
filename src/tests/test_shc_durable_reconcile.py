"""Reconcile decision, fair scheduling and retention for the durable prototype.

Committed-cursor/process behaviour (claims, kills, concurrent reconcilers,
adoption followed by worker convergence) is proven by
``scripts/audit/shc-durable-reconcile-committed.py``; TransactionCase cannot
prove independent commits and is deliberately not used for that claim.
"""
import json
import threading
import uuid
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged

from ..models.smart_home_provision_operation import (
    _ABSENCE_SETTLE,
    _BACKOFF_CAP,
    _MAX_CREATE_ATTEMPTS,
    _RECONCILE_BACKOFF_BASE,
    _RECONCILE_MAX_OBSERVATIONS,
    _backoff,
    _internal_operation,
    _parse_observation,
    _reconcile_decision,
)
from .smart_home_fixture import sh_model

_MODEL = 'woow_paas_platform.smart_home_provision_operation'
_NAME = 'paas-prod-sm-fixture-op-' + uuid.uuid4().hex
_URL = 'http://operator.invalid'
_ACCOUNT = 'synthetic-account'
_NOW = fields.Datetime.from_string('2026-10-14 08:00:00')


def _state(**overrides):
    state = {
        'tunnel_name': _NAME, 'provider_account_id': _ACCOUNT, 'operator_url': _URL,
        'deploy_env': 'prod', 'remote_tunnel_id': False, 'home_tunnel_id': False,
        'retry_count': 0, 'create_attempts': 1, 'absent_since': False,
    }
    state.update(overrides)
    return state


def _observation(tunnels=None, **overrides):
    inventory = {
        'paas_env': 'prod', 'shared_tunnel_id': 'shared', 'partial': False,
        'failed_kinds': [],
        'resources': [{'id': tid, 'name': name, 'kind': 'tunnel'} for name, tid in (tunnels or [])]
        + [{'id': 'app-1', 'name': _NAME, 'kind': 'access_app'}],
    }
    inventory.update(overrides.pop('inventory', {}))
    before = overrides.pop('before', {'provider_account_id': _ACCOUNT})
    after = overrides.pop('after', {'provider_account_id': _ACCOUNT})
    return _parse_observation(before, inventory, after, overrides.pop('operator_url', _URL))


@tagged('post_install', '-at_install')
class TestShcDurableReconcileDecision(TransactionCase):
    """Pure decision table: every branch, no I/O."""

    def decide(self, state=None, observation=None, now=_NOW):
        return _reconcile_decision(state or _state(), observation or _observation(), now)

    def test_exactly_one_live_tunnel_is_adopted(self):
        tid = str(uuid.uuid4())
        decision = self.decide(observation=_observation([(_NAME, tid), ('other', str(uuid.uuid4()))]))
        self.assertEqual(decision, {'action': 'adopt', 'tunnel_id': tid})

    def test_adoption_agrees_with_received_id(self):
        tid = str(uuid.uuid4())
        decision = self.decide(_state(remote_tunnel_id=tid), _observation([(_NAME, tid)]))
        self.assertEqual(decision['action'], 'adopt')

    def test_duplicates_are_manual_never_adopted(self):
        decision = self.decide(observation=_observation([
            (_NAME, str(uuid.uuid4())), (_NAME, str(uuid.uuid4()))]))
        self.assertEqual(decision, {'action': 'manual', 'code': 'reconcile_duplicate'})

    def test_conflicting_received_or_bound_id_is_manual(self):
        observed = _observation([(_NAME, str(uuid.uuid4()))])
        for key in ('remote_tunnel_id', 'home_tunnel_id'):
            decision = self.decide(_state(**{key: str(uuid.uuid4())}), observed)
            self.assertEqual(decision['code'], 'reconcile_identity_conflict', key)

    def test_non_uuid_provider_id_is_manual(self):
        decision = self.decide(observation=_observation([(_NAME, 'not-a-uuid')]))
        self.assertEqual(decision['code'], 'reconcile_identity_conflict')

    def test_vanished_received_id_is_manual_not_recreated(self):
        decision = self.decide(_state(remote_tunnel_id=str(uuid.uuid4())), _observation([]))
        self.assertEqual(decision, {'action': 'manual', 'code': 'reconcile_anchor_missing'})

    def test_first_absence_only_waits(self):
        decision = self.decide(observation=_observation([]))
        self.assertEqual(decision['action'], 'absent_wait')
        self.assertEqual(decision['absent_since'], _NOW)
        self.assertEqual(decision['next_attempt_at'], _NOW + _ABSENCE_SETTLE)

    def test_absence_inside_settle_window_keeps_first_observation(self):
        first = _NOW - _ABSENCE_SETTLE + timedelta(seconds=1)
        decision = self.decide(_state(absent_since=first), _observation([]))
        self.assertEqual(decision['action'], 'absent_wait')
        self.assertEqual(decision['absent_since'], first)

    def test_confirmed_absence_requeues_once(self):
        first = _NOW - _ABSENCE_SETTLE
        self.assertEqual(self.decide(_state(absent_since=first), _observation([])), {'action': 'requeue'})
        exhausted = self.decide(
            _state(absent_since=first, create_attempts=_MAX_CREATE_ATTEMPTS), _observation([]))
        self.assertEqual(exhausted, {'action': 'manual', 'code': 'reconcile_absent_after_retry'})

    def test_legacy_zero_create_attempts_counts_as_one(self):
        first = _NOW - _ABSENCE_SETTLE
        decision = self.decide(_state(absent_since=first, create_attempts=0), _observation([]))
        self.assertEqual(decision, {'action': 'requeue'})

    def test_binding_drift_is_manual(self):
        cases = [
            _observation([], after={'provider_account_id': 'other'}),
            _observation([], before={'provider_account_id': 'other'}, after={'provider_account_id': 'other'}),
            _observation([], operator_url='http://elsewhere.invalid'),
        ]
        for observation in cases:
            self.assertEqual(self.decide(observation=observation)['code'], 'reconcile_binding_changed')

    def test_unusable_observation_backs_off_then_exhausts(self):
        unusable = [
            {'ok': False, 'reason': 'provider_unavailable'},
            _observation([], inventory={'partial': True}),
            _observation([], inventory={'failed_kinds': ['tunnel']}),
            _observation([], inventory={'resources': 'oops'}),
            _observation([], inventory={'resources': ['oops']}),
            _observation([], inventory={'paas_env': None}),
            _observation([], before={}),
            _observation([], after=None),
        ]
        for observation in unusable:
            decision = self.decide(observation=observation)
            self.assertEqual(decision['action'], 'backoff', observation)
            self.assertEqual(decision['retry_count'], 1)
            self.assertEqual(decision['next_attempt_at'], _NOW + _RECONCILE_BACKOFF_BASE)
        last = self.decide(_state(retry_count=_RECONCILE_MAX_OBSERVATIONS - 1),
                           {'ok': False, 'reason': 'provider_unavailable'})
        self.assertEqual(last['action'], 'manual')
        self.assertEqual(last['code'], 'reconcile_exhausted')

    def test_environment_mismatch_backs_off(self):
        decision = self.decide(observation=_observation(
            [(_NAME, str(uuid.uuid4()))], inventory={'paas_env': 'stg'}))
        self.assertEqual(decision['action'], 'backoff')
        self.assertEqual(decision['reason'], 'environment_mismatch')

    def test_partial_list_never_concludes_presence(self):
        decision = self.decide(observation=_observation(
            [(_NAME, str(uuid.uuid4()))], inventory={'partial': True}))
        self.assertEqual(decision['action'], 'backoff')

    def test_backoff_is_monotonic_and_capped(self):
        delays = [_backoff(_RECONCILE_BACKOFF_BASE, n) for n in range(1, 40)]
        self.assertEqual(delays, sorted(delays))
        self.assertEqual(delays[0], _RECONCILE_BACKOFF_BASE)
        self.assertEqual(delays[-1], _BACKOFF_CAP)


@tagged('post_install', '-at_install')
class TestShcDurableReconcileScheduling(TransactionCase):
    """Selection, flag gating and retention inside one rolled-back transaction."""

    def setUp(self):
        super().setUp()
        self.calls = []
        calls = self.calls

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                calls.append(('GET', self.path))
                body = json.dumps({'detail': 'synthetic unavailable'}).encode()
                self.send_response(503)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        params = self.env['ir.config_parameter'].sudo()
        params.set_param('woow_paas_platform.operator_url',
                         'http://127.0.0.1:%s' % self.server.server_port)
        params.set_param('woow_paas_platform.operator_api_key', 'synthetic-only-key')
        params.set_param('woow_paas_platform.paas_domain', 'shc-prototype.invalid')
        self.actor = self.env.ref('base.user_admin')
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'Reconcile isolated workspace', 'owner_id': self.actor.id,
        })
        plan = self.env['sh.subscription.plan'].create({
            'name': 'Reconcile synthetic plan', 'sh_plan_price': 0,
            'sh_duration': 1, 'sh_unit': 'month', 'sh_no_of_billing_cycle': -1,
        })
        self.product = self.env['product.template'].create({
            'name': 'Reconcile synthetic product', 'type': 'service',
            'sh_product_subscribe': True, 'sh_subscription_plan_id': plan.id,
        })
        self.plan = plan
        self.Operation = self.env[_MODEL]

    def _operation(self, phase, error_code=False, **values):
        sub = self.env['sh.subscription.subscription'].create({
            'sh_partner_id': self.actor.partner_id.id,
            'product_id': self.product.product_variant_id.id,
            'sh_subscription_plan_id': self.plan.id, 'sh_source': 'manual',
            'state': 'in_progress',
        })
        home = sh_model(self.env).create({
            'name': 'Reconcile home %s' % uuid.uuid4().hex[:8],
            'workspace_id': self.workspace.id, 'subscription_id': sub.id,
        })
        op_uuid = str(uuid.uuid4())
        return _internal_operation(self.Operation).create({
            'operation_uuid': op_uuid, 'home_id': home.id,
            'workspace_id': self.workspace.id, 'requester_id': self.actor.id,
            'subscription_id': sub.id, 'operator_url': _URL,
            'provider_account_id': _ACCOUNT, 'paas_domain': 'shc-prototype.invalid',
            'deploy_env': 'prod', 'tunnel_name': 'fixture-op-' + uuid.UUID(op_uuid).hex,
            'subdomain': 'fixture-' + uuid.UUID(op_uuid).hex[:12], 'service_port': 8123,
            'phase': phase, 'error_code': error_code, **values,
        })

    def _set_flags(self, prototype, reconcile):
        params = self.env['ir.config_parameter'].sudo()
        params.set_param('woow_paas_platform.shc_durable_provision_prototype_enabled', str(prototype))
        params.set_param('woow_paas_platform.shc_durable_provision_reconcile_enabled', str(reconcile))

    def test_backed_off_failures_cannot_starve_new_intent(self):
        now = fields.Datetime.now()
        failing = self.Operation.browse()
        for _index in range(12):
            failing |= self._operation(
                'anchored', 'configuration_failed', retry_count=3,
                next_attempt_at=now - timedelta(seconds=1))
        fresh = self._operation('queued')
        selected = self.Operation._worker_candidates(limit=10)
        self.assertIn(fresh, selected)
        self.assertEqual(selected[0], fresh)
        backed_off = self._operation(
            'anchored', 'configuration_failed', retry_count=1,
            next_attempt_at=now + timedelta(minutes=5))
        self.assertNotIn(backed_off, self.Operation._worker_candidates(limit=100))
        self.assertTrue(failing)

    def test_reconcile_candidates_only_due_unknown_outcomes(self):
        now = fields.Datetime.now()
        due = self._operation('recovery_required', 'create_outcome_unknown')
        interrupted = self._operation('recovery_required', 'create_worker_interrupted',
                                      next_attempt_at=now - timedelta(seconds=1))
        anchor = self._operation('recovery_required', 'anchor_commit_failed')
        claimed = self._operation('recovery_required', 'create_outcome_unknown',
                                  next_attempt_at=now + timedelta(minutes=1))
        manual = [
            self._operation('recovery_required', code)
            for code in ('intent_preflight_invalid', 'anchor_mismatch', 'reconcile_duplicate',
                         'reconcile_exhausted', 'reconcile_authority_invalid')
        ]
        selected = self.Operation._reconcile_candidates(limit=100)
        self.assertEqual(set(selected.ids), {due.id, interrupted.id, anchor.id})
        self.assertNotIn(claimed, selected)
        self.assertFalse(set(selected.ids) & {op.id for op in manual})

    def test_reconcile_and_worker_off_by_default_without_provider_calls(self):
        self._operation('recovery_required', 'create_outcome_unknown')
        params = self.env['ir.config_parameter'].sudo()
        self.assertFalse(params.get_param('woow_paas_platform.shc_durable_provision_reconcile_enabled'))
        self.assertEqual(self.Operation._reconcile_batch(), {})
        self._set_flags(True, False)
        self.assertEqual(self.Operation._reconcile_batch(), {})
        self._set_flags(False, True)
        self.assertEqual(self.Operation._reconcile_batch(), {})
        self.assertEqual(self.calls, [])

    def test_retention_purges_only_old_completed_rows(self):
        old_completed = self._operation('completed')
        new_completed = self._operation('completed')
        old_manual = self._operation('recovery_required', 'reconcile_duplicate')
        old_anchored = self._operation('anchored', 'configuration_failed')
        self.env.flush_all()
        self.env.cr.execute(
            'UPDATE woow_paas_platform_smart_home_provision_operation '
            "SET write_date = now() at time zone 'UTC' - interval '31 days' WHERE id IN %s",
            [(old_completed.id, old_manual.id, old_anchored.id)])
        self.env.invalidate_all()
        self.assertEqual(self.Operation._purge_completed(), 1)
        self.assertFalse(old_completed.exists())
        self.assertTrue(new_completed.exists())
        self.assertTrue(old_manual.exists())
        self.assertTrue(old_anchored.exists())

    def test_retention_window_is_configurable_and_can_be_disabled(self):
        completed = self._operation('completed')
        self.env.flush_all()
        self.env.cr.execute(
            'UPDATE woow_paas_platform_smart_home_provision_operation '
            "SET write_date = now() at time zone 'UTC' - interval '3 days' WHERE id = %s",
            [completed.id])
        self.env.invalidate_all()
        params = self.env['ir.config_parameter'].sudo()
        params.set_param('woow_paas_platform.shc_durable_provision_retention_days', '0')
        self.assertEqual(self.Operation._purge_completed(), 0)
        params.set_param('woow_paas_platform.shc_durable_provision_retention_days', 'junk')
        self.assertEqual(self.Operation._purge_completed(), 0)
        params.set_param('woow_paas_platform.shc_durable_provision_retention_days', '2')
        self.assertEqual(self.Operation._purge_completed(), 1)
        self.assertFalse(completed.exists())

    def test_unlink_requires_capability_and_completed_phase(self):
        completed = self._operation('completed')
        queued = self._operation('queued')
        for records in (completed.sudo(), completed.sudo().with_context(
                woow_shc_provision_operation_capability=True)):
            try:
                records.unlink()
            except AccessError:
                pass
            else:
                self.fail('journal unlink without the private capability must be refused')
        try:
            _internal_operation(queued).unlink()
        except AccessError:
            pass
        else:
            self.fail('unfinished journal must be retained')
        self.assertTrue(queued.exists())
        _internal_operation(completed).unlink()
        self.assertFalse(completed.exists())

    def test_home_delete_blocked_by_unfinished_and_allowed_when_completed(self):
        blocked = self._operation('recovery_required', 'reconcile_duplicate')
        home = blocked.home_id
        try:
            home.unlink()
        except UserError:
            pass
        else:
            self.fail('manual-review journal must block home deletion')
        self.assertTrue(home.exists())
        self.assertTrue(blocked.exists())
        done = self._operation('completed')
        done_home = done.home_id
        done_home.unlink()
        self.assertFalse(done_home.exists())
        self.assertFalse(done.exists())

    def test_new_config_parameters_are_registered_for_uninstall(self):
        from ..hooks import _CONFIG_PARAMETERS
        for key in ('woow_paas_platform.shc_durable_provision_reconcile_enabled',
                    'woow_paas_platform.shc_durable_provision_retention_days'):
            self.assertIn(key, _CONFIG_PARAMETERS)
