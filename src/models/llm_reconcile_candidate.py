"""Dormant I2 persisted observations; no scheduler, AP client, or incident sink.

Only a trusted sudo caller may supply a fresh, verified upstream sample. This
foundation deliberately never supplies I3 evidence, so upward crossings remain
pending. Activation, upstream identity/I8 proof, and coverage are separate work.
All work stays in the caller's transaction; no implicit commit or fresh cursor.
"""
import time

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from .billing_incident import _is_odoo_module_uninstall
from .llm_common import LlmQuotaInteger, llm_unconfigured
from ..services.llm_reconcile_balance import BalanceSample, Candidate, reconcile

MODEL = 'woow_paas_platform.llm_reconcile_candidate'
_CAPABILITY_KEY = 'woow_llm_i2_candidate_capability'
_CAPABILITY = object()
MAX_SAMPLE_AGE = 60


def _cap(records):
    return records.with_context({}, **{_CAPABILITY_KEY: _CAPABILITY})


class LlmReconcileCandidate(models.Model):
    _name = MODEL
    _description = 'AI quota reconciliation candidate foundation'
    _rec_name = 'account_id'

    account_id = fields.Many2one(
        'woow_paas_platform.llm_account', required=True, readonly=True,
        ondelete='cascade', index=True)
    direction = fields.Selection([('up', 'Up'), ('down', 'Down')], readonly=True)
    first_quota = LlmQuotaInteger(readonly=True)
    first_used_quota = LlmQuotaInteger(readonly=True)
    first_request_count = LlmQuotaInteger(readonly=True)
    first_sampled_at = LlmQuotaInteger(readonly=True)
    last_sampled_at = LlmQuotaInteger(required=True, readonly=True)
    outcome = fields.Selection([
        (value, value) for value in ('clear', 'candidate_up', 'candidate_down',
                                    'evidence_pending', 'mismatch')],
        required=True, readonly=True)

    _sql_constraints = [
        ('account_unique', 'UNIQUE(account_id)', 'An account already has I2 observation state.'),
        ('sample_valid',
         'CHECK(last_sampled_at >= 0 AND (direction IS NULL OR '
         '(first_used_quota >= 0 AND first_request_count >= 0 AND '
         'first_sampled_at >= 0 AND first_sampled_at <= last_sampled_at)))',
         'Invalid I2 observation state.'),
    ]

    @api.model
    def _require_capability(self):
        if self.env.context.get(_CAPABILITY_KEY) is not _CAPABILITY:
            raise AccessError(_('I2 state can only be written by the platform service.'))

    @api.model_create_multi
    def create(self, vals_list):
        self._require_capability()
        records = super().create(vals_list)
        return records.with_context(**{_CAPABILITY_KEY: False})

    def write(self, vals):
        self._require_capability()
        return super().write(vals)

    def unlink(self):
        if not _is_odoo_module_uninstall(self.env):
            self._require_capability()
        return super().unlink()

    @api.model
    def _ledger_bounds(self, account_id):
        """Read L/U and signed acknowledgements in one statement/snapshot.

        Private service helper: SQL bypasses ACLs, so require sudo. Never use
        read_group SUM: Odoo's numeric caster converts PostgreSQL numeric to
        float. Cast every SUM to text *inside SQL*, then convert to Python int.
        """
        if not self.env.su:
            raise AccessError(_('I2 observation requires the platform service.'))
        if type(account_id) is not int or account_id <= 0:
            raise ValidationError(_('Select an AI account.'))
        self.env['woow_paas_platform.llm_credit_order'].flush_model(
            ['account_id', 'source', 'state', 'quota'])
        self.env['woow_paas_platform.llm_known_discrepancy'].flush_model(
            ['account_id', 'quota_delta'])
        self.env.cr.execute("""
            SELECT COALESCE(SUM(quota) FILTER (WHERE state = 'credited'), 0)::text,
                   COALESCE(SUM(quota) FILTER (WHERE state IN
                     ('credited', 'sending', 'unknown', 'needs_review')), 0)::text,
                   (SELECT COALESCE(SUM(quota_delta), 0)::text
                    FROM woow_paas_platform_llm_known_discrepancy WHERE account_id = %s)
            FROM woow_paas_platform_llm_credit_order
            WHERE account_id = %s AND source IN ('user', 'manual', 'dr', 'recharge')
        """, (account_id, account_id))
        return tuple(int(value) for value in self.env.cr.fetchone())

    @api.model
    def _observe(self, account_id, sample):
        """Persist/recheck one account; return a pure Decision or None if skipped.

        Unconfigured DB: no candidate read/write. Closed/missing account:
        discard old candidate, never classify forfeited balance as a shortfall.
        Account row lock serializes observations and closure (NOWAIT; the caller
        must retry serialization/lock contention); no lock is held
        over a network call because this method cannot make one. The caller
        must collect and verify the sample *before* entry. Each accepted sample
        is strictly newer and at most 60 seconds old, with no future timestamp.
        """
        if llm_unconfigured(self.env):
            return None
        if not self.env.su:
            raise AccessError(_('I2 observation requires the platform service.'))
        if type(account_id) is not int or account_id <= 0:
            raise ValidationError(_('Select an AI account.'))
        if not isinstance(sample, BalanceSample):
            raise ValidationError(_('A verified I2 sample is required.'))
        now = int(time.time())
        if not 0 <= now - sample.sampled_at <= MAX_SAMPLE_AGE:
            raise ValidationError(_('The I2 sample is stale or from the future.'))
        Account = self.env['woow_paas_platform.llm_account']
        Account.flush_model(['state'])
        self.env.cr.execute(
            'SELECT state FROM woow_paas_platform_llm_account WHERE id = %s FOR NO KEY UPDATE NOWAIT',
            (account_id,))
        account = self.env.cr.fetchone()
        state = self.search([('account_id', '=', account_id)], limit=1)
        # Another observer may have populated a cached record before our lock.
        state.invalidate_recordset()
        if not account or account[0] == 'closed':
            if state:
                _cap(state).unlink()
            return None
        if state and sample.sampled_at <= state.last_sampled_at:
            raise ValidationError(_('The I2 sample is not newer than the last observation.'))
        candidate = None
        if state and state.direction:
            candidate = Candidate(state.direction, BalanceSample(
                state.first_quota, state.first_used_quota,
                state.first_request_count, state.first_sampled_at))
        lower, upper, known = self._ledger_bounds(account_id)
        decision = reconcile(sample, lower, upper, known_discrepancy=known,
                             candidate=candidate)  # I3 coverage is unavailable, never zero.
        retained = decision.candidate
        if decision.outcome == 'mismatch':
            # Keep the quiet baseline for later incident wiring/deduplication.
            retained = candidate
        values = {
            'account_id': account_id, 'last_sampled_at': sample.sampled_at,
            'outcome': decision.outcome,
            'direction': retained.direction if retained else False,
            'first_quota': retained.sample.quota if retained else 0,
            'first_used_quota': retained.sample.used_quota if retained else 0,
            'first_request_count': retained.sample.request_count if retained else 0,
            'first_sampled_at': retained.sample.sampled_at if retained else 0,
        }
        if state:
            _cap(state).write(values)
        else:
            _cap(self).create(values)
        return decision
