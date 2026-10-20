"""Append-only, named-operator reconciliation acknowledgements (LLM-12b)."""
import re
import uuid

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .billing_incident import _is_odoo_module_uninstall
from .llm_backoffice import _named_system_operator
from .llm_common import LlmQuotaInteger, llm_unconfigured

MODEL = 'woow_paas_platform.llm_known_discrepancy'
TABLE = 'woow_paas_platform_llm_known_discrepancy'
EVENT_MODEL = 'woow_paas_platform.llm_event'
_CAPABILITY_KEY = 'woow_llm_known_discrepancy_capability'
_CAPABILITY = object()
_REQUEST_RE = re.compile(
    r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
    r'[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')
_DECIMAL_RE = re.compile(r'^-?[0-9]+$')
_INT64_MIN = -(2 ** 63)
_INT64_MAX = 2 ** 63 - 1
_REASON_MIN = 10
_REASON_MAX = 2000


def _cap(records):
    # Discard caller-supplied defaults and context flags before the trusted
    # write; only this module's unforgeable object is carried into CRUD.
    return records.with_context({}, **{_CAPABILITY_KEY: _CAPABILITY})


def _without_capability(records):
    return records.with_context(**{_CAPABILITY_KEY: False})


def _strict_quota_delta(value):
    """Accept only an exact Python int at the service boundary."""
    if type(value) is not int:
        raise ValidationError(_('Enter a whole signed quota integer.'))
    result = value
    if result == 0 or not _INT64_MIN <= result <= _INT64_MAX:
        raise ValidationError(_('The quota difference must be non-zero and fit signed 64-bit range.'))
    return result


def _parse_quota_delta_text(value):
    """Convert the wizard's ASCII decimal Char to int before calling the service."""
    if not isinstance(value, str):
        raise ValidationError(_('Enter a whole signed quota integer.'))
    text = value.strip()
    if len(text) > 20 or not _DECIMAL_RE.fullmatch(text):
        raise ValidationError(_('Enter a whole signed quota integer.'))
    return _strict_quota_delta(int(text, 10))


def _is_request_id_unique_error(error):
    """Recognize only this table's request_id UNIQUE violation through Odoo wrappers."""
    pending = [error]
    seen = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        diag = getattr(current, 'diag', None)
        constraint = getattr(diag, 'constraint_name', '') if diag else ''
        if (getattr(current, 'pgcode', None) == '23505'
                and constraint == TABLE + '_request_id_uniq'):
            return True
        for chained in (getattr(current, '__cause__', None),
                        getattr(current, '__context__', None)):
            if chained is not None:
                pending.append(chained)
    return False


def _strict_request_id(value):
    if not isinstance(value, str) or not _REQUEST_RE.fullmatch(value):
        raise ValidationError(_('A canonical UUID request id is required.'))
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise ValidationError(_('A canonical UUID request id is required.')) from None
    if str(parsed) != value.lower():
        raise ValidationError(_('A canonical UUID request id is required.'))
    return str(parsed)


class LlmKnownDiscrepancy(models.Model):
    """An immutable manual reconciliation entry; positive means upstream has more quota."""

    _name = MODEL
    _description = 'Known AI quota discrepancy'
    _order = 'created_at desc, id desc'
    _rec_name = 'request_id'
    _sql_constraints = [
        ('request_id_uniq', 'UNIQUE(request_id)', 'This discrepancy request was already recorded.'),
        ('quota_delta_nonzero', 'CHECK(quota_delta <> 0)', 'A discrepancy must be non-zero.'),
        ('reason_length',
         'CHECK(char_length(btrim(reason)) >= 10 AND char_length(reason) <= 2000)',
         'The reason must contain 10 to 2000 characters.'),
    ]

    account_id = fields.Many2one(
        'woow_paas_platform.llm_account', string='AI account', required=True, readonly=True, index=True,
        ondelete='restrict')
    quota_delta = LlmQuotaInteger(
        string='Quota difference', required=True, readonly=True, copy=False, groups='base.group_system')
    quota_delta_text = fields.Char(
        string='Quota difference', compute='_compute_quota_delta_text',
        compute_sudo=True, readonly=True)
    reason = fields.Text(required=True, readonly=True, copy=False, groups='base.group_system')
    created_uid = fields.Many2one(
        'res.users', string='Recorded by', required=True, readonly=True, copy=False, ondelete='restrict')
    llm_event_id = fields.Many2one(
        EVENT_MODEL, string='Audit event', readonly=True, copy=False, ondelete='set null', index=True)
    request_id = fields.Char(string='Request ID', required=True, readonly=True, copy=False, index=True, size=36)
    created_at = fields.Datetime(string='Recorded at', required=True, readonly=True, copy=False, index=True,
                                 default=fields.Datetime.now)

    @api.depends('quota_delta')
    def _compute_quota_delta_text(self):
        for record in self:
            # Keep int64 values exact in the browser, where JS Number cannot
            # represent every integer above 2**53 - 1.
            record.quota_delta_text = str(record.quota_delta)

    @api.model
    def _require_capability(self):
        if self.env.context.get(_CAPABILITY_KEY) is not _CAPABILITY:
            raise AccessError(_('Known discrepancies can only be recorded by the platform service.'))

    @api.model_create_multi
    def create(self, vals_list):
        self._require_capability()
        records = super().create(vals_list)
        return _without_capability(records)

    def write(self, vals):
        self._require_capability()
        if set(vals) != {'llm_event_id'} or any(record.llm_event_id for record in self):
            raise AccessError(_('Known discrepancy certificates are immutable.'))
        return super().write(vals)

    def unlink(self):
        if _is_odoo_module_uninstall(self.env):
            return super().unlink()
        raise AccessError(_('Known discrepancy certificates cannot be deleted.'))

    @api.model
    def _acknowledge(self, account_id, quota_delta, reason, request_id):
        """Create once and write its audit event in this transaction.

        The actor is always ``env.user``. The same request UUID and normalized
        payload return the first row; reuse with another payload is rejected.
        """
        operator = _named_system_operator(self.env)
        if type(account_id) is not int or account_id <= 0:
            raise ValidationError(_('Select an AI account.'))
        delta = _strict_quota_delta(quota_delta)
        if not isinstance(reason, str):
            raise ValidationError(_('A reason of at least 10 characters is required.'))
        reason = reason.strip()
        if not _REASON_MIN <= len(reason) <= _REASON_MAX:
            raise ValidationError(_('A reason must contain 10 to 2000 characters.'))
        request_id = _strict_request_id(request_id)
        if llm_unconfigured(self.env):
            raise UserError(_('AI Models are not configured; no discrepancy was recorded.'))

        account = self.env['woow_paas_platform.llm_account'].sudo().browse(account_id).exists()
        if len(account) != 1:
            raise ValidationError(_('The selected AI account does not exist.'))

        Model = self.sudo()
        existing = Model.search([('request_id', '=', request_id)], limit=1)
        if existing:
            self._check_idempotent_payload(existing, account_id, delta, reason, operator.id)
            return existing

        values = {
            'account_id': account_id,
            'quota_delta': delta,
            'reason': reason,
            'created_uid': operator.id,
            'llm_event_id': False,
            'request_id': request_id,
            'created_at': fields.Datetime.now(),
        }
        try:
            with self.env.cr.savepoint():
                discrepancy = _cap(Model).create(values)
                event = self.env[EVENT_MODEL]
                event_values = event._llm_record_values(
                    'known_discrepancy', account_id=account_id, uid=operator.id,
                    target_model=MODEL, target_id=discrepancy.id, result='ok')
                event_id = event._llm_insert_event_cr(
                    self.env.cr, event_values, fields.Datetime.now())
                _cap(discrepancy).write({'llm_event_id': event_id})
        except Exception as exc:
            if not _is_request_id_unique_error(exc):
                raise
            existing = Model.search([('request_id', '=', request_id)], limit=1)
            if not existing:
                # Under Odoo's REPEATABLE READ transaction, a concurrent
                # winner can commit after our snapshot and remain invisible
                # here even though the UNIQUE index has rejected our INSERT.
                # Read only the committed idempotency row on a fresh cursor;
                # return its id without trying to read it through the stale
                # transaction snapshot. The UI loads the target in its next
                # request, after this transaction commits.
                with self.env.registry.cursor() as cr:
                    cr.execute(
                        'SELECT id, account_id, quota_delta, reason, created_uid '
                        'FROM %s WHERE request_id = %%s' % TABLE,
                        (request_id,))
                    row = cr.fetchone()
                if not row:
                    raise
                if (row[1], int(row[2]), row[3], row[4]) != (
                        account_id, delta, reason, operator.id):
                    raise ValidationError(
                        self.env._('This request id was already used for another discrepancy.')) from None
                return Model.browse(row[0])
            self._check_idempotent_payload(existing, account_id, delta, reason, operator.id)
            return existing
        return discrepancy

    @api.model
    def _check_idempotent_payload(self, record, account_id, delta, reason, operator_id):
        if (record.account_id.id, record.quota_delta, record.reason,
                record.created_uid.id) != (account_id, delta, reason, operator_id):
            raise ValidationError(self.env._('This request id was already used for another discrepancy.'))

    @api.model
    def _quota_total(self, account_id):
        """Return the exact signed Python-int sum for the next reconciliation pass."""
        if type(account_id) is not int or account_id <= 0:
            raise ValidationError(_('Select an AI account.'))
        self.flush_model(['account_id', 'quota_delta'])
        self.env.cr.execute(
            # Odoo's global numeric typecaster returns float. Fetch text so the
            # PostgreSQL numeric SUM stays exact even beyond signed int64.
            'SELECT COALESCE(SUM(quota_delta), 0)::text FROM %s WHERE account_id = %%s' % TABLE,
            (account_id,))
        return int(self.env.cr.fetchone()[0])


class LlmAccountDiscrepancyAction(models.Model):
    _inherit = 'woow_paas_platform.llm_account'

    def action_open_known_discrepancy_wizard(self):
        _named_system_operator(self.env)
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Acknowledge known quota difference'),
            'res_model': 'woow_paas_platform.llm_known_discrepancy_wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_account_id': self.id},
        }


class LlmKnownDiscrepancyWizard(models.TransientModel):
    _name = 'woow_paas_platform.llm_known_discrepancy_wizard'
    _description = 'Acknowledge known AI quota difference'

    account_id = fields.Many2one(
        'woow_paas_platform.llm_account', required=True, readonly=True,
        ondelete='cascade')
    quota_delta = fields.Char(string='Quota difference', required=True, size=20)
    reason = fields.Text(required=True)
    request_id = fields.Char(string='Request ID', required=True, readonly=True, size=36)

    @api.model
    def default_get(self, names):
        _named_system_operator(self.env)
        values = super().default_get(names)
        # Do not accept a caller supplied default_request_id.
        values['request_id'] = str(uuid.uuid4())
        return values

    def action_acknowledge(self):
        _named_system_operator(self.env)
        self.ensure_one()
        record = self.env[MODEL]._acknowledge(
            self.account_id.id, _parse_quota_delta_text(self.quota_delta),
            self.reason, self.request_id)
        return {
            'type': 'ir.actions.act_window',
            'name': _('Known quota differences'),
            'res_model': MODEL,
            'view_mode': 'form',
            'res_id': record.id,
            'target': 'current',
        }
