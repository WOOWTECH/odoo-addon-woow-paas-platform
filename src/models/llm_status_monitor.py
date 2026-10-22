"""Inactive five-minute anonymous I8 status monitor.

Receipts are durable and generation fenced. Shared guard columns are never
written: runtime reads overlay this monitor's verdict on the existing guard.
No sample/armed state is created at installation. A manual cron invocation is
an explicit first monitoring attempt and arms freshness checks permanently.
"""
import hashlib
import logging

from odoo import api, fields, models

from ..services import newapi_client as nac
from ..services import llm_status_monitor as policy
from ..services.llm_status_invariants import validate_status
from ..services.llm_sql_budget import deadline_cursor
from .llm_common import (
    LLM_CONFIG_PARAMETERS, LlmQuotaInteger, PARAM_PROXY_URL, PARAM_SHOW_LLM, PARAM_PILOT_PARTNER_IDS,
    PARAM_CANARY_PARTNER_ID, _truthy, _parse_pilot, llm_config, llm_unconfigured, run_llm_cron,
)
from .llm_event import LlmAlert, _begin
from .llm_runtime_state import STATE_TABLE, status_probe

_logger = logging.getLogger(__name__)
STATUS_CRON_XMLID = 'woow_paas_platform.ir_cron_llm_status_monitor'
_STATUS_COLUMNS = ('status_armed', 'status_generation', 'status_started_at',
                   'status_completed_at', 'status_success_at', 'status_result', 'status_fingerprint')


def _environment(cr):
    # Direct SQL deliberately avoids cached ICP values and caller snapshots.
    cr.execute('SELECT key, value FROM ir_config_parameter WHERE key = ANY(%s)',
               (list(LLM_CONFIG_PARAMETERS),), log_exceptions=False)
    values = dict(cr.fetchall())
    raw = '\n'.join('%s=%s' % (key, (values.get(key) or '').strip())
                    for key in sorted(LLM_CONFIG_PARAMETERS))
    fingerprint = hashlib.sha256(raw.encode()).hexdigest()
    state, url = nac.proxy_url_state(values.get(PARAM_PROXY_URL))
    configured = state == 'usable' and bool(
        _truthy(values.get(PARAM_SHOW_LLM)) or _parse_pilot(values.get(PARAM_PILOT_PARTNER_IDS))
        or (values.get(PARAM_CANARY_PARTNER_ID) or '').strip())
    return fingerprint, url, configured


def _now(cr):
    cr.execute("SELECT clock_timestamp() AT TIME ZONE 'UTC'")
    return cr.fetchone()[0]


class LlmStatusMonitor(models.Model):
    _inherit = 'woow_paas_platform.llm_runtime_state'

    status_armed = fields.Boolean(readonly=True, default=False)
    status_generation = LlmQuotaInteger(readonly=True, default=0)
    status_started_at = fields.Datetime(readonly=True)
    status_completed_at = fields.Datetime(readonly=True)
    status_success_at = fields.Datetime(readonly=True)
    status_result = fields.Char(readonly=True)
    status_fingerprint = fields.Char(readonly=True)

    @api.model
    def _llm_status_guard(self, cr, row):
        if not row.get('status_armed'):
            # Activation without a first receipt must also fail closed. Merely
            # installing this inactive XML record leaves behavior unchanged.
            cr.execute("SELECT c.active FROM ir_cron c JOIN ir_model_data d "
                       "ON d.res_id = c.id AND d.model = 'ir.cron' "
                       "WHERE d.module = 'woow_paas_platform' "
                       "AND d.name = 'ir_cron_llm_status_monitor'")
            active = cr.fetchone()
            if not active or not active[0]:
                return row
            row = dict(row, status_armed=True)
        fingerprint, _url, _configured = _environment(cr)
        return policy.effective_guard(row, _now(cr), fingerprint)

    @api.model
    def _cron_llm_status_monitor(self):
        config = llm_config(self.env)
        if llm_unconfigured(self.env, config):
            return False
        return run_llm_cron(self.env, STATUS_CRON_XMLID, self._status_tick,
                            config=config, items=1, outbound=False)

    def _status_tick(self, config, tick):
        claim = self._status_claim(tick.deadline)
        if not claim:
            return False
        result = 'unavailable'
        try:
            client = self.env['woow_paas_platform.llm_account']._llm_client(nac.ROLE_CRON)
            # Factory may have a cached ICP endpoint. Never send to that endpoint
            # unless it matches the fresh DB configuration used for this claim.
            if client.base_url == claim['url']:
                with status_probe(client):
                    response = client.status(timeout=(2, 8), deadline=tick.deadline - 5)
                if isinstance(response, nac.Ok):
                    verdict = validate_status({'success': True, 'data': response.data})
                    result = 'ok' if verdict.ok else 'invalid'
        except Exception:  # noqa: BLE001 - fixed result only, never log secrets/traceback
            result = 'unavailable'
        saved = self._status_finish(claim, result, tick.deadline)
        if saved and saved != 'ok':
            try:
                self.env['woow_paas_platform.llm_event']._llm_alert(
                    LlmAlert.STATUS_INVALID, error_code='llm_status_invalid', deadline=tick.deadline)
            except Exception:  # noqa: BLE001 - the guard receipt is already committed
                _logger.warning('[LLM] status monitor alert could not be queued')
        return False

    def _status_claim(self, deadline):
        with deadline_cursor(self.env.registry.cursor, deadline) as cr:
            _begin(self.env, cr, read_committed=True)
            fingerprint, url, configured = _environment(cr)
            if not configured:
                return None
            self._llm_ensure_row(cr)
            cr.execute('SELECT %s FROM %s FOR UPDATE SKIP LOCKED' % (
                ', '.join(_STATUS_COLUMNS), STATE_TABLE))
            row = cr.dictfetchone()
            now = _now(cr)
            if not row or not policy.due(row, now):
                return None
            generation = (row['status_generation'] or 0) + 1
            cr.execute('UPDATE %s SET status_armed = true, status_generation = %%s, '
                       'status_started_at = %%s, status_completed_at = NULL, '
                       "status_result = 'pending', status_fingerprint = %%s" % STATE_TABLE,
                       (generation, now, fingerprint))
            # Normal cursor exit commits the lease before any HTTP call.
            return {'status_generation': generation, 'url': url, 'fingerprint': fingerprint}

    def _status_finish(self, claim, result, deadline):
        if result not in ('ok', 'invalid', 'unavailable'):
            raise ValueError('invalid_status_result')
        with deadline_cursor(self.env.registry.cursor, deadline) as cr:
            _begin(self.env, cr, read_committed=True)
            cr.execute('SELECT %s FROM %s FOR UPDATE SKIP LOCKED' % (
                ', '.join(_STATUS_COLUMNS), STATE_TABLE))
            row = cr.dictfetchone()
            now = _now(cr)
            if not row or not policy.owns(row, claim['status_generation'], now):
                return False
            fingerprint, _url, configured = _environment(cr)
            if (not configured or fingerprint != claim['fingerprint']
                    or row['status_fingerprint'] != claim['fingerprint']):
                result = 'config_changed'
            cr.execute('UPDATE %s SET status_completed_at = %%s, status_result = %%s, '
                       "status_success_at = CASE WHEN %%s = 'ok' THEN %%s ELSE status_success_at END"
                       % STATE_TABLE, (now, result, result, now))
            return result
