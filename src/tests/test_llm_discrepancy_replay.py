"""Real stale-RR replay, without threads or commits to global configuration.

Only marker-owned fixtures are committed. The production acknowledge method,
UNIQUE conflict handling, fresh lookup, and financial audit all run unchanged.
Main wires this test after integrating the discrepancy model.
"""
import uuid
from contextlib import contextmanager
from unittest import TestCase
from unittest.mock import patch

from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase, new_test_user, tagged
from odoo.tools import mute_logger

from ..models import llm_known_discrepancy as discrepancy
from ..models.llm_common import _llm_internal
from ..models.llm_event import EVENT_TABLE, _env_on

ACCOUNT = 'woow_paas_platform.llm_account'
ACCOUNT_TABLE = 'woow_paas_platform_llm_account'


@tagged('post_install', '-at_install', 'llm_discrepancy_replay')
class TestLlmDiscrepancyReplay(TransactionCase):

    @contextmanager
    def _connection(self):
        # Bypass TestCursor even during registry preload; bind every Environment
        # to this registry instead of calling the process-global Registry().
        with self.registry._db.cursor() as cr:
            cr.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
            cr.execute("SET LOCAL lock_timeout = '2s'")
            cr.execute("SET LOCAL statement_timeout = '10s'")
            yield cr, _env_on(self.registry, cr)

    def test_committed_winner_is_replayed_from_stale_rr_snapshot(self):
        marker = 'llm_discrepancy_replay_' + uuid.uuid4().hex
        request_id = str(uuid.uuid4())
        reason = 'Owned reconciliation evidence ' + marker
        delta = 9007199254740993
        owned = {'partners': {}, 'users': {}, 'account': None, 'ledger': None, 'audit': None}
        # Registry.cursor inside the production fallback must also be a real
        # connection when a surrounding Odoo runner enables TestCursor mode.
        # This process-local factory patch changes no service or database state.
        with patch.object(discrepancy, 'llm_unconfigured', return_value=False), \
                patch.object(self.registry, 'cursor', self.registry._db.cursor), \
                patch('requests.sessions.Session.request',
                      side_effect=AssertionError('Replay must not make HTTP requests')), \
                patch.object(type(self.env['mail.mail']), 'send',
                             side_effect=AssertionError('Replay must not send mail')):
            try:
                with self._connection() as (cr, env):
                    env = env(context=dict(
                        env.context, no_reset_password=True,
                        mail_create_nosubscribe=True, tracking_disable=True))
                    partner = env['res.partner'].create({'name': marker})
                    owned['partners'][partner.id] = marker
                    # A closed, remote-ID-less fixture cannot enter provisioning,
                    # free-cap, or key workers in a future shared-suite run.
                    account = _llm_internal(env[ACCOUNT]).create({
                        'partner_id': partner.id, 'state': 'closed'})
                    owned['account'] = account.id
                    for suffix in ('winner', 'other'):
                        login = marker + '_' + suffix
                        user = new_test_user(
                            env, login=login, name=login, email=login + '@example.test',
                            groups='base.group_user,base.group_system')
                        user._llm_set_named_human_eligible(True)
                        owned['users'][user.id] = (login, user.partner_id.id)
                        owned['partners'][user.partner_id.id] = login
                    env.flush_all()
                    cr.commit()
                actor_id, other_id = owned['users']
                account_id = owned['account']
                with self._connection() as (loser_cr, loser_env):
                    loser_cr.execute('SHOW transaction_isolation')
                    self.assertEqual(loser_cr.fetchone()[0], 'repeatable read')
                    query = 'SELECT id FROM ' + discrepancy.TABLE + ' WHERE request_id = %s'
                    loser_cr.execute(query, (request_id,))
                    self.assertIsNone(loser_cr.fetchone())  # Fix the losing RR snapshot.
                    with self._connection() as (winner_cr, winner_env):
                        winner = winner_env[discrepancy.MODEL].with_user(actor_id)._acknowledge(
                            account_id, delta, reason, request_id)
                        winner_env.flush_all()
                        winner_id, audit_id = winner.id, winner.llm_event_id.id
                        self.assertTrue(audit_id)
                        owned.update(ledger=winner_id, audit=audit_id)
                        winner_cr.commit()
                    loser_cr.execute(query, (request_id,))
                    self.assertIsNone(loser_cr.fetchone(), 'Winner must remain invisible in RR')
                    service = loser_env[discrepancy.MODEL].with_user(actor_id)
                    with mute_logger('odoo.sql_db'):
                        replay = service._acknowledge(account_id, delta, reason, request_id)
                    # Do not read fields through the stale snapshot/MissingError.
                    self.assertEqual(replay.id, winner_id)
                    for actor, amount in ((actor_id, delta + 1), (other_id, delta)):
                        with self.subTest(actor=actor, amount=amount), \
                                TestCase.assertRaises(self, ValidationError), \
                                mute_logger('odoo.sql_db'):
                            loser_env[discrepancy.MODEL].with_user(actor)._acknowledge(
                                account_id, amount, reason, request_id)
                    loser_cr.rollback()
                with self._connection() as (cr, _env):
                    cr.execute(
                        'SELECT id, account_id, quota_delta, reason, created_uid, llm_event_id '
                        'FROM ' + discrepancy.TABLE + ' WHERE request_id = %s', (request_id,))
                    self.assertEqual(cr.fetchall(), [
                        (winner_id, account_id, delta, reason, actor_id, audit_id)])
                    cr.execute(
                        'SELECT id, action, target_model, target_id, uid FROM ' + EVENT_TABLE
                        + ' WHERE account_id = %s OR (target_model = %s AND target_id = %s)',
                        (account_id, discrepancy.MODEL, winner_id))
                    self.assertEqual(cr.fetchall(), [
                        (audit_id, 'known_discrepancy', discrepancy.MODEL, winner_id, actor_id)])
            finally:
                if owned['account']:
                    self._cleanup_owned(owned, request_id, reason, delta, marker)

    def _cleanup_owned(self, owned, request_id, reason, delta, marker):
        """Validate provenance BEFORE deleting; any mismatch rolls everything back."""
        with self._connection() as (cr, env):
            account_id = owned['account']
            user_ids, partner_ids = list(owned['users']), list(owned['partners'])
            cr.execute('SELECT id, name FROM res_partner WHERE id = ANY(%s)', (partner_ids,))
            partners = dict(cr.fetchall())
            cr.execute('SELECT id, login, partner_id FROM res_users WHERE id = ANY(%s)',
                       (user_ids,))
            users = {uid: (login, partner) for uid, login, partner in cr.fetchall()}
            cr.execute('SELECT partner_id, state FROM ' + ACCOUNT_TABLE + ' WHERE id = %s',
                       (account_id,))
            account = cr.fetchone()
            # Fixture creation itself may have rolled back before its commit.
            if not partners and not users and not account:
                return
            message = 'Owned replay cleanup refused; fixture retained: ' + marker
            self.assertEqual(partners, owned['partners'], message)
            self.assertEqual(users, owned['users'], message)
            account_partner = next(pid for pid, name in owned['partners'].items() if name == marker)
            self.assertEqual(account, (account_partner, 'closed'), message)
            cr.execute(
                'SELECT id, request_id, account_id, quota_delta, reason, created_uid, llm_event_id '
                'FROM ' + discrepancy.TABLE
                + ' WHERE request_id = %s OR account_id = %s OR created_uid = ANY(%s)',
                (request_id, account_id, user_ids))
            ledgers = cr.fetchall()
            ledger_ids, audit_ids = [], []
            for ledger_id, request, account_ref, amount, text, actor, audit_id in ledgers:
                self.assertEqual((ledger_id, audit_id), (owned['ledger'], owned['audit']), message)
                self.assertEqual((request, account_ref, amount, text, actor),
                                 (request_id, account_id, delta, reason, user_ids[0]), message)
                self.assertTrue(audit_id, message)
                ledger_ids.append(ledger_id)
                audit_ids.append(audit_id)
            cr.execute(
                'SELECT id, account_id, partner_id, action, target_model, target_id, uid '
                'FROM ' + EVENT_TABLE
                + ' WHERE account_id = %s OR uid = ANY(%s) OR id = ANY(%s) '
                'OR (target_model = %s AND target_id = ANY(%s))',
                (account_id, user_ids, audit_ids, discrepancy.MODEL, ledger_ids))
            events = cr.fetchall()
            self.assertEqual({row[0] for row in events}, set(audit_ids), message)
            for event_id, account_ref, partner_ref, action, model, target, actor in events:
                self.assertEqual(
                    (account_ref, partner_ref, action, model, actor),
                    (account_id, account_partner, 'known_discrepancy',
                     discrepancy.MODEL, user_ids[0]), message)
                self.assertIn((target, event_id),
                              [(row[0], row[-1]) for row in ledgers], message)
            # A foreign FK reference aborts this whole cleanup transaction; do
            # not broaden a delete or remove records to force cleanup through.
            cr.execute('DELETE FROM ' + discrepancy.TABLE + ' WHERE id = ANY(%s)', (ledger_ids,))
            cr.execute('DELETE FROM ' + EVENT_TABLE + ' WHERE id = ANY(%s)', (audit_ids,))
            cr.execute('DELETE FROM ' + ACCOUNT_TABLE + ' WHERE id = %s AND partner_id = %s',
                       (account_id, account_partner))
            env['res.users'].browse(user_ids).unlink()
            env['res.partner'].browse(partner_ids).exists().unlink()
            env.flush_all()
            cr.commit()
