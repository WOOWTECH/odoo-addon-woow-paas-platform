"""Suspension races and recovery without live upstream requests."""
import json
import os
import threading
import uuid
from datetime import timedelta, timezone
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.tests.common import TransactionCase, tagged

from ..models import llm_notifications as notices
from ..models.llm_common import LlmCronTick, _llm_internal
from ..services import newapi_client as nac
from .llm_fake_newapi import FakeNewApi
from .llm_test_support import ACCOUNT, KEY, CommittedLlmFixture, env_for, llm_env
from .test_llm_platform_foundation import _ConfiguredCase, _new_account

NOTICE = 'woow_paas_platform.llm_notification'


@tagged('post_install', '-at_install', 'llm_notifications')
class TestLlmSuspensionRecovery(_ConfiguredCase):

    def setUp(self):
        super().setUp()
        # ORM assigns create_date itself; exercise recovery after its real quiet window.
        self.now = fields.Datetime.now() + timedelta(minutes=2)
        self.account = _new_account(self.env, 'notification recovery', user_id=987660)
        _llm_internal(self.account).write({
            'state': 'suspended', 'suspend_reason': 'not_me', 'not_me_notice_id': 31,
        })
        self.fake = FakeNewApi(clock=lambda: self.now.replace(tzinfo=timezone.utc).timestamp())
        self.fake.users[987660] = {'id': 987660, 'status': 1}
        self.fake.pats['test-up'] = {'user_id': 987660, 'expires_at': None}
        self.client = self.fake.client(nac.ROLE_CRON)
        for method, result in (
            ('_llm_tenant_ip', '198.18.0.1'), ('_llm_up_token', 'test-up'),
            ('_llm_client', self.client),
        ):
            p = patch.object(type(self.account), method, return_value=result)
            p.start()
            self.addCleanup(p.stop)

    def key(self, state='failed_reveal', **extra):
        return _llm_internal(self.env[KEY].sudo()).create({
            'account_id': self.account.id, 'kind': 'paid', 'state': state,
            'newapi_name': 'wp_%d_%s' % (self.account.id, uuid.uuid4().hex[:4]), **extra,
        })

    def token(self, name, up='test-up'):
        result = self.client.user_create_token(
            '198.18.0.1', up, nac.free_trial_token_body(name, 1_900_000_000))
        self.assertTrue(result.ok)
        return max(self.fake.tokens)

    def tick(self):
        before = len(self.fake.calls)
        tick = LlmCronTick(30, 1)
        with patch.object(notices, '_utcnow', return_value=self.now):
            more = self.env[NOTICE]._llm_not_me_disable_tick(
                {}, tick, account_ids=[self.account.id])
        self.assertLessEqual(len(self.fake.calls) - before, 1, 'one upstream operation per tick')
        self.assertLessEqual(tick.done, 1)
        self.assertNotIn('user_reveal_token', [name for name, _ip in self.fake.calls])
        self.env.flush_all()
        self.env.invalidate_all()
        return more

    def drain(self, keys, limit=30):
        for _ in range(limit):
            if all(key.not_me_disable_plan and key.not_me_disable_plan['complete']
                   and key.not_me_disabled_notice_id == self.account.not_me_notice_id for key in keys):
                return
            self.tick()
        self.fail('recovery did not finish within its bounded test ticks')

    def test_missing_id_is_found_beyond_first_page_then_disabled_without_reveal(self):
        key = self.key()
        token_id = self.token(key.newapi_name)
        for i in range(100):
            self.token('wp_999999_%04d' % i)
        self.assertTrue(self.tick())
        self.assertEqual(key.not_me_scan_page, 2)
        self.assertFalse(key.newapi_token_id)
        self.assertTrue(self.tick())
        self.assertIn(str(token_id), key.not_me_disable_plan['pending'])
        self.assertFalse(self.tick())
        self.assertEqual(key.state, 'failed_reveal')
        self.assertEqual(key.not_me_disabled_notice_id, 31)
        self.assertEqual(self.fake.tokens[token_id]['status'], 2)
        self.assertFalse(self.tick(), 'completed quarantine is excluded for this generation')

    def test_absent_interrupted_create_stays_pending_until_late_token_is_found(self):
        key = self.key('creating')
        self.assertFalse(self.tick())
        self.assertEqual(key.state, 'failed_reveal')
        self.assertFalse(key.not_me_disabled_notice_id)
        self.assertFalse(key.newapi_token_id)
        self.assertNotIn('user_set_token_status', [name for name, _ip in self.fake.calls])
        token_id = self.token(key.newapi_name)
        self.now += timedelta(seconds=61)
        self.drain([key])
        self.assertEqual(key.state, 'failed_reveal')
        self.assertEqual(self.fake.tokens[token_id]['status'], 2)

    def test_previous_suspension_does_not_hide_keys_from_a_new_report(self):
        key = self.key('disabled', disabled_by='suspend',
                       not_me_disabled_notice_id=30, not_me_attempt_notice_id=30,
                       not_me_disable_next_at=self.now + timedelta(hours=1))
        token_id = self.token(key.newapi_name)
        _llm_internal(key).write({'newapi_token_id': token_id})
        self.drain([key])
        self.assertEqual(key.not_me_disabled_notice_id, 31)
        # A resumed account reports again: stale receipts, IDs and retry clocks
        # must not hide newly created same-name siblings from the new generation.
        self.fake.tokens[token_id]['status'] = 1
        later_id = self.token(key.newapi_name)
        _llm_internal(self.account).write({'not_me_notice_id': 32})
        self.drain([key])
        self.assertEqual(key.not_me_disabled_notice_id, 32)
        self.assertEqual(set(key.not_me_disable_plan['disabled']), {token_id, later_id})
        self.assertTrue(all(self.fake.tokens[i]['status'] == 2 for i in (token_id, later_id)))

    def test_recovered_trial_stays_quarantined_beside_replacement_trial(self):
        failed = self.key(kind='free_trial')
        failed_id = self.token(failed.newapi_name)
        replacement = self.key('active', kind='free_trial')
        replacement_id = self.token(replacement.newapi_name)
        _llm_internal(replacement).write({'newapi_token_id': replacement_id})
        self.drain([failed, replacement])
        self.assertEqual((failed.state, replacement.state), ('failed_reveal', 'disabled'))
        self.assertEqual(failed.disabled_by, 'suspend')
        self.assertEqual(failed.not_me_disabled_notice_id, 31)
        self.assertEqual(replacement.not_me_disabled_notice_id, 31)
        self.assertTrue(all(self.fake.tokens[i]['status'] == 2 for i in (failed_id, replacement_id)))
        self.assertFalse(self.tick(), 'neither completed key should be claimed again')

    def test_all_same_page_matches_are_disabled_without_touching_other_tokens(self):
        key = self.key()
        token_ids = [self.token(key.newapi_name) for _ in range(3)]
        unrelated = self.token(key.newapi_name[:-1] + ('0' if key.newapi_name[-1] != '0' else '1'))
        self.fake.users[987662] = {'id': 987662, 'status': 1}
        self.fake.pats['other-up'] = {'user_id': 987662, 'expires_at': None}
        foreign = self.token(key.newapi_name, up='other-up')
        self.drain([key])
        self.assertEqual(set(key.not_me_disable_plan['disabled']), set(token_ids))
        self.assertFalse(key.newapi_token_id, 'do not select one arbitrary recovered ID')
        self.assertTrue(all(self.fake.tokens[i]['status'] == 2 for i in token_ids))
        self.assertEqual(self.fake.tokens[unrelated]['status'], 1)
        self.assertEqual(self.fake.tokens[foreign]['status'], 1)
        encoded = json.dumps(key.not_me_disable_plan)
        self.assertNotIn(key.newapi_name, encoded)
        self.assertFalse(any(self.fake.tokens[i]['key'] in encoded for i in token_ids),
                         'the durable plan must never contain key material')

    def test_known_id_does_not_hide_same_name_tokens_on_later_pages(self):
        key = self.key('active')
        older = self.token(key.newapi_name)
        for i in range(100):
            self.token('wp_999999_%04d' % i)
        newer = self.token(key.newapi_name)
        _llm_internal(key).write({'newapi_token_id': newer})
        self.drain([key])
        self.assertEqual(set(key.not_me_disable_plan['disabled']), {older, newer})
        self.assertTrue(all(self.fake.tokens[i]['status'] == 2 for i in (older, newer)))
        self.assertEqual(sum(name == 'user_list_tokens' for name, _ip in self.fake.calls), 2)

    def test_scan_and_disable_failures_preserve_cursor_and_allow_other_work(self):
        key = self.key()
        older = self.token(key.newapi_name)
        for i in range(100):
            self.token('wp_999999_%04d' % i)
        newer = self.token(key.newapi_name)
        other = self.key('active')
        other_id = self.token(other.newapi_name)
        _llm_internal(other).write({'newapi_token_id': other_id})
        self.fake.fail_before['user_list_tokens'] = nac.Unknown('proxy_timeout')
        self.tick()
        self.assertEqual(key.not_me_scan_page, 1)
        self.assertFalse(key.not_me_disable_plan['complete'])
        self.drain([other])
        self.assertFalse(key.not_me_disabled_notice_id)
        self.now += timedelta(seconds=61)
        self.tick()  # Re-read failed page 1 and persist the new match.
        self.assertIn(str(newer), key.not_me_disable_plan['pending'])
        # Upstream applies the write but its response is unknown: retain the ID.
        self.fake.fail_next['user_set_token_status'] = nac.Unknown('proxy_timeout')
        self.tick()
        self.assertIn(str(newer), key.not_me_disable_plan['pending'])
        self.assertEqual(self.fake.tokens[newer]['status'], 2)
        self.tick()  # Page 2 progresses while that ID is backed off.
        self.assertIn(str(older), key.not_me_disable_plan['pending'])
        self.tick()
        self.assertEqual(self.fake.tokens[older]['status'], 2)
        self.assertFalse(key.not_me_disabled_notice_id)
        self.now += timedelta(seconds=61)
        self.drain([key])
        self.assertEqual(set(key.not_me_disable_plan['disabled']), {older, newer})

    def test_scan_and_remote_success_rollback_keep_the_obligation_recoverable(self):
        key = self.key()
        token_id = self.token(key.newapi_name)
        with self.assertRaisesRegex(RuntimeError, 'rollback scan'):
            with self.env.cr.savepoint():
                self.tick()
                self.assertIn(str(token_id), key.not_me_disable_plan['pending'])
                raise RuntimeError('rollback scan')
        self.env.invalidate_all()
        self.assertFalse(key.not_me_disable_plan)
        self.tick()  # Replay the list read; no cursor advancement was lost.
        with self.assertRaisesRegex(RuntimeError, 'rollback disable'):
            with self.env.cr.savepoint():
                self.tick()
                raise RuntimeError('rollback disable')
        self.env.invalidate_all()
        self.assertEqual(self.fake.tokens[token_id]['status'], 2)
        self.assertIn(str(token_id), key.not_me_disable_plan['pending'])
        self.assertFalse(key.not_me_disabled_notice_id)
        self.drain([key])  # The same idempotent disable now persists its receipt.
        self.assertEqual(key.not_me_disabled_notice_id, 31)


@tagged('post_install', '-at_install', 'llm_notifications')
class TestLlmSuspensionManagementRace(TransactionCase):

    def setUp(self):
        super().setUp()
        p = patch.dict(os.environ, llm_env())
        p.start()
        self.addCleanup(p.stop)
        self.fixture = CommittedLlmFixture(self)
        self.fixture.extra_cron_ids = [
            self.env.ref('woow_paas_platform.ir_cron_llm_not_me_disable').id]
        self.fixture.start()
        account_id = self.fixture.activate(self.fixture.partner('notification race'))
        with self.registry.cursor() as cr:
            env = env_for(self.registry, cr)
            account = env[ACCOUNT].browse(account_id)
            _llm_internal(account).write({'state': 'active', 'newapi_user_id': 987661})
            key = _llm_internal(env[KEY].sudo()).create({
                'account_id': account.id, 'kind': 'paid', 'state': 'disabled',
                'disabled_by': 'user', 'newapi_token_id': 780,
                'newapi_name': 'wp_race_' + uuid.uuid4().hex[:8],
            })
            self.account_id, self.key_id = account.id, key.id

    def test_inflight_enable_that_rolls_back_finishes_before_final_disable(self):
        calls = []
        client = MagicMock()
        client.user_set_token_status.side_effect = lambda *a, **kw: (
            calls.append(a[3]) or nac.Ok({}))
        client.user_list_tokens.return_value = nac.Ok({'total': 0, 'items': []})
        outcome = {}

        def work():
            try:
                with self.registry.cursor() as cr:
                    env = env_for(self.registry, cr)
                    outcome['more'] = env[NOTICE]._llm_not_me_disable_tick(
                        {}, LlmCronTick(30, 1), account_ids=[self.account_id])
            except Exception as exc:
                outcome['error'] = type(exc).__name__

        Account = type(self.env[ACCOUNT])
        with patch.object(Account, '_llm_tenant_ip', return_value='198.18.0.1'), \
                patch.object(Account, '_llm_up_token', return_value='test-up'), \
                patch.object(Account, '_llm_client', return_value=client), \
                patch.object(notices, '_utcnow',
                             return_value=fields.Datetime.now() + timedelta(minutes=2)):
            with self.registry.cursor() as writer:
                writer.execute('SELECT id FROM woow_paas_platform_llm_api_key '
                               'WHERE id=%s FOR UPDATE', (self.key_id,))
                with self.registry.cursor() as cr:
                    cr.execute("UPDATE woow_paas_platform_llm_account SET state='suspended', "
                               "suspend_reason='not_me',not_me_notice_id=32 WHERE id=%s",
                               (self.account_id,))
                thread = threading.Thread(target=work, daemon=True)
                thread.start()
                try:
                    thread.join(timeout=3)
                    self.assertFalse(thread.is_alive(), 'locked management key must be skipped')
                    self.assertNotIn('error', outcome)
                    self.assertTrue(outcome['more'])
                    self.assertEqual(calls, [], 'no remote disable before owning the key lock')
                    # The older HTTP request enables upstream, then aborts locally.
                    client.user_set_token_status('198.18.0.1', 'test-up', 780, True)
                finally:
                    writer.rollback()
                    thread.join(timeout=8)
            outcome.clear()
            work()  # Disable the known ID while holding the management lock.
            work()  # Confirm no same-name siblings before recording completion.
        self.assertNotIn('error', outcome)
        self.assertEqual(calls, [True, False])
        with self.registry.cursor() as cr:
            cr.execute('SELECT state,disabled_by,not_me_disabled_notice_id '
                       'FROM woow_paas_platform_llm_api_key WHERE id=%s', (self.key_id,))
            self.assertEqual(cr.fetchone(), ('disabled', 'suspend', 32))
