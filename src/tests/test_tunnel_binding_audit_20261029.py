"""HA issue #38, point 2: the binding history (who bound, released, was refused).

The binding keeps only the CURRENT holder, so "what happened to this binding" had no answer
except the server log. Every state change and every refusal now leaves a line in
`woow_paas_platform.tunnel_binding_event`, for Smart Home and Security Access alike (mirror
endpoints). Pinned here: the vocabulary of events, that repeats and renames stay silent, that a
retrying refused installation does not flood the table, who sits behind a release, that the
history is for system administrators only, and that a failing audit line never fails the
operation it describes (while Odoo's read-only and serialization retries still get through).
"""
import uuid
from datetime import timedelta
from unittest.mock import patch

from psycopg2.errors import ReadOnlySqlTransaction

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests.common import HttpCase, TransactionCase, tagged

from odoo.addons.woow_paas_platform.models.tunnel_binding_mixin import (
    TunnelBindingConflict,
)

from .test_tunnel_binding_1766 import _SCOPES, UID_A, UID_B, _BindingFixture

EVENT = 'woow_paas_platform.tunnel_binding_event'
UID_C = 'c' * 32


class _History:
    def lines(self, record):
        return self.env[EVENT].sudo().search(
            [('anchor_model', '=', record._name), ('anchor_id', '=', record.id)], order='id')

    def names(self, record):
        return self.lines(record).mapped('event')


@tagged('post_install', '-at_install')
class TestTunnelBindingHistory(_BindingFixture, _History, TransactionCase):

    def setUp(self):
        super().setUp()
        self._setup_fixture()

    def _records(self):
        return (('sh', self.home), ('sa', self.sa))

    def test_first_claim_is_recorded_and_repeats_and_renames_are_silent(self):
        for label, record in self._records():
            with self.subTest(label):
                record._tunnel_binding_claim(UID_A, 'living room')
                line = self.lines(record)
                self.assertEqual(line.mapped('event'), ['claimed'])
                self.assertEqual((line.previous_state, line.new_state), ('unbound', 'claimed'))
                self.assertEqual((line.instance_uid, line.instance_label), (UID_A, 'living room'))
                self.assertEqual(line.actor, 'ha-instance')
                record._tunnel_binding_claim(UID_A, 'living room')       # restart
                record._tunnel_binding_claim(UID_A, 'first floor')       # rename
                self.assertEqual(self.names(record), ['claimed'])

    def test_legacy_binding_lifecycle(self):
        for label, record in self._records():
            with self.subTest(label):
                record._tunnel_binding_claim('', '')
                record._tunnel_binding_claim('', '')                     # an old component restarts
                record._tunnel_binding_claim(UID_A, 'same machine')      # the same machine updated
                self.assertEqual(self.names(record), ['legacy_claimed', 'upgraded'])

    def test_confirmed_new_entry_over_a_legacy_binding_is_a_take_over(self):
        for label, record in self._records():
            with self.subTest(label):
                record._tunnel_binding_claim('', '')
                record._tunnel_binding_claim(UID_B, 'new machine', new_entry=True, confirmed=True)
                self.assertEqual(self.names(record), ['legacy_claimed', 'taken_over'])
                self.assertEqual(self.lines(record)[-1].instance_uid, UID_B)

    def test_refusals_are_recorded_once_per_installation_and_window(self):
        for label, record in self._records():
            with self.subTest(label):
                record._tunnel_binding_claim(UID_A, 'living room')
                for _ in range(3):                                       # a retrying installation
                    with self.assertRaises(TunnelBindingConflict):
                        record._tunnel_binding_claim(UID_B, 'demo machine')
                with self.assertRaises(TunnelBindingConflict):           # a different installation
                    record._tunnel_binding_claim(UID_C, 'third machine')
                with self.assertRaises(TunnelBindingConflict):           # no identity at all
                    record._tunnel_binding_claim('', '')
                lines = self.lines(record)
                self.assertEqual(lines.mapped('event'),
                                 ['claimed', 'refused_claim', 'refused_claim', 'refused_claim'])
                self.assertEqual(lines[1].instance_uid, UID_B)
                self.assertEqual(lines[1].held_by, 'living room')
                self.assertEqual(lines[2].instance_uid, UID_C)
                self.assertIn('without an installation id', lines[3].detail)
                # the binding itself was not touched by any of them
                self.assertEqual(record.bound_instance_uid, UID_A)

    def test_an_unconfirmed_new_entry_over_a_legacy_binding_is_recorded_as_refused(self):
        for label, record in self._records():
            with self.subTest(label):
                record._tunnel_binding_claim('', '')
                with self.assertRaises(TunnelBindingConflict):
                    record._tunnel_binding_claim(UID_B, 'new machine', new_entry=True)
                line = self.lines(record)[-1]
                self.assertEqual(line.event, 'refused_claim')
                self.assertIn('not confirmed', line.detail)

    def test_releases_say_who_released(self):
        for label, record in self._records():
            with self.subTest(label):
                record._tunnel_binding_claim(UID_A, 'living room')
                self.assertEqual(record._tunnel_binding_release(UID_A), (True, ''))
                released = self.lines(record)[-1]
                self.assertEqual((released.event, released.actor, released.instance_uid),
                                 ('released', 'ha-instance', UID_A))
                self.assertEqual((released.previous_state, released.new_state), ('claimed', 'unbound'))
                # a release of something nobody holds is a quiet success
                self.assertEqual(record._tunnel_binding_release(UID_A), (True, ''))
                self.assertEqual(self.names(record), ['claimed', 'released'])
                # an owner / admin releasing it from the portal or the backend
                record._tunnel_binding_claim(UID_B, 'demo machine')
                record.action_release_binding()
                admin_release = self.lines(record)[-1]
                self.assertEqual((admin_release.event, admin_release.actor), ('released', self.env.user.login))
                self.assertEqual(admin_release.held_by, 'demo machine')
                record.action_release_binding()                          # already unbound: nothing to say
                self.assertEqual(self.names(record), ['claimed', 'released', 'claimed', 'released'])

    def test_a_release_by_someone_else_is_refused_and_recorded(self):
        for label, record in self._records():
            with self.subTest(label):
                record._tunnel_binding_claim(UID_A, 'living room')
                self.assertEqual(record._tunnel_binding_release(UID_B), (False, 'living room'))
                line = self.lines(record)[-1]
                self.assertEqual((line.event, line.instance_uid, line.held_by),
                                 ('refused_release', UID_B, 'living room'))
                self.assertEqual(record.binding_state, 'claimed')

    def test_the_history_outlives_the_service(self):
        record = self.sa
        record._tunnel_binding_claim(UID_A, 'living room')
        anchor_id = record.id
        record.sudo().unlink()
        self.assertTrue(self.env[EVENT].sudo().search_count(
            [('anchor_model', '=', 'woow_paas_platform.security_access'), ('anchor_id', '=', anchor_id)]))

    def test_a_failing_audit_line_never_fails_the_operation(self):
        for label, record in self._records():
            with self.subTest(label):
                with patch.object(type(self.env[EVENT]), 'create', side_effect=RuntimeError('disk full')), \
                        self.assertLogs('odoo.addons.woow_paas_platform.models.tunnel_binding_event', level=40):
                    self.assertEqual(record._tunnel_binding_claim(UID_A, 'living room'), 'claimed')
                self.assertEqual(record.bound_instance_uid, UID_A)
                self.assertFalse(self.lines(record))

    def test_odoos_read_only_retry_gets_through_the_audit_line(self):
        """The HA token endpoints start read-only; swallowing this error would break the retry."""
        with patch.object(type(self.env[EVENT]), 'create',
                          side_effect=ReadOnlySqlTransaction('cannot execute INSERT in a read-only transaction')):
            with self.assertRaises(ReadOnlySqlTransaction):
                self.home._tunnel_binding_claim(UID_A, 'living room')

    def test_only_system_administrators_can_read_it_and_nobody_can_write_it(self):
        self.home._tunnel_binding_claim(UID_A, 'living room')
        admin = self.env['res.users'].sudo().create({
            'name': 'Binding auditor', 'login': 'binding_auditor_' + uuid.uuid4().hex[:6],
            'groups_id': [(6, 0, [self.env.ref('base.group_system').id])],
        })
        self.assertTrue(self.env[EVENT].with_user(admin).search([('anchor_id', '=', self.home.id)]))
        with self.assertRaises(AccessError):
            self.env[EVENT].with_user(self.owner).search([])
        line = self.env[EVENT].sudo().search([('anchor_id', '=', self.home.id)], limit=1)
        for action in (lambda: self.env[EVENT].with_user(admin).create({
                'anchor_model': 'x', 'anchor_id': 1, 'event': 'released'}),
                lambda: line.with_user(admin).write({'detail': 'edited'}),
                lambda: line.with_user(admin).unlink()):
            with self.assertRaises(AccessError):
                action()

    def test_the_history_button_opens_the_records_own_history(self):
        action = self.sa.action_open_binding_history()
        self.assertEqual(action['res_model'], EVENT)
        self.assertEqual(action['domain'], [('anchor_model', '=', 'woow_paas_platform.security_access'),
                                            ('anchor_id', '=', self.sa.id)])


@tagged('post_install', '-at_install')
class TestTunnelBindingHistoryEndpoints(_BindingFixture, _History, HttpCase):
    """Real HTTP: a refusal answered with 409 still leaves its line (the read-only first attempt is retried)."""

    def setUp(self):
        super().setUp()
        self._setup_fixture()
        self.client_rec = self.env.ref('woow_paas_platform.oauth_client_ha_smart_home')

    def _get(self, url):
        token = self.env['woow_paas_platform.oauth_token'].sudo().create({
            'access_token': 'ha-audit-' + uuid.uuid4().hex,
            'scope': _SCOPES,
            'expires_at': fields.Datetime.now() + timedelta(hours=1),
            'refresh_expires_at': fields.Datetime.now() + timedelta(days=30),
            'client_id': self.client_rec.id,
            'user_id': self.owner.id,
        })
        return self.url_open(url, headers={'Authorization': 'Bearer ' + token.access_token})

    def test_claim_refusal_and_upgrade_over_http_leave_their_lines(self):
        for label, record, base in (
                ('sh', self.home, '/api/smarthome/homes/%d/tunnel-token' % self.home.id),
                ('sa', self.sa, '/api/security-access/accesses/%d/tunnel-token' % self.sa.id)):
            with self.subTest(label):
                self.assertEqual(self._get(base + '?instance_uid=%s&instance_label=pi' % UID_A).status_code, 200)
                refused = self._get(base + '?instance_uid=%s&instance_label=sho' % UID_B)
                self.assertEqual(refused.status_code, 409, refused.text)
                self.env.invalidate_all()
                lines = self.lines(record)
                self.assertEqual(lines.mapped('event'), ['claimed', 'refused_claim'])
                self.assertEqual((lines[1].instance_uid, lines[1].held_by), (UID_B, 'pi'))
