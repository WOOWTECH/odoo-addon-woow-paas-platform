"""Durable, idempotent handoff from backoffice RPCs to ROLE_CRON."""
import hashlib
import json
import time

import psycopg2
from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from ..services import newapi_client as nac

from .llm_backoffice import (BackofficeBudgetExceeded, _bounded_lock_wait,
                             _named_system_operator)
from .llm_common import _llm_internal, llm_config, llm_unconfigured, run_llm_cron

_OP_CAP_KEY = 'woow_llm_backoffice_operation_cap'
_OP_CAP = object()
_OP_MODEL = 'woow_paas_platform.llm_backoffice_operation'
_ORDER = 'woow_paas_platform.llm_credit_order'


def _cap(records):
    return records.with_context(**{_OP_CAP_KEY: _OP_CAP})


class LlmBackofficeOperation(models.Model):
    _name = 'woow_paas_platform.llm_backoffice_operation'
    _description = 'Queued AI backoffice operation'
    _order = 'id desc'

    request_id = fields.Char(required=True, readonly=True, copy=False, index=True)
    fingerprint = fields.Char(required=True, readonly=True, copy=False, groups='base.group_system')
    operation = fields.Selection([
        ('manual_credit', 'Manual credit'), ('review', 'Review decision'),
        ('recharge', 'Late-credit recharge'), ('disable_all_keys', 'Disable all API keys'),
    ], required=True, readonly=True, copy=False)
    order_id = fields.Many2one(_ORDER, readonly=True, ondelete='restrict', index=True)
    account_id = fields.Many2one('woow_paas_platform.llm_account', readonly=True, ondelete='restrict')
    operator_id = fields.Many2one('res.users', required=True, readonly=True, ondelete='restrict')
    decision = fields.Char(readonly=True)
    points = fields.Integer(readonly=True)
    reason = fields.Text(readonly=True, groups='base.group_system')
    state = fields.Selection([
        ('pending', 'Pending'), ('done', 'Complete'), ('refused', 'Refused'),
    ], required=True, readonly=True, default='pending', index=True)
    result_code = fields.Char(readonly=True)
    result_summary = fields.Char(readonly=True)
    evidence_json = fields.Json(readonly=True, groups='base.group_system')
    key_cursor = fields.Integer(readonly=True, default=0, groups='base.group_system')
    key_max_id = fields.Integer(readonly=True, default=0, groups='base.group_system')
    key_target_count = fields.Integer(readonly=True, default=0, groups='base.group_system')
    key_retry_ids_json = fields.Json(readonly=True, default=list, groups='base.group_system')
    key_retry_attempts = fields.Integer(readonly=True, default=0, groups='base.group_system')
    key_done_count = fields.Integer(readonly=True, default=0)
    key_failed_count = fields.Integer(readonly=True, default=0)
    processed_at = fields.Datetime(readonly=True)

    _sql_constraints = [
        ('request_unique', 'UNIQUE(request_id)', 'Operation request identifiers are unique.'),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        if self.env.context.get(_OP_CAP_KEY) is not _OP_CAP:
            raise AccessError(_('Backoffice operations can only be queued by guarded actions.'))
        return super().create(vals_list)

    def write(self, vals):
        if self.env.context.get(_OP_CAP_KEY) is not _OP_CAP:
            raise AccessError(_('Backoffice operations are immutable outside the worker.'))
        return super().write(vals)

    def unlink(self):
        raise AccessError(_('Backoffice operation history cannot be deleted.'))

    @api.model
    def _enqueue(self, *, operation, request_id, operator, account=None, order=None,
                 decision=None, points=None, reason=None):
        operator = _named_system_operator(self.env, operator)
        if llm_unconfigured(self.env):
            raise UserError(_('AI Models are not configured. This operation cannot be queued.'))
        if not isinstance(reason, str) or len(reason.strip()) < 10:
            raise ValidationError(_('A reason of at least 10 characters is required.'))
        if operation not in ('manual_credit', 'review', 'recharge', 'disable_all_keys'):
            raise ValidationError(_('Unsupported backoffice operation.'))
        values = {
            'operation': operation, 'operator_id': operator.id,
            'account_id': account.id if account else False,
            'order_id': order.id if order else False, 'decision': decision or False,
            'points': points or 0, 'reason': reason.strip(),
        }
        if operation == 'disable_all_keys':
            if account or order or decision or points:
                raise ValidationError(_('A key-disable operation cannot target an individual record.'))
            key_model = self.env['woow_paas_platform.llm_api_key'].sudo()
            key_domain = [
                ('state', 'in', ('active', 'disabled')),
                ('newapi_token_id', '>', 0),
                ('account_id.newapi_user_id', '>', 0),
            ]
            key_max_id = key_model.search(
                key_domain, order='id desc', limit=1).id or 0
            key_target_count = key_model.search_count(key_domain)
        else:
            key_max_id = 0
            key_target_count = 0
        fingerprint = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
        model = _cap(self.sudo())
        existing = model.search([('request_id', '=', request_id)], limit=1)
        if existing:
            if existing.fingerprint != fingerprint:
                raise ValidationError(_('This backoffice request was already used.'))
            return existing
        if operation == 'disable_all_keys':
            values['key_max_id'] = key_max_id
            values['key_target_count'] = key_target_count
        return model.create(dict(values, request_id=request_id, fingerprint=fingerprint))

    @api.model
    def _cron_process(self):
        xmlid = 'woow_paas_platform.ir_cron_llm_backoffice_operations'
        config = llm_config(self.env)
        return run_llm_cron(self.env, xmlid,
                            lambda _cfg, tick: self._process_batch(tick),
                            items=10, config=config, outbound=True)

    @api.model
    def _process_batch(self, tick):
        if llm_unconfigured(self.env):
            return False
        ids = []
        with self.env.registry.cursor() as cr:
            cr.execute("SELECT id FROM woow_paas_platform_llm_backoffice_operation "
                       "WHERE state = 'pending' ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 10")
            ids = [row[0] for row in cr.fetchall()]
        for operation_id in ids:
            if not tick.can_take(need_seconds=8):
                break
            with self.env.registry.cursor() as cr:
                # This must precede the first operation-row query: the manual
                # ledger commits on another cursor and the result FK must be
                # visible in this transaction's snapshot.
                cr.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
                try:
                    _bounded_lock_wait(cr, tick.deadline)
                    cr.execute(
                        'SELECT id FROM woow_paas_platform_llm_backoffice_operation '
                        'WHERE id = %s AND state = %s FOR UPDATE SKIP LOCKED',
                        (operation_id, 'pending'))
                    if not cr.fetchone():
                        continue
                except (BackofficeBudgetExceeded, psycopg2.errors.LockNotAvailable):
                    tick.more = True
                    break
                tick.took()
                env = api.Environment(cr, self.env.uid, {})
                op = env[_OP_MODEL].sudo().browse(operation_id).exists()
                if not op or op.state != 'pending':
                    continue
                actor = op.operator_id
                try:
                    with cr.savepoint():
                        if op.operation == 'manual_credit':
                            service = env['woow_paas_platform.llm_manual_credit_service']
                            result = service._repair_accepted_manual(
                                op.id, op.fingerprint, op.account_id.id, op.points,
                                op.reason, actor.id)
                            if result is None:
                                _named_system_operator(env, actor)
                                result = service.with_user(actor)._create_manual(
                                    op.account_id.id, op.points, op.reason, op.id)
                            if not result['notice_accepted']:
                                # The independently committed manual order has
                                # its durable false notice marker. Keep this
                                # idempotent operation pending for repair.
                                continue
                            result_order_id = result['id']
                            result_summary = result['name']
                            result_evidence = False
                        elif op.operation == 'review':
                            _named_system_operator(env, actor)
                            order = op.order_id.sudo().with_user(actor)
                            deadline = min(time.monotonic() + 8, tick.deadline)
                            order._admin_review(op.decision, op.reason,
                                                operator_id=actor.id, deadline=deadline)
                            result_order_id = order.id
                            result_summary = order.name
                            result_evidence = order.evidence_json
                        elif op.operation == 'recharge':
                            _named_system_operator(env, actor)
                            order = op.order_id.sudo().with_user(actor)
                            deadline = min(time.monotonic() + 8, tick.deadline)
                            result_id = order._create_recharge(
                                op.reason, operator_id=actor.id, deadline=deadline)
                            order = env[_ORDER].sudo().browse(result_id)
                            result_order_id = result_id
                            result_summary = order.name
                            result_evidence = order.evidence_json
                        elif op.operation == 'disable_all_keys':
                            _named_system_operator(env, actor)
                            self._process_disable_keys(env, op, actor, tick=tick)
                            continue
                        _cap(op).write({'state': 'done', 'order_id': result_order_id,
                                        'result_code': 'ok', 'result_summary': result_summary,
                                        'evidence_json': result_evidence,
                                        'processed_at': fields.Datetime.now()})
                except (BackofficeBudgetExceeded, psycopg2.errors.LockNotAvailable):
                    tick.more = True
                    continue
                except (UserError, ValidationError, AccessError) as exc:
                    if op.operation == 'manual_credit':
                        with env.registry.cursor() as repair_cr:
                            repair_cr.execute(
                                'SELECT id FROM woow_paas_platform_llm_credit_order '
                                "WHERE client_request_id = %s AND source = 'manual' "
                                'AND manual_notice_accepted = FALSE LIMIT 1',
                                ('manual-wizard-%d' % op.id,))
                            if repair_cr.fetchone():
                                # The manual ledger commit is independent of
                                # this operation transaction. Retry its notice
                                # repair instead of losing the obligation.
                                continue
                    safe_evidence = getattr(exc, 'safe_evidence', None)
                    if safe_evidence is None and op.order_id:
                        safe_evidence = op.order_id.evidence_json
                    _cap(op).write({'state': 'refused', 'result_code': 'refused',
                                    'result_summary': _('Operation refused; see sanitized evidence.'),
                                    'evidence_json': safe_evidence or False,
                                    'processed_at': fields.Datetime.now()})
        return len(ids) == 10 or tick.more

    @api.model
    def _process_disable_keys(self, env, op, actor, tick=None):
        """Disable at most ten snapshotted PaaS mirrors per tick; retry failures durably."""
        Key = env['woow_paas_platform.llm_api_key'].sudo()
        retry_ids = list(op.key_retry_ids_json or [])
        cursor = op.key_cursor
        candidates = []
        stale_retry_ids = []
        for key_id in retry_ids[:10]:
            key = Key.browse(key_id).exists()
            if key:
                candidates.append((key, True))
            else:
                stale_retry_ids.append(key_id)
        retry_ids = [item for item in retry_ids if item not in stale_retry_ids]
        room = 10 - len(candidates)
        if room:
            candidates.extend((key, False) for key in Key.search([
                ('id', '>', cursor), ('id', '<=', op.key_max_id),
                ('state', '=', 'active'), ('newapi_token_id', '>', 0),
                ('account_id.newapi_user_id', '>', 0),
            ], order='id', limit=room))
        done = op.key_done_count
        failed = max(0, op.key_failed_count - len(stale_retry_ids))
        retry_attempts = op.key_retry_attempts
        remaining_retries = retry_ids[:]
        last_cursor = cursor
        for key, is_retry in candidates:
            if tick and not tick.can_take(need_seconds=1):
                break
            if tick:
                _bounded_lock_wait(env.cr, tick.deadline)
            key.flush_recordset()
            env.cr.execute(
                'SELECT id FROM woow_paas_platform_llm_api_key '
                'WHERE id = %s AND account_id = %s FOR NO KEY UPDATE',
                (key.id, key.account_id.id))
            if not env.cr.fetchone():
                continue
            if tick:
                tick.took()
            key.invalidate_recordset(['state', 'newapi_token_id', 'account_id'])
            account = key.account_id.sudo()
            if key.state != 'active':
                done += 1
                if is_retry and key.id in remaining_retries:
                    remaining_retries.remove(key.id)
                    failed = max(0, failed - 1)
                last_cursor = max(last_cursor, key.id)
                continue
            try:
                with env.cr.savepoint():
                    token_id = key.newapi_token_id
                    if type(token_id) is not int or not (1 <= token_id <= nac.INT32_MAX):
                        raise nac.NewApiInputError('token_id')
                    ip = account._llm_tenant_ip()
                    up = account._llm_up_token(nac.ROLE_CRON)
                    client = account._llm_client(nac.ROLE_CRON)
                    result = client.user_set_token_status(
                        ip, up, token_id, False, timeout=nac.TIMEOUT_CRON_WRITE,
                        deadline=min(time.monotonic() + 8,
                                     tick.deadline if tick else time.monotonic() + 8))
                    if not isinstance(result, nac.Ok):
                        raise UserError(_('Upstream key status could not be confirmed.'))
                    _llm_internal(key).write({'state': 'disabled', 'disabled_by': 'ops'})
                    env['woow_paas_platform.llm_event']._llm_record(
                        'key_status', account_id=account.id, partner_id=account.partner_id.id,
                        uid=actor.id, target_model='woow_paas_platform.llm_api_key',
                        target_id=key.id, result='ok')
                done += 1
                if is_retry and key.id in remaining_retries:
                    remaining_retries.remove(key.id)
                    failed = max(0, failed - 1)
            except Exception:  # sanitized item failure; retry ID, never exception text
                retry_attempts += 1
                if key.id in remaining_retries:
                    remaining_retries.remove(key.id)
                else:
                    failed += 1
                remaining_retries.append(key.id)
            last_cursor = max(last_cursor, key.id)
        more = bool(remaining_retries) or bool(Key.search_count([
            ('id', '>', last_cursor), ('id', '<=', op.key_max_id),
            ('state', '=', 'active'), ('newapi_token_id', '>', 0),
            ('account_id.newapi_user_id', '>', 0),
        ]))
        vals = {'key_cursor': last_cursor, 'key_retry_ids_json': remaining_retries,
                'key_done_count': done, 'key_failed_count': failed,
                'key_retry_attempts': retry_attempts}
        retry_limit = max(5, 5 * op.key_target_count)
        if more and remaining_retries and retry_attempts >= retry_limit:
            vals.update({'state': 'refused', 'result_code': 'partial_failure',
                         'result_summary': _('Key disable stopped after bounded retries; some keys may remain active.'),
                         'processed_at': fields.Datetime.now()})
        elif not more:
            vals.update({'state': 'done', 'result_code': 'ok',
                         'result_summary': _('Key disable completed: %(done)s disabled; %(failed)s failed.',
                                             done=done, failed=failed),
                         'processed_at': fields.Datetime.now()})
        _cap(op).write(vals)
