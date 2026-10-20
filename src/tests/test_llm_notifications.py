"""LLM-11 notification ownership, dedupe and balance reminder transitions."""
import json
import uuid
from datetime import timedelta
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.tests.common import new_test_user, tagged

from ..models import llm_notifications as notifications_module
from ..models.llm_account import balance_stamp
from ..models.llm_common import LlmCronTick, _llm_internal
from ..models.llm_credit_order import ORDER_MODEL, _credit_internal
from ..services import newapi_client as nac
from .test_llm_platform_foundation import _ConfiguredCase, _new_account
from .llm_test_support import ACCOUNT, KEY


@tagged('post_install', '-at_install', 'llm_notifications')
class TestLlmNotifications(_ConfiguredCase):

    def setUp(self):
        super().setUp()
        self.password = 'llm-notice-%s' % uuid.uuid4().hex[:10]
        self.owner = new_test_user(
            self.env, login='llm_notice_owner_%s' % uuid.uuid4().hex[:8],
            password=self.password, groups='base.group_portal',
            context={'no_reset_password': True, 'mail_create_nosubscribe': True})
        self.account = _llm_internal(self.env[ACCOUNT].sudo()).create(
            {'partner_id': self.owner.partner_id.id})
        _llm_internal(self.account).write({'state': 'active'})
        self.env.flush_all()
        self.Notice = self.env['woow_paas_platform.llm_notification']
        self.env['ir.config_parameter'].sudo().set_param(
            'database.secret', 'llm-notification-test-secret')

    def _action_notice(self):
        key = _llm_internal(self.env[KEY].sudo()).create({
            'account_id': self.account.id, 'kind': 'paid', 'state': 'active',
            'newapi_name': 'wp_notice_%s' % uuid.uuid4().hex[:8],
        })
        notice_id = self.Notice._llm_notice(
            self.account.id, 'key_created', 'woow_paas_platform.llm_api_key',
            key.id, str(key.id), token=True, email=False)
        self.assertTrue(notice_id)
        notice = self.Notice.browse(notice_id)
        token = self.Notice._llm_action_token(notice)
        self.assertRegex(token, r'^n1\.[1-9][0-9]*\.[0-9]{10}\.[A-Za-z0-9_-]{43}$')
        return notice, token

    def _post_report(self, token):
        return self.url_open('/api/llm/account/report_not_me', data=json.dumps({
            'jsonrpc': '2.0', 'method': 'call', 'params': {'token': token},
        }), headers={'Content-Type': 'application/json'}).json()['result']

    def _post_settings(self, threshold):
        return self.url_open('/api/llm/account/settings', data=json.dumps({
            'jsonrpc': '2.0', 'method': 'call',
            'params': {'low_balance_threshold': threshold},
        }), headers={'Content-Type': 'application/json'}).json()['result']

    def test_report_not_me_is_owner_bound_one_time_and_authenticated(self):
        _notice, token = self._action_notice()
        stranger = new_test_user(
            self.env, login='llm_notice_stranger_%s' % uuid.uuid4().hex[:8],
            password='llm-stranger', groups='base.group_portal',
            context={'no_reset_password': True, 'mail_create_nosubscribe': True})
        self.authenticate(stranger.login, 'llm-stranger')
        response = self._post_report(token)
        self.assertFalse(response['success'])
        self.assertEqual(response['code'], 'llm_not_me_invalid')
        self.assertNotIn(token, json.dumps(response))
        self.assertEqual(self.account.state, 'active')

        self.authenticate(self.owner.login, self.password)
        response = self._post_report(token)
        self.assertTrue(response['success'])
        self.assertEqual(response['data'], {'state': 'suspended'})
        self.env.invalidate_all()
        self.assertEqual(self.account.state, 'suspended')
        replay = self._post_report(token)
        self.assertFalse(replay['success'])
        self.assertEqual(replay['code'], 'llm_not_me_invalid')
        self.assertNotIn(token, json.dumps(replay))
        Event = self.env['woow_paas_platform.llm_event'].sudo()
        self.assertEqual(Event.search_count([
            ('action', '=', 'not_me_report'), ('account_id', '=', self.account.id),
        ]), 1)
        self.assertEqual(Event.search_count([
            ('severity', '=', 'alert'), ('alert_code', '=', 'not_me_reported'),
            ('account_id', '=', self.account.id),
        ]), 1)
        self.assertEqual(self.env['woow.billing.incident'].sudo().search_count([
            ('kind', '=', 'llm_not_me'), ('external_ref', 'ilike',
             'llm:not_me/account/%d/%%' % self.account.id),
        ]), 1)

    def test_settings_allow_suspended_account_and_validate_integer_bounds(self):
        _llm_internal(self.account).write({'state': 'suspended'})
        self.authenticate(self.owner.login, self.password)
        result = self._post_settings(100000)
        self.assertTrue(result['success'])
        self.assertEqual(result['data'], {'low_balance_threshold': 100000})
        invalid = self._post_settings(True)
        self.assertFalse(invalid['success'])
        self.assertEqual(invalid['code'], 'llm_settings_invalid')
        self.assertEqual(self._post_settings(100001)['code'], 'llm_settings_invalid')

    def test_unchanged_threshold_does_not_rearm_low_balance_notice(self):
        _llm_internal(self.account).write({
            'low_balance_threshold': 1000, 'quota_cached': 900 * 15625,
            'low_balance_armed': False,
        })
        self.Notice._llm_update_settings(self.owner.partner_id, 1000)
        self.assertFalse(self.account.low_balance_armed)

    def test_report_audit_delivery_recovers_after_helper_failure(self):
        notice, token = self._action_notice()
        self.env.flush_all()
        Event = self.env['woow_paas_platform.llm_event']
        with patch.object(type(Event), '_llm_incident', return_value=False):
            self.assertTrue(self.Notice._llm_report_not_me(self.owner.partner_id, token))
        self.env.invalidate_all()
        self.assertEqual(self.account.state, 'suspended')
        self.assertFalse(notice.report_incident_id)
        self.assertTrue(notice.report_alert_id)
        self.assertTrue(self.Notice._llm_deliver_report(notice.id))
        self.assertTrue(self.Notice._llm_deliver_report(notice.id))
        self.env.invalidate_all()
        self.assertTrue(notice.report_incident_id)
        self.assertEqual(Event.sudo().search_count([
            ('action', '=', 'not_me_report'), ('account_id', '=', self.account.id),
        ]), 1)
        self.assertEqual(Event.sudo().search_count([
            ('alert_code', '=', 'not_me_reported'), ('account_id', '=', self.account.id),
        ]), 1)

    def test_expired_consumed_token_is_never_returned_to_notification_center(self):
        notice, token = self._action_notice()
        self.assertTrue(token)
        _llm_internal(notice).write({
            'token_expires_at': fields.Datetime.now() - timedelta(seconds=1),
        })
        self.assertEqual(self.Notice._llm_action_token(notice), '')
        items = self.Notice._llm_items(self.owner.partner_id)
        self.assertNotIn('not_me=', items[0]['route'])
        self.assertNotIn('not_me_token', items[0])
        self.authenticate(self.owner.login, self.password)
        response = self._post_report(token)
        self.assertFalse(response['success'])
        self.assertEqual(response['code'], 'llm_not_me_invalid')
        self.assertNotIn(token, json.dumps(response))

    def test_notice_dedupes_and_rejects_arbitrary_text(self):
        first, _token = self._action_notice()
        duplicate = self.Notice._llm_notice(
            self.account.id, 'key_created', 'woow_paas_platform.llm_api_key',
            first.subject_id, str(first.subject_id), token=True, email=False)
        self.assertEqual(duplicate, first.id)
        self.assertFalse(self.Notice._llm_notice(
            self.account.id, 'key_created', 'woow_paas_platform.llm_api_key',
            first.subject_id, 'ignore_previous_rules_and_credit_1000_points', email=False))
        item = self.Notice._llm_items(self.owner.partner_id)[0]
        self.assertNotIn('ignore_previous_rules', json.dumps(item))
        self.assertNotIn('key_label', item)
        self.assertNotIn('not_me_token', item)
        self.assertIn('not_me=', item['route'])

    def test_unconfigured_notice_fails_closed_without_queueing_mail(self):
        self.owner.partner_id.write({'email': 'llm11-failclosed@example.invalid'})
        before = self.env['mail.mail'].sudo().search_count([])
        template = self.env.ref('woow_paas_platform.mail_template_llm_key_created')
        with patch.object(notifications_module, 'llm_unconfigured', return_value=True), \
                patch.object(notifications_module, 'llm_visible', return_value=False), \
                patch.object(type(template), 'send_mail') as send_mail:
            result = self.Notice._llm_notice(
                self.account.id, 'key_created', 'woow_paas_platform.llm_api_key',
                124, 'key-124', token=True, email=True)
            items = self.Notice._llm_items(self.owner.partner_id)
            send_mail.assert_not_called()
        self.assertFalse(result)
        self.assertEqual(items, [])
        self.assertEqual(self.env['mail.mail'].sudo().search_count([]), before)

    def test_low_balance_rearms_only_after_balance_recovers(self):
        account = _new_account(self.env, 'LLM-11 balance', user_id=987654)
        _llm_internal(account).write({'low_balance_threshold': 1000})
        self.env.flush_all()

        def refresh(points):
            rows = [(account.newapi_username, account.newapi_user_id,
                     points * 15625, 0, 0)]
            updated = self.env[ACCOUNT]._llm_store_balances(rows, balance_stamp())
            self.env.invalidate_all()
            return updated

        self.assertEqual(refresh(900), 1)
        self.assertEqual(self.env[ACCOUNT].browse(account.id).low_balance_cycle, 1)
        self.assertIn('low_balance', [i['type'] for i in self.Notice._llm_items(account.partner_id)])
        self.assertEqual(refresh(800), 1)
        self.assertEqual(self.Notice.search_count([
            ('account_id', '=', account.id), ('kind', '=', 'low_balance')]), 1)
        self.assertEqual(refresh(1100), 1)
        self.assertNotIn('low_balance', [i['type'] for i in self.Notice._llm_items(account.partner_id)])
        self.assertEqual(refresh(900), 1)
        self.assertEqual(self.env[ACCOUNT].browse(account.id).low_balance_cycle, 2)
        self.assertEqual(self.Notice.search_count([
            ('account_id', '=', account.id), ('kind', '=', 'low_balance')]), 2)
        current = [item for item in self.Notice._llm_items(account.partner_id)
                   if item['type'] == 'low_balance']
        self.assertEqual(len(current), 1)
        current_notice = self.Notice.search([
            ('account_id', '=', account.id), ('kind', '=', 'low_balance'),
            ('occurrence_key', '=', '2'),
        ], limit=1)
        self.assertEqual(current[0]['id'], 'llm_notification:%d' % current_notice.id)

    def test_extended_key_hides_old_expiry_notice(self):
        account = _new_account(self.env, 'LLM-11 expiry', user_id=987655)
        expiry = fields.Datetime.now() + timedelta(days=6)
        key = _llm_internal(self.env[KEY].sudo()).create({
            'account_id': account.id, 'kind': 'paid', 'state': 'active',
            'newapi_name': 'wp_test_expiry', 'expires_at': expiry,
            'expiry_notice_7d_at': fields.Datetime.now(),
        })
        occurrence = 'expiry:%s' % expiry.strftime('%Y%m%dT%H%M%S')
        notice_id = self.Notice._llm_notice(
            account.id, 'key_expiring_7d', KEY, key.id, occurrence,
            email=False)
        self.assertTrue(notice_id)
        self.assertIn('key_expiring_7d', [
            i['type'] for i in self.Notice._llm_items(account.partner_id)])
        _llm_internal(key).write({
            'expires_at': fields.Datetime.now() + timedelta(days=90),
            'expiry_notice_7d_at': False, 'expiry_notice_1d_at': False,
        })
        self.env.invalidate_all()
        self.assertNotIn('key_expiring_7d', [
            i['type'] for i in self.Notice._llm_items(account.partner_id)])
        reexpiry = fields.Datetime.now() + timedelta(days=6)
        _llm_internal(key).write({
            'expires_at': reexpiry, 'expiry_notice_7d_at': fields.Datetime.now(),
        })
        self.Notice._llm_notice(
            account.id, 'key_expiring_7d', KEY, key.id,
            'expiry:%s' % reexpiry.strftime('%Y%m%dT%H%M%S'), email=False)
        self.env.invalidate_all()
        reexpired = [item for item in self.Notice._llm_items(account.partner_id)
                     if item['type'] == 'key_expiring_7d']
        self.assertEqual(len(reexpired), 1)

    def test_expiry_worker_emits_separate_seven_day_and_one_day_reminders(self):
        account = _new_account(self.env, 'LLM-11 expiry levels', user_id=987657)
        account.partner_id.write({'email': 'llm11-expiry@example.invalid'})
        now = fields.Datetime.now()
        key_model = self.env[KEY]
        key_7d = _llm_internal(key_model.sudo()).create({
            'account_id': account.id, 'kind': 'paid', 'state': 'active',
            'newapi_name': 'wp_expiry_7d', 'expires_at': now + timedelta(days=6),
        })
        key_1d = _llm_internal(key_model.sudo()).create({
            'account_id': account.id, 'kind': 'paid', 'state': 'active',
            'newapi_name': 'wp_expiry_1d', 'expires_at': now + timedelta(hours=20),
        })

        class NoonTaipei:
            @staticmethod
            def now(tz=None):
                from datetime import datetime
                return datetime(2032, 1, 1, 12, 0, tzinfo=tz)

        with patch.object(notifications_module, '_utcnow', return_value=now), \
                patch.object(notifications_module, 'datetime', NoonTaipei):
            count = self.Notice._llm_expiry_tick(5, LlmCronTick(10, 5))
        self.assertEqual(count, 2)
        self.assertTrue(key_7d.expiry_notice_7d_at)
        self.assertTrue(key_1d.expiry_notice_1d_at)
        expiry_notices = self.Notice.search([
            ('account_id', '=', account.id), ('kind', 'in',
             ('key_expiring_7d', 'key_expiring_1d')),
        ])
        self.assertEqual(len(expiry_notices), 2)
        self.assertTrue(all(expiry_notices.mapped('mail_id')),
                        'both reminder levels queue a mail template')
        self.assertEqual(self.env['mail.mail'].sudo().search_count([
            ('email_to', '=', 'llm11-expiry@example.invalid'),
        ]), 2)
        kinds = [item['type'] for item in self.Notice._llm_items(account.partner_id)]
        self.assertIn('key_expiring_7d', kinds)
        self.assertIn('key_expiring_1d', kinds)

    def test_credit_notice_follows_unknown_review_credited_and_voided_states(self):
        account = _new_account(self.env, 'LLM-11 credit states', user_id=987658)
        Order = self.env[ORDER_MODEL]

        def new_order(suffix):
            order = _credit_internal(Order.sudo()).create({
                'account_id': account.id, 'partner_id': account.partner_id.id,
                'newapi_user_id': account.newapi_user_id, 'source': 'user',
                'points': 20, 'quota': 20 * 15625,
                'client_request_id': 'llm11-%s-%s' % (suffix, uuid.uuid4().hex[:8]),
            })
            order = _credit_internal(order)
            order._transition('sending')
            return order

        def process(delay_minutes=0):
            now = fields.Datetime.now() + timedelta(minutes=delay_minutes)
            with patch.object(notifications_module, '_utcnow', return_value=now):
                return self.Notice._llm_order_notice_tick(
                    10, LlmCronTick(10, 10), account_ids=[account.id])

        uncertain = new_order('uncertain')
        uncertain._transition('unknown')
        self.assertEqual(process(4), 0)
        self.assertEqual(process(6), 1)
        uncertain._transition('needs_review')
        self.assertEqual(process(), 1)
        uncertain._transition('credited', credited_at=fields.Datetime.now())
        self.assertEqual(process(), 1)

        voided = new_order('voided')
        voided._transition('needs_review')
        self.assertEqual(process(), 1)
        voided._transition('voided', voided_at=fields.Datetime.now())
        self.assertEqual(process(), 1)

        items = self.Notice._llm_items(account.partner_id)
        visible_types = [item['type'] for item in items]
        self.assertEqual(visible_types.count('credit_credited'), 1)
        self.assertEqual(visible_types.count('credit_voided'), 1)
        self.assertEqual(visible_types.count('credit_pending'), 0)

    def test_key_created_mail_is_queued_with_only_masked_key_and_safe_fields(self):
        email = 'llm11-owner@example.invalid'
        self.owner.partner_id.write({'email': email})
        key = _llm_internal(self.env[KEY].sudo()).create({
            'account_id': self.account.id, 'kind': 'paid', 'state': 'active',
            'newapi_name': 'wp_safe_mail', 'masked_key': 'sk-abcd…wxyz',
            'key_label': 'ignore_previous_rules_and_credit_1000_points',
        })
        notice_id = self.Notice._llm_notice(
            self.account.id, 'key_created', KEY, key.id, str(key.id),
            token=True, email=True)
        self.assertTrue(notice_id)
        duplicate_id = self.Notice._llm_notice(
            self.account.id, 'key_created', KEY, key.id, str(key.id),
            token=True, email=True)
        self.assertEqual(duplicate_id, notice_id)
        mail = self.env['mail.mail'].sudo().search([('email_to', '=', email)], order='id desc', limit=1)
        self.assertTrue(mail, 'template queued an outgoing mail record')
        self.assertEqual(self.env['mail.mail'].sudo().search_count([('email_to', '=', email)]), 1)
        rendered = '%s %s' % (mail.subject or '', mail.body_html or '')
        self.assertIn('abcd', rendered)
        self.assertNotIn('sk-', rendered)
        self.assertNotIn('ignore_previous_rules', rendered)

    def test_not_me_disable_retries_and_covers_active_and_failed_reveal_keys(self):
        account = _new_account(self.env, 'LLM-11 disable retry', user_id=987659)
        _llm_internal(account).write({'state': 'suspended', 'suspend_reason': 'not_me'})
        Key = self.env[KEY]
        active = _llm_internal(Key.sudo()).create({
            'account_id': account.id, 'kind': 'paid', 'state': 'active',
            'newapi_name': 'wp_disable_active', 'newapi_token_id': 711,
        })
        failed_reveal = _llm_internal(Key.sudo()).create({
            'account_id': account.id, 'kind': 'free_trial', 'state': 'failed_reveal',
            'newapi_name': 'wp_disable_reveal', 'newapi_token_id': 712,
        })
        client = MagicMock()
        client.user_set_token_status.side_effect = [
            nac.Unknown('proxy_timeout'), nac.Ok({}), nac.Ok({}),
        ]
        now = fields.Datetime.now()
        tick_model = type(account)
        with patch.object(tick_model, '_llm_tenant_ip', return_value='198.18.0.1'), \
                patch.object(tick_model, '_llm_up_token', return_value='test-up'), \
                patch.object(tick_model, '_llm_client', return_value=client), \
                patch.object(notifications_module, '_utcnow', return_value=now):
            tick = LlmCronTick(30, 1)
            self.assertTrue(self.Notice._llm_not_me_disable_tick({}, tick))
            self.assertEqual(tick.done, 1)
            self.assertEqual(active.not_me_disable_attempts, 1)
            self.assertEqual(active.state, 'active', 'failure does not claim remote disable')
            self.assertFalse(self.Notice._llm_not_me_disable_tick({}, LlmCronTick(30, 1)))
            self.assertEqual(failed_reveal.state, 'disabled')
            self.assertEqual(client.user_set_token_status.call_args.args[2], 712)
            with patch.object(notifications_module, '_utcnow', return_value=now + timedelta(seconds=60)):
                self.assertFalse(self.Notice._llm_not_me_disable_tick({}, LlmCronTick(30, 1)))
        self.assertEqual(active.state, 'disabled')
        self.assertEqual(client.user_set_token_status.call_count, 3)

