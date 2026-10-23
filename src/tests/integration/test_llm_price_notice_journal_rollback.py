"""真 PostgreSQL 回歸：notice journal 回執須活過呼叫端交易 rollback。

Journal schema 目前仍是提案，沒有 migration；本測試只在測試資料庫建立，
並在清理時移除。Journal 以自己的 Registry cursor 提交 attempt/binding，
呼叫端的 Odoo 設定寫入則整筆 rollback，再由第三條交易讀回 journal。
acceptance 由測試直接合成呼叫，僅驗持久化，不證明真 SMTP 接受。
"""
import uuid

import odoo
from odoo import api
from odoo.modules.registry import Registry
from odoo.tests.common import BaseCase, get_db_name, tagged

from ...services.llm_price_notice_odoo_adapter import (
    BINDING_SCHEMA,
    MailBinding,
    OdooJournal,
    RegistryConnectionFactory,
)
from ...services.llm_price_notice_transport import Attempt, SCHEMA

_TABLES = (
    'llm_notice_odoo_mail_binding',
    'llm_notice_transport_acceptance',
    'llm_notice_transport_attempt',
)


@tagged('-standard', '-at_install', 'post_install', 'llm_price_notice_journal_rollback')
class TestLlmPriceNoticeJournalRealRollback(BaseCase):
    def setUp(self):
        super().setUp()
        self.registry = Registry(get_db_name())
        self.attempt_uuid = str(uuid.uuid4())
        self.config_key = 'test.llm_price_notice.rollback.' + self.attempt_uuid
        self._lock_cursor = self.registry.cursor()
        self._lock_cursor.execute(
            "SELECT pg_advisory_lock(hashtext('llm_price_notice_journal_rollback_test'))")
        self._lock_cursor.fetchone()
        self.addCleanup(self._release_schema_lock)
        with self.registry.cursor() as cr:
            # Never drop tables owned by a later installed migration or another
            # test run. This probe is valid only on an isolated scratch DB.
            for table in _TABLES:
                cr.execute('SELECT to_regclass(%s)', (table,))
                self.assertIsNone(cr.fetchone()[0], 'journal table already exists: ' + table)
            for statement in (*SCHEMA, BINDING_SCHEMA):
                cr.execute(statement)
        self.addCleanup(self._purge_journal_schema)

    def _release_schema_lock(self):
        try:
            self._lock_cursor.execute(
                "SELECT pg_advisory_unlock(hashtext('llm_price_notice_journal_rollback_test'))")
            self._lock_cursor.fetchone()
        finally:
            self._lock_cursor.close()

    def _purge_journal_schema(self):
        try:
            with self.registry.cursor() as cr:
                cr.execute('DELETE FROM llm_notice_odoo_mail_binding WHERE attempt_uuid = %s',
                           (self.attempt_uuid,))
                cr.execute('DELETE FROM llm_notice_transport_acceptance WHERE attempt_uuid = %s',
                           (self.attempt_uuid,))
                cr.execute('DELETE FROM llm_notice_transport_attempt WHERE attempt_uuid = %s',
                           (self.attempt_uuid,))
                for table in _TABLES:
                    cr.execute('DROP TABLE IF EXISTS %s' % table)
        finally:
            self._lock_cursor.commit()

    def test_committed_journal_receipt_survives_outer_odoo_rollback(self):
        attempt = Attempt(
            self.attempt_uuid, 'a' * 64, 'stg', 'paas:synthetic',
            'recipient@example.test', 1, 'b' * 64)
        binding = MailBinding(attempt, get_db_name(), 987654321, 876543210)

        # Transaction 1: create the proposed schema, durably, for this test DB.
        # Transaction 2: journal writes commit independently of the caller.
        outer = self.registry.cursor()
        try:
            env = api.Environment(outer, odoo.SUPERUSER_ID, {})
            # Hold an uncommitted Odoo write before the independent journal
            # commit; otherwise this only proves the easy idle-caller case.
            env['ir.config_parameter'].set_param(self.config_key, 'must-roll-back')
            journal = OdooJournal(RegistryConnectionFactory(self.registry, outer))
            journal.prepare(attempt)
            journal.bind(binding)
            self.assertEqual(journal.ready_binding(self.attempt_uuid), binding)
            # Synthetic transport observation: this proves PG receipt durability,
            # not a real SMTP server's acceptance or permission to dispatch.
            journal._claim(self.attempt_uuid)
            journal._accept(self.attempt_uuid)
            outer.rollback()
        finally:
            outer.close()

        # Transaction 3: a fresh connection proves both sides of the boundary.
        with self.registry.cursor() as verify:
            verify.execute('SELECT value FROM ir_config_parameter WHERE key = %s',
                           (self.config_key,))
            self.assertIsNone(verify.fetchone())
            journal = OdooJournal(RegistryConnectionFactory(self.registry, verify))
            self.assertEqual(journal.attempt(self.attempt_uuid), attempt)
            verify.execute('SELECT database_name, mail_id, obligation_id '
                           'FROM llm_notice_odoo_mail_binding WHERE attempt_uuid = %s',
                           (self.attempt_uuid,))
            self.assertEqual(verify.fetchone(),
                             (binding.database_name, binding.mail_id, binding.obligation_id))
            verify.execute('SELECT status FROM llm_notice_transport_attempt '
                           'WHERE attempt_uuid = %s', (self.attempt_uuid,))
            self.assertEqual(verify.fetchone()[0], 'accepted')
            verify.execute('SELECT accepted_at_us, data_code '
                           'FROM llm_notice_transport_acceptance WHERE attempt_uuid = %s',
                           (self.attempt_uuid,))
            accepted_at_us, data_code = verify.fetchone()
            self.assertGreater(accepted_at_us, 0)
            self.assertEqual(data_code, 250)
