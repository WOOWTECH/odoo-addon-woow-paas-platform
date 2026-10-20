"""Backoffice-only LLM credit services and guarded review actions (LLM-12a).

All RPC-facing methods repeat the human-operator check. Mutations stay behind
private model services and the existing object-identity ledger capability.
"""
import logging
import hashlib
import json
import time
from datetime import datetime, timedelta, timezone

import psycopg2

from odoo import SUPERUSER_ID, _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from markupsafe import Markup, escape

from ..services import llm_credit_evidence as credit_evidence
from ..services import newapi_client as nac
from .llm_common import alert_targets, llm_config, llm_unconfigured, run_llm_cron
from .llm_credit_order import ORDER_MODEL, ORDER_TABLE, _credit_internal
from .llm_event import EVENT_TABLE, LlmAlert, LlmIncident, _begin

_logger = logging.getLogger(__name__)
_MANUAL_DAILY_LIMIT = 3000
_MANUAL_LOCK = 938174211
_TAIPEI = timezone(timedelta(hours=8))
_NAMED_FLAG_KEY = 'woow_llm_named_human_provision'
_NAMED_FLAG_CAPABILITY = object()


class BackofficeBudgetExceeded(UserError):
    """Transient worker budget exhaustion; keep the durable operation pending."""



def _named_system_operator(env, user=None):
    user = user or env.user
    user = user.sudo().exists()
    if len(user) != 1:
        raise AccessError(_('A named system administrator is required.'))
    protected_ids = {SUPERUSER_ID, env.ref('base.user_root').id, env.ref('base.user_admin').id}
    if (not user or user.id in protected_ids
            or not user.has_group('base.group_system') or not user.active or user.share
            or not user.login or not user.email or '@' not in user.email.strip()
            or not user.llm_named_human_eligible):
        raise AccessError(_('A named system administrator is required.'))
    return user


def _review_evidence_error(message, evidence):
    error = UserError(message)
    # Only verifier-produced, closed numeric evidence reaches this attribute;
    # the durable operation stores it separately from free-text messages.
    error.safe_evidence = evidence
    return error


def _bounded_lock_wait(cr, deadline):
    """Cap DB lock acquisition by the caller's monotonic deadline."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise BackofficeBudgetExceeded(
            _('The operation time budget expired; it remains queued.'))
    milliseconds = max(1, min(1000, int(remaining * 1000)))
    cr.execute("SELECT set_config('lock_timeout', %s, true)", ('%dms' % milliseconds,))


def _record_financial_event(env, account, partner, operator_uid, order_id, points):
    event = env['woow_paas_platform.llm_event']
    values = event._llm_record_values(
        'order_transition', account_id=account.id, partner_id=partner.id,
        uid=operator_uid, target_model=ORDER_MODEL, target_id=order_id,
        result='ok', details={'count': points})
    event._llm_insert_event_cr(env.cr, values, fields.Datetime.now())


class LlmNamedHumanUser(models.Model):
    """Named-human eligibility is provisioned only by trusted server policy.

    Do not add this flag to a writable settings form or an RPC method. A
    deployment/configuration administrator provisions it through the private
    method below after identifying an individual employee. The default is
    false; neither an address pattern nor group membership establishes that a
    login is human. The object-identity capability also defeats JSON context
    forgery and ordinary ``sudo().write`` calls.
    """
    _inherit = 'res.users'

    llm_named_human_eligible = fields.Boolean(
        string='Named human eligible for AI backoffice', default=False, copy=False,
        groups='base.group_system', readonly=True,
        help='Provisioned by trusted server configuration after confirming this is an individual.')

    @api.model_create_multi
    def create(self, vals_list):
        if any('llm_named_human_eligible' in vals for vals in vals_list):
            raise AccessError(_('Named-human eligibility is provisioned by system configuration.'))
        # Keep an explicit server-owned value in vals. Removing the field (or
        # leaving it absent) lets default_<field> context and ir.default seed
        # it later inside Odoo's super().create() default resolution.
        safe_vals_list = [dict(vals, llm_named_human_eligible=False) for vals in vals_list]
        return super().create(safe_vals_list)

    def write(self, vals):
        if ('llm_named_human_eligible' in vals
                and self.env.context.get(_NAMED_FLAG_KEY) is not _NAMED_FLAG_CAPABILITY):
            raise AccessError(_('Named-human eligibility is provisioned by system configuration.'))
        return super().write(vals)

    def _llm_set_named_human_eligible(self, eligible):
        """Private deployment/configuration provisioning hook; never expose via RPC."""
        if type(eligible) is not bool:
            raise ValidationError(_('Eligibility must be explicitly true or false.'))
        protected_ids = {SUPERUSER_ID, self.env.ref('base.user_root').id,
                         self.env.ref('base.user_admin').id}
        if eligible and any(user.id in protected_ids or not user.active or user.share
                            or not user.login or not user.email or not user.email.strip()
                            or '@' not in user.email.strip()
                            or not user.has_group('base.group_system') for user in self):
            raise ValidationError(_('Only an active, named internal system user can be provisioned.'))
        return self.sudo().with_context(**{
            _NAMED_FLAG_KEY: _NAMED_FLAG_CAPABILITY,
        }).write({'llm_named_human_eligible': eligible})


class LlmManualCreditService(models.AbstractModel):
    _name = 'woow_paas_platform.llm_manual_credit_service'
    _description = 'Private manual AI credit service'

    @api.model
    def _repair_accepted_manual(self, request_id, fingerprint, account_id, points,
                                reason, operator_uid):
        """Repair notice obligations for an already committed, fingerprinted grant.

        This is a cron-only recovery seam. It never creates or changes a grant,
        and deliberately does not re-authorize the original human operator.
        """
        values = {
            'operation': 'manual_credit', 'operator_id': operator_uid,
            'account_id': account_id, 'order_id': False, 'decision': False,
            'points': points, 'reason': reason.strip() if isinstance(reason, str) else reason,
        }
        expected = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
        if expected != fingerprint:
            raise ValidationError(_('The queued manual credit fingerprint is invalid.'))
        client_request_id = 'manual-wizard-%d' % request_id
        with self.env.registry.cursor() as cr:
            cr.execute(
                'SELECT id, account_id, partner_id, name, state, points, reason, '
                'requested_by_uid, manual_notice_accepted FROM ' + ORDER_TABLE +
                ' WHERE client_request_id = %s AND source = %s LIMIT 1',
                (client_request_id, 'manual'))
            row = cr.fetchone()
        if not row:
            return None
        order_id, found_account, partner_id, name, state, found_points, found_reason, uid, accepted = row
        if (found_account != account_id or found_points != points
                or found_reason != reason or uid != operator_uid):
            raise ValidationError(_('The committed manual credit does not match its request.'))
        if not accepted:
            Event = self.env['woow_paas_platform.llm_event']
            with self.env.registry.cursor() as cr:
                cr.execute('SELECT 1 FROM woow_paas_platform_llm_event '
                           "WHERE action = 'manual_credit' AND target_model = %s "
                           'AND target_id = %s LIMIT 1', (ORDER_MODEL, order_id))
                event_exists = bool(cr.fetchone())
            if not event_exists:
                Event._llm_record(
                    'manual_credit', account_id=found_account, partner_id=partner_id,
                    uid=operator_uid, target_model=ORDER_MODEL, target_id=order_id,
                    result='ok', details={'count': points})
            alert_id = Event._llm_alert(
                LlmAlert.MANUAL_CREDIT, account_id=found_account,
                target_model=ORDER_MODEL, target_id=order_id,
                numbers={'account_id': found_account, 'order_id': order_id,
                         'points': points, 'operator_uid': operator_uid})
            if alert_id:
                with self.env.registry.cursor() as cr:
                    cr.execute('UPDATE ' + ORDER_TABLE +
                               ' SET manual_notice_accepted = TRUE, write_date = NOW() '
                               'WHERE id = %s AND manual_notice_accepted = FALSE', (order_id,))
                accepted = True
        return {'id': order_id, 'name': name, 'state': state,
                'account_id': found_account, 'notice_accepted': bool(accepted)}

    @api.model
    def _create_manual(self, account_id, points, reason, request_id):
        """Serialize, cap, and persist a manual order in one READ COMMITTED transaction."""
        operator = _named_system_operator(self.env)
        config = llm_config(self.env)
        runtime = self.env['woow_paas_platform.llm_runtime_state']._llm_read()
        if llm_unconfigured(self.env) or not config['orders_enabled'] or runtime['guard_tripped']:
            raise UserError(_('AI Models are not configured. Manual credit is unavailable.'))
        if type(points) is not int or not 1 <= points <= 1000:
            raise ValidationError(_('Manual credit must be between 1 and 1000 whole points.'))
        if not isinstance(reason, str) or len(reason.strip()) < 10:
            raise ValidationError(_('A reason of at least 10 characters is required.'))
        if type(account_id) is not int or account_id <= 0:
            raise ValidationError(_('Select an AI account.'))
        if type(request_id) is not int or request_id <= 0:
            raise ValidationError(_('The manual credit request is invalid.'))

        result = None
        created_new = False
        cap_failed = False
        notice_accepted = False
        day_start = datetime.now(_TAIPEI).replace(hour=0, minute=0, second=0, microsecond=0)
        next_day = day_start + timedelta(days=1)
        with self.env.registry.cursor() as cr:
            _begin(self.env, cr, read_committed=True)
            cr.execute('SELECT pg_advisory_xact_lock(%s)', (_MANUAL_LOCK,))
            env = api.Environment(cr, self.env.uid, dict(self.env.context))
            stable_request_id = 'manual-wizard-%d' % request_id
            prior = env[ORDER_MODEL].sudo().search([
                ('client_request_id', '=', stable_request_id),
            ], limit=1)
            if prior:
                if (prior.source != 'manual' or prior.account_id.id != account_id
                        or prior.points != points or prior.reason != reason.strip()
                        or prior.requested_by_uid.id != operator.id):
                    raise ValidationError(_('This manual credit request was already used.'))
                result = (prior.id, prior.account_id.id, prior.partner_id.id,
                          prior.name, prior.state, prior.manual_notice_accepted)
                notice_accepted = prior.manual_notice_accepted
            if result:
                pass
            else:
                cr.execute(
                    'SELECT COALESCE(SUM(points), 0) FROM ' + ORDER_TABLE +
                    " WHERE source = 'manual' AND state <> 'cancelled' "
                    'AND create_date >= %s AND create_date < %s',
                    (day_start.astimezone(timezone.utc).replace(tzinfo=None),
                     next_day.astimezone(timezone.utc).replace(tzinfo=None)))
                used = cr.fetchone()[0]
                if used + points > _MANUAL_DAILY_LIMIT:
                    cap_failed = True
                else:
                    account = env['woow_paas_platform.llm_account'].sudo().browse(account_id).exists()
                    if not account or account.state not in ('active', 'suspended'):
                        raise ValidationError(_('Manual credit requires an active or suspended AI account.'))
                    order = _credit_internal(env[ORDER_MODEL].sudo()).create({
                        'account_id': account.id,
                        'partner_id': account.partner_id.id,
                        'newapi_user_id': account.newapi_user_id,
                        'source': 'manual',
                        'points': points,
                        'quota': points * 15625,
                        'rate_r_snapshot': 32,
                        'q_snapshot': 15625,
                        'client_request_id': stable_request_id,
                        'reason': reason.strip(),
                        'requested_by_uid': operator.id,
                        'manual_notice_accepted': False,
                    })
                    result = (order.id, account.id, account.partner_id.id,
                              order.name, order.state, False)
                    created_new = True
        if cap_failed:
            self._report_manual_cap(account_id)
            raise UserError(_('The platform daily manual credit limit has been reached.'))
        order_id, result_account_id, partner_id, order_name, state, notice_accepted = result
        alert_id = None
        if created_new or not notice_accepted:
            Event = self.env['woow_paas_platform.llm_event']
            # A durable false marker is committed with the order. Replays
            # repair the event/alert obligation without reading the order
            # through the caller's possibly stale repeatable-read snapshot.
            with self.env.registry.cursor() as cr:
                cr.execute('SELECT 1 FROM woow_paas_platform_llm_event '
                           "WHERE action = 'manual_credit' AND target_model = %s "
                           'AND target_id = %s LIMIT 1', (ORDER_MODEL, order_id))
                event_exists = bool(cr.fetchone())
            if not event_exists:
                Event._llm_record(
                    'manual_credit', account_id=result_account_id, partner_id=partner_id,
                    uid=operator.id, target_model=ORDER_MODEL, target_id=order_id,
                    result='ok', details={'count': points})
            alert_id = Event._llm_alert(
                LlmAlert.MANUAL_CREDIT, account_id=result_account_id,
                target_model=ORDER_MODEL, target_id=order_id,
                numbers={'account_id': result_account_id, 'order_id': order_id,
                         'points': points, 'operator_uid': operator.id})
            if alert_id:
                with self.env.registry.cursor() as cr:
                    cr.execute('UPDATE ' + ORDER_TABLE +
                               ' SET manual_notice_accepted = TRUE, write_date = NOW() '
                               'WHERE id = %s AND manual_notice_accepted = FALSE', (order_id,))
            self.env.ref('woow_paas_platform.ir_cron_llm_credit')._trigger()
        return {'id': order_id, 'name': order_name, 'state': state,
                'account_id': result_account_id,
                'notice_accepted': notice_accepted or bool(alert_id)}

    @api.model
    def _report_manual_cap(self, account_id):
        """Persist only numeric, PaaS-generated identifiers in cap alerts/incidents."""
        if llm_unconfigured(self.env):
            return
        try:
            self.env['woow_paas_platform.llm_event']._llm_incident(
                LlmIncident.MANUAL_CREDIT_BUDGET, account_id=account_id,
                error_code='llm_manual_credit_budget', needs_human=True)
            self.env['woow_paas_platform.llm_event']._llm_alert(
                LlmAlert.MANUAL_CREDIT_BUDGET, account_id=account_id,
                error_code='llm_manual_credit_budget',
                numbers={'account_id': account_id})
        except Exception:  # preserve the denial even when an alert sink is unavailable
            _logger.warning('[LLM] manual credit cap alert could not be recorded')


class LlmBackofficeOrder(models.Model):
    _inherit = 'woow_paas_platform.llm_credit_order'

    reason = fields.Text(readonly=True, copy=False, groups='base.group_system')
    admin_review_reason = fields.Text(readonly=True, copy=False,
                                      groups='base.group_system')
    manual_notice_accepted = fields.Boolean(readonly=True, copy=False, default=True,
                                            groups='base.group_system')

    def action_open_audit_events(self):
        user = self.env.user
        protected_ids = {SUPERUSER_ID, self.env.ref('base.user_root').id,
                         self.env.ref('base.user_admin').id}
        if (user.id in protected_ids or not user.active
                or user.share
                or not (user.has_group('base.group_system')
                        or user.has_group('account.group_account_manager'))):
            raise AccessError(_('Backoffice read access is required.'))
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': _('Order audit events'),
                'res_model': 'woow_paas_platform.llm_event', 'view_mode': 'list,form',
                'domain': [('target_model', '=', ORDER_MODEL), ('target_id', '=', self.id)],
                'target': 'current'}

    def _admin_review(self, decision, reason, operator_id=None, deadline=None):
        operator = _named_system_operator(
            self.env, self.env['res.users'].browse(operator_id) if operator_id else self.env.user)
        self.ensure_one()
        if self.state != 'needs_review':
            raise UserError(_('Only orders awaiting review can be handled here.'))
        if not isinstance(reason, str) or len(reason.strip()) < 10:
            raise ValidationError(_('A reason of at least 10 characters is required.'))
        if decision not in ('credited', 'retry', 'void'):
            raise ValidationError(_('Unsupported review decision.'))

        deadline = min(float('inf') if deadline is None else deadline,
                       time.monotonic() + 8)
        _bounded_lock_wait(self.env.cr, deadline)
        # Serialize competing reviewers before taking any external sample.
        # NO KEY UPDATE is compatible with the event helper's FK KEY SHARE.
        self.flush_recordset()
        self.env.cr.execute('SELECT id FROM ' + ORDER_TABLE +
                            ' WHERE id = %s FOR NO KEY UPDATE', (self.id,))
        if not self.env.cr.fetchone():
            raise UserError(_('This order is no longer available.'))
        self.invalidate_recordset()
        if self.state != 'needs_review':
            raise UserError(_('Only orders awaiting review can be handled here.'))
        account = self.account_id
        if not account:
            raise UserError(_('The account snapshot is unavailable. Keep this order in review.'))
        account.flush_recordset()
        _bounded_lock_wait(self.env.cr, deadline)
        self.env.cr.execute(
            'SELECT id FROM woow_paas_platform_llm_account WHERE id = %s FOR NO KEY UPDATE',
            (account.id,))
        if not self.env.cr.fetchone():
            raise UserError(_('The account snapshot is unavailable. Keep this order in review.'))
        account.invalidate_recordset(['newapi_user_id', 'newapi_username', 'state'])
        if self.newapi_user_id != account.newapi_user_id:
            raise UserError(_('The account snapshot changed; keep this order in review.'))
        if llm_unconfigured(self.env) or self.env[
                'woow_paas_platform.llm_runtime_state']._llm_read()['guard_tripped']:
            raise UserError(_('AI Models are not configured for evidence checks. Keep this order in review.'))
        start = self._llm_verify_epoch(self.sent_at)
        if start is None:
            raise UserError(_('The order has no valid send timestamp. Keep it in review.'))
        before = self._llm_verify_before_snapshot({
            'quota_before': self.quota_before,
            'used_quota_before': self.used_quota_before,
            'request_count_before': self.request_count_before,
            'before_snapshot_present': self.before_snapshot_present,
            'before_sampled_at': self.before_sampled_at,
        }, start)
        if not before:
            raise UserError(_('The order has no valid original snapshot. Keep it in review.'))
        try:
            client = account._llm_client(nac.ROLE_CRON)
        except nac.NewApiError:
            raise UserError(_('Could not verify upstream evidence. Keep this order in review.')) from None
        end = int(datetime.now(timezone.utc).timestamp())
        logs = client.ap_log({'p': 1, 'page_size': 100, 'type': 1,
                              'start_timestamp': max(0, start - 60),
                              'end_timestamp': end,
                              'username': account.newapi_username},
                             timeout=nac.TIMEOUT_CRON_READ,
                             deadline=deadline,
                             priority=nac.AP_PRIORITY_OTHER)
        sample = client.ap_get_user(self.newapi_user_id, timeout=nac.TIMEOUT_CRON_READ,
                                    deadline=deadline,
                                    priority=nac.AP_PRIORITY_OTHER)
        if not isinstance(logs, nac.Ok) or not isinstance(sample, nac.Ok):
            raise UserError(_('Could not verify upstream evidence. Keep this order in review.'))
        try:
            items, more = self._llm_verify_log_page(logs.data, 1)
            if more:
                raise UserError(_('The evidence query is incomplete. Keep this order in review.'))
            proof = None
            for log in items:
                proof = credit_evidence.credit_log_proof(
                    log, target_user_id=self.newapi_user_id, quota=self.quota,
                    quota_before=before.quota if before else None, sent_at=start,
                    observed_at=end, expected_request_id=self.newapi_request_id or None,
                    service_admin_id=llm_config(self.env).get('service_admin_id'))
                if proof:
                    break
            current = self._llm_verify_snapshot(sample.data, self.newapi_user_id)
        except (credit_evidence.EvidenceError, TypeError, ValueError):
            raise UserError(_('Upstream evidence was invalid. Keep this order in review.')) from None

        evidence = {'second': self._llm_verify_snapshot_evidence(current, fields.Datetime.now())}
        if proof:
            evidence['log'] = self._llm_verify_log_evidence(proof)
            if self._llm_verify_request_id_claimed(proof.request_id, self.id):
                raise UserError(_('This upstream log is already linked to another order. Keep this order in review.'))
        if decision in ('retry', 'void') and proof:
            raise _review_evidence_error(
                _('Matching upstream credit evidence was found. Keep this order in review.'), evidence)
        if decision in ('retry', 'void') and current != before:
            raise _review_evidence_error(
                _('The fresh quota snapshot differs from the original. Keep this order in review.'),
                evidence)
        if decision == 'retry':
            if self.attempt >= 6:
                raise UserError(_('The upstream retry limit has been reached. Keep this order in review.'))
            if not self.convert_event_id:
                raise UserError(_('The order has no owned upstream reservation. Keep it in review.'))
            self.env.cr.execute(
                'UPDATE %s SET result = %%s, error_code = NULL, write_date = NOW() '
                'WHERE id = %%s AND action = %%s AND account_id = %%s '
                'AND target_model = %%s AND target_id = %%s '
                "AND result IN ('unknown', 'not_executed')" % EVENT_TABLE,
                ('not_executed', self.convert_event_id, 'convert', account.id,
                 ORDER_MODEL, self.id))
            if self.env.cr.rowcount != 1:
                raise UserError(_('The upstream reservation is not safe to retry. Keep this order in review.'))
        if decision == 'credited':
            credited_values = {
                'credit_evidence': 'manual',
                'credited_at': fields.Datetime.now(),
                'evidence_json': evidence,
                'lease_until': False,
                'last_error_code': False,
                'admin_review_reason': reason.strip(),
            }
            if proof:
                # Preserve the ledger's global partial unique claim when an
                # exact log proves the manual credit decision.
                credited_values['newapi_request_id'] = proof.request_id
            _credit_internal(self.sudo())._transition('credited', **credited_values)
        elif decision == 'retry':
            _credit_internal(self.sudo())._transition('paid', evidence_json=evidence,
                                                next_attempt_at=False, lease_until=False,
                                                verify_token=False, verify_lease_until=False,
                                                last_error_code=False,
                                                admin_review_reason=reason.strip())
            self.env.ref('woow_paas_platform.ir_cron_llm_credit')._trigger()
        elif self.source == 'user':
            _credit_internal(self.sudo()).write({'admin_review_reason': reason.strip(),
                                          'evidence_json': evidence})
            self.sudo()._llm_void_wallet()
        else:
            _credit_internal(self.sudo())._transition('cancelled', evidence_json=evidence,
                                                lease_until=False, last_error_code=False,
                                                admin_review_reason=reason.strip())
        _record_financial_event(self.env, account, self.partner_id, operator.id,
                                self.id, self.points)
        return True

    def action_open_manual_review(self):
        _named_system_operator(self.env)
        self.ensure_one()
        if self.state != 'needs_review':
            raise UserError(_('Only orders awaiting review can be handled here.'))
        return {'type': 'ir.actions.act_window', 'name': _('Review AI credit order'),
                'res_model': 'woow_paas_platform.llm_review_wizard',
                'view_mode': 'form', 'target': 'new',
                'context': {'default_order_id': self.id}}

    def action_open_recharge_wizard(self):
        _named_system_operator(self.env)
        self.ensure_one()
        if self.source != 'user' or self.state != 'voided':
            raise UserError(_('Only a voided user conversion can be recharged.'))
        return {'type': 'ir.actions.act_window', 'name': _('Recharge late AI credit'),
                'res_model': 'woow_paas_platform.llm_recharge_wizard',
                'view_mode': 'form', 'target': 'new',
                'context': {'default_order_id': self.id}}

    def _create_recharge(self, reason, operator_id=None, deadline=None):
        operator = _named_system_operator(
            self.env, self.env['res.users'].browse(operator_id) if operator_id else self.env.user)
        self.ensure_one()
        if llm_unconfigured(self.env) or self.env[
                'woow_paas_platform.llm_runtime_state']._llm_read()['guard_tripped']:
            raise UserError(_('AI Models are not configured. Recharge is unavailable.'))
        if not isinstance(reason, str) or len(reason.strip()) < 10:
            raise ValidationError(_('A reason of at least 10 characters is required.'))
        deadline = min(float('inf') if deadline is None else deadline,
                       time.monotonic() + 8)
        _bounded_lock_wait(self.env.cr, deadline)
        self.flush_recordset()
        self.env.cr.execute('SELECT id FROM ' + ORDER_TABLE +
                            ' WHERE id=%s FOR NO KEY UPDATE', (self.id,))
        self.invalidate_recordset()
        if self.source != 'user' or self.state != 'voided':
            raise UserError(_('Only a voided user conversion can be recharged.'))
        prior = self.search([('source', '=', 'recharge'),
                             ('recharge_of_order_id', '=', self.id)], limit=1)
        if prior:
            return prior.id
        account = self.account_id
        account.flush_recordset()
        _bounded_lock_wait(self.env.cr, deadline)
        self.env.cr.execute(
            'SELECT id FROM woow_paas_platform_llm_account WHERE id = %s FOR NO KEY UPDATE',
            (account.id,))
        if not self.env.cr.fetchone():
            raise UserError(_('The account snapshot is unavailable for recharge.'))
        account.invalidate_recordset(['newapi_user_id', 'newapi_username', 'state'])
        if not self.before_snapshot_present or self.newapi_user_id != account.newapi_user_id:
            raise UserError(_('A valid original account snapshot is required for recharge.'))
        sent_at = self._llm_verify_epoch(self.sent_at)
        if sent_at is None:
            raise UserError(_('A valid send timestamp is required for recharge.'))
        try:
            client = account._llm_client(nac.ROLE_CRON)
        except nac.NewApiError:
            raise UserError(_('Could not verify the late credit evidence.')) from None
        observed_at = int(datetime.now(timezone.utc).timestamp())
        result = client.ap_log({
            'p': 1, 'page_size': 100, 'type': 1,
            'start_timestamp': max(0, sent_at - 60), 'end_timestamp': observed_at,
            'username': account.newapi_username,
        }, timeout=nac.TIMEOUT_CRON_READ, deadline=deadline,
            priority=nac.AP_PRIORITY_OTHER)
        if not isinstance(result, nac.Ok):
            raise UserError(_('Could not verify the late credit evidence.'))
        try:
            items, more = self._llm_verify_log_page(result.data, 1)
            if more:
                raise UserError(_('The evidence query is incomplete.'))
            before = self._llm_verify_before_snapshot({
                'quota_before': self.quota_before,
                'used_quota_before': self.used_quota_before,
                'request_count_before': self.request_count_before,
                'before_snapshot_present': self.before_snapshot_present,
                'before_sampled_at': self.before_sampled_at,
            }, sent_at)
            if before is None:
                raise UserError(_('A valid original account snapshot is required for recharge.'))
            proof = None
            for log in items:
                proof = credit_evidence.credit_log_proof(
                    log, target_user_id=self.newapi_user_id, quota=self.quota,
                    quota_before=before.quota if before else None,
                    sent_at=sent_at, observed_at=observed_at,
                    expected_request_id=self.newapi_request_id or None,
                    service_admin_id=llm_config(self.env).get('service_admin_id'))
                if proof:
                    break
        except (credit_evidence.EvidenceError, TypeError, ValueError):
            raise UserError(_('The late credit evidence was invalid.')) from None
        if not proof:
            raise UserError(_('No matching upstream credit log was found. Recharge was refused.'))
        self.env.cr.execute('SELECT id FROM ' + ORDER_TABLE +
                            ' WHERE newapi_request_id = %s AND id <> %s',
                            (proof.request_id, self.id))
        claimed_order_ids = {row[0] for row in self.env.cr.fetchall()}
        if claimed_order_ids - {self.id, self.recharge_of_order_id.id}:
            raise UserError(_('This upstream log is already linked to another order.'))
        try:
            with self.env.cr.savepoint():
                account = self.account_id.sudo()
                partner = self.partner_id.sudo()
                if not account or self.newapi_user_id != account.newapi_user_id:
                    raise UserError(_('The account identity changed; recharge was refused.'))
                # Claim the exact upstream request on the original order in
                # the same savepoint as the recharge and wallet settlement.
                # The ledger's existing partial unique index arbitrates races.
                if not self.newapi_request_id:
                    _credit_internal(self.sudo()).write({'newapi_request_id': proof.request_id})
                elif self.newapi_request_id != proof.request_id:
                    raise UserError(_('The upstream request claim changed; recharge was refused.'))
                partner.invalidate_recordset(['wallet_balance'])
                if partner.wallet_balance < self.points:
                    raise UserError(_('The points wallet does not have enough points.'))
                order = _credit_internal(self.sudo()).create({
                    'account_id': account.id, 'partner_id': partner.id,
                    'newapi_user_id': self.newapi_user_id, 'source': 'recharge',
                    'points': self.points, 'quota': self.quota,
                    'rate_r_snapshot': self.rate_r_snapshot, 'q_snapshot': self.q_snapshot,
                    'client_request_id': 'recharge-%d' % self.id,
                    'reason': reason.strip(), 'requested_by_uid': operator.id,
                    'recharge_of_order_id': self.id,
                    'state': 'credited', 'credited_at': fields.Datetime.now(),
                    'credit_evidence': 'manual',
                    'evidence_json': {'log': self._llm_verify_log_evidence(proof)},
                })
                product = self.env.ref('woow_paas_platform.product_llm_credit')
                company = self.env.company
                invoice = self.env['account.move'].sudo().with_context(
                    woow_force_issue=False).create({
                        'move_type': 'out_invoice', 'partner_id': partner.id,
                        'company_id': company.id, 'currency_id': company.currency_id.id,
                        'invoice_date': fields.Date.context_today(self),
                        'woow_llm_credit_order_id': order.id,
                        'woow_invoice_issue_state': 'not_required',
                        'woow_skip_upstream_hook': True,
                        'invoice_line_ids': [(0, 0, {
                            'product_id': product.id, 'name': product.display_name,
                            'quantity': self.points, 'price_unit': 1, 'discount': 0,
                            'tax_ids': [(5, 0, 0)],
                        })],
                    })
                Wallet = self.env['wallet.transaction'].sudo()
                floor = Wallet.search([], order='id desc', limit=1).id or 0
                if not invoice._pay_from_wallet():
                    raise UserError(_('The recharge wallet payment failed.'))
                transactions = Wallet.search([
                    ('partner_id', '=', partner.id), ('id', '>', floor),
                    ('transaction_type', '=', 'deduct'),
                ])
                invoice.invalidate_recordset(['amount_tax', 'amount_total', 'payment_state'])
                if (len(transactions) != 1 or transactions.amount != -self.points
                        or invoice.amount_tax != 0 or invoice.amount_total != self.points
                        or invoice.payment_state != 'paid' or invoice.state != 'posted'
                        or invoice.invoice_line_ids.tax_ids):
                    raise UserError(_('The recharge did not settle exactly.'))
                _credit_internal(order).write({
                    'invoice_id': invoice.id, 'wallet_transaction_id': transactions.id,
                })
                recharge_id = order.id
                _record_financial_event(self.env, account, partner, operator.id,
                                        recharge_id, self.points)
        except psycopg2.IntegrityError as exc:
            # The transaction that won the unique index can be invisible to
            # this request's RR snapshot. Resolve idempotency on a fresh RC
            # cursor after the savepoint has restored this transaction.
            with self.env.registry.cursor() as cr:
                _begin(self.env, cr, read_committed=True)
                cr.execute('SELECT id FROM ' + ORDER_TABLE +
                           " WHERE source = 'recharge' AND recharge_of_order_id = %s LIMIT 1",
                           (self.id,))
                prior = cr.fetchone()
            if prior:
                return prior[0]
            if getattr(getattr(exc, 'diag', None), 'constraint_name', None) == \
                    'woow_llm_credit_request_uniq':
                raise UserError(_('This upstream log is already linked to another order.')) from None
            raise
        except UserError as exc:
            if 'does not have enough points' in str(exc):
                self.env['woow_paas_platform.llm_event']._llm_incident(
                    LlmIncident.CREDIT_UNKNOWN, account_id=self.account_id.id,
                    order_id=self.id, error_code='llm_credit_unknown', points=self.points,
                    needs_human=True)
                self.env['woow_paas_platform.llm_event']._llm_alert(
                    LlmAlert.CREDIT_REVIEW, account_id=self.account_id.id,
                    target_model=ORDER_MODEL, target_id=self.id,
                    error_code='llm_credit_unknown', numbers={'points': self.points})
            raise
        return recharge_id


class LlmManualCreditCron(models.AbstractModel):
    _name = 'woow_paas_platform.llm_manual_credit_cron'
    _description = 'Queued monthly manual AI credit report'

    @api.model
    def _cron_manual_credit_monthly_report(self):
        """Queue one configured-recipient report; never invoke the mail transport."""
        xmlid = 'woow_paas_platform.ir_cron_llm_manual_credit_monthly_report'
        config = llm_config(self.env)
        return run_llm_cron(
            self.env, xmlid,
            lambda _config, _tick: self._queue_monthly_report(),
            items=1, config=config, outbound=False)

    @api.model
    def _queue_monthly_report(self):
        if llm_unconfigured(self.env):
            return False
        recipients = alert_targets(self.env)['emails']
        if not recipients:
            return False
        now_tw = datetime.now(_TAIPEI)
        current_start = now_tw.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        previous_start = (current_start - timedelta(days=1)).replace(day=1)
        start_utc = previous_start.astimezone(timezone.utc).replace(tzinfo=None)
        end_utc = current_start.astimezone(timezone.utc).replace(tzinfo=None)
        period = previous_start.strftime('%Y-%m')
        with self.env.registry.cursor() as cr:
            cr.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
            cr.execute('SELECT pg_advisory_xact_lock(%s)', (938174212,))
            env = api.Environment(cr, self.env.uid, dict(self.env.context))
            report_model = env['woow_paas_platform.llm_manual_credit_report']
            if report_model.sudo().search_count([('period_id', '=', period)]):
                return False
            cr.execute(
                'SELECT count(*), COALESCE(sum(points), 0) '
                'FROM woow_paas_platform_llm_credit_order '
                "WHERE source = 'manual' AND state <> 'cancelled' "
                'AND create_date >= %s AND create_date < %s', (start_utc, end_utc))
            total_count, total_points = cr.fetchone()
            cr.execute(
                'SELECT requested_by_uid, reason, count(*), COALESCE(sum(points), 0) '
                'FROM woow_paas_platform_llm_credit_order '
                "WHERE source = 'manual' AND state <> 'cancelled' "
                'AND create_date >= %s AND create_date < %s '
                'GROUP BY requested_by_uid, reason ORDER BY requested_by_uid, reason',
                (start_utc, end_utc))
            detail_rows = cr.fetchall()
            User = env['res.users'].sudo()
            body_rows = []
            for uid, reason, count, points in detail_rows:
                user = User.browse(uid).exists() if uid else User.browse()
                operator = user.name if user else _('Unknown operator')
                body_rows.append(Markup(
                    '<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>') % (
                        escape(operator), escape(str(uid or '')), escape(str(count)),
                        escape(str(points)), escape(reason or '')))
            body = Markup(
                '<p>Completed Taipei month: %s</p>'
                '<p>Manual orders: %s; total points: %s</p>'
                '<table><thead><tr><th>Operator</th><th>User ID</th><th>Orders</th>'
                '<th>Points</th><th>Reason</th></tr></thead><tbody>%s</tbody></table>') % (
                    escape(period), escape(str(total_count)), escape(str(total_points)),
                    Markup('').join(body_rows))
            mail = env['mail.mail'].sudo().create({
                'subject': _('[WOOW PaaS] Manual AI credit report: %s') % period,
                'email_to': ','.join(recipients), 'body_html': body,
                'auto_delete': True,
            })
            env['woow_paas_platform.llm_manual_credit_report']._create_period(
                period_id=period, start_utc=start_utc, end_utc=end_utc,
                order_count=total_count, points_total=total_points, mail_ref=mail.id)
            return True
