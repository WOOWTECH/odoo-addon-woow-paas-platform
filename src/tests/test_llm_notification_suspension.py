"""Suspension races and recovery without live upstream requests."""
import os
import threading
import uuid
from datetime import timedelta
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.tests.common import TransactionCase, tagged

from ..models import llm_notifications as notices
from ..models.llm_common import LlmCronTick, _llm_internal
from ..services import newapi_client as nac
from .llm_test_support import ACCOUNT, KEY, CommittedLlmFixture, env_for, llm_env
from .test_llm_platform_foundation import _ConfiguredCase, _new_account

NOTICE = 'woow_paas_platform.llm_notification'


@tagged('post_install', '-at_install', 'llm_notifications')
class TestLlmSuspensionRecovery(_ConfiguredCase):

    def setUp(self):
        super().setUp()
        self.account = _new_account(self.env, 'notification recovery', user_id=987660)
        _llm_internal(self.account).write({
            'state': 'suspended', 'suspend_reason': 'not_me', 'not_me_notice_id': 31,
        })
        self.client = MagicMock()
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
            'newapi_name': 'wp_recover_' + uuid.uuid4().hex[:8], **extra,
        })

    def tick(self):
        return self.env[NOTICE]._llm_not_me_disable_tick({}, LlmCronTick(30, 1))

    def test_missing_id_is_found_beyond_first_page_then_disabled_without_reveal(self):
        key = self.key()
        self.client.user_list_tokens.side_effect = [
            nac.Ok({'total': 101, 'items': [
                {'id': i + 1, 'name': 'other_%d' % i} for i in range(100)]}),
            nac.Ok({'total': 101, 'items': [{'id': 777, 'name': key.newapi_name}]}),
        ]
        self.client.user_set_token_status.return_value = nac.Ok({})
        self.assertTrue(self.tick())
        self.assertEqual(key.not_me_scan_page, 2)
        self.assertFalse(key.newapi_token_id)
        self.assertTrue(self.tick())
        self.assertEqual(key.newapi_token_id, 777)
        self.assertFalse(self.tick())
        self.assertEqual(key.state, 'disabled')
        self.assertEqual(key.not_me_disabled_notice_id, 31)
        self.assertEqual([c.args[2:4] for c in self.client.user_list_tokens.call_args_list],
                         [(1, 100), (2, 100)])
        self.client.user_reveal_token.assert_not_called()

    def test_absent_interrupted_create_stays_pending_until_late_token_is_found(self):
        now = fields.Datetime.now()
        key = self.key('creating', create_date=now - timedelta(minutes=2))
        self.client.user_list_tokens.side_effect = [
            nac.Ok({'total': 0, 'items': []}),
            nac.Ok({'total': 1, 'items': [{'id': 778, 'name': key.newapi_name}]}),
        ]
        self.client.user_set_token_status.return_value = nac.Ok({})
        with patch.object(notices, '_utcnow', return_value=now):
            self.assertFalse(self.tick())  # Backoff, never a false "disabled".
        self.assertEqual(key.state, 'failed_reveal')
        self.assertFalse(key.newapi_token_id)
        self.client.user_set_token_status.assert_not_called()
        with patch.object(notices, '_utcnow', return_value=now + timedelta(seconds=61)):
            self.assertTrue(self.tick())
            self.assertFalse(self.tick())
        self.assertEqual(key.state, 'disabled')

    def test_previous_suspension_does_not_hide_keys_from_a_new_report(self):
        key = self.key('disabled', newapi_token_id=779, disabled_by='suspend',
                       not_me_disabled_notice_id=30, not_me_attempt_notice_id=30,
                       not_me_disable_next_at=fields.Datetime.now() + timedelta(hours=1))
        self.client.user_set_token_status.return_value = nac.Ok({})
        self.assertFalse(self.tick())
        self.assertEqual(key.not_me_disabled_notice_id, 31)
        self.client.user_set_token_status.assert_called_once()


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
        outcome = {}

        def work():
            try:
                with self.registry.cursor() as cr:
                    env = env_for(self.registry, cr)
                    outcome['more'] = env[NOTICE]._llm_not_me_disable_tick(
                        {}, LlmCronTick(30, 1))
            except Exception as exc:
                outcome['error'] = type(exc).__name__

        Account = type(self.env[ACCOUNT])
        with patch.object(Account, '_llm_tenant_ip', return_value='198.18.0.1'), \
                patch.object(Account, '_llm_up_token', return_value='test-up'), \
                patch.object(Account, '_llm_client', return_value=client):
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
            work()
        self.assertNotIn('error', outcome)
        self.assertEqual(calls, [True, False])
        with self.registry.cursor() as cr:
            cr.execute('SELECT state,disabled_by,not_me_disabled_notice_id '
                       'FROM woow_paas_platform_llm_api_key WHERE id=%s', (self.key_id,))
            self.assertEqual(cr.fetchone(), ('disabled', 'suspend', 32))
