"""Durable, tenant-bound notifications and one-time account action tokens."""
import base64
import hashlib
import hmac
import logging
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from odoo import SUPERUSER_ID, _, api, fields, models
from odoo.exceptions import AccessError

from .billing_incident import _is_odoo_module_uninstall
from .llm_common import (
    LlmQuotaInteger, _is_llm_internal, _llm_internal, _without_llm_internal, llm_unconfigured, llm_visible,
    run_llm_cron,
)
from .llm_event import LlmAlert, LlmIncident
from ..services import newapi_client as nac

_logger = logging.getLogger(__name__)
MODEL = 'woow_paas_platform.llm_notification'
TABLE = 'woow_paas_platform_llm_notification'
_TOKEN_RE = re.compile(r'n1\.([1-9][0-9]{0,9})\.([0-9]{10})\.([A-Za-z0-9_-]{43})\Z')
_NOTICE_KINDS = (
    ('low_balance', 'Low balance'), ('key_expiring_7d', 'Key expiring'),
    ('key_expiring_1d', 'Key expiring'), ('key_created', 'Key created'),
    ('credit_converted', 'Credit converted'), ('credit_credited', 'Credit credited'),
    ('credit_pending', 'Credit pending'), ('credit_voided', 'Credit voided'),
)
_TEMPLATES = {
    'low_balance': 'mail_template_llm_low_balance',
    'key_expiring_7d': 'mail_template_llm_key_expiring',
    'key_expiring_1d': 'mail_template_llm_key_expiring',
    'key_created': 'mail_template_llm_key_created',
    'credit_converted': 'mail_template_llm_credit_converted',
    'credit_pending': 'mail_template_llm_credit_pending',
    'credit_voided': 'mail_template_llm_credit_voided',
}
_ORDER_NOTICE_STATES = frozenset(('unknown', 'needs_review', 'credited', 'voided'))


def _utcnow():
    return fields.Datetime.now()


def _display_time(value):
    return value.replace(tzinfo=timezone.utc).astimezone(
        ZoneInfo('Asia/Taipei')).isoformat(sep=' ', timespec='seconds')


class LlmNotification(models.Model):
    _name = 'woow_paas_platform.llm_notification'
    _description = 'AI Model Notification'
    _order = 'create_date desc, id desc'

    account_id = fields.Many2one('woow_paas_platform.llm_account', required=True,
                                 index=True, ondelete='cascade', readonly=True)
    partner_id = fields.Many2one('res.partner', required=True, index=True,
                                 ondelete='cascade', readonly=True)
    kind = fields.Selection(_NOTICE_KINDS, required=True, index=True, readonly=True)
    subject_model = fields.Char(required=True, readonly=True)
    subject_id = fields.Integer(required=True, readonly=True)
    occurrence_key = fields.Char(required=True, readonly=True)
    severity = fields.Selection([('info', 'Info'), ('warning', 'Warning')],
                                required=True, readonly=True)
    number_value = LlmQuotaInteger(readonly=True)
    token_expires_at = fields.Datetime(readonly=True, groups='base.group_system')
    consumed_at = fields.Datetime(readonly=True, groups='base.group_system')
    report_incident_id = fields.Integer(readonly=True, groups='base.group_system')
    report_alert_id = fields.Integer(readonly=True, groups='base.group_system')
    mail_id = fields.Many2one('mail.mail', readonly=True, ondelete='set null',
                              groups='base.group_system')

    _sql_constraints = [
        ('notice_dedupe', 'UNIQUE(kind, subject_model, subject_id, occurrence_key)',
         'This AI model notification already exists.'),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        if not _is_llm_internal(self.env):
            raise AccessError(_('AI model notifications can only be created by the platform.'))
        return _without_llm_internal(super().create(vals_list))

    def write(self, vals):
        if not _is_llm_internal(self.env):
            raise AccessError(_('AI model notifications can only be changed by the platform.'))
        return super().write(vals)

    def unlink(self):
        if not _is_odoo_module_uninstall(self.env):
            raise AccessError(_('AI model notifications cannot be deleted.'))
        return super().unlink()

    @api.model
    def _llm_notice(self, account_id, kind, subject_model, subject_id, occurrence_key,
                    *, number_value=None, email=True, token=False):
        """Atomically dedupe, persist the notice, action expiry and queued mail.

        Call only after the account/subject is committed. The unique constraint is the
        final race guard; duplicate calls return the existing notification.
        """
        if llm_unconfigured(self.env):
            return False
        expected_model = ('woow_paas_platform.llm_account' if kind == 'low_balance' else
                          'woow_paas_platform.llm_api_key' if kind.startswith('key_') else
                          'woow_paas_platform.llm_credit_order' if kind.startswith('credit_') else '')
        if (kind not in dict(_NOTICE_KINDS) or subject_model != expected_model
                or type(subject_id) is not int or not 0 < subject_id <= 2147483647
                or not isinstance(occurrence_key, str)
                or (number_value is not None and
                    (type(number_value) is not int or abs(number_value) > 2**63 - 1))
                or type(email) is not bool or type(token) is not bool):
            return False
        if kind == 'low_balance':
            if not re.fullmatch(r'[1-9][0-9]{0,9}', occurrence_key):
                return False
        elif kind.startswith('key_expiring_'):
            if not re.fullmatch(r'expiry:[0-9]{8}T[0-9]{6}', occurrence_key):
                return False
        elif kind in ('key_created', 'credit_converted'):
            if occurrence_key != str(subject_id):
                return False
        elif occurrence_key not in _ORDER_NOTICE_STATES or not kind.startswith('credit_'):
            return False
        if token and kind not in ('key_created', 'credit_converted'):
            return False
        account = self.env['woow_paas_platform.llm_account'].sudo().browse(account_id).exists()
        if not account or account.state == 'closed':
            return False
        if kind == 'low_balance' and account.quota_cached >= account.low_balance_threshold * 15625:
            return False
        if subject_model == 'woow_paas_platform.llm_api_key':
            subject = self.env[subject_model].sudo().browse(subject_id).exists()
            if not subject or subject.account_id.id != account.id:
                return False
        elif subject_model == 'woow_paas_platform.llm_credit_order':
            subject = self.env[subject_model].sudo().browse(subject_id).exists()
            if (not subject or subject.account_id.id != account.id
                    or (kind in ('credit_pending', 'credit_credited', 'credit_voided')
                        and subject.state != occurrence_key)):
                return False
        elif subject_id != account.id:
            return False
        dedupe = [('kind', '=', kind), ('subject_model', '=', subject_model),
                  ('subject_id', '=', subject_id), ('occurrence_key', '=', occurrence_key)]
        existing = self.sudo().search(dedupe, limit=1)
        if existing:
            return existing.id
        token_expiry = _utcnow() + timedelta(hours=24) if token else False
        # The notice ID is included in the token, so create first in a savepoint and
        # then bind its digest. Both the notice and mail queue commit together.
        try:
            with self.env.cr.savepoint():
                notice = _llm_internal(self.sudo()).create({
                    'account_id': account.id, 'partner_id': account.partner_id.id,
                    'kind': kind, 'subject_model': subject_model, 'subject_id': subject_id,
                    'occurrence_key': str(occurrence_key),
                    'severity': 'warning' if kind in ('low_balance', 'key_expiring_7d',
                                                      'key_expiring_1d', 'credit_pending') else 'info',
                    'number_value': number_value,
                })
                if token_expiry:
                    _llm_internal(notice).write({'token_expires_at': token_expiry})
                if email and account.partner_id.email and kind in _TEMPLATES:
                    template = self.env.ref('woow_paas_platform.' + _TEMPLATES[kind],
                                             raise_if_not_found=False)
                    if not template:
                        raise RuntimeError('mail template unavailable')
                    if template:
                        base = self.env['ir.config_parameter'].sudo().get_param('web.base.url') or ''
                        token_text = self._llm_action_token(notice) if token_expiry else ''
                        url = ('%s/woow#/llm/overview?not_me=%s' % (base.rstrip('/'), token_text)
                               if token_text else '')
                        safe_ctx = {'llm_not_me_url': url, 'llm_notice_id': notice.id,
                                    'llm_subject_id': subject_id,
                                    'llm_number_value': number_value or 0,
                                    'llm_notice_time': _display_time(_utcnow()),
                                    'llm_expiry_url': '', 'llm_expiry_time': '',
                                    'llm_credit_url': '%s/woow#/llm/credit' % base.rstrip('/')}
                        if subject_model == 'woow_paas_platform.llm_api_key':
                            key = self.env[subject_model].sudo().browse(subject_id).exists()
                            safe_ctx['llm_masked_key'] = (
                                (key.masked_key or '').removeprefix('sk-') if key else '')
                            if key and kind == 'key_created' and key.create_date:
                                safe_ctx['llm_notice_time'] = _display_time(key.create_date)
                            if key and key.expires_at:
                                safe_ctx['llm_expiry_time'] = _display_time(key.expires_at)
                                safe_ctx['llm_expiry_url'] = (
                                    '%s/woow#/llm/keys?extend=%s' % (
                                        base.rstrip('/'), key.id))
                        elif subject_model == 'woow_paas_platform.llm_credit_order':
                            order = self.env[subject_model].sudo().browse(subject_id).exists()
                            if order and kind == 'credit_converted' and order.create_date:
                                safe_ctx['llm_notice_time'] = _display_time(order.create_date)
                        mail_id = template.sudo().with_context(
                            **safe_ctx,
                        ).send_mail(account.partner_id.id, force_send=False, raise_exception=False)
                        if mail_id:
                            _llm_internal(notice).write({'mail_id': mail_id})
                        else:
                            raise RuntimeError('mail queue unavailable')
                return notice.id
        except Exception:  # noqa: BLE001 - notice failure must not break the LLM action
            _logger.warning('[LLM] notification enqueue failed (kind=%s subject=%s:%s)',
                            kind, subject_model, subject_id)
            return False

    @api.model
    def _llm_report_not_me(self, partner, token):
        """Consume an owner-bound 24-hour action token, suspend locally, then queue key disables."""
        if llm_unconfigured(self.env) or not isinstance(token, str) or len(token) > 512:
            return False
        match = _TOKEN_RE.fullmatch(token)
        if not match:
            return False
        notice_id = int(match.group(1))
        expiry_epoch = int(match.group(2))
        signature = match.group(3)
        secret = self.env['ir.config_parameter'].sudo().get_param('database.secret')
        if not secret:
            return False
        message = 'n1.%d.%d' % (notice_id, expiry_epoch)
        expected = base64.urlsafe_b64encode(
            hmac.new(secret.encode('utf-8'), message.encode('ascii'), hashlib.sha256).digest()
        ).decode('ascii').rstrip('=')
        if not hmac.compare_digest(signature, expected):
            return False
        now = _utcnow()
        account_id = False
        with self.env.registry.cursor() as cr:
            cr.execute('SET LOCAL lock_timeout = %s', ('5s',))
            cr.execute(
                'SELECT n.account_id FROM %s n '
                'JOIN woow_paas_platform_llm_account a ON a.id=n.account_id '
                'WHERE n.id=%%s AND n.partner_id=%%s '
                'AND n.consumed_at IS NULL AND n.token_expires_at > %%s '
                'AND n.token_expires_at >= to_timestamp(%%s) AND a.partner_id=%%s '
                'AND a.state=%%s FOR NO KEY UPDATE OF a SKIP LOCKED '
                'FOR UPDATE OF n SKIP LOCKED' % TABLE,
                (notice_id, partner.id, now, expiry_epoch, partner.id, 'active'))
            row = cr.fetchone()
            if not row:
                return False
            account_id = row[0]
            cr.execute(
                "UPDATE woow_paas_platform_llm_account SET state='suspended', "
                "suspend_reason='not_me', write_uid=%s, write_date=%s WHERE id=%s AND state='active'",
                (SUPERUSER_ID, now, account_id))
            if cr.rowcount != 1:
                raise RuntimeError('suspension transition failed')
            cr.execute(
                'UPDATE %s SET consumed_at=%%s, write_uid=%%s, write_date=%%s '
                'WHERE id=%%s AND consumed_at IS NULL' % TABLE,
                (now, SUPERUSER_ID, now, notice_id))
            if cr.rowcount != 1:
                raise RuntimeError('token consumption failed')
            # Commit the audit with the suspension. Delivery obligations remain
            # visible on the consumed notice until both helpers have succeeded.
            Event = self.env['woow_paas_platform.llm_event']
            Event._llm_insert_event_cr(cr, Event._llm_record_values(
                'not_me_report', account_id=account_id, uid=self.env.uid), now)
        # The existing key-disable cron owns remote mutations. Trigger only after
        # the suspension commit; its normal bounded claim loop retries failures.
        # Audit helpers are post-commit too: isolate each failure so none can
        # prevent the disable queue from being triggered or undo suspension.
        try:
            self._llm_queue_account_disable(account_id)
        except Exception:  # noqa: BLE001 - suspended state and scheduled cron remain authoritative
            _logger.warning('[LLM] not-me disable trigger failed account=%s', account_id)
        self._llm_deliver_report(notice_id)
        return True

    @api.model
    def _llm_deliver_report(self, notice_id):
        """Repair post-commit obligations without replaying account suspension."""
        if llm_unconfigured(self.env):
            return False
        try:
            with self.env.registry.cursor() as cr:
                cr.execute('SET LOCAL lock_timeout = %s', ('5s',))
                cr.execute(
                    'SELECT account_id, consumed_at, report_incident_id, report_alert_id '
                    'FROM %s WHERE id=%%s AND consumed_at IS NOT NULL '
                    'FOR UPDATE SKIP LOCKED' % TABLE, (notice_id,))
                row = cr.fetchone()
                if not row:
                    return False
                account_id, consumed_at, incident_id, alert_id = row
                env = api.Environment(cr, SUPERUSER_ID, {})
                Event = env['woow_paas_platform.llm_event']
                if not incident_id:
                    # Recover the crash window after the helper's own commit.
                    incident_id = env['woow.billing.incident'].sudo().search([
                        ('kind', '=', LlmIncident.NOT_ME.value),
                        ('source_model', '=', 'woow_paas_platform.llm_account'),
                        ('source_ref', '=', account_id), ('create_date', '>=', consumed_at),
                    ], order='id', limit=1).id or Event._llm_incident(
                        LlmIncident.NOT_ME, account_id=account_id)
                if not alert_id:
                    alert_id = Event.sudo().search([
                        ('alert_code', '=', LlmAlert.NOT_ME_REPORTED.value),
                        ('account_id', '=', account_id), ('create_date', '>=', consumed_at),
                    ], order='id', limit=1).id or Event._llm_alert(
                        LlmAlert.NOT_ME_REPORTED, account_id=account_id)
                cr.execute(
                    'UPDATE %s SET report_incident_id=%%s, report_alert_id=%%s '
                    'WHERE id=%%s' % TABLE,
                    (incident_id or None, alert_id or None, notice_id))
                return bool(incident_id and alert_id)
        except Exception:  # noqa: BLE001 - durable consumed notice remains queued
            _logger.warning('[LLM] not-me audit delivery deferred notice=%s', notice_id)
            return False

    @api.model
    def _llm_action_token(self, notice):
        if (not notice.token_expires_at or notice.consumed_at
                or notice.token_expires_at <= _utcnow()):
            return ''
        secret = self.env['ir.config_parameter'].sudo().get_param('database.secret')
        if not secret:
            return ''
        expires = int(notice.token_expires_at.replace(tzinfo=timezone.utc).timestamp())
        message = 'n1.%d.%d' % (notice.id, expires)
        signature = base64.urlsafe_b64encode(
            hmac.new(secret.encode('utf-8'), message.encode('ascii'), hashlib.sha256).digest()
        ).decode('ascii').rstrip('=')
        return '%s.%s' % (message, signature)

    @api.model
    def _llm_update_settings(self, partner, threshold):
        if (llm_unconfigured(self.env) or type(threshold) is not int
                or not 0 <= threshold <= 100000):
            return False
        account = self.env['woow_paas_platform.llm_account']._llm_account_for_partner(partner)
        if not account or account.state not in ('active', 'suspended'):
            return False
        values = {'low_balance_threshold': threshold}
        if threshold != account.low_balance_threshold:
            values['low_balance_armed'] = True
        if account.quota_cached >= threshold * 15625:
            values['low_balance_armed'] = True
            values['low_balance_notice_pending'] = False
        _llm_internal(account).write(values)
        return {'low_balance_threshold': threshold}

    @api.model
    def _llm_queue_account_disable(self, account_id):
        cron = self.env.ref('woow_paas_platform.ir_cron_llm_not_me_disable',
                            raise_if_not_found=False)
        if cron:
            cron.sudo()._trigger()

    @api.model
    def _cron_llm_not_me_disable(self):
        """Bounded background disable: at most one key remote write per tick."""
        return run_llm_cron(self.env, 'woow_paas_platform.ir_cron_llm_not_me_disable',
                            self._llm_not_me_disable_tick, items=1, outbound=True)

    @api.model
    def _llm_not_me_disable_tick(self, config, tick):
        if not tick.can_take(nac.MIN_CALL_SECONDS):
            return False
        key = self.env['woow_paas_platform.llm_api_key'].sudo().search([
            ('account_id.state', '=', 'suspended'), ('account_id.suspend_reason', '=', 'not_me'),
            ('state', 'in', ('active', 'failed_reveal')),
            '|', ('not_me_disable_next_at', '=', False),
            ('not_me_disable_next_at', '<=', _utcnow()),
        ], order='not_me_disable_next_at,id', limit=1)
        if not key:
            return False
        account = key.account_id
        if not key.newapi_token_id:
            # An active mirror without an upstream id is inconsistent; keep the
            # queue row visible for a later repair and never report success.
            _logger.warning('[LLM] not-me disable pending account=%s key=%s missing token id',
                            account.id, key.id)
            return self._llm_defer_key_disable(key)
        try:
            ip = account._llm_tenant_ip()
            up = account._llm_up_token()
            client = account._llm_client(nac.ROLE_CRON)
        except Exception:  # noqa: BLE001 - no credential or config details in logs
            _logger.warning('[LLM] not-me disable deferred account=%s key=%s', account.id, key.id)
            return self._llm_defer_key_disable(key)
        tick.took()
        result = client.user_set_token_status(ip, up, key.newapi_token_id, False,
                                              deadline=tick.deadline)
        if result.ok:
            _llm_internal(key).write({'state': 'disabled', 'disabled_by': 'suspend',
                                      'not_me_disable_next_at': False})
        else:
            _logger.warning('[LLM] not-me disable deferred account=%s key=%s result=%s',
                            account.id, key.id, result.kind)
            self._llm_defer_key_disable(key)
        return self._llm_has_pending_disables()

    @api.model
    def _llm_defer_key_disable(self, key):
        attempts = key.not_me_disable_attempts + 1
        delay = min(3600, 60 * (2 ** min(attempts - 1, 6)))
        _llm_internal(key).write({
            'not_me_disable_attempts': attempts,
            'not_me_disable_next_at': _utcnow() + timedelta(seconds=delay),
        })
        return self._llm_has_pending_disables()

    @api.model
    def _llm_has_pending_disables(self):
        return bool(self.env['woow_paas_platform.llm_api_key'].sudo().search_count([
            ('account_id.state', '=', 'suspended'), ('account_id.suspend_reason', '=', 'not_me'),
            ('state', 'in', ('active', 'failed_reveal')),
            '|', ('not_me_disable_next_at', '=', False),
            ('not_me_disable_next_at', '<=', _utcnow()),
        ], limit=1))

    @api.model
    def _cron_llm_notifications(self):
        return run_llm_cron(self.env, 'woow_paas_platform.ir_cron_llm_notifications',
                            self._llm_notifications_tick, items=5, outbound=False)

    @api.model
    def _llm_notifications_tick(self, config, tick):
        Notice = self.sudo()
        Account = self.env['woow_paas_platform.llm_account'].sudo()
        Key = self.env['woow_paas_platform.llm_api_key'].sudo()
        pending_reports = Notice.search([
            ('consumed_at', '!=', False),
            '|', ('report_incident_id', '=', False), ('report_alert_id', '=', False),
        ], order='id', limit=tick.items)
        for report in pending_reports:
            if not tick.can_take():
                return True
            self._llm_deliver_report(report.id)
            tick.took()
        if tick.done >= tick.items:
            return True
        pending_ids = Account.search([('low_balance_notice_pending', '=', True)],
                                     order='id', limit=tick.items - tick.done).ids
        for account_id in pending_ids:
            if not tick.can_take():
                return True
            account = Account.browse(account_id)
            notice_id = Notice._llm_notice(
                account.id, 'low_balance', 'woow_paas_platform.llm_account', account.id,
                occurrence_key=str(account.low_balance_cycle),
                number_value=max(0, int(account.quota_cached // 15625)))
            tick.took()
            if notice_id:
                _llm_internal(Account.browse(account.id)).write(
                    {'low_balance_notice_pending': False})
            else:
                tick.delay = 60
                return True
        if tick.done >= tick.items:
            return True
        remaining = tick.items - tick.done
        for key in Key.search([('created_notice_pending', '=', True)], order='id', limit=remaining):
            if not tick.can_take():
                return True
            notice_id = Notice._llm_notice(
                key.account_id.id, 'key_created', 'woow_paas_platform.llm_api_key', key.id,
                occurrence_key=str(key.id), token=True, email=True)
            if not notice_id:
                tick.took()
                tick.delay = 60
                return True
            _llm_internal(key).write({'created_notice_pending': False})
            tick.took()
        remaining = tick.items - tick.done
        if remaining <= 0:
            return self._llm_has_pending_notifications()
        self._llm_expiry_tick(remaining, tick)
        remaining = tick.items - tick.done
        if remaining > 0:
            self._llm_order_notice_tick(remaining, tick)
        more = self._llm_has_pending_notifications()
        if more:
            tick.delay = 60
        return more

    @api.model
    def _llm_has_pending_notifications(self):
        now = _utcnow()
        Account = self.env['woow_paas_platform.llm_account'].sudo()
        Order = self.env['woow_paas_platform.llm_credit_order'].sudo()
        Key = self.env['woow_paas_platform.llm_api_key'].sudo()
        local_now = datetime.now(ZoneInfo('Asia/Taipei'))
        return bool(self.sudo().search_count([
                        ('consumed_at', '!=', False), '|',
                        ('report_incident_id', '=', False), ('report_alert_id', '=', False),
                    ], limit=1)
                    or Account.search_count([('low_balance_notice_pending', '=', True)], limit=1)
                    or Key.search_count([('created_notice_pending', '=', True)], limit=1)
                    or Order.search_count([('acceptance_notice_pending', '=', True)], limit=1)
                    or self._llm_has_unnotified_order_state()
                    or self._llm_has_unnotified_unknown()
                    or (local_now.hour >= 9 and self._llm_has_due_expiry(Key, now)))

    @api.model
    def _llm_has_unnotified_order_state(self):
        self.env.cr.execute(
            "SELECT 1 FROM woow_paas_platform_llm_credit_order "
            "WHERE state IN ('credited','needs_review','voided') "
            'AND state IS DISTINCT FROM notified_state LIMIT 1')
        return bool(self.env.cr.fetchone())

    @api.model
    def _llm_has_unnotified_unknown(self):
        self.env.cr.execute(
            "SELECT 1 FROM woow_paas_platform_llm_credit_order "
            "WHERE state='unknown' AND COALESCE(sent_at, create_date) <= %s "
            'AND state IS DISTINCT FROM notified_state LIMIT 1',
            (_utcnow() - timedelta(minutes=5),))
        return bool(self.env.cr.fetchone())

    @api.model
    def _llm_has_due_expiry(self, Key, now):
        return bool(Key.search_count([
            ('state', 'in', ('active', 'disabled')), ('expires_at', '>', now),
            ('expires_at', '<=', now + timedelta(days=1)), ('expiry_notice_1d_at', '=', False),
        ], limit=1) or Key.search_count([
            ('state', 'in', ('active', 'disabled')), ('expires_at', '>', now + timedelta(days=1)),
            ('expires_at', '<=', now + timedelta(days=7)), ('expiry_notice_7d_at', '=', False),
        ], limit=1))

    @api.model
    def _llm_expiry_tick(self, limit, tick):
        now = _utcnow()
        local_now = datetime.now(ZoneInfo('Asia/Taipei'))
        if local_now.hour < 9:
            return 0
        Key = self.env['woow_paas_platform.llm_api_key'].sudo()
        due = Key.search([('state', 'in', ('active', 'disabled')), ('expires_at', '>', now),
                          ('expires_at', '<=', now + timedelta(days=1)),
                          ('expiry_notice_1d_at', '=', False)], order='expires_at,id', limit=limit)
        if len(due) < limit:
            due |= Key.search([('state', 'in', ('active', 'disabled')),
                               ('expires_at', '>', now + timedelta(days=1)),
                               ('expires_at', '<=', now + timedelta(days=7)),
                               ('expiry_notice_7d_at', '=', False)], order='expires_at,id',
                              limit=limit - len(due))
        processed = 0
        for key in due:
            if not tick.can_take():
                break
            is_1d = key.expires_at <= now + timedelta(days=1)
            field = 'expiry_notice_1d_at' if is_1d else 'expiry_notice_7d_at'
            if getattr(key, field):
                continue
            kind = 'key_expiring_1d' if is_1d else 'key_expiring_7d'
            expiry_key = 'expiry:%s' % key.expires_at.strftime('%Y%m%dT%H%M%S')
            notice_id = self._llm_notice(
                key.account_id.id, kind, 'woow_paas_platform.llm_api_key', key.id,
                occurrence_key=expiry_key, email=True)
            if notice_id:
                _llm_internal(key).write({field: now})
            processed += 1
            tick.took()
        return processed

    @api.model
    def _llm_order_notice_tick(self, limit, tick, *, account_ids=None):
        Order = self.env['woow_paas_platform.llm_credit_order'].sudo()
        scope = [('account_id', 'in', account_ids)] if account_ids is not None else []
        processed = 0
        for order in Order.search(scope + [('acceptance_notice_pending', '=', True)],
                                  order='id', limit=limit):
            if not tick.can_take():
                return processed
            notice_id = self._llm_notice(
                order.account_id.id, 'credit_converted', 'woow_paas_platform.llm_credit_order',
                order.id, occurrence_key=str(order.id), number_value=order.points,
                email=True, token=True)
            if notice_id:
                from .llm_credit_order import _credit_internal
                _credit_internal(order).write({'acceptance_notice_pending': False})
            processed += 1
            tick.took()
        remaining = limit - processed
        if remaining <= 0:
            return processed
        now = _utcnow()
        Order.flush_model(['state', 'notified_state', 'sent_at', 'create_date', 'account_id'])
        self.env.cr.execute(
            "SELECT id FROM woow_paas_platform_llm_credit_order WHERE ("
            "(state IN ('credited','needs_review','voided') AND state IS DISTINCT FROM notified_state) "
            "OR (state='unknown' AND state IS DISTINCT FROM notified_state "
            "AND COALESCE(sent_at, create_date) <= %s)) "
            + ('AND account_id = ANY(%s) ' if account_ids is not None else '')
            + 'ORDER BY id LIMIT %s',
            [now - timedelta(minutes=5)]
            + ([list(account_ids)] if account_ids is not None else []) + [remaining])
        candidates = Order.browse([row[0] for row in self.env.cr.fetchall()])
        for order in candidates:
            if not tick.can_take():
                break
            kind = {'credited': 'credit_credited', 'needs_review': 'credit_pending',
                    'voided': 'credit_voided', 'unknown': 'credit_pending'}[order.state]
            notice_id = self._llm_notice(
                order.account_id.id, kind, 'woow_paas_platform.llm_credit_order', order.id,
                occurrence_key=order.state, number_value=order.points,
                email=order.state != 'credited')
            if notice_id:
                from .llm_credit_order import _credit_internal
                _credit_internal(order).write({'notified_state': order.state})
            processed += 1
            tick.took()
        return processed

    @api.model
    def _llm_items(self, partner):
        if not llm_visible(self.env, partner):
            return []
        account = self.env['woow_paas_platform.llm_account']._llm_account_for_partner(partner)
        if not account:
            return []
        now = _utcnow()
        self.flush_model()
        account.flush_model(['quota_cached', 'low_balance_threshold', 'low_balance_cycle'])
        self.env['woow_paas_platform.llm_api_key'].flush_model([
            'account_id', 'state', 'expires_at', 'expiry_notice_1d_at', 'expiry_notice_7d_at',
        ])
        self.env['woow_paas_platform.llm_credit_order'].flush_model(['account_id', 'state'])
        self.env.cr.execute(
            'SELECT n.id FROM %s n '
            'JOIN woow_paas_platform_llm_account a ON a.id=n.account_id '
            'LEFT JOIN woow_paas_platform_llm_api_key k '
            "ON n.subject_model='woow_paas_platform.llm_api_key' AND k.id=n.subject_id "
            'LEFT JOIN woow_paas_platform_llm_credit_order o '
            "ON n.subject_model='woow_paas_platform.llm_credit_order' AND o.id=n.subject_id "
            'WHERE n.account_id=%%s AND (n.kind<>%%s OR '
            '(a.quota_cached < a.low_balance_threshold * 15625 '
            'AND n.occurrence_key=a.low_balance_cycle::text)) '
            'AND (n.kind NOT IN (%%s,%%s) OR (k.account_id=a.id AND '
            'k.state IN (%%s,%%s) AND k.expires_at>%%s '
            'AND ((n.kind=%%s AND k.expiry_notice_1d_at IS NOT NULL '
            "AND k.expires_at<=%%s AND n.occurrence_key = 'expiry:' || "
            "to_char(k.expires_at, 'YYYYMMDD\"T\"HH24MISS')) OR "
            '(n.kind=%%s AND k.expiry_notice_7d_at IS NOT NULL '
            'AND k.expires_at>%%s AND k.expires_at<=%%s '
            "AND n.occurrence_key = 'expiry:' || "
            "to_char(k.expires_at, 'YYYYMMDD\"T\"HH24MISS'))))) "
            'AND (n.kind NOT IN (%%s,%%s,%%s) OR '
            '(o.account_id=a.id AND o.state=n.occurrence_key '
            'AND (n.kind<>%%s OR n.create_date>=%%s))) '
            'ORDER BY n.id DESC LIMIT 30' % TABLE,
            (account.id, 'low_balance', 'key_expiring_7d', 'key_expiring_1d', 'active', 'disabled',
             now, 'key_expiring_1d', now + timedelta(days=1), 'key_expiring_7d',
             now + timedelta(days=1), now + timedelta(days=7), 'credit_pending',
             'credit_voided', 'credit_credited', 'credit_credited', now - timedelta(hours=24),
             ))
        notice_ids = [row[0] for row in self.env.cr.fetchall()]
        notices = self.sudo().browse(notice_ids)
        items = []
        for notice in notices:
            key = notice.subject_model == 'woow_paas_platform.llm_api_key'
            order = notice.subject_model == 'woow_paas_platform.llm_credit_order'
            route = 'llm/keys' if key or notice.kind.startswith('key_') else 'llm/credit'
            action_token = self._llm_action_token(notice)
            if action_token and notice.kind in ('key_created', 'credit_converted'):
                route = 'llm/overview?not_me=' + action_token
            title, message = self._llm_notice_text(notice)
            items.append({
                'id': 'llm_notification:%d' % notice.id,
                'type': notice.kind,
                'severity': notice.severity,
                'title': title,
                'message': message,
                'ts': _iso(notice.create_date),
                'route': route,
                'subject_id': notice.subject_id if key or order else None,
            })
        if account.quota_cached < 0:
            items.append({'id': 'llm_overdraft:%d' % account.id, 'type': 'llm_overdraft',
                          'severity': 'warning', 'title': _('AI credit is overdrawn'),
                          'message': _('Your AI credit is %(points)s points below zero.',
                                       points=abs(account.quota_cached) // 15625),
                          'ts': _iso(account.balance_synced_at), 'route': 'llm/credit'})
        today = datetime.now(ZoneInfo('Asia/Taipei')).date()
        usage = self.env['woow_paas_platform.llm_free_usage'].sudo().search([
            ('account_id', '=', account.id), ('day_tpe', '=', today), ('capped_at', '!=', False),
        ], limit=1)
        if usage:
            items.append({'id': 'llm_trial_capped:%d:%s' % (account.id, today),
                          'type': 'llm_trial_capped', 'severity': 'info',
                          'title': _('Today’s free trial limit has been reached'),
                          'message': _('Your free trial requests are used up for today.'),
                          'ts': _iso(usage.capped_at), 'route': 'llm/overview'})
        if account.state == 'suspended':
            items.append({'id': 'llm_suspended:%d' % account.id, 'type': 'llm_suspended',
                          'severity': 'error', 'title': _('AI models service suspended'),
                          'message': _('Your AI models service is suspended.'),
                          'ts': _iso(account.write_date), 'route': 'llm/overview'})
        current_terms = self.env['woow_paas_platform.llm_terms']._llm_current()
        if account.state == 'active' and current_terms and account.tos_version != current_terms.version:
            items.append({'id': 'llm_terms_updated:%d:%s' % (account.id, current_terms.id),
                          'type': 'llm_terms_updated', 'severity': 'warning',
                          'title': _('AI models terms have been updated'),
                          'message': _('Please review and accept the current terms.'),
                          'ts': _iso(current_terms.published_at), 'route': 'llm/docs'})
        return items

    @api.model
    def _llm_notice_text(self, notice):
        if notice.kind == 'low_balance':
            return _('Low AI credit'), _('Your AI credit is below the reminder threshold.')
        if notice.kind.startswith('key_expiring'):
            return _('API key expiring soon'), _('API key %(id)s is approaching its expiry.',
                                                   id=notice.subject_id)
        if notice.kind == 'key_created':
            return _('New API key created'), _('API key %(id)s was created.', id=notice.subject_id)
        if notice.kind == 'credit_converted':
            return _('Points converted'), _('%(points)s points were converted.',
                                             points=notice.number_value or 0)
        if notice.kind == 'credit_credited':
            return _('Conversion credited'), _('Conversion order %(id)s was credited.',
                                               id=notice.subject_id)
        if notice.kind == 'credit_pending':
            return _('Conversion being checked'), _('Your points are safe. We will process this and notify you.')
        return _('Conversion not completed'), _('The points were returned to your wallet.')


def _iso(value):
    return value.isoformat() + '+00:00' if value else None
