"""Real PostgreSQL schema locks must not deadlock the module loader."""
import time
from contextlib import contextmanager
from unittest.mock import patch

from odoo import Command, fields
from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase, tagged
from odoo.tools import mute_logger
from psycopg2.errors import LockNotAvailable


@tagged('post_install', '-at_install', 'billing_attachment_install')
class TestBillingAttachmentModuleInstall(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Attachment = self.env['ir.attachment'].sudo()
        partner = self.env['res.partner'].create({
            'name': 'Attachment schema-lock fixture',
        })
        self.activity = self.env['mail.activity'].create({
            'res_model_id': self.env['ir.model']._get('res.partner').id,
            'res_id': partner.id,
            'activity_type_id': self.env.ref('mail.mail_activity_data_todo').id,
            'user_id': self.env.uid,
            'date_deadline': fields.Date.today(),
        })
        self.attachment = self.Attachment.create({
            'name': 'schema-lock.txt',
            'raw': b'ordinary attachment',
            'res_model': 'mail.activity',
            'res_id': self.activity.id,
        })
        self.activity.write({
            'attachment_ids': [Command.link(self.attachment.id)],
        })
        self.env.flush_all()
        self.assertTrue(self.env.registry.ready)
        self.assertFalse(self.env.registry._init)

    def _lock_schema(self):
        self.env.cr.execute("SET LOCAL lock_timeout = '3s'")
        self.env.cr.execute(
            'LOCK TABLE ir_attachment, activity_attachment_rel, '
            'mail_activity, mail_message IN ACCESS EXCLUSIVE MODE')

    @contextmanager
    def _module_loader(self):
        # These server-owned flags are set by Registry.new(), not RPC context.
        with patch.object(self.env.registry, '_init', True), \
                patch.object(self.env.registry, 'ready', False):
            yield

    def test_module_data_create_write_and_read_under_own_schema_lock(self):
        self._lock_schema()
        with self._module_loader():
            image = self.Attachment.with_context(install_mode=True).create({
                'name': 'website-library.txt',
                'raw': b'website module data',
                'public': True,
            })
            image.write({'name': 'website-library-updated.txt'})
            self.assertEqual(image.read(['name'])[0]['name'],
                             'website-library-updated.txt')
            image.unlink()

    def test_module_loader_keeps_uncommitted_activity_provenance(self):
        self._lock_schema()
        with self._module_loader():
            self.assertEqual(
                self.Attachment._activity_m2m_links(self.activity.ids),
                {(self.attachment.id, self.activity.id)},
            )
            self.assertEqual(
                self.Attachment._activity_carrier_attachment_ids(
                    self.activity.ids),
                {self.attachment.id},
            )
            snapshot = self.Attachment._fresh_billing_attachment_provenance(
                self.attachment.ids)
            self.assertIn(
                (self.attachment.id, 'mail.activity', self.activity.id),
                snapshot['attachment_rows'],
            )

    def test_module_loader_still_denies_protected_attachment_mutations(self):
        incident = self.env['woow.billing.incident']._create_test_fixture({
            'external_ref': 'attachment-module-install',
            'kind': 'charged_not_credited',
            'state': 'needs_human',
        })
        # Model pre-installation can contain historical evidence rows.
        self.env.cr.execute(
            'UPDATE ir_attachment SET res_model=%s,res_id=%s WHERE id=%s',
            ('woow.billing.incident', incident.id, self.attachment.id),
        )
        self.attachment.invalidate_recordset()
        self._lock_schema()
        with self._module_loader():
            with self.assertRaises(AccessError):
                self.Attachment.create({
                    'name': 'forbidden.txt',
                    'raw': b'forbidden evidence',
                    'res_model': 'woow.billing.incident',
                    'res_id': incident.id,
                })
            with self.assertRaises(AccessError):
                self.attachment.write({'public': True})
            with self.assertRaises(AccessError):
                self.attachment.unlink()

    @mute_logger('odoo.sql_db')
    def test_forged_install_context_does_not_bypass_live_fresh_read(self):
        self._lock_schema()
        started = time.monotonic()
        with self.assertRaises(LockNotAvailable):
            self.attachment.with_context(
                install_mode=True, _init=True, registry_ready=False,
            ).write({'name': 'must-not-be-written.txt'})
        self.assertLess(time.monotonic() - started, 8)
        self.assertEqual(self.attachment.name, 'schema-lock.txt')
        # The independent failed read must not abort or commit our transaction.
        self.env.cr.execute('SELECT 1')
        self.assertEqual(self.env.cr.fetchone()[0], 1)

    @mute_logger('odoo.sql_db')
    def test_live_activity_visibility_reads_are_bounded_and_fail_closed(self):
        self._lock_schema()
        for read in (
                self.Attachment._activity_m2m_links,
                self.Attachment._activity_carrier_attachment_ids):
            with self.subTest(reader=read.__name__):
                started = time.monotonic()
                with self.assertRaises(LockNotAvailable):
                    read(self.activity.ids)
                self.assertLess(time.monotonic() - started, 8)
        self.env.cr.execute('SELECT 1')
        self.assertEqual(self.env.cr.fetchone()[0], 1)
