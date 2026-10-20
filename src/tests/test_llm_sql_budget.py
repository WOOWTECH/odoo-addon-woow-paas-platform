"""Real PostgreSQL coverage for the per-cursor SQL deadline wrapper.

These tests only use transient cursors and a transaction-scoped advisory lock;
they create no persistent fixtures. The test runner must provide its own empty
Odoo database. Raw registry database cursors avoid taking the Registry lock.
"""

import time
import uuid
from unittest.mock import patch

from psycopg2.errors import LockNotAvailable, QueryCanceled

from odoo.tests import TransactionCase, tagged

from ..models import llm_event as llm_event_module
from ..models.llm_common import ERR_LLM_INTERNAL
from ..services.llm_sql_budget import SqlBudgetExpired, deadline_cursor


_TIMEOUTS = (SqlBudgetExpired, QueryCanceled, LockNotAvailable)


@tagged("post_install", "-at_install")
class TestLlmSqlBudget(TransactionCase):
    def _cursor_factory(self):
        # Do not use registry.cursor(): the budget wrapper is exercised against
        # independent PostgreSQL sessions without contending for Registry._lock.
        return self.registry._db.cursor

    def test_deadline_bounds_advisory_lock_wait_and_is_per_cursor(self):
        holder = self.registry._db.cursor()
        unrelated = self.registry._db.cursor()
        lock_key = uuid.uuid4().int & ((1 << 63) - 1)
        try:
            holder.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))

            started = time.monotonic()
            with self.assertRaises(_TIMEOUTS):
                with deadline_cursor(
                    self._cursor_factory(),
                    deadline=time.monotonic() + 0.4,
                    lock_seconds=5.0,
                ) as waiter:
                    unrelated.execute("SELECT 1")
                    self.assertEqual(unrelated.fetchone()[0], 1)
                    # A five-second lock timeout would make this test fail its
                    # elapsed-time bound; the absolute budget must take priority.
                    waiter.execute(
                        "SELECT pg_advisory_xact_lock(%s)", (lock_key,)
                    )
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 2.0, "lock wait exceeded the SQL budget")

            with deadline_cursor(self._cursor_factory(), deadline=None) as normal:
                normal.execute("SELECT pg_sleep(0.05), 7")
                self.assertEqual(normal.fetchone()[1], 7)
        finally:
            # Transaction-scoped advisory locks are released on rollback.
            try:
                holder.rollback()
            finally:
                holder.close()
                unrelated.close()

    def test_successive_statements_share_one_absolute_deadline(self):
        started = time.monotonic()
        deadline = started + 0.32
        # The inner assertion proves the second statement expires; the outer
        # one captures deadline_cursor's independent exit/commit budget check.
        with self.assertRaises(SqlBudgetExpired):
            with deadline_cursor(
                self._cursor_factory(), deadline=deadline, lock_seconds=5.0
            ) as cr:
                cr.execute('SAVEPOINT "llm_sql_budget_cleanup"')
                cr.execute("SELECT pg_sleep(0.20)")
                with self.assertRaises(_TIMEOUTS):
                    # This call has only the remainder of the original budget.
                    cr.execute("SELECT pg_sleep(0.20)")

                # Cleanup SQL is permitted after expiry, including when the timed
                # out PostgreSQL statement left the transaction in failed state.
                cr.execute('ROLLBACK TO SAVEPOINT "llm_sql_budget_cleanup"')
                cr.execute('RELEASE SAVEPOINT "llm_sql_budget_cleanup"')

                # Invalid SQL distinguishes local expiry refusal from a server
                # parse error: this statement must never reach PostgreSQL.
                with self.assertRaises(SqlBudgetExpired):
                    cr.execute("SELECT definitely_not_valid_sql")

        self.assertLess(
            time.monotonic() - started,
            2.0,
            "successive statements did not share the absolute SQL deadline",
        )

    def test_already_expired_cursor_refuses_sql_but_allows_savepoint_cleanup(self):
        unrelated = self.registry._db.cursor()
        try:
            with self.assertRaises(SqlBudgetExpired):
                with deadline_cursor(
                    self._cursor_factory(),
                    deadline=time.monotonic() + 0.1,
                    lock_seconds=5.0,
                ) as cr:
                    cr.execute('SAVEPOINT "llm_sql_budget_expired_cleanup"')
                    time.sleep(0.15)

                    with self.assertRaises(SqlBudgetExpired):
                        # If sent to PostgreSQL this would raise a syntax error.
                        cr.execute("SELECT definitely_not_valid_sql")

                    cr.execute(
                        'ROLLBACK TO SAVEPOINT "llm_sql_budget_expired_cleanup"'
                    )
                    cr.execute(
                        'RELEASE SAVEPOINT "llm_sql_budget_expired_cleanup"'
                    )

                    # The expired wrapper is local to its cursor; an unrelated
                    # one still sends and receives SQL normally.
                    unrelated.execute("SELECT 23")
                    self.assertEqual(unrelated.fetchone()[0], 23)
        finally:
            unrelated.close()

    def test_llm_alert_decorator_and_nested_cursor_share_deadline(self):
        alert = llm_event_module.LlmAlert.TEST
        target_model = "woow_paas_platform.llm_runtime_state"
        target_id = (uuid.uuid4().int % (2 ** 31 - 1)) + 1
        dedupe_key = llm_event_module.alert_dedupe_key(
            alert,
            target_model=target_model,
            target_id=target_id,
            error_code=ERR_LLM_INTERNAL,
        )
        holder = self.registry._db.cursor()
        try:
            holder.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ("woow_llm_alert:%s" % dedupe_key,),
            )

            service = self.env["woow_paas_platform.llm_event"]
            deadline = time.monotonic() + 0.4
            started = time.monotonic()
            with patch.object(llm_event_module, "llm_unconfigured", return_value=False), \
                    patch.object(
                        llm_event_module,
                        "alert_targets",
                        return_value={"emails": (), "webhook": None},
                    ):
                with self.assertRaises(_TIMEOUTS):
                    service._llm_alert(
                        alert,
                        target_model=target_model,
                        target_id=target_id,
                        error_code=ERR_LLM_INTERNAL,
                        deadline=deadline,
                    )
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 2.0, "_llm_alert exceeded its shared SQL budget")

            # The held per-alert lock is before the dedupe lookup and all event,
            # burst-carrier, email, and webhook work. Scope this check to this
            # unique target so other full-suite fixtures cannot affect it.
            check = self.registry._db.cursor()
            try:
                check.execute(
                    "SELECT count(*) FROM woow_paas_platform_llm_event "
                    "WHERE target_model = %s AND target_id = %s AND alert_code = %s",
                    (target_model, target_id, alert.value),
                )
                self.assertEqual(check.fetchone()[0], 0)
            finally:
                check.close()
        finally:
            try:
                holder.rollback()
            finally:
                holder.close()
