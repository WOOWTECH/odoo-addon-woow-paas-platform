"""Durable, source-relation-free billing incident ledger (#864 PR-D).

Incident business fields are deliberately scalar snapshots.  Their source rows
may exist only in an outer transaction which later rolls back, so adding a
source Many2one here would make the fail-open record fail exactly when it is
needed most.  ``mail.thread`` contributes only Odoo mail metadata so standard
``mail.activity`` create/done/unlink semantics work against an already committed
incident.  The model is a global finance ledger: every account manager reads all
rows across companies by design.
"""

import hashlib
import logging
import math
import re
from datetime import date, datetime

from markupsafe import Markup, escape
from psycopg2 import sql

from odoo import SUPERUSER_ID, _, api, fields, models
from odoo.exceptions import AccessError, UserError


_logger = logging.getLogger(__name__)


class _BillingIncidentResolutionTransition:
    """Unforgeable in-process capability for the first resolution write."""

    __slots__ = ()

    def __repr__(self):
        return '<billing incident resolution transition>'


_BILLING_INCIDENT_RESOLUTION_TRANSITION = (
    _BillingIncidentResolutionTransition())
_BILLING_INCIDENT_RESOLUTION_CTX_KEY = (
    'woow_billing_incident_resolution_transition')
_ODOO_MODULE_UNINSTALL_CTX_KEY = '_force_unlink'


def _resolution_transition(records):
    """Attach the module-private first-resolution capability to ``records``."""
    return records.with_context(**{
        _BILLING_INCIDENT_RESOLUTION_CTX_KEY:
            _BILLING_INCIDENT_RESOLUTION_TRANSITION,
    })


def _is_resolution_transition(env):
    return (
        env.context.get(_BILLING_INCIDENT_RESOLUTION_CTX_KEY)
        is _BILLING_INCIDENT_RESOLUTION_TRANSITION
    )


def _is_odoo_module_uninstall(env):
    """Accept core's unlink hint only while this addon is actually uninstalling."""
    if env.context.get(_ODOO_MODULE_UNINSTALL_CTX_KEY) is not True:
        return False
    module = env['ir.module.module'].sudo().search([
        ('name', '=', 'woow_paas_platform'),
    ], limit=1)
    return module.state == 'to remove'


class BillingIncident(models.Model):
    _name = 'woow.billing.incident'
    _inherit = ['mail.thread']
    _description = 'Woow Billing Incident'
    _rec_name = 'external_ref'
    _order = 'last_seen_at desc, id desc'

    _KIND_SELECTION = (
        ('intent', 'Payment intent'),
        ('charged_not_credited', 'Charged but not credited'),
        ('callback_tx_missing', 'Callback transaction missing'),
        ('invoice_half_settled', 'Invoice half settled'),
        ('bind_failed', 'Card binding failed'),
        ('stuck_pending', 'Stuck pending payment'),
        ('manual_invoice_stale', 'Manual invoice stale'),
    )
    _STATE_SELECTION = (
        ('open', 'Open'),
        ('resolved', 'Resolved'),
        ('orphan', 'Orphan'),
        ('needs_human', 'Needs human review'),
    )
    _MANUAL_RESULT_SELECTION = (
        ('ecpay_found', 'Found in ECPay'),
        ('ecpay_not_found', 'Not found in ECPay'),
    )

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
        selection=list(_KIND_SELECTION),
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
        selection=list(_STATE_SELECTION),
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
        selection=list(_MANUAL_RESULT_SELECTION),
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

    _KINDS = frozenset(key for key, _label in _KIND_SELECTION)
    _STATES = frozenset(key for key, _label in _STATE_SELECTION)
    _LOG_STATES = _STATES - {'resolved'}
    _MANUAL_RESULTS = frozenset(
        key for key, _label in _MANUAL_RESULT_SELECTION)
    _MANAGER_WRITABLE_FIELDS = frozenset({
        'manual_check_result',
        'resolution_note',
    })
    _RESOLUTION_TRANSITION_FIELDS = frozenset({
        'state',
        'resolved_at',
        'resolved_by_ref',
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
        r'\bcard[\s_-]*number\b|\b(?:cvv2?|cvc2?|cid|csc)\b|'
        r'\b(?:card[\s_-]*)?security[\s_-]*(?:code|number|no)\b|'
        r'\b(?:sad|sensitive[\s_-]*authentication[\s_-]*data)\b|'
        r'\bpan\b)',
        re.IGNORECASE,
    )
    _PAN_CANDIDATE_RE = re.compile(
        r'(?<!\d)(?:\d[\s-]*){13,19}(?!\d)')
    _RAW_MTN_RE = re.compile(r'[A-Za-z0-9_-]{1,20}\Z')
    _NAMESPACED_EXTERNAL_REF_RE = re.compile(
        r'[A-Za-z][A-Za-z0-9_-]{0,31}:'
        r'[A-Za-z0-9][A-Za-z0-9._/-]{0,94}\Z')
    _TRADE_NO_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}\Z')
    _CONTROL_CHARACTER_RE = re.compile(r'[\x00-\x1f\x7f]')

    def _lock_and_refresh_states(self, extra_fields=()):
        """Lock every row and reread lifecycle state from this cursor."""
        record_ids = sorted(set(self.ids))
        if not record_ids:
            return {}
        self.env.cr.execute(
            sql.SQL(
                'SELECT id FROM {table} WHERE id = ANY(%s) '
                'ORDER BY id FOR UPDATE'
            ).format(table=sql.Identifier(self._table)),
            (record_ids,),
        )
        locked_ids = [row[0] for row in self.env.cr.fetchall()]
        if locked_ids != record_ids:
            raise UserError(_('This billing incident no longer exists.'))
        field_names = ['state', *extra_fields]
        self.invalidate_recordset(field_names)
        return {
            row['id']: row['state']
            for row in self.read(['state', *extra_fields])
        }

    def write(self, values):
        """Lock/re-read first; only one capability may perform first resolve."""
        states = self._lock_and_refresh_states()
        if any(state == 'resolved' for state in states.values()):
            raise AccessError(_(
                'Resolved billing incidents are immutable.'
            ))

        if _is_resolution_transition(self.env):
            if (set(values) != self._RESOLUTION_TRANSITION_FIELDS
                    or values.get('state') != 'resolved'):
                raise AccessError(_(
                    'Billing incident resolution may update only the exact '
                    'first-resolution audit fields.'
                ))
            resolution_note = values.get('resolution_note')
            if (resolution_note not in (None, False)
                    and self._free_text_ingress_category(
                        'resolution_note', resolution_note)):
                raise UserError(_(
                    'Resolution notes must contain only safe, '
                    'non-sensitive summaries.'
                ))
            return super().write(values)

        # ``sudo()`` is an ACL tool, not a lifecycle capability.  Generic
        # unresolved writes remain the account-manager manual-entry seam only.
        if (self.env.su or not self.env.user.has_group(
                'account.group_account_manager')):
            raise AccessError(_(
                'Only account managers may update billing incidents.'
            ))
        forbidden = set(values) - self._MANAGER_WRITABLE_FIELDS
        if forbidden:
            raise AccessError(_(
                'Account managers may update only the manual verification '
                'result and resolution note.'
            ))
        resolution_note = values.get('resolution_note')
        if (resolution_note not in (None, False)
                and self._free_text_ingress_category(
                    'resolution_note', resolution_note)):
            raise UserError(_(
                'Resolution notes must contain only safe, '
                'non-sensitive summaries.'
            ))
        return super().write(values)

    def unlink(self):
        """Ledger rows are permanent; only core's real module uninstall may drop."""
        if _is_odoo_module_uninstall(self.env):
            return super().unlink()
        raise AccessError(_(
            'Billing incidents are immutable ledger entries and cannot be '
            'deleted.'
        ))

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
    def _free_text_ingress_category(self, field_name, value):
        """Shared PCI validator for detail and every resolution-note seam."""
        if not isinstance(value, str):
            return 'invalid_type'
        return self._sensitive_content_category(field_name, value)

    @api.model
    def _provider_reference_category(self, field_name, value):
        """Validate dedicated provider keys without treating numeric MTN as PAN."""
        if not isinstance(value, str):
            return 'invalid_type'
        category = self._sensitive_content_category(field_name, value)
        if category:
            return category
        if self._CONTROL_CHARACTER_RE.search(value):
            return 'control_character'
        if field_name == 'external_ref':
            if (self._RAW_MTN_RE.fullmatch(value)
                    or self._NAMESPACED_EXTERNAL_REF_RE.fullmatch(value)):
                return False
            return 'invalid_provider_reference_shape'
        if field_name == 'trade_no':
            if self._TRADE_NO_RE.fullmatch(value):
                return False
            return 'invalid_provider_reference_shape'
        return 'unsupported_provider_reference_field'

    @api.model
    def _external_ref_fingerprint(self, external_ref):
        return hashlib.sha256(external_ref.encode('utf-8')).hexdigest()

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
        unknown = set(snapshot) - self._LOG_FIELDS
        if unknown:
            _logger.error(
                '[BILLING_INCIDENT] _log rejected snapshot '
                'category=unknown_fields count=%s',
                len(unknown),
            )
            return False
        external_ref = snapshot.get('external_ref')
        if not isinstance(external_ref, str) or not external_ref.strip():
            _logger.error(
                '[BILLING_INCIDENT] _log rejected field=external_ref '
                'category=required type=%s',
                type(external_ref).__name__,
            )
            return False

        normalized = {}
        for name, value in snapshot.items():
            if name in self._LOG_CHAR_FIELDS:
                if value is None or value is False:
                    if name in ('external_ref', 'kind'):
                        _logger.error(
                            '[BILLING_INCIDENT] _log rejected field=%s '
                            'category=required type=%s',
                            name, type(value).__name__,
                        )
                        return False
                    normalized[name] = None
                elif isinstance(value, str):
                    normalized[name] = value
                else:
                    _logger.error(
                        '[BILLING_INCIDENT] _log rejected field=%s '
                        'category=invalid_type type=%s',
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
                        '[BILLING_INCIDENT] _log rejected field=%s '
                        'category=invalid_type type=%s',
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
                        '[BILLING_INCIDENT] _log rejected field=%s '
                        'category=invalid_number type=%s',
                        name, type(value).__name__,
                    )
                    return False
            elif name in self._LOG_DATETIME_FIELDS:
                if value is None or value is False:
                    normalized[name] = None
                elif isinstance(value, (str, date, datetime)):
                    try:
                        normalized[name] = fields.Datetime.to_datetime(value)
                    except (TypeError, ValueError):
                        _logger.error(
                            '[BILLING_INCIDENT] _log rejected field=%s '
                            'category=invalid_datetime type=%s',
                            name, type(value).__name__,
                        )
                        return False
                else:
                    _logger.error(
                        '[BILLING_INCIDENT] _log rejected field=%s '
                        'category=invalid_type type=%s',
                        name, type(value).__name__,
                    )
                    return False
        for name, value in normalized.items():
            if not isinstance(value, str):
                continue
            if name in ('external_ref', 'trade_no'):
                category = self._provider_reference_category(name, value)
            else:
                category = self._free_text_ingress_category(name, value)
            if category:
                _logger.error(
                    '[BILLING_INCIDENT] _log rejected field=%s '
                    'category=%s type=%s',
                    name, category, type(value).__name__,
                )
                return False
        kind = normalized.get('kind')
        if kind not in self._KINDS:
            _logger.error(
                '[BILLING_INCIDENT] _log rejected field=kind '
                'category=unsupported_selection type=%s',
                type(kind).__name__,
            )
            return False
        if ('state' in normalized
                and normalized['state'] not in self._LOG_STATES):
            _logger.error(
                '[BILLING_INCIDENT] _log rejected field=state '
                'category=unsupported_selection type=%s',
                type(normalized['state']).__name__,
            )
            return False
        for name in self._NONNEGATIVE_INPUT_COUNTERS:
            if name in normalized and (
                    normalized[name] is None or normalized[name] < 0):
                _logger.error(
                    '[BILLING_INCIDENT] _log rejected field=%s '
                    'category=negative_or_null type=%s',
                    name, type(normalized[name]).__name__,
                )
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

            ref_fingerprint = self._external_ref_fingerprint(
                values['external_ref'])
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
                # Odoo's default SQL exception logger renders query params;
                # provider references must never be copied into an error log.
                cr.execute(
                    query,
                    [insert_values[name] for name in columns],
                    log_exceptions=False,
                )
                row = cr.fetchone()
                if not row:
                    cr.execute(
                        sql.SQL(
                            'SELECT id, kind, state FROM {table} '
                            'WHERE external_ref = %s'
                        ).format(table=sql.Identifier(self._table)),
                        (values['external_ref'],),
                        log_exceptions=False,
                    )
                    existing = cr.fetchone()
                    if not existing:
                        _logger.critical(
                            '[BILLING_INCIDENT] upsert returned no row '
                            'fingerprint=%s',
                            ref_fingerprint,
                        )
                        cr.commit()
                        return False
                    incident_id, existing_kind, existing_state = existing
                    if existing_kind != values['kind']:
                        _logger.critical(
                            '[BILLING_INCIDENT] kind mismatch fingerprint=%s '
                            'existing=%s incoming=%s; refusing downgrade',
                            ref_fingerprint, existing_kind, values['kind'],
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
                        'fingerprint=%s state=%s',
                        ref_fingerprint, existing_state,
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
                        '[BILLING_INCIDENT] incident id=%s persisted but '
                        'review activity creation failed; incident remains '
                        'durable',
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
        """Best-effort: one dedicated review activity per incident/user."""
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

            review_type = env.ref(
                'woow_paas_platform.'
                'mail_activity_type_billing_incident_review',
                raise_if_not_found=False,
            )
            model = env['ir.model']._get(self._name)
            users = self._account_manager_users(env)
            if not users:
                _logger.error(
                    '[BILLING_INCIDENT] account.group_account_manager has zero '
                    'users; incident id=%s has no review assignee', incident_id)
                cr.commit()
                return False
            if not (review_type and model):
                _logger.error(
                    '[BILLING_INCIDENT] cannot create review activity for '
                    'incident id=%s (review_type=%s model=%s)',
                    incident_id, bool(review_type), bool(model))
                cr.commit()
                return False

            Activity = env['mail.activity'].sudo()
            existing_user_ids = set(Activity.search([
                ('res_model', '=', self._name),
                ('res_id', '=', incident_id),
                ('activity_type_id', '=', review_type.id),
                ('active', '=', True),
            ]).user_id.ids)
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
            activity_values = [
                {
                    'res_model_id': model.id,
                    'res_id': incident_id,
                    'activity_type_id': review_type.id,
                    'user_id': user.id,
                    'summary': summary,
                    'note': str(note),
                    'date_deadline': fields.Date.today(),
                }
                for user in users
                if user.id not in existing_user_ids
            ]
            if activity_values:
                Activity.create(activity_values)
            cr.commit()
            return True

    @api.model
    def _clear_review_activities(self, incident_id):
        """Best-effort second transaction; delete only our review rows."""
        with self.env.registry.cursor() as cr:
            cr.execute('SET LOCAL lock_timeout = %s',
                       (self._LOCK_TIMEOUT,))
            env = api.Environment(cr, SUPERUSER_ID, {})
            self._acquire_review_activity_lock(cr, incident_id)
            review_type = env.ref(
                'woow_paas_platform.'
                'mail_activity_type_billing_incident_review',
                raise_if_not_found=False,
            )
            if review_type:
                env['mail.activity'].sudo().with_context(
                    active_test=False).search([
                        ('res_model', '=', self._name),
                        ('res_id', '=', incident_id),
                        ('activity_type_id', '=', review_type.id),
                    ]).unlink()
            cr.commit()
            return True

    def _transition_resolved_current_cursor(
            self, manual_check_result, resolution_note, resolved_by_ref):
        """Private UI transition; never open the independent `_resolve` cursor."""
        self.ensure_one()
        self._acquire_review_activity_lock(self.env.cr, self.id)
        states = self._lock_and_refresh_states()
        if states[self.id] == 'resolved':
            raise UserError(_('This billing incident is already resolved.'))
        _resolution_transition(self.sudo()).write({
            'state': 'resolved',
            'resolved_at': fields.Datetime.now(),
            'resolved_by_ref': resolved_by_ref,
            'manual_check_result': manual_check_result,
            'resolution_note': resolution_note,
        })
        # The UI transaction may already hold this incident row lock.  Delete
        # only this mechanism's review row on this cursor to avoid self-lock.
        review_type = self.env.ref(
            'woow_paas_platform.'
            'mail_activity_type_billing_incident_review',
            raise_if_not_found=False,
        )
        if review_type:
            self.env['mail.activity'].sudo().with_context(
                active_test=False).search([
                    ('res_model', '=', self._name),
                    ('res_id', '=', self.id),
                    ('activity_type_id', '=', review_type.id),
                ]).unlink()
        return True

    def action_mark_manually_resolved(self):
        """Record an ECPay verification and close; never credit/refund money."""
        self.ensure_one()
        if not self.env.user.has_group('account.group_account_manager'):
            raise AccessError(_(
                'Only account managers may resolve billing incidents.'
            ))
        self.check_access('write')
        self._acquire_review_activity_lock(self.env.cr, self.id)
        states = self._lock_and_refresh_states(
            ('manual_check_result', 'resolution_note'))
        if states[self.id] == 'resolved':
            raise UserError(_('This billing incident is already resolved.'))
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
        if self._free_text_ingress_category(
                'resolution_note', resolution_note):
            raise UserError(_(
                'Resolution notes must contain only safe, non-sensitive '
                'summaries.'
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
            ref_category = self._provider_reference_category(
                'external_ref', external_ref)
            if ref_category:
                _logger.error(
                    '[BILLING_INCIDENT] _resolve rejected field=external_ref '
                    'category=%s type=%s',
                    ref_category, type(external_ref).__name__,
                )
                return False
            if resolution_note is not None:
                category = self._free_text_ingress_category(
                    'resolution_note', resolution_note)
                if category:
                    _logger.error(
                        '[BILLING_INCIDENT] _resolve rejected '
                        'field=resolution_note category=%s type=%s',
                        category, type(resolution_note).__name__,
                    )
                    return False
            if (resolved_by_ref is not None
                    and (not isinstance(resolved_by_ref, int)
                         or isinstance(resolved_by_ref, bool))):
                _logger.error(
                    '[BILLING_INCIDENT] _resolve rejected '
                    'field=resolved_by_ref category=invalid_type type=%s',
                    type(resolved_by_ref).__name__,
                )
                return False
            if (manual_check_result is not None
                    and manual_check_result not in self._MANUAL_RESULTS):
                _logger.error(
                    '[BILLING_INCIDENT] _resolve rejected '
                    'field=manual_check_result '
                    'category=unsupported_selection type=%s',
                    type(manual_check_result).__name__,
                )
                return False

            with self.env.registry.cursor() as cr:
                cr.execute('SET LOCAL lock_timeout = %s',
                           (self._LOCK_TIMEOUT,))
                env = api.Environment(cr, SUPERUSER_ID, {})
                incident = env[self._name].search([
                    ('external_ref', '=', external_ref),
                ], limit=1)
                if not incident:
                    cr.commit()
                    return False
                incident_id = incident.id
                self._acquire_review_activity_lock(cr, incident_id)
                states = incident._lock_and_refresh_states((
                    'manual_check_result',
                    'resolution_note',
                    'resolved_by_ref',
                ))
                if states[incident_id] == 'resolved':
                    cr.commit()
                    return incident_id
                _resolution_transition(incident.sudo()).write({
                    'state': 'resolved',
                    'resolved_at': fields.Datetime.now(),
                    'resolved_by_ref': (
                        resolved_by_ref
                        if resolved_by_ref is not None
                        else incident.resolved_by_ref
                    ),
                    'manual_check_result': (
                        manual_check_result
                        if manual_check_result is not None
                        else incident.manual_check_result
                    ),
                    'resolution_note': (
                        resolution_note
                        if resolution_note is not None
                        else incident.resolution_note
                    ),
                })
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
