"""Real review/update bodies and SQL on SQLite; not PG isolation/commit proof.

Only DB-API placeholders and API decorators are adapted by the existing worker
loader. No product owner predicate or outcome method is replaced. Real PG
committed-cursor regressions live in src/tests/test_llm_credit_attempt.py.
Run: python3 tests/test_llm_credit_invalid_review.py
"""

import copy
import sqlite3
import types
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from test_llm_credit_worker import Worker

ORDER = "woow_paas_platform_llm_credit_order"
LEDGER = "woow_paas_platform_llm_credit_attempt"
UUID = "00000000-0000-4000-8000-000000000001"
INVALID = Worker._llm_verify_review.__globals__["CREDIT_EVIDENCE_INVALID"]
CONFLICT = Worker._llm_verify_review.__globals__["CREDIT_EVIDENCE_CONFLICT"]
NOW = 100


class Cursor:
    def __init__(self, db):
        self.cursor = db.cursor()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.cursor.close()

    def execute(self, sql, params=(), **_kwargs):
        self.cursor.execute(sql.replace("%s", "?"), params)

    def dictfetchone(self):
        row = self.cursor.fetchone()
        return (
            None
            if row is None
            else dict(zip([d[0] for d in self.cursor.description], row))
        )


class SQLReview(Worker):
    def __init__(self, *, history=True, event=0, ledger_changes=None):
        self.db = sqlite3.connect(":memory:", isolation_level=None)
        self.db.execute(
            f"CREATE TABLE {ORDER} (id INTEGER PRIMARY KEY, account_id INTEGER, attempt INTEGER, state TEXT, convert_event_id INTEGER, newapi_user_id INTEGER, points INTEGER, quota INTEGER, verify_token TEXT, verify_lease_until INTEGER, lease_until INTEGER, next_attempt_at INTEGER, last_error_code TEXT, verify_log_page INTEGER, verify_log_end_at INTEGER, write_uid INTEGER, write_date INTEGER)"
        )
        self.db.execute(
            f"CREATE TABLE {LEDGER} (order_id INTEGER, account_id INTEGER, attempt_no INTEGER, target_user_id INTEGER, quota INTEGER, attempt_uuid TEXT, reservation_event_id INTEGER, dispatch_claimed_at INTEGER)"
        )
        self.db.execute(
            f"INSERT INTO {ORDER} VALUES (1,2,1,?, ?,3,100,1562500,?,200,150,NULL,NULL,0,NULL,1,90)",
            ("unknown", event, "owner"),
        )
        ledger = dict(
            order_id=1,
            account_id=2,
            attempt_no=1,
            target_user_id=3,
            quota=1562500,
            attempt_uuid=UUID,
            reservation_event_id=7,
            dispatch_claimed_at=90,
        )
        ledger.update(ledger_changes or {})
        if history:
            self.db.execute(
                f"INSERT INTO {LEDGER} VALUES (?,?,?,?,?,?,?,?)", tuple(ledger.values())
            )
        self.claim = dict(
            id=1,
            account_id=2,
            attempt=1,
            state="unknown",
            token="owner",
            event_id=event,
            attempt_uuid=UUID if history else None,
            points=100,
        )
        self.alerts = []
        self.incidents = []
        self.rc_intents = 0
        worker = self

        class Env:
            registry = types.SimpleNamespace(cursor=lambda: Cursor(worker.db))

            def __getitem__(self, _name):
                return types.SimpleNamespace(_llm_incident=self.incident)

            def incident(self, *args, **kwargs):
                worker.incidents.append(kwargs)
                return False

        self.env = Env()

    def _llm_credit_alert(self, claim, code):
        self.alerts.append(code)

    def snapshot(self):
        return self.db.execute(f"SELECT * FROM {ORDER}").fetchall(), self.db.execute(
            f"SELECT * FROM {LEDGER}"
        ).fetchall()

    def row(self):
        cur = self.db.execute(f"SELECT * FROM {ORDER}")
        return dict(zip([d[0] for d in cur.description], cur.fetchone()))


class TestInvalidReservationReview(unittest.TestCase):
    @contextmanager
    def worker(self, **kwargs):
        w = SQLReview(**kwargs)

        def begin(env, cr, read_committed=False):
            self.assertIs(env, w.env)
            self.assertTrue(read_committed)
            w.rc_intents += 1

        ns = Worker._llm_verify_review.__globals__
        with patch.dict(
            ns,
            {
                "_begin": begin,
                "fields": types.SimpleNamespace(
                    Datetime=types.SimpleNamespace(now=lambda: NOW)
                ),
                "psycopg2": types.SimpleNamespace(
                    errors=types.SimpleNamespace(
                        UniqueViolation=type("UniqueViolation", (Exception,), {})
                    )
                ),
                "LlmIncident": types.SimpleNamespace(CREDIT_UNKNOWN="credit_unknown"),
            },
        ):
            try:
                yield w
            finally:
                w.db.close()

    def test_invalid_reservation_review_is_review_only(self):
        with self.worker() as w:
            ledger = w.snapshot()[1]
            self.assertTrue(w._llm_verify_claimed(w.claim, {}, 0))
            row = w.row()
            self.assertEqual(row["state"], "needs_review")
            self.assertEqual(row["last_error_code"], INVALID)
            self.assertIsNone(row["verify_token"])
            self.assertIsNone(row["verify_lease_until"])
            self.assertIsNone(row["lease_until"])
            self.assertIsNone(row["next_attempt_at"])
            self.assertEqual(row["convert_event_id"], 0)
            self.assertEqual(w.snapshot()[1], ledger)
            self.assertEqual(w.alerts, [INVALID])
            self.assertEqual(len(w.incidents), 1)

    def test_none_event_also_uses_exact_scalar_cas(self):
        with self.worker(event=None) as w:
            self.assertTrue(w._llm_verify_review(w.claim, INVALID))
            self.assertEqual(w.row()["state"], "needs_review")
            self.assertIsNone(w.row()["convert_event_id"])

    def test_wrong_reason_and_valid_or_malformed_event_never_use_special_update(self):
        for key, value in [
            ("reason", CONFLICT),
            ("event_id", 7),
            ("event_id", False),
            ("event_id", "0"),
            ("event_id", 0.0),
        ]:
            with self.subTest(key=key, value=value), self.worker() as w:
                claim = copy.deepcopy(w.claim)
                reason = INVALID
                if key == "reason":
                    reason = value
                else:
                    claim[key] = value
                before = w.snapshot()
                self.assertFalse(
                    w._llm_verify_invalid_reservation_review(claim, reason)
                )
                self.assertEqual(w.snapshot(), before)

    def test_stale_owner_claims_do_not_change_any_row_or_ledger(self):
        for key, value in [
            ("id", 9),
            ("account_id", 9),
            ("attempt", 2),
            ("state", "sending"),
            ("state", "paid"),
            ("token", "stale"),
            ("event_id", None),
            ("event_id", -1),
            ("attempt_uuid", None),
            ("attempt_uuid", "00000000-0000-4000-8000-000000000002"),
        ]:
            with self.subTest(key=key), self.worker() as w:
                bad = dict(w.claim, **{key: value})
                before = w.snapshot()
                self.assertFalse(w._llm_verify_invalid_reservation_review(bad, INVALID))
                self.assertEqual(w.snapshot(), before)
                self.assertFalse(w.alerts)
                self.assertFalse(w.incidents)

    def test_expired_verifier_and_terminal_state_are_fenced(self):
        for column, value in [
            ("verify_lease_until", NOW),
            ("verify_lease_until", None),
            ("state", "credited"),
        ]:
            with self.subTest(column=column, value=value), self.worker() as w:
                w.db.execute(f"UPDATE {ORDER} SET {column}=?", (value,))
                if column == "state":
                    w.claim["state"] = value
                before = w.snapshot()
                self.assertFalse(
                    w._llm_verify_invalid_reservation_review(w.claim, INVALID)
                )
                self.assertEqual(w.snapshot(), before)

    def test_bad_ledger_cannot_even_authorize_special_review(self):
        for changes in [
            dict(order_id=9),
            dict(account_id=9),
            dict(attempt_no=2),
            dict(target_user_id=9),
            dict(quota=9),
            dict(reservation_event_id=0),
            dict(attempt_uuid="00000000-0000-4000-8000-000000000002"),
        ]:
            with (
                self.subTest(changes=changes),
                self.worker(ledger_changes=changes) as w,
            ):
                before = w.snapshot()
                self.assertFalse(
                    w._llm_verify_invalid_reservation_review(w.claim, INVALID)
                )
                self.assertEqual(w.snapshot(), before)

    def test_no_history_does_not_authorize_a_forged_durable_uuid(self):
        with self.worker(history=False) as w:
            w.claim["attempt_uuid"] = UUID
            self.assertFalse(w._llm_verify_review(w.claim, INVALID))
            self.assertEqual(w.row()["state"], "unknown")
        with self.worker(history=False) as w:
            self.assertTrue(w._llm_verify_review(w.claim, INVALID))
            self.assertEqual(w.row()["state"], "needs_review")

    def test_credit_retry_and_other_error_review_keep_strict_owner_guard(self):
        with self.worker() as w:
            before = w.snapshot()
            for values in (
                {"state": "credited"},
                {"state": "paid"},
                {"state": "needs_review"},
            ):
                self.assertFalse(w._llm_verify_update(w.claim, values))
                self.assertEqual(w.snapshot(), before)
            self.assertFalse(w._llm_verify_finish(w.claim, "paid"))
            self.assertFalse(w._llm_verify_review(w.claim, CONFLICT))
            self.assertEqual(w.snapshot(), before)
            self.assertFalse(w.alerts)
            self.assertFalse(w.incidents)

    def test_sql_state_whitelist_is_not_just_a_caller_side_check(self):
        class MisleadingClaim(dict):
            def get(self, key, default=None):
                return "unknown" if key == "state" else super().get(key, default)

        with self.worker() as w:
            w.db.execute(f"UPDATE {ORDER} SET state='credited'")
            bad = MisleadingClaim(w.claim, state="credited")
            before = w.snapshot()
            self.assertFalse(w._llm_verify_invalid_reservation_review(bad, INVALID))
            self.assertEqual(w.snapshot(), before)

    def test_negative_event_scalar_can_only_quarantine(self):
        with self.worker(event=-1) as w:
            ledger = w.snapshot()[1]
            self.assertTrue(w._llm_verify_review(w.claim, INVALID))
            self.assertEqual(w.row()["state"], "needs_review")
            self.assertEqual(w.row()["convert_event_id"], -1)
            self.assertEqual(w.snapshot()[1], ledger)

    def test_review_is_single_transition_late_claim_cannot_write_again(self):
        with self.worker() as w:
            self.assertTrue(w._llm_verify_review(w.claim, INVALID))
            before = w.snapshot()
            self.assertFalse(w._llm_verify_review(w.claim, INVALID))
            self.assertFalse(w._llm_verify_update(w.claim, {"state": "credited"}))
            self.assertEqual(w.snapshot(), before)
            self.assertEqual(len(w.alerts), 1)


if __name__ == "__main__":
    unittest.main()
