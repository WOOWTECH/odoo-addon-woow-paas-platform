"""LLM-13 named-operator, suspension/restoration and user-deactivation contracts."""
import json
import os
import uuid
from datetime import timedelta
from unittest.mock import MagicMock, patch

from odoo import SUPERUSER_ID, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests.common import TransactionCase, new_test_user, tagged
from psycopg2.errors import SerializationFailure

from ..models import llm_account_lifecycle as lifecycle
from ..models import llm_notifications as notifications
from ..models.llm_common import LlmCronTick, LlmError, _llm_internal
from ..services import newapi_client as nac
from .llm_fake_newapi import FakeNewApi
from .llm_test_support import (
    ACCOUNT,
    EVENT,
    KEY,
    CommittedLlmFixture,
    env_for,
    llm_env,
    reset_llm_config,
)
from .test_llm_platform_foundation import _ConfiguredCase

NOTICE = 'woow_paas_platform.llm_notification'


@tagged('post_install', '-at_install', 'llm_lifecycle')
class TestLlmAccountLifecycle(_ConfiguredCase):

    def setUp(self):
        super().setUp()
        suffix = uuid.uuid4().hex[:10]
        self.owner = new_test_user(
            self.env, login='lifecycle_owner_' + suffix, groups='base.group_portal')
        self.operator = new_test_user(
            self.env, login='lifecycle_operator_' + suffix,
            groups='base.group_system,base.group_user', email='operator@example.test')
        self.operator._llm_set_named_human_eligible(True)
        self.account = _llm_internal(self.env[ACCOUNT].sudo()).create({
            'partner_id': self.owner.partner_id.id, 'state': 'active',
            'newapi_user_id': 987680, 'quota_cached': 100000,
        })
        _llm_internal(self.account).write({'newapi_username': f'wqa{self.account.id:010d}'})
        self.now = fields.Datetime.now() + timedelta(minutes=2)
        self.fake = FakeNewApi()
        self.fake.users[987680] = {'id': 987680, 'status': 1}
        self.fake.pats['test-up'] = {'user_id': 987680, 'expires_at': None}
        self.client = self.fake.client(nac.ROLE_CRON)
        cls = type(self.account)
        for target, name, value in (
            (cls, '_llm_tenant_ip', '198.18.0.1'),
            (cls, '_llm_up_token', 'test-up'), (cls, '_llm_client', self.client),
            (notifications, '_utcnow', self.now), (lifecycle, '_utcnow', self.now),
        ):
            mock = patch.object(target, name, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)
        self.client_factory = cls._llm_client
        self.Notice = self.env[NOTICE]

    def action(self, name):
        return getattr(self.account.with_user(self.operator), name)()

    def key(self, state='active', reason=False, **extra):
        key = _llm_internal(self.env[KEY].sudo()).create({
            'account_id': self.account.id, 'kind': 'paid', 'state': state,
            'disabled_by': reason,
            **extra,
        })
        _llm_internal(key).write({'newapi_name': f'wp_{key.id}_{uuid.uuid4().hex[:4]}'})
        result = self.client.user_create_token(
            '198.18.0.1', 'test-up', nac.free_trial_token_body(key.newapi_name, 1900000000))
        self.assertTrue(result.ok)
        token_id = max(self.fake.tokens)
        _llm_internal(key).write({'newapi_token_id': token_id})
        self.fake.tokens[token_id]['status'] = 1 if state == 'active' else 2
        return key

    def tick(self):
        before = len(self.fake.calls)
        tick = LlmCronTick(30, 1)
        self.Notice._llm_not_me_disable_tick({}, tick, account_ids=[self.account.id])
        self.assertLessEqual(len(self.fake.calls) - before, 1)
        self.assertLessEqual(tick.done, 1)
        self.env.flush_all()
        self.env.invalidate_all()

    def quarantine(self):
        for _ in range(100):
            keys = self.account.api_key_ids.filtered(lambda k: k.state not in ('deleted', 'lost'))
            gen = self.account.suspend_generation or self.account.not_me_notice_id
            if all(k.state not in ('active', 'creating') and k.not_me_disabled_notice_id == gen
                   and (k.not_me_disable_plan or {}).get('complete') for k in keys):
                return
            self.tick()
        self.fail('quarantine did not converge')

    def restore(self):
        for _ in range(100):
            if not self.account.resume_pending:
                return
            self.tick()
        self.fail('restoration did not converge')

    def events(self, action=None):
        domain = [('account_id', '=', self.account.id)]
        if action:
            domain.append(('action', '=', action))
        return self.env[EVENT].sudo().search(domain, order='id')

    def test_named_actor_required_for_every_public_action_even_with_sudo_context(self):
        ordinary_system = new_test_user(
            self.env, login='lifecycle_service_' + uuid.uuid4().hex,
            groups='base.group_system', email='service@example.test')
        accountant = new_test_user(
            self.env, login='lifecycle_accountant_' + uuid.uuid4().hex,
            groups='account.group_account_manager')
        actors = [self.owner, ordinary_system, accountant, self.env.ref('base.user_admin'),
                  self.env.ref('base.user_root'), self.env.ref('base.public_user')]
        for actor in actors:
            for action in ('action_llm_suspend_ops', 'action_llm_suspend_policy', 'action_llm_resume'):
                for sudo in (False, True):
                    record = self.account.with_user(actor).with_context(
                        woow_llm_named_human_provision=True, llm_internal=True)
                    if sudo:
                        record = record.sudo()
                    with self.subTest(uid=actor.id, action=action, sudo=sudo), self.assertRaises(AccessError):
                        getattr(record, action)()
        self.assertEqual(self.account.state, 'active')
        self.assertFalse(self.events())
        self.assertFalse(self.fake.calls)

    def test_suspend_is_local_idempotent_audited_and_blocks_new_work(self):
        self.action('action_llm_suspend_ops')
        generation = self.account.suspend_generation
        self.action('action_llm_suspend_ops')
        self.assertEqual((self.account.state, self.account.suspend_reason), ('suspended', 'ops'))
        self.assertEqual(self.account.suspend_generation, generation)
        events = self.events('suspend')
        self.assertEqual(len(events), 1)
        self.assertEqual(events.uid, self.operator)
        self.assertEqual(json.loads(events.details_json),
                         {'reason': 'llm_suspend_ops', 'count': generation})
        for call in (
            lambda: self.account._llm_require_active(),
            lambda: self.env[KEY]._llm_create_free_trial_key(
                self.owner.partner_id, uuid.uuid4().hex, self.owner, ''),
            lambda: self.env[KEY]._llm_set_key_status(self.owner.partner_id, 99999, True),
            lambda: self.env['woow_paas_platform.llm_credit_order']._llm_convert(
                self.owner.partner_id, 100, uuid.uuid4().hex, True, True, self.owner, {}, ''),
        ):
            with self.assertRaises(LlmError) as caught:
                call()
            self.assertEqual(caught.exception.code, 'llm_suspended')
        self.assertFalse(self.fake.calls)

    def test_mixed_keys_only_suspend_disabled_keys_are_restored(self):
        active = self.key()
        restricted = [self.key('disabled', reason) for reason in
                      ('user', 'ops', 'overdraft', 'free_daily_cap', 'dr_reconcile')]
        expired = self.key(expires_at=self.now - timedelta(days=1))
        exhausted = self.key(quota_limit_points=1, used_quota_cached=15625)
        failed = self.key('failed_reveal')
        self.action('action_llm_suspend_policy')
        self.quarantine()
        self.assertTrue(all(t['status'] == 2 for t in self.fake.tokens.values()))
        self.assertEqual(active.disabled_by, 'suspend')
        self.assertEqual([k.disabled_by for k in restricted],
                         ['user', 'ops', 'overdraft', 'free_daily_cap', 'dr_reconcile'])
        self.action('action_llm_resume')
        self.assertEqual(self.account.state, 'suspended')
        self.restore()
        self.assertEqual(self.account.state, 'active')
        self.assertEqual((active.state, active.disabled_by), ('active', False))
        self.assertEqual(self.fake.tokens[active.newapi_token_id]['status'], 1)
        for key in [*restricted, expired, exhausted, failed]:
            self.assertNotEqual(key.state, 'active')
            self.assertEqual(self.fake.tokens[key.newapi_token_id]['status'], 2)
        self.assertEqual(self.events('resume').mapped('result'), ['started', 'ok'])
        self.assertTrue(all(e.uid == self.operator for e in self.events()))
        self.assertNotIn('user_reveal_token', [name for name, _ip in self.fake.calls])
        self.assertTrue(all(call.args[0] == nac.ROLE_CRON for call in self.client_factory.call_args_list))

    def test_resume_waits_for_every_receipt_including_delayed_and_creating_keys(self):
        key = self.key()
        self.action('action_llm_suspend_ops')
        _llm_internal(key).write({'not_me_disable_next_at': self.now + timedelta(hours=1),
                                  'not_me_attempt_notice_id': self.account.suspend_generation})
        self.fake.calls.clear()
        with self.assertRaises(UserError):
            self.action('action_llm_resume')
        self.assertFalse(self.account.resume_pending)
        self.assertFalse(self.fake.calls)
        _llm_internal(key).write({'not_me_disable_next_at': False})
        self.quarantine()
        self.key('creating')
        with self.assertRaises(UserError):
            self.action('action_llm_resume')

    def test_user_deactivation_suspends_same_transaction_without_auto_restore(self):
        key = self.key()
        self.owner.write({'active': False})
        self.assertEqual((self.account.state, self.account.suspend_reason),
                         ('suspended', 'account_inactive'))
        self.assertEqual(self.events('suspend').uid.id, self.env.uid)
        self.quarantine()
        self.assertEqual(self.fake.tokens[key.newapi_token_id]['status'], 2)
        with self.assertRaises(UserError):
            self.action('action_llm_resume')
        gen = self.account.suspend_generation
        self.owner.write({'active': False})
        self.assertEqual(self.account.suspend_generation, gen)
        self.owner.write({'active': True})
        self.assertEqual(self.account.state, 'suspended')
        self.assertFalse(self.account.resume_pending)
        self.action('action_llm_resume')
        self.restore()
        self.assertEqual(self.account.state, 'active')

    def test_deactivation_rolls_back_with_its_suspension_and_audit(self):
        try:
            with self.env.cr.savepoint():
                self.owner.write({'active': False})
                self.assertEqual(self.account.state, 'suspended')
                self.assertTrue(self.events('suspend'))
                raise RuntimeError('abort enclosing request')
        except RuntimeError:
            pass
        self.assertTrue(self.owner.active)
        self.assertEqual(self.account.state, 'active')
        self.assertFalse(self.events())

    def test_policy_change_starts_new_generation_but_closed_is_never_reopened(self):
        self.action('action_llm_suspend_ops')
        gen = self.account.suspend_generation
        self.action('action_llm_suspend_policy')
        self.assertGreater(self.account.suspend_generation, gen)
        self.assertEqual(self.account.suspend_reason, 'policy')
        _llm_internal(self.account).write({'state': 'closed'})
        for action in ('action_llm_suspend_ops', 'action_llm_suspend_policy', 'action_llm_resume'):
            with self.assertRaises(LlmError) as caught:
                self.action(action)
            self.assertEqual(caught.exception.code, 'llm_closed')
        self.owner.write({'active': False})
        self.assertEqual(self.account.state, 'closed')

    def test_operator_revocation_during_resume_requarantines_already_restored_keys(self):
        first, second = self.key(), self.key()
        self.action('action_llm_suspend_ops')
        self.quarantine()
        self.action('action_llm_resume')
        self.tick()
        self.assertIn('active', (first.state, second.state))
        self.operator._llm_set_named_human_eligible(False)
        self.tick()
        self.assertFalse(self.account.resume_pending)
        self.quarantine()
        self.assertEqual(self.account.state, 'suspended')
        self.assertTrue(all(t['status'] == 2 for t in self.fake.tokens.values()))
        self.assertIn('failed', self.events('resume').mapped('result'))

    def test_owner_deactivation_cancels_partial_restore_and_invalidates_receipts(self):
        self.key()
        self.key()
        self.action('action_llm_suspend_ops')
        self.quarantine()
        gen = self.account.suspend_generation
        self.action('action_llm_resume')
        self.tick()
        self.owner.write({'active': False})
        self.assertGreater(self.account.suspend_generation, gen)
        self.assertFalse(self.account.resume_pending)
        self.quarantine()
        self.assertEqual(self.account.suspend_reason, 'account_inactive')
        self.assertTrue(all(t['status'] == 2 for t in self.fake.tokens.values()))

    def test_unknown_resume_keeps_gate_closed_and_retries_idempotently(self):
        key = self.key()
        self.action('action_llm_suspend_ops')
        self.quarantine()
        self.action('action_llm_resume')
        with patch.object(self.client, 'user_set_token_status', return_value=nac.Unknown('timeout')):
            self.tick()
        self.assertEqual(self.account.state, 'suspended')
        self.assertEqual((key.state, key.disabled_by), ('disabled', 'suspend'))
        self.assertTrue(self.account.resume_pending)
        self.assertIn('unknown', self.events('key_status').mapped('result'))
        _llm_internal(key).write({'not_me_disable_next_at': False})
        self.restore()
        self.assertEqual((self.account.state, key.state), ('active', 'active'))

    def test_suspend_fences_provisioning_and_resume_preserves_previous_state(self):
        _llm_internal(self.account).write({'state': 'provisioning', 'provision_lease_token': 'old-lease'})
        self.owner.write({'active': False})
        self.assertFalse(self.account.provision_lease_token)
        self.assertEqual(self.account.suspend_return_state, 'provisioning')
        self.owner.write({'active': True})
        self.action('action_llm_resume')
        self.restore()
        self.assertEqual(self.account.state, 'provisioning')

    def test_unconfigured_actions_and_cron_do_not_call_upstream(self):
        reset_llm_config(self.env)
        with self.assertRaises(UserError):
            self.action('action_llm_suspend_ops')
        self.Notice._cron_llm_not_me_disable()
        self.assertFalse(self.events())
        self.assertFalse(self.fake.calls)

    def test_audit_failure_aborts_the_request_and_cannot_commit_an_unaudited_suspend(self):
        Event = type(self.env[EVENT])
        try:
            with self.env.cr.savepoint(), patch.object(Event, '_llm_insert_event_cr',
                                                       side_effect=RuntimeError('audit unavailable')):
                self.action('action_llm_suspend_ops')
        except RuntimeError:
            pass
        else:
            self.fail('audit failure was swallowed')
        self.assertEqual(self.account.state, 'active')
        self.assertFalse(self.events())
        self.assertFalse(self.fake.calls)

    def test_direct_write_and_json_context_cannot_change_lifecycle(self):
        for record in (self.account, self.account.sudo().with_context(llm_internal=True)):
            with self.assertRaises(AccessError):
                record.write({'state': 'active', 'resume_pending': True})
        self.assertEqual(self.account.state, 'active')
        self.assertFalse(self.account.resume_pending)

    def test_late_status_snapshot_does_not_erase_a_user_disable_during_rescan(self):
        key = self.key('disabled', 'user')
        self.action('action_llm_suspend_ops')
        self.quarantine()
        self.assertEqual(key.not_me_disable_plan['disabled_by_before_suspend'], 'user')
        # Simulate a delayed mirror update. Its old status must not replace
        # the durable original reason when the same generation scans again.
        _llm_internal(key).write({'state': 'active', 'disabled_by': False})
        self.quarantine()
        self.assertEqual((key.state, key.disabled_by), ('disabled', 'user'))
        self.action('action_llm_resume')
        self.restore()
        self.assertEqual((key.state, key.disabled_by), ('disabled', 'user'))
        self.assertEqual(self.fake.tokens[key.newapi_token_id]['status'], 2)

    def test_resume_honors_a_daily_cap_recorded_while_account_was_suspended(self):
        key = self.key(kind='free_trial')
        self.action('action_llm_suspend_ops')
        self.quarantine()
        from ..models.llm_free_usage import taipei_day
        _llm_internal(self.env['woow_paas_platform.llm_free_usage'].sudo()).create({
            'account_id': self.account.id, 'day_tpe': taipei_day(),
            'success_count': 200, 'capped_at': self.now,
        })
        self.action('action_llm_resume')
        self.restore()
        self.assertEqual(self.account.state, 'active')
        self.assertEqual((key.state, key.disabled_by), ('disabled', 'free_daily_cap'))
        self.assertEqual(self.fake.tokens[key.newapi_token_id]['status'], 2)

    def test_legacy_receipt_without_original_reason_cannot_reenable_a_key(self):
        key = self.key()
        self.action('action_llm_suspend_ops')
        self.quarantine()
        plan = dict(key.not_me_disable_plan)
        plan.pop('disabled_by_before_suspend')
        _llm_internal(key).write({'not_me_disable_plan': plan})
        before = len(self.fake.calls)
        with self.assertRaises(UserError):
            self.action('action_llm_resume')
        self.assertEqual(self.account.state, 'suspended')
        self.assertFalse(self.account.resume_pending)
        self.assertFalse(self.events('resume'))
        self.assertEqual(len(self.fake.calls), before)
        self.assertEqual(self.fake.tokens[key.newapi_token_id]['status'], 2)
        # Changing suspension reason/generation must not manufacture provenance
        # from the old 'suspend' marker and bypass the legacy restoration gate.
        self.action('action_llm_suspend_policy')
        self.quarantine()
        self.assertNotIn('disabled_by_before_suspend', key.not_me_disable_plan)
        before = len(self.fake.calls)
        with self.assertRaises(UserError):
            self.action('action_llm_resume')
        self.assertEqual(len(self.fake.calls), before)
        self.assertFalse(self.account.resume_pending)
        self.assertEqual(self.fake.tokens[key.newapi_token_id]['status'], 2)

    def test_replayed_resume_does_not_duplicate_acceptance_or_replace_its_actor(self):
        self.key()
        self.action('action_llm_suspend_ops')
        self.quarantine()
        self.action('action_llm_resume')
        self.action('action_llm_resume')
        self.assertEqual(self.events('resume').mapped('result'), ['started'])
        self.restore()
        self.assertEqual(self.events('resume').mapped('result'), ['started', 'ok'])

    def test_stale_key_create_preflight_is_rechecked_under_the_insert_lock(self):
        # Simulate a preflight already accepted by an older request. The insert
        # seam must reject suspension independently of the caller's ORM cache.
        self.action('action_llm_suspend_ops')
        for state, code in (('suspended', 'llm_suspended'), ('closed', 'llm_closed')):
            _llm_internal(self.account).write({'state': state})
            with self.assertRaises(LlmError) as caught:
                self.env[KEY]._llm_insert_creating(
                    self.account, 'paid', str(uuid.uuid4()), self.owner, '', None, None)
            self.assertEqual(caught.exception.code, code)
        self.assertFalse(self.account.api_key_ids)
        self.assertFalse(self.fake.calls)

    def test_batch_user_deactivation_handles_all_partners_and_preserves_closed_history(self):
        other = new_test_user(
            self.env, login='lifecycle_other_' + uuid.uuid4().hex, groups='base.group_portal')
        account = _llm_internal(self.env[ACCOUNT].sudo()).create({
            'partner_id': other.partner_id.id, 'state': 'active',
        })
        closed = _llm_internal(self.env[ACCOUNT].sudo()).create({
            'partner_id': self.owner.partner_id.id, 'state': 'closed',
        })
        (self.owner | other).write({'active': False})
        self.assertEqual((self.account.state, account.state, closed.state),
                         ('suspended', 'suspended', 'closed'))
        self.assertEqual((self.account.suspend_reason, account.suspend_reason),
                         ('account_inactive', 'account_inactive'))


@tagged('post_install', '-at_install', 'llm_lifecycle')
class TestLlmLifecycleConcurrency(TransactionCase):
    """Real independent cursors; no mocked locks, commits or isolation level."""

    def setUp(self):
        super().setUp()
        env_patch = patch.dict(os.environ, llm_env())
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.fixture = CommittedLlmFixture(self)
        self.fixture.extra_cron_ids = [self.env.ref(
            'woow_paas_platform.ir_cron_llm_not_me_disable').id]
        self.fixture.start()
        self.account_id = self.fixture.activate(self.fixture.partner('lifecycle concurrency'))
        with self.registry.cursor() as cr:
            env = env_for(self.registry, cr)
            account = env[ACCOUNT].browse(self.account_id)
            _llm_internal(account).write({'state': 'active', 'newapi_user_id': 987681})
            key = _llm_internal(env[KEY].sudo()).create({
                'account_id': account.id, 'kind': 'paid', 'state': 'disabled',
                'disabled_by': 'user', 'newapi_token_id': 777,
                'newapi_name': 'wp_lifecycle_race_' + uuid.uuid4().hex[:8],
            })
            self.key_id = key.id

    def suspend(self):
        with self.registry.cursor() as cr:
            account = env_for(self.registry, cr)[ACCOUNT].browse(self.account_id)
            account._llm_suspend('ops', SUPERUSER_ID)

    def test_stale_create_request_cannot_insert_after_suspension_commits(self):
        with self.registry.cursor() as stale:
            env = env_for(self.registry, stale)
            account = env[ACCOUNT].browse(self.account_id)
            self.assertEqual(account.state, 'active')
            self.suspend()
            with self.assertRaises(LlmError) as caught:
                env[KEY]._llm_insert_creating(
                    account, 'paid', str(uuid.uuid4()), None, '', None, None)
            self.assertEqual(caught.exception.code, 'llm_suspended')
        with self.registry.cursor() as cr:
            cr.execute('SELECT COUNT(*) FROM woow_paas_platform_llm_api_key '
                       'WHERE account_id=%s', (self.account_id,))
            self.assertEqual(cr.fetchone()[0], 1)

    def test_stale_enable_fails_before_client_construction_or_external_io(self):
        client = MagicMock()
        with self.registry.cursor() as stale:
            env = env_for(self.registry, stale)
            account = env[ACCOUNT].browse(self.account_id)
            self.assertEqual(account.state, 'active')
            partner = account.partner_id
            self.suspend()
            with patch.object(type(account), '_llm_client', return_value=client) as factory:
                try:
                    env[KEY]._llm_set_key_status(partner, self.key_id, True)
                except SerializationFailure:
                    stale.rollback()
                else:
                    self.fail('stale enable was not fenced before remote I/O')
                factory.assert_not_called()
            self.assertFalse(client.mock_calls)

    def test_disable_worker_skips_an_account_locked_by_a_lifecycle_transition(self):
        self.suspend()
        client = MagicMock()
        with self.registry.cursor() as owner:
            owner.execute('SELECT id FROM woow_paas_platform_llm_account '
                          'WHERE id=%s FOR NO KEY UPDATE', (self.account_id,))
            with self.registry.cursor() as worker:
                env = env_for(self.registry, worker)
                with patch.object(type(env[ACCOUNT]), '_llm_client', return_value=client) as factory:
                    tick = LlmCronTick(30, 1)
                    self.assertTrue(env[NOTICE]._llm_not_me_disable_tick(
                        {}, tick, account_ids=[self.account_id]))
                    self.assertEqual(tick.done, 0)
                    factory.assert_not_called()
        self.assertFalse(client.mock_calls)
