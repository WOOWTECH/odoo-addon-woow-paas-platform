"""Durable, idempotent handoff from backoffice RPCs to ROLE_CRON."""
import hashlib
import json
import time
from datetime import datetime, timedelta, timezone

import psycopg2
from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from ..services import newapi_client as nac
from ..services import llm_token_recovery as token_recovery

from .llm_backoffice import (BackofficeBudgetExceeded, _bounded_lock_wait,
                             _named_system_operator)
from .llm_common import _llm_internal, llm_config, llm_unconfigured, run_llm_cron
from .llm_event import _env_on

_OP_CAP_KEY = 'woow_llm_backoffice_operation_cap'
_OP_CAP = object()
_OP_MODEL = 'woow_paas_platform.llm_backoffice_operation'
_ORDER = 'woow_paas_platform.llm_credit_order'
_OBL_MODEL = 'woow_paas_platform.llm_backoffice_key_obligation'
_OBL_CAP_KEY = 'woow_llm_backoffice_key_obligation_cap'
_OBL_CAP = object()


def _cap(records):
    return records.with_context(**{_OP_CAP_KEY: _OP_CAP})


def _valid_token_page(items, total, page, page_size):
    return token_recovery.valid_page(items, total, page, page_size, nac.INT32_MAX)


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
    last_attempt_at = fields.Datetime(readonly=True, index=True)
    result_code = fields.Char(readonly=True)
    result_summary = fields.Char(readonly=True)
    evidence_json = fields.Json(readonly=True, groups='base.group_system')
    key_target_count = fields.Integer(readonly=True, default=0, groups='base.group_system')
    key_done_count = fields.Integer(readonly=True, default=0)
    key_failed_count = fields.Integer(readonly=True, default=0)
    key_obligation_ids = fields.One2many(_OBL_MODEL, 'operation_id', readonly=True,
                                         groups='base.group_system')
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
                ('state', 'in', ('active', 'disabled', 'failed_reveal', 'creating')),
            ]
            keys = key_model.search(key_domain, order='id')
            key_target_count = len(keys)
            key_snapshots = [{
                'key_id': key.id, 'account_id_snapshot': key.account_id.id,
                'newapi_user_id_snapshot': key.account_id.newapi_user_id or 0,
                'newapi_name_snapshot': key.newapi_name or False,
                'token_id_snapshot': key.newapi_token_id or 0,
                'state_snapshot': key.state,
            } for key in keys]
        else:
            key_target_count = 0
            key_snapshots = []
        fingerprint = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
        model = _cap(self.sudo())
        existing = model.search([('request_id', '=', request_id)], limit=1)
        if existing:
            if existing.fingerprint != fingerprint:
                raise ValidationError(_('This backoffice request was already used.'))
            return existing
        if operation == 'disable_all_keys':
            values['key_target_count'] = key_target_count
        queued = model.create(dict(values, request_id=request_id, fingerprint=fingerprint))
        if key_snapshots:
            _obligation_cap(self.env[_OBL_MODEL].sudo()).create([{
                'operation_id': queued.id, **snapshot,
                'scan_required': bool(snapshot['newapi_name_snapshot'])
                                 or not snapshot['token_id_snapshot'],
                'scan_complete': not bool(snapshot['newapi_name_snapshot'])
                                 and bool(snapshot['token_id_snapshot']),
                'pending_tokens_json': ({str(snapshot['token_id_snapshot']): {
                    'attempts': 0, 'retry_at': 0,
                }} if snapshot['token_id_snapshot'] else {}),
                'prefer_scan': not bool(snapshot['token_id_snapshot']),
            } for snapshot in key_snapshots])
        return queued

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
            cr.execute(
                "SELECT op.id FROM woow_paas_platform_llm_backoffice_operation op "
                "WHERE op.state = 'pending' AND (op.operation <> 'disable_all_keys' OR "
                "NOT EXISTS (SELECT 1 FROM woow_paas_platform_llm_backoffice_key_obligation ob "
                "WHERE ob.operation_id = op.id AND ob.state = 'pending') OR "
                "EXISTS (SELECT 1 FROM woow_paas_platform_llm_backoffice_key_obligation ob "
                "WHERE ob.operation_id = op.id AND ob.state = 'pending' "
                "AND (ob.next_work_at IS NULL OR ob.next_work_at <= (NOW() AT TIME ZONE 'UTC')))) "
                "ORDER BY op.last_attempt_at NULLS FIRST, op.id "
                "FOR UPDATE OF op SKIP LOCKED LIMIT 10")
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
                bound_env = _env_on(self.env.registry, cr)
                env = bound_env['res.users'].with_user(self.env.uid).with_context(
                    dict(self.env.context)).env
                op = env[_OP_MODEL].sudo().browse(operation_id).exists()
                if not op or op.state != 'pending':
                    continue
                _cap(op).write({'last_attempt_at': fields.Datetime.now()})
                actor = op.operator_id
                actor_env = bound_env['res.users'].with_user(actor).with_context(
                    dict(self.env.context)).env
                try:
                    with cr.savepoint():
                        if op.operation == 'manual_credit':
                            service = env['woow_paas_platform.llm_manual_credit_service']
                            result = service._repair_accepted_manual(
                                op.id, op.fingerprint, op.account_id.id, op.points,
                                op.reason, actor.id)
                            if result is None:
                                _named_system_operator(actor_env, actor)
                                result = actor_env[
                                    'woow_paas_platform.llm_manual_credit_service']._create_manual(
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
                            _named_system_operator(actor_env, actor)
                            order = actor_env[_ORDER].sudo().browse(op.order_id.id).with_user(actor)
                            deadline = min(time.monotonic() + 8, tick.deadline)
                            order._admin_review(op.decision, op.reason,
                                                operator_id=actor.id, deadline=deadline)
                            result_order_id = order.id
                            result_summary = order.name
                            result_evidence = order.evidence_json
                        elif op.operation == 'recharge':
                            _named_system_operator(actor_env, actor)
                            order = actor_env[_ORDER].sudo().browse(op.order_id.id).with_user(actor)
                            deadline = min(time.monotonic() + 8, tick.deadline)
                            result_id = order._create_recharge(
                                op.reason, operator_id=actor.id, deadline=deadline)
                            order = env[_ORDER].sudo().browse(result_id)
                            result_order_id = result_id
                            result_summary = order.name
                            result_evidence = order.evidence_json
                        elif op.operation == 'disable_all_keys':
                            _named_system_operator(actor_env, actor)
                            self._process_disable_keys(actor_env, op, actor, tick=tick)
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
        """Claim one fair per-key unit; unresolved identity remains durably pending."""
        Obligation = env[_OBL_MODEL].sudo()
        env.cr.execute(
            'SELECT id FROM woow_paas_platform_llm_backoffice_key_obligation '
            'WHERE operation_id = %s AND state = %s '
            'AND (next_work_at IS NULL OR next_work_at <= NOW()) '
            'ORDER BY work_count, next_work_at NULLS FIRST, id '
            'LIMIT 1 FOR UPDATE SKIP LOCKED', (op.id, 'pending'))
        row = env.cr.fetchone()
        if not row:
            pending = Obligation.search_count([
                ('operation_id', '=', op.id), ('state', '=', 'pending')])
            if pending and tick:
                tick.more = True
                tick.delay = 5
            _cap(op).write({
                'state': 'done' if not pending else 'pending',
                'result_code': 'ok' if not pending else 'pending',
                'result_summary': _('All snapshotted API keys are disabled or were not created.') if not pending
                                  else _('Some API keys still need verification.'),
                'key_done_count': Obligation.search_count([
                    ('operation_id', '=', op.id), ('state', '=', 'done')]),
                'key_failed_count': pending,
                'processed_at': fields.Datetime.now() if not pending else False,
            })
            return
        obligation = Obligation.browse(row[0])
        if obligation.next_work_at and obligation.next_work_at > fields.Datetime.now():
            if tick:
                tick.more = True
                tick.delay = 5
            return
        if tick and not tick.can_take(need_seconds=nac.MIN_CALL_SECONDS):
            tick.more = True
            return
        Key = env['woow_paas_platform.llm_api_key'].sudo()
        key = Key.browse(obligation.key_id.id).exists()
        if not key or key.account_id.id != obligation.account_id_snapshot:
            self._obligation_defer(obligation, 'mirror_missing')
            return
        if tick:
            _bounded_lock_wait(env.cr, tick.deadline)
        # Match free-cap and management lock order: account then key. NO KEY
        # UPDATE keeps FK checks from blocking while serializing state changes.
        env.cr.execute(
            'SELECT k.id FROM woow_paas_platform_llm_account a '
            'JOIN woow_paas_platform_llm_api_key k ON k.account_id = a.id '
            'WHERE a.id = %s AND k.id = %s FOR NO KEY UPDATE OF a, k',
            (obligation.account_id_snapshot, key.id))
        if not env.cr.fetchone():
            self._obligation_defer(obligation, 'mirror_missing')
            return
        key.invalidate_recordset()
        account = env['woow_paas_platform.llm_account'].sudo().browse(
            obligation.account_id_snapshot).exists()
        if account:
            account.invalidate_recordset(['newapi_user_id', 'partner_id', 'state'])
        if (not account or account.newapi_user_id != obligation.newapi_user_id_snapshot
                or key.newapi_name != obligation.newapi_name_snapshot):
            self._obligation_defer(obligation, 'identity_changed')
            return
        if type(obligation.newapi_user_id_snapshot) is not int or obligation.newapi_user_id_snapshot <= 0:
            self._obligation_defer(obligation, 'upstream_user_unresolved')
            return
        # A definite initial rejection may have committed before this snapshot's
        # obligation INSERT became visible to its finalizer. Only its explicit
        # receipt proves noncreation; user deletion of an unknown key does not.
        if key.creation_rejected and key.state == 'deleted' and not key.newapi_token_id:
            self._settle_not_created_obligations(obligation)
            if obligation.state == 'done':
                return
        # This durable stop precedes remote work, so the free-cap reset worker
        # can never race a failed/slow upstream disable and reactivate the key.
        if key.disabled_by != 'ops':
            _llm_internal(key).write({'disabled_by': 'ops'})
        # A finalizer can commit between the snapshot read and obligation INSERT.
        # Merge its committed ID under the key lock even when discovery is down.
        pending_tokens = dict(obligation.pending_tokens_json or {})
        if (key.newapi_token_id and key.newapi_token_id not in (obligation.disabled_tokens_json or [])
                and str(key.newapi_token_id) not in pending_tokens):
            pending_tokens[str(key.newapi_token_id)] = {'attempts': 0, 'retry_at': 0}
            _obligation_cap(obligation).write({
                'pending_tokens_json': pending_tokens, 'prefer_scan': False,
            })
        if tick:
            tick.took()
        try:
            action = token_recovery.next_action(
                scan_required=obligation.scan_required,
                scan_complete=obligation.scan_complete,
                pending=pending_tokens, prefer_scan=obligation.prefer_scan,
                now=int(time.time()))
            if action == 'scan':
                self._scan_obligation_page(obligation, account, key, actor, tick)
            elif action == 'disable':
                self._disable_obligation_token(obligation, account, key, actor, tick)
            elif not pending_tokens and not obligation.scan_required and obligation.disabled_tokens_json:
                if self._obligation_creator_quiet(obligation, key):
                    _obligation_cap(obligation).write({'state': 'done', 'result_code': 'ok'})
                    if key.state not in ('failed_reveal', 'deleted'):
                        _llm_internal(key).write({'state': 'disabled', 'disabled_by': 'ops'})
            else:
                retry_times = [entry['retry_at'] for entry in pending_tokens.values()]
                next_at = min(retry_times) if retry_times else int(time.time()) + 60
                _obligation_cap(obligation).write({
                    'next_work_at': datetime.fromtimestamp(next_at, timezone.utc)
                    .replace(tzinfo=None),
                })
        except Exception:  # persist only a safe code, never exception text or credentials
            self._obligation_defer(obligation, 'upstream_unconfirmed')
        op.invalidate_recordset(['key_obligation_ids'])
        pending = Obligation.search_count([('operation_id', '=', op.id), ('state', '=', 'pending')])
        done = Obligation.search_count([('operation_id', '=', op.id), ('state', '=', 'done')])
        _cap(op).write({
            'state': 'pending' if pending else 'done',
            'result_code': 'pending' if pending else 'ok',
            'result_summary': (_('Disable progress: %(done)s complete, %(pending)s pending.',
                                 done=done, pending=pending) if pending
                               else _('All snapshotted API keys are disabled or were not created.')),
            'key_done_count': done, 'key_failed_count': pending,
            'processed_at': fields.Datetime.now() if not pending else False,
        })

    @api.model
    def _disable_obligation_token(self, obligation, account, key, actor, tick):
        pending = dict(obligation.pending_tokens_json or {})
        now_epoch = int(time.time())
        due = sorted((value.get('attempts', 0), value.get('retry_at', 0), token)
                     for token, value in pending.items() if value.get('retry_at', 0) <= now_epoch)
        if not due:
            # A token retry or the next name scan is the next durable work.
            self._obligation_defer(obligation, 'retry_wait')
            if tick:
                tick.more = True
                tick.delay = 5
            return
        token = due[0][2]
        ip = account._llm_tenant_ip()
        up = account._llm_up_token(nac.ROLE_CRON)
        client = account._llm_client(nac.ROLE_CRON)
        deadline = min(time.monotonic() + 8, tick.deadline) if tick else time.monotonic() + 8
        result = client.user_set_token_status(
            ip, up, int(token), False, timeout=nac.TIMEOUT_CRON_WRITE, deadline=deadline)
        if not isinstance(result, nac.Ok):
            entry = pending[token]
            entry['attempts'] = entry.get('attempts', 0) + 1
            entry['retry_at'] = now_epoch + min(3600, 60 * 2 ** min(entry['attempts'] - 1, 6))
            _obligation_cap(obligation).write({'pending_tokens_json': pending, 'result_code': 'retry',
                              'work_count': obligation.work_count + 1,
                              'prefer_scan': True,
                              'next_work_at': False})
            if tick:
                tick.more = True
                tick.delay = 5
            return
        pending.pop(token)
        disabled = list(obligation.disabled_tokens_json or [])
        if int(token) not in disabled:
            disabled.append(int(token))
        _obligation_cap(obligation).write({'pending_tokens_json': pending, 'disabled_tokens_json': disabled,
                          'result_code': 'token_disabled',
                          'prefer_scan': True,
                          'work_count': obligation.work_count + 1, 'next_work_at': False})
        if (not pending and not obligation.scan_required
                and self._obligation_creator_quiet(obligation, key)):
            _obligation_cap(obligation).write({'state': 'done', 'result_code': 'ok'})
            if key.state not in ('failed_reveal', 'creating', 'deleted'):
                _llm_internal(key).write({'state': 'disabled', 'disabled_by': 'ops'})
        env = obligation.env
        env['woow_paas_platform.llm_event']._llm_record(
            'key_status', account_id=account.id, partner_id=account.partner_id.id,
            uid=actor.id, target_model='woow_paas_platform.llm_api_key',
            target_id=key.id, result='ok')

    @api.model
    def _scan_obligation_page(self, obligation, account, key, actor, tick):
        page_size = 100
        page = obligation.scan_page or 1
        ip = account._llm_tenant_ip()
        up = account._llm_up_token(nac.ROLE_CRON)
        client = account._llm_client(nac.ROLE_CRON)
        deadline = min(time.monotonic() + 8, tick.deadline) if tick else time.monotonic() + 8
        result = client.user_list_tokens(ip, up, page, page_size,
                                         timeout=nac.TIMEOUT_CRON_READ, deadline=deadline)
        data = result.data if isinstance(result, nac.Ok) else None
        items = data.get('items') if isinstance(data, dict) else None
        total = data.get('total') if isinstance(data, dict) else None
        if not _valid_token_page(items, total, page, page_size):
            raise UserError(_('Upstream key identity could not be verified.'))
        pending = dict(obligation.pending_tokens_json or {})
        disabled = set(obligation.disabled_tokens_json or [])
        for item in items:
            if item['name'] == obligation.newapi_name_snapshot and item['id'] not in disabled:
                pending.setdefault(str(item['id']), {'attempts': 0, 'retry_at': 0})
        head = obligation.scan_head
        if page == 1:
            head = token_recovery.page_fingerprint(items)
        elif obligation.scan_total != total:
            _obligation_cap(obligation).write({
                'scan_page': 1, 'scan_total': False, 'scan_head': False,
                'pending_tokens_json': pending, 'work_count': obligation.work_count + 1,
                'result_code': 'scan_restarted',
            })
            return
        next_page = page + 1
        is_last = page * page_size >= total
        if is_last:
            if pending:
                _obligation_cap(obligation).write({
                    'scan_page': 1, 'scan_total': total, 'scan_head': head,
                    'pending_tokens_json': pending, 'prefer_scan': False,
                    'work_count': obligation.work_count + 1,
                    'result_code': 'tokens_pending',
                    'next_work_at': (False if any(
                        value.get('retry_at', 0) <= int(time.time())
                        for value in pending.values()) else
                        datetime.fromtimestamp(
                            min(value['retry_at'] for value in pending.values()),
                            timezone.utc).replace(tzinfo=None)),
                })
                return
            if not self._obligation_creator_quiet(obligation, key):
                return
            # Re-read page one and compare its exact head before accepting the
            # traversal. This is forbidden while any token ID is pending.
            verify = client.user_list_tokens(
                ip, up, 1, page_size, timeout=nac.TIMEOUT_CRON_READ, deadline=deadline)
            verify_data = verify.data if isinstance(verify, nac.Ok) else None
            verify_items = verify_data.get('items') if isinstance(verify_data, dict) else None
            verify_total = verify_data.get('total') if isinstance(verify_data, dict) else None
            if not _valid_token_page(verify_items, verify_total, 1, page_size):
                raise UserError(_('Upstream key identity could not be verified.'))
            baseline_total = total if page == 1 else obligation.scan_total
            if not token_recovery.head_matches(
                    total=total, baseline_total=baseline_total, head=head,
                    verify_items=verify_items, verify_total=verify_total,
                    page_size=page_size, max_id=nac.INT32_MAX):
                _obligation_cap(obligation).write({'scan_page': 1, 'scan_total': False,
                                  'scan_head': False, 'pending_tokens_json': pending,
                                  'prefer_scan': False,
                                  'work_count': obligation.work_count + 1,
                                  'result_code': 'scan_restarted'})
                return
            # An unknown create with no discovered token is never treated as
            # absence proof. A previously observed, disabled token plus a full
            # exact-name scan is the only resolvable failed-reveal case.
            if disabled:
                if not token_recovery.stable_scan_complete(
                        pending=pending, disabled=disabled, total=total,
                        baseline_total=baseline_total, head=head,
                        verify_items=verify_items, verify_total=verify_total,
                        page_size=page_size, max_id=nac.INT32_MAX):
                    raise UserError(_('Upstream key identity could not be verified.'))
                _obligation_cap(obligation).write({'scan_page': 1, 'scan_total': total, 'scan_head': head,
                                  'scan_complete': True, 'state': 'done', 'result_code': 'ok',
                                  'work_count': obligation.work_count + 1, 'next_work_at': False})
                if key.state not in ('failed_reveal', 'creating', 'deleted'):
                    _llm_internal(key).write({'state': 'disabled', 'disabled_by': 'ops'})
                return
            _obligation_cap(obligation).write({'scan_page': 1, 'scan_total': total, 'scan_head': head,
                              'work_count': obligation.work_count + 1,
                              'result_code': 'identity_unresolved',
                              'next_work_at': fields.Datetime.now() + timedelta(minutes=1)})
            return
        _obligation_cap(obligation).write({'scan_page': next_page, 'scan_total': total, 'scan_head': head,
                          'pending_tokens_json': pending, 'work_count': obligation.work_count + 1,
                          'prefer_scan': False,
                          'result_code': 'scan_page'})

    @api.model
    def _obligation_creator_quiet(self, obligation, key):
        """A stop fences later POSTs; wait out the bounded request already sent."""
        if key.state != 'creating' and obligation.state_snapshot != 'creating':
            return True
        quiet_after = key.create_date + timedelta(seconds=nac.KEY_CREATE_BUDGET_SECONDS + 10)
        if fields.Datetime.now() < quiet_after:
            _obligation_cap(obligation).write({
                'result_code': 'creator_pending', 'next_work_at': quiet_after,
                'work_count': obligation.work_count + 1,
            })
            return False
        # Do not occupy the live-trial unique index after an interrupted create.
        if key.state == 'creating':
            _llm_internal(key).write({'state': 'failed_reveal'})
        return True

    @api.model
    def _lock_key_obligations(self, key_id):
        """Finalizers use the worker's operation -> obligation -> key lock order."""
        self.env.cr.execute(
            'SELECT op.id FROM woow_paas_platform_llm_backoffice_operation op '
            'WHERE EXISTS (SELECT 1 FROM woow_paas_platform_llm_backoffice_key_obligation ob '
            'WHERE ob.operation_id=op.id AND ob.key_id=%s) ORDER BY op.id FOR UPDATE', (key_id,))
        operation_ids = [row[0] for row in self.env.cr.fetchall()]
        if not operation_ids:
            return self.env[_OBL_MODEL].sudo().browse()
        self.env.cr.execute(
            'SELECT id FROM woow_paas_platform_llm_backoffice_key_obligation '
            'WHERE key_id=%s AND operation_id=ANY(%s) ORDER BY id FOR UPDATE',
            (key_id, operation_ids))
        return self.env[_OBL_MODEL].sudo().browse([row[0] for row in self.env.cr.fetchall()])

    @api.model
    def _settle_not_created_obligations(self, obligations):
        """Accept definite local noncreation, never erase an observed token ID."""
        for obligation in obligations:
            if (obligation.pending_tokens_json or obligation.token_id_snapshot
                    or obligation.disabled_tokens_json):
                continue
            _obligation_cap(obligation).write({
                'state': 'done', 'result_code': 'not_created', 'next_work_at': False,
                'scan_required': False, 'scan_complete': False,
                'scan_head': False, 'scan_total': False, 'scan_page': 1,
            })
            op = obligation.operation_id
            remaining = self.env[_OBL_MODEL].sudo().search_count([
                ('operation_id', '=', op.id), ('state', '=', 'pending')])
            _cap(op).write({
                'state': 'pending' if remaining else 'done',
                'result_code': 'pending' if remaining else 'ok',
                'key_done_count': op.key_target_count - remaining, 'key_failed_count': remaining,
                'processed_at': False if remaining else fields.Datetime.now(),
                'result_summary': (_('Some API keys still need verification.') if remaining else
                                   _('All snapshotted API keys are disabled or were not created.')),
            })

    @api.model
    def _reenqueue_finalized_token(self, obligations, token_id):
        """Same transaction as finalization: an unreceipted ID reopens its batch."""
        if not token_id:
            return
        for obligation in obligations:
            if token_id in (obligation.disabled_tokens_json or []):
                continue
            pending = dict(obligation.pending_tokens_json or {})
            pending.setdefault(str(token_id), {'attempts': 0, 'retry_at': 0})
            _obligation_cap(obligation).write({
                'pending_tokens_json': pending, 'state': 'pending',
                'scan_required': bool(obligation.newapi_name_snapshot),
                'scan_complete': False, 'scan_page': 1, 'scan_total': False,
                'scan_head': False, 'prefer_scan': False, 'next_work_at': False,
                'result_code': 'finalized_token_pending',
            })
            op = obligation.operation_id
            remaining = self.env[_OBL_MODEL].sudo().search_count([
                ('operation_id', '=', op.id), ('state', '=', 'pending')])
            _cap(op).write({
                'state': 'pending', 'result_code': 'pending', 'processed_at': False,
                'last_attempt_at': False,
                'key_done_count': op.key_target_count - remaining, 'key_failed_count': remaining,
                'result_summary': _('Some API keys still need verification.'),
            })

    @api.model
    def _obligation_defer(self, obligation, code):
        attempts = obligation.work_count + 1
        delay = min(3600, 30 * 2 ** min(attempts - 1, 7))
        values = {'work_count': attempts, 'result_code': code,
                  'next_work_at': fields.Datetime.now() + timedelta(seconds=delay)}
        if code == 'upstream_unconfirmed':
            # Discovery outages must not block an independently disableable ID.
            # Keep the scan incomplete, but hand the next due unit to a token.
            values['prefer_scan'] = False
            retries = [entry.get('retry_at', 0)
                       for entry in (obligation.pending_tokens_json or {}).values()]
            if retries:
                values['next_work_at'] = min(values['next_work_at'], datetime.fromtimestamp(
                    max(int(time.time()), min(retries)), timezone.utc).replace(tzinfo=None))
        _obligation_cap(obligation).write(values)


class LlmBackofficeKeyObligation(models.Model):
    """Per-key, retryable proof obligation for a batch disable request."""
    _name = _OBL_MODEL
    _description = 'Backoffice API key disable obligation'
    _order = 'work_count, next_work_at, id'

    _sql_constraints = [
        ('operation_key_unique', 'UNIQUE(operation_id, key_id)',
         'A key can have only one obligation in a disable operation.'),
    ]

    operation_id = fields.Many2one(_OP_MODEL, required=True, readonly=True,
                                   ondelete='cascade', index=True)
    key_id = fields.Many2one('woow_paas_platform.llm_api_key', required=True, readonly=True,
                             ondelete='restrict', index=True)
    account_id_snapshot = fields.Integer(required=True, readonly=True)
    newapi_user_id_snapshot = fields.Integer(required=True, readonly=True)
    newapi_name_snapshot = fields.Char(readonly=True)
    token_id_snapshot = fields.Integer(readonly=True)
    state_snapshot = fields.Char(required=True, readonly=True)
    state = fields.Selection([('pending', 'Pending'), ('done', 'Complete')],
                             default='pending', required=True, readonly=True, index=True)
    result_code = fields.Char(readonly=True)
    work_count = fields.Integer(default=0, readonly=True, index=True)
    next_work_at = fields.Datetime(readonly=True, index=True)
    scan_required = fields.Boolean(readonly=True)
    scan_complete = fields.Boolean(readonly=True)
    scan_page = fields.Integer(default=1, readonly=True)
    scan_total = fields.Integer(readonly=True)
    scan_head = fields.Char(readonly=True)
    prefer_scan = fields.Boolean(readonly=True)
    pending_tokens_json = fields.Json(readonly=True)
    disabled_tokens_json = fields.Json(readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        if self.env.context.get(_OBL_CAP_KEY) is not _OBL_CAP:
            raise AccessError(_('Key disable obligations are worker managed.'))
        return super().create(vals_list)

    def write(self, vals):
        if self.env.context.get(_OBL_CAP_KEY) is not _OBL_CAP:
            raise AccessError(_('Key disable obligations are worker managed.'))
        return super().write(vals)

    def unlink(self):
        raise AccessError(_('Key disable obligations cannot be deleted.'))


def _obligation_cap(records):
    return records.with_context(**{_OBL_CAP_KEY: _OBL_CAP})
