"""#2034：SWR queue writers must survive a real concurrent PostgreSQL update.

These cases deliberately use ``BaseCase`` plus real registry connections.  A
holder transaction updates the existing cache row, while the production
``INSERT .. ON CONFLICT DO UPDATE`` waits on that transaction.  Odoo's default
REPEATABLE READ then raises a real SQLSTATE 40001 after the holder commits;
READ COMMITTED must re-evaluate the row and queue the refresh instead.

Only marker-owned closed accounts/cache rows are committed, and cleanup uses
their exact IDs.  No upstream client, mail transport, service, or cron executes.
"""
import json
import threading
import time
import uuid
from datetime import timedelta
from unittest.mock import patch

import odoo
from odoo import api, fields
from odoo.modules.registry import Registry
from odoo.tests.common import BaseCase, get_db_name, tagged

from ..models import llm_swr as llm_swr_module
from ..models.llm_common import _llm_internal
from ..models.llm_swr import (
    CACHE_MODEL,
    CACHE_TABLE,
    KIND_SELF,
    KIND_USAGE_DAY,
)
from .llm_test_support import ACCOUNT, env_for


@tagged('post_install', '-at_install', 'llm_swr_concurrency_2034')
class TestLlmSwrQueueConcurrency2034(BaseCase):

    def setUp(self):
        super().setUp()
        self.registry = Registry(get_db_name())
        self.marker = 'llm-swr-2034-' + uuid.uuid4().hex
        self.partner_id = None
        self.account_id = None
        self.addCleanup(self._purge)
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            partner = env['res.partner'].with_context(
                mail_create_nolog=True,
                mail_create_nosubscribe=True,
                tracking_disable=True,
            ).create({'name': self.marker})
            account = _llm_internal(env[ACCOUNT].sudo()).create({
                'partner_id': partner.id,
                # A closed fixture cannot be claimed by provisioning or an LLM
                # worker if a shared sandbox server happens to poll meanwhile.
                'state': 'closed',
            })
            self.partner_id = partner.id
            self.account_id = account.id
            cr.commit()

    def _purge(self):
        try:
            with self.registry.cursor() as cr:
                if self.account_id:
                    cr.execute(
                        'DELETE FROM %s WHERE account_id = %%s' % CACHE_TABLE,
                        (self.account_id,),
                    )
                    cr.execute(
                        'DELETE FROM woow_paas_platform_llm_account WHERE id = %s',
                        (self.account_id,),
                    )
                if self.partner_id:
                    cr.execute(
                        'DELETE FROM res_partner WHERE id = %s',
                        (self.partner_id,),
                    )
                cr.commit()
        except Exception:  # pragma: no cover - cleanup must not hide the result
            llm_swr_module._logger.exception(
                '#2034 SWR concurrency fixture cleanup failed (marker=%s)',
                self.marker,
            )

    def _insert_cache(self, kind, *, cache_key=None, payload=None):
        fetched_at = fields.Datetime.now() - timedelta(hours=1)
        now = fields.Datetime.now()
        with self.registry.cursor() as cr:
            cr.execute(
                'INSERT INTO %s (account_id, kind, cache_key, payload, fetched_at, '
                'last_result, create_uid, create_date, write_uid, write_date) '
                'VALUES (%%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s) '
                'RETURNING id' % CACHE_TABLE,
                (self.account_id, kind, cache_key, payload, fetched_at, 'ok',
                 odoo.SUPERUSER_ID, now, odoo.SUPERUSER_ID, now),
            )
            row_id = cr.fetchone()[0]
            cr.commit()
        return row_id, fetched_at

    def _wait_for_blocked_upsert(self, thread, timeout=4.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and thread.is_alive():
            with self.registry.cursor() as cr:
                cr.execute(
                    'SELECT EXISTS (SELECT 1 FROM pg_locks l '
                    'LEFT JOIN pg_database d ON d.oid = l.database '
                    "WHERE l.locktype IN ('transactionid', 'tuple') "
                    'AND NOT l.granted AND l.pid <> pg_backend_pid() '
                    'AND cardinality(pg_blocking_pids(l.pid)) > 0 '
                    'AND (d.datname IS NULL OR d.datname = current_database()))',
                )
                if cr.fetchone()[0]:
                    return True
            time.sleep(0.05)
        return False

    def _race(self, row_id, call):
        holder = self.registry._db.cursor()
        outcome = {}

        def worker():
            try:
                with self.registry._db.cursor() as cr:
                    # Avoid Registry(cr.dbname): the Odoo test runner holds the
                    # global registry lock while post-install tests execute.
                    env = env_for(self.registry, cr)
                    outcome['value'] = call(env)
            except BaseException as exc:  # noqa: BLE001 - returned to main test thread
                outcome['error'] = exc

        try:
            holder.execute("SET LOCAL statement_timeout = '10s'")
            holder.execute(
                'UPDATE %s SET last_result = %%s WHERE id = %%s' % CACHE_TABLE,
                ('holder-' + self.marker[-8:], row_id),
            )
            thread = threading.Thread(
                target=worker,
                name='llm-swr-2034-' + self.marker[-8:],
                daemon=True,
            )
            thread.start()
            blocked = self._wait_for_blocked_upsert(thread)
            if blocked:
                holder.commit()
            else:
                holder.rollback()
        finally:
            holder.close()
        thread.join(10)
        self.assertFalse(thread.is_alive(), 'SWR queue writer 在 holder release 後仍未結束')
        self.assertTrue(blocked, '前置失敗：production cache UPSERT 沒有真的等待 holder')
        self.assertNotIn('error', outcome, outcome)
        return outcome.get('value')

    def _requested_at(self, row_id):
        with self.registry.cursor() as cr:
            cr.execute(
                'SELECT requested_at FROM %s WHERE id = %%s' % CACHE_TABLE,
                (row_id,),
            )
            return cr.fetchone()[0]

    def _cron_class(self):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            return type(env['ir.cron'])

    def test_singleton_queue_reloads_row_after_concurrent_commit(self):
        row_id, fetched_at = self._insert_cache(KIND_SELF, payload='{}')

        def queue(env):
            account = env[ACCOUNT].browse(self.account_id)
            return env[CACHE_MODEL]._llm_swr_touch(account, KIND_SELF, fetched_at)

        # BaseCase may run while the process-global registry is in TestCursor
        # mode because other test classes were loaded.  Force this probe and
        # the production helper's nested cursor onto real PG connections; also
        # expose the production isolation branch used outside test mode.
        with patch.object(type(self.registry), 'cursor',
                          lambda registry: registry._db.cursor()), \
                patch.object(type(self.registry), 'in_test_mode',
                             lambda registry: False), \
                patch.object(self._cron_class(), '_trigger', autospec=True,
                             return_value=True), \
                patch.object(llm_swr_module._logger, 'warning') as warning:
            queued = self._race(row_id, queue)

        self.assertTrue(queued, '真並發 commit 後仍應成功排入 singleton refresh')
        self.assertTrue(self._requested_at(row_id))
        warning.assert_not_called()

    def test_daily_bulk_queue_reloads_row_after_concurrent_commit(self):
        day = (fields.Date.today() - timedelta(days=1)).isoformat()
        payload = json.dumps({'day': day, 'items': []})
        row_id, _fetched_at = self._insert_cache(
            KIND_USAGE_DAY,
            cache_key=day,
            payload=payload,
        )

        def queue(env):
            account = env[ACCOUNT].browse(self.account_id)
            return env[CACHE_MODEL]._llm_usage_day_snapshots(
                account,
                [fields.Date.to_date(day)],
            )

        with patch.object(type(self.registry), 'cursor',
                          lambda registry: registry._db.cursor()), \
                patch.object(type(self.registry), 'in_test_mode',
                             lambda registry: False), \
                patch.object(self._cron_class(), '_trigger', autospec=True,
                             return_value=True), \
                patch.object(llm_swr_module._logger, 'warning') as warning:
            snapshots = self._race(row_id, queue)

        self.assertIn(day, snapshots, 'stale last-good daily snapshot must still be returned')
        self.assertTrue(snapshots[day]['stale'])
        self.assertTrue(self._requested_at(row_id),
                        '真並發 commit 後仍應成功排入 daily bulk refresh')
        warning.assert_not_called()
