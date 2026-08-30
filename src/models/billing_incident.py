"""Durable, relation-free billing incident ledger (#864 PR-D).

Incident rows are deliberately scalar snapshots.  Their source rows may exist
only in an outer transaction which later rolls back, so adding a Many2one here
would make the fail-open record fail exactly when it is needed most.  The model
is a global finance ledger: every account manager reads all rows across
companies by design.
"""

import logging
import math
import re
from datetime import date, datetime

from markupsafe import Markup, escape
from psycopg2 import sql

from odoo import SUPERUSER_ID, _, api, fields, models
from odoo.exceptions import AccessError, UserError


_logger = logging.getLogger(__name__)


class BillingIncident(models.Model):
    _name = 'woow.billing.incident'
    _description = 'Woow Billing Incident'
    _rec_name = 'external_ref'
    _order = 'last_seen_at desc, id desc'

    external_ref = fields.Char(
        required=True,
        index=True,
        help=(
            'Payment incidents store the raw ECPay MerchantTradeNo. '
            'Non-payment callers must use a namespaced key such as '
            '`invoice:<stable-reference>`. Billing incidents are a global '
            'finance ledger: every account manager can read every incident '
            'across companies by design.'
        ),
    )
    kind = fields.Selection(
        selection=[
            ('intent', 'Payment intent'),
            ('charged_not_credited', 'Charged but not credited'),
            ('callback_tx_missing', 'Callback transaction missing'),
            ('invoice_half_settled', 'Invoice half settled'),
            ('bind_failed', 'Card binding failed'),
            ('stuck_pending', 'Stuck pending payment'),
            ('manual_invoice_stale', 'Manual invoice stale'),
        ],
        required=True,
        index=True,
    )
    partner_ref = fields.Integer(index=True)
    partner_email = fields.Char()
    payment_token_ref = fields.Integer(index=True)
    amount = fields.Float()
    credits = fields.Float()
    bonus = fields.Float()
    currency = fields.Char()
    trade_no = fields.Char(index=True)
    rtn_code = fields.Char()
    failure_code = fields.Char()
    state = fields.Selection(
        selection=[
            ('open', 'Open'),
            ('resolved', 'Resolved'),
            ('orphan', 'Orphan'),
            ('needs_human', 'Needs human review'),
        ],
        required=True,
        default='open',
        index=True,
    )
    skip_reason = fields.Char()
    last_skipped_at = fields.Datetime()
    skip_count = fields.Integer(required=True, default=0)
    attempts = fields.Integer(required=True, default=0)
    next_query_at = fields.Datetime(index=True)
    query_attempts = fields.Integer(required=True, default=0)
    first_seen_at = fields.Datetime(
        required=True,
        default=fields.Datetime.now,
        index=True,
    )
    last_seen_at = fields.Datetime(
        required=True,
        default=fields.Datetime.now,
        index=True,
    )
    occurrences = fields.Integer(required=True, default=1)
    assessment_reason = fields.Char()
    detail = fields.Text()
    source_model = fields.Char(
        help='Scalar source model name only; this is not an ORM relation.',
    )
    source_ref = fields.Integer(
        help='Scalar source row id which may legitimately dangle after rollback.',
    )
    manual_check_result = fields.Selection(
        selection=[
            ('ecpay_found', 'Found in ECPay'),
            ('ecpay_not_found', 'Not found in ECPay'),
        ],
    )
    resolution_note = fields.Text()
    resolved_at = fields.Datetime()
    resolved_by_ref = fields.Integer()

    _sql_constraints = [
        (
            'external_ref_uniq',
            'UNIQUE(external_ref)',
            'Billing incident external reference must be unique.',
        ),
        (
            'counters_nonnegative',
            'CHECK(skip_count >= 0 AND attempts >= 0 AND query_attempts >= 0)',
            'Billing incident counters must be non-negative.',
        ),
        (
            'occurrences_positive',
            'CHECK(occurrences >= 1)',
            'Billing incident occurrences must be at least one.',
        ),
        (
            'seen_at_order',
            'CHECK(first_seen_at <= last_seen_at)',
            'Billing incident first seen time cannot be after last seen time.',
        ),
    ]

    # Safety budget, not a performance knob.  The helper may be called while an
    # outer transaction still holds a lock; waiting without a bound would
    # self-deadlock the request thread.
    _LOCK_TIMEOUT = '1s'

    _KINDS = frozenset({
        'intent',
        'charged_not_credited',
        'callback_tx_missing',
        'invoice_half_settled',
        'bind_failed',
        'stuck_pending',
        'manual_invoice_stale',
    })
    _LOG_STATES = frozenset({'open', 'orphan', 'needs_human'})
    _MANUAL_RESULTS = frozenset({'ecpay_found', 'ecpay_not_found'})
    _MANAGER_WRITABLE_FIELDS = frozenset({
        'manual_check_result',
        'resolution_note',
    })

    _LOG_CHAR_FIELDS = frozenset({
        'external_ref', 'kind', 'partner_email', 'currency', 'trade_no',
        'rtn_code', 'failure_code', 'state', 'skip_reason',
        'assessment_reason', 'detail', 'source_model',
    })
    _LOG_INTEGER_FIELDS = frozenset({
        'partner_ref', 'payment_token_ref', 'skip_count', 'attempts',
        'query_attempts', 'source_ref',
    })
    _LOG_FLOAT_FIELDS = frozenset({'amount', 'credits', 'bonus'})
    _LOG_DATETIME_FIELDS = frozenset({'last_skipped_at', 'next_query_at'})
    _LOG_FIELDS = (
        _LOG_CHAR_FIELDS | _LOG_INTEGER_FIELDS | _LOG_FLOAT_FIELDS |
        _LOG_DATETIME_FIELDS
    )
    _NONNEGATIVE_INPUT_COUNTERS = frozenset({
        'skip_count', 'attempts', 'query_attempts',
    })
    _SENSITIVE_MARKER_RE = re.compile(
        r'(?:\bresult[\s_-]*data\b|\bcard[46]?[\s_-]*no\b|'
        r'\bcard[\s_-]*number\b|\bcvv2?\b|\bpan\b)',
        re.IGNORECASE,
    )
    _PAN_CANDIDATE_RE = re.compile(
        r'(?<!\d)(?:\d[\s-]*){13,19}(?!\d)')
    _TRADE_REFERENCE_PREFIX_RE = re.compile(
        r'(?:merchant[\s_-]*trade[\s_-]*no|trade[\s_-]*no|mtn)'
        r'\s*[:=#-]?\s*$',
        re.IGNORECASE,
    )

    def write(self, values):
        """Keep the audit snapshot immutable outside controlled sudo flows."""
        if not self.env.su:
            if not self.env.user.has_group(
                    'account.group_account_manager'):
                raise AccessError(_(
                    'Only account managers may update billing incidents.'
                ))
            forbidden = set(values) - self._MANAGER_WRITABLE_FIELDS
            if forbidden:
                raise AccessError(_(
                    'Account managers may update only the manual verification '
                    'result and resolution note.'
                ))
        return super().write(values)

    @api.model
    def _sensitive_content_category(self, field_name, value):
        if self._SENSITIVE_MARKER_RE.search(value):
            return 'sensitive_marker'
        # These two fields are explicitly shaped as ECPay references.  Numeric
        # MTNs/TradeNos must not be mistaken for PANs by coincidence.
        if field_name in ('external_ref', 'trade_no'):
            return False
        for match in self._PAN_CANDIDATE_RE.finditer(value):
            digits = ''.join(character for character in match.group()
                             if character.isdigit())
            if not 13 <= len(digits) <= 19:
                continue
            prefix = value[max(0, match.start() - 40):match.start()]
            if self._TRADE_REFERENCE_PREFIX_RE.search(prefix):
                continue
            checksum = 0
            for index, character in enumerate(reversed(digits)):
                number = int(character)
                if index % 2:
                    number *= 2
                    if number > 9:
                        number -= 9
                checksum += number
            if checksum % 10 == 0:
                return 'luhn_pan'
        return False

    @api.model
    def _validate_log_snapshot(self, snapshot):
        """Return a SQL-ready scalar snapshot, or ``False`` fail-soft.

        Validation happens before the independent cursor.  The allowlist is a
        shape boundary, not a complete PCI boundary: callers remain responsible
        for passing only safe summaries.  Obvious sensitive markers and
        Luhn-valid PAN-like detail are rejected as defense in depth.
        """
        if not isinstance(snapshot, dict):
            _logger.error(
                '[BILLING_INCIDENT] _log rejected non-dict snapshot type=%s',
                type(snapshot).__name__,
            )
            return False
        if any(not isinstance(key, str) for key in snapshot):
            _logger.error(
                '[BILLING_INCIDENT] _log rejected snapshot with non-string key')
            return False
        unknown = sorted(set(snapshot) - self._LOG_FIELDS)
        if unknown:
            _logger.error(
                '[BILLING_INCIDENT] _log rejected unknown snapshot keys=%s',
                unknown,
            )
            return False
        external_ref = snapshot.get('external_ref')
        kind = snapshot.get('kind')
        if not isinstance(external_ref, str) or not external_ref.strip():
            _logger.error(
                '[BILLING_INCIDENT] _log requires a non-empty external_ref')
            return False
        if kind not in self._KINDS:
            _logger.error(
                '[BILLING_INCIDENT] _log requires a supported kind; got=%r',
                kind,
            )
            return False
        if 'state' in snapshot and snapshot['state'] not in self._LOG_STATES:
            _logger.error(
                '[BILLING_INCIDENT] _log rejected state=%r',
                snapshot['state'],
            )
            return False

        normalized = {}
        for name, value in snapshot.items():
            if name in self._LOG_CHAR_FIELDS:
                if value is None or value is False:
                    if name in ('external_ref', 'kind'):
                        return False
                    normalized[name] = None
                elif isinstance(value, str):
                    normalized[name] = value
                else:
                    _logger.error(
                        '[BILLING_INCIDENT] _log rejected %s type=%s',
                        name, type(value).__name__,
                    )
                    return False
            elif name in self._LOG_INTEGER_FIELDS:
                if value is None or value is False:
                    normalized[name] = None
                elif isinstance(value, int) and not isinstance(value, bool):
                    normalized[name] = value
                else:
                    _logger.error(
                        '[BILLING_INCIDENT] _log rejected %s type=%s',
                        name, type(value).__name__,
                    )
                    return False
            elif name in self._LOG_FLOAT_FIELDS:
                if value is None or value is False:
                    normalized[name] = None
                elif (isinstance(value, (int, float))
                      and not isinstance(value, bool)
                      and math.isfinite(value)):
                    normalized[name] = float(value)
                else:
                    _logger.error(
                        '[BILLING_INCIDENT] _log rejected %s type/value', name)
                    return False
            elif name in self._LOG_DATETIME_FIELDS:
                if value is None or value is False:
                    normalized[name] = None
                elif isinstance(value, (str, date, datetime)):
                    try:
                        normalized[name] = fields.Datetime.to_datetime(value)
                    except (TypeError, ValueError):
                        _logger.error(
                            '[BILLING_INCIDENT] _log rejected %s datetime=%r',
                            name, value,
                        )
                        return False
                else:
                    _logger.error(
                        '[BILLING_INCIDENT] _log rejected %s type=%s',
                        name, type(value).__name__,
                    )
                    return False
        for name, value in normalized.items():
            if not isinstance(value, str):
                continue
            category = self._sensitive_content_category(name, value)
            if category:
                _logger.error(
                    '[BILLING_INCIDENT] _log rejected sensitive content '
                    'field=%s category=%s',
                    name, category,
                )
                return False
        for name in self._NONNEGATIVE_INPUT_COUNTERS:
            if name in normalized and (
                    normalized[name] is None or normalized[name] < 0):
                _logger.error(
                    '[BILLING_INCIDENT] _log rejected negative/null %s', name)
                return False
        return normalized

    @api.model
    def _log(self, snapshot):
        """Atomically insert/update one durable incident in a new transaction.

        Callers must freeze every needed ORM value into a flat scalar ``dict``
        *before* entering a payment/external-call failure region.  This method
        never browses or updates the outer topup, invoice, partner, or token.
        It returns only the integer incident id (or ``False``) so no recordset
        backed by a closed cursor can escape.
        """
        try:
            values = self._validate_log_snapshot(snapshot)
            if values is False:
                return False

            log_ref = str(escape(values['external_ref']))
            now = fields.Datetime.now()
            # Duplicate observations may refresh evidence, never lifecycle.
            # State changes belong to explicit transition methods.
            explicit_evidence = [
                name for name in values
                if name not in ('external_ref', 'kind', 'state')
            ]
            insert_values = dict(values)
            insert_values.setdefault('state', 'open')
            insert_values.setdefault('skip_count', 0)
            insert_values.setdefault('attempts', 0)
            insert_values.setdefault('query_attempts', 0)
            insert_values.update({
                'first_seen_at': now,
                'last_seen_at': now,
                'occurrences': 1,
                'create_uid': SUPERUSER_ID,
                'write_uid': SUPERUSER_ID,
                'create_date': now,
                'write_date': now,
            })
            columns = list(insert_values)
            updates = [
                sql.SQL('{field} = EXCLUDED.{field}').format(
                    field=sql.Identifier(name))
                for name in explicit_evidence
            ]
            updates.extend([
                sql.SQL('last_seen_at = EXCLUDED.last_seen_at'),
                sql.SQL('occurrences = {table}.occurrences + 1').format(
                    table=sql.Identifier(self._table)),
                sql.SQL('write_uid = EXCLUDED.write_uid'),
                sql.SQL('write_date = EXCLUDED.write_date'),
            ])
            query = sql.SQL(
                'INSERT INTO {table} ({columns}) VALUES ({placeholders}) '
                'ON CONFLICT (external_ref) DO UPDATE SET {updates} '
                'WHERE {table}.kind = EXCLUDED.kind '
                "AND {table}.state <> 'resolved' "
                'RETURNING id, state'
            ).format(
                table=sql.Identifier(self._table),
                columns=sql.SQL(', ').join(map(sql.Identifier, columns)),
                placeholders=sql.SQL(', ').join(
                    sql.Placeholder() for _name in columns),
                updates=sql.SQL(', ').join(updates),
            )

            with self.env.registry.cursor() as cr:
                # Must be the first statement: the independent connection can
                # otherwise wait on the caller's own outer transaction forever.
                cr.execute('SET LOCAL lock_timeout = %s',
                           (self._LOCK_TIMEOUT,))
                api.Environment(cr, SUPERUSER_ID, {})
                cr.execute(query, [insert_values[name] for name in columns])
                row = cr.fetchone()
                if not row:
                    cr.execute(
                        sql.SQL(
                            'SELECT id, kind, state FROM {table} '
                            'WHERE external_ref = %s'
                        ).format(table=sql.Identifier(self._table)),
                        (values['external_ref'],),
                    )
                    existing = cr.fetchone()
                    if not existing:
                        _logger.critical(
                            '[BILLING_INCIDENT] upsert returned no row for %s',
                            log_ref,
                        )
                        cr.commit()
                        return False
                    incident_id, existing_kind, existing_state = existing
                    if existing_kind != values['kind']:
                        _logger.critical(
                            '[BILLING_INCIDENT] kind mismatch external_ref=%s '
                            'existing=%s incoming=%s; refusing downgrade',
                            log_ref, existing_kind, values['kind'],
                        )
                        cr.commit()
                        return False
                    # A late duplicate is evidence, not permission to reopen a
                    # resolved incident.  Return its stable scalar id unchanged.
                    if existing_state == 'resolved':
                        cr.commit()
                        return incident_id
                    _logger.critical(
                        '[BILLING_INCIDENT] unresolved upsert produced no row '
                        'external_ref=%s state=%s',
                        log_ref, existing_state,
                    )
                    cr.commit()
                    return False
                incident_id, incident_state = row
                cr.commit()
            if incident_state in ('orphan', 'needs_human'):
                try:
                    self._schedule_review_activities(incident_id)
                except Exception:
                    # The incident transaction is already committed.  Activity
                    # is a second best-effort transaction and must never change
                    # the caller-visible result or recurse through `_log`.
                    _logger.error(
                        '[BILLING_INCIDENT] incident id=%s persisted but Todo '
                        'activity creation failed; incident remains durable',
                        incident_id,
                        exc_info=True,
                    )
            return incident_id
        except Exception:
            _logger.critical(
                '[BILLING_INCIDENT] money evidence could not be persisted; '
                'the caller flow continues unchanged',
                exc_info=True,
            )
            return False

    @api.model
    def _account_manager_users(self, env):
        group = env.ref(
            'account.group_account_manager', raise_if_not_found=False)
        return group.users if group else env['res.users']

    @api.model
    def _acquire_review_activity_lock(self, cr, incident_id):
        cr.execute(
            'SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))',
            ('woow.billing.incident.activity:%s' % incident_id,),
        )

    @api.model
    def _schedule_review_activities(self, incident_id):
        """Best-effort second transaction: one Todo per incident/user."""
        with self.env.registry.cursor() as cr:
            cr.execute('SET LOCAL lock_timeout = %s',
                       (self._LOCK_TIMEOUT,))
            env = api.Environment(cr, SUPERUSER_ID, {})
            # Serialize search+create across concurrent duplicate callbacks.
            self._acquire_review_activity_lock(cr, incident_id)
            cr.execute(
                sql.SQL(
                    'SELECT external_ref, kind, state FROM {table} WHERE id = %s'
                ).format(table=sql.Identifier(self._table)),
                (incident_id,),
            )
            incident = cr.fetchone()
            if not incident or incident[2] not in ('orphan', 'needs_human'):
                cr.commit()
                return False

            todo = env.ref(
                'mail.mail_activity_data_todo', raise_if_not_found=False)
            model = env['ir.model']._get(self._name)
            users = self._account_manager_users(env)
            if not users:
                _logger.error(
                    '[BILLING_INCIDENT] account.group_account_manager has zero '
                    'users; incident id=%s has no Todo assignee', incident_id)
                cr.commit()
                return False
            if not (todo and model):
                _logger.error(
                    '[BILLING_INCIDENT] cannot create Todo for incident id=%s '
                    '(todo=%s model=%s)',
                    incident_id, bool(todo), bool(model))
                cr.commit()
                return False

            cr.execute(
                'SELECT user_id FROM mail_activity WHERE res_model = %s '
                'AND res_id = %s AND activity_type_id = %s',
                (self._name, incident_id, todo.id),
            )
            existing_user_ids = {row[0] for row in cr.fetchall()}
            external_ref, kind, _state = incident
            summary = _('Billing incident requires manual review')
            note_template = Markup.escape(_(
                'Billing incident %(external_ref)s (%(kind)s) requires an '
                'ECPay verification and an explicit resolution note.'
            ))
            note = note_template % {
                'external_ref': escape(external_ref),
                'kind': escape(kind),
            }
            safe_res_name = str(escape(external_ref))
            now = fields.Datetime.now()
            for user in users:
                if user.id in existing_user_ids:
                    continue
                # `mail.activity.create()` calls message_subscribe/notify on
                # the target and therefore requires mail.thread.  This ledger
                # deliberately has no chatter/mixin, so insert the Todo row
                # directly in this best-effort transaction.
                cr.execute(
                    'INSERT INTO mail_activity '
                    '(res_model_id, res_model, res_id, res_name, '
                    'activity_type_id, user_id, summary, note, date_deadline, '
                    'automated, active, create_uid, write_uid, create_date, '
                    'write_date) VALUES '
                    '(%s, %s, %s, %s, %s, %s, %s, %s, %s, '
                    'false, true, %s, %s, %s, %s)',
                    (
                        model.id, self._name, incident_id, safe_res_name,
                        todo.id, user.id, summary, str(note), fields.Date.today(),
                        SUPERUSER_ID, SUPERUSER_ID, now, now,
                    ),
                )
            cr.commit()
            return True

    @api.model
    def _clear_review_activities(self, incident_id):
        """Best-effort second transaction; delete only our Todo rows."""
        with self.env.registry.cursor() as cr:
            cr.execute('SET LOCAL lock_timeout = %s',
                       (self._LOCK_TIMEOUT,))
            env = api.Environment(cr, SUPERUSER_ID, {})
            self._acquire_review_activity_lock(cr, incident_id)
            todo = env.ref(
                'mail.mail_activity_data_todo', raise_if_not_found=False)
            if todo:
                cr.execute(
                    'DELETE FROM mail_activity '
                    'WHERE res_model = %s AND res_id = %s '
                    'AND activity_type_id = %s',
                    (self._name, incident_id, todo.id),
                )
            cr.commit()
            return True

    def _transition_resolved_current_cursor(
            self, manual_check_result, resolution_note, resolved_by_ref):
        """Private UI transition; never open the independent `_resolve` cursor."""
        self.ensure_one()
        self._acquire_review_activity_lock(self.env.cr, self.id)
        self.env.cr.execute(
            sql.SQL('SELECT state FROM {table} WHERE id = %s').format(
                table=sql.Identifier(self._table)),
            (self.id,),
        )
        row = self.env.cr.fetchone()
        if not row:
            raise UserError(_('This billing incident no longer exists.'))
        if row[0] == 'resolved':
            raise UserError(_('This billing incident is already resolved.'))
        self.sudo().write({
            'state': 'resolved',
            'resolved_at': fields.Datetime.now(),
            'resolved_by_ref': resolved_by_ref,
            'manual_check_result': manual_check_result,
            'resolution_note': resolution_note,
        })
        # The UI transaction may already hold this incident row lock.  Delete
        # only this mechanism's Todo on the same cursor to avoid self-lock.
        todo = self.env.ref(
            'mail.mail_activity_data_todo', raise_if_not_found=False)
        if todo:
            self.env.cr.execute(
                'DELETE FROM mail_activity '
                'WHERE res_model = %s AND res_id = %s '
                'AND activity_type_id = %s',
                (self._name, self.id, todo.id),
            )
        return True

    def action_mark_manually_resolved(self):
        """Record an ECPay verification and close; never credit/refund money."""
        self.ensure_one()
        if not self.env.user.has_group('account.group_account_manager'):
            raise AccessError(_(
                'Only account managers may resolve billing incidents.'
            ))
        self.check_access('write')
        manual_check_result = self.manual_check_result
        resolution_note = (self.resolution_note or '').strip()
        resolved_by_ref = self.env.user.id
        if manual_check_result not in self._MANUAL_RESULTS:
            raise UserError(_(
                'Choose whether the MerchantTradeNo was found in ECPay '
                'before resolving this incident.'
            ))
        if not resolution_note:
            raise UserError(_(
                'A resolution note describing the ECPay verification is '
                'required.'
            ))
        return self.sudo()._transition_resolved_current_cursor(
            manual_check_result,
            resolution_note,
            resolved_by_ref,
        )

    @api.model
    def _resolve(self, external_ref, resolution_note=None,
                 resolved_by_ref=None, manual_check_result=None):
        """Resolve by external key in an independent, fail-soft transaction.

        Missing keys are not created.  Repeated calls preserve the first audit
        values and return the same integer id.
        """
        try:
            if not isinstance(external_ref, str) or not external_ref.strip():
                _logger.error(
                    '[BILLING_INCIDENT] _resolve requires external_ref')
                return False
            if resolution_note is not None and not isinstance(
                    resolution_note, str):
                _logger.error(
                    '[BILLING_INCIDENT] _resolve rejected resolution_note type')
                return False
            if (resolved_by_ref is not None
                    and (not isinstance(resolved_by_ref, int)
                         or isinstance(resolved_by_ref, bool))):
                _logger.error(
                    '[BILLING_INCIDENT] _resolve rejected resolved_by_ref type')
                return False
            if (manual_check_result is not None
                    and manual_check_result not in self._MANUAL_RESULTS):
                _logger.error(
                    '[BILLING_INCIDENT] _resolve rejected manual_check_result')
                return False

            now = fields.Datetime.now()
            assignments = [
                sql.SQL("state = 'resolved'"),
                sql.SQL('resolved_at = %s'),
                sql.SQL('write_uid = %s'),
                sql.SQL('write_date = %s'),
            ]
            params = [now, SUPERUSER_ID, now]
            if resolution_note is not None:
                assignments.append(sql.SQL('resolution_note = %s'))
                params.append(resolution_note)
            if resolved_by_ref is not None:
                assignments.append(sql.SQL('resolved_by_ref = %s'))
                params.append(resolved_by_ref)
            if manual_check_result is not None:
                assignments.append(sql.SQL('manual_check_result = %s'))
                params.append(manual_check_result)
            query = sql.SQL(
                'UPDATE {table} SET {assignments} '
                "WHERE id = %s AND state <> 'resolved' RETURNING id"
            ).format(
                table=sql.Identifier(self._table),
                assignments=sql.SQL(', ').join(assignments),
            )

            with self.env.registry.cursor() as cr:
                cr.execute('SET LOCAL lock_timeout = %s',
                           (self._LOCK_TIMEOUT,))
                api.Environment(cr, SUPERUSER_ID, {})
                cr.execute(
                    sql.SQL(
                        'SELECT id FROM {table} WHERE external_ref = %s'
                    ).format(table=sql.Identifier(self._table)),
                    (external_ref,),
                )
                existing = cr.fetchone()
                if not existing:
                    cr.commit()
                    return False
                incident_id = existing[0]
                self._acquire_review_activity_lock(cr, incident_id)
                cr.execute(query, params + [incident_id])
                row = cr.fetchone()
                if not row:
                    cr.execute(
                        sql.SQL(
                            'SELECT state FROM {table} WHERE id = %s'
                        ).format(table=sql.Identifier(self._table)),
                        (incident_id,),
                    )
                    existing_state = cr.fetchone()
                    if not existing_state:
                        cr.commit()
                        return False
                    if existing_state[0] != 'resolved':
                        _logger.critical(
                            '[BILLING_INCIDENT] _resolve update returned no row '
                            'incident_id=%s state=%s',
                            incident_id, existing_state[0])
                        cr.commit()
                        return False
                cr.commit()
            try:
                self._clear_review_activities(incident_id)
            except Exception:
                _logger.error(
                    '[BILLING_INCIDENT] incident id=%s resolved but activity '
                    'cleanup failed; resolution remains committed',
                    incident_id,
                    exc_info=True,
                )
            return incident_id
        except Exception:
            _logger.critical(
                '[BILLING_INCIDENT] resolve bookkeeping failed; caller flow '
                'continues unchanged',
                exc_info=True,
            )
            return False
