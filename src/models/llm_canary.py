"""Dedicated internal canary: durable claim, one-time key, public probe, cleanup.

No API key is stored. A crash always resumes cleanup by the committed request
UUID; it never creates another token for an unfinished round. All receipt and
claim writes use independent bounded READ COMMITTED transactions.
"""
import hashlib
import json
import logging
import time
import uuid
from datetime import datetime, timedelta

from odoo import api, fields, models

from ..services import llm_canary as policy
from ..services import llm_canary_probe
from ..services import newapi_client as nac
from ..services.llm_sql_budget import deadline_cursor
from . import llm_common as common
from .llm_event import LlmAlert, _begin, _env_on
from .llm_runtime_state import STATE_TABLE

_logger = logging.getLogger(__name__)
CRON_XMLID = 'woow_paas_platform.ir_cron_llm_canary'
ACCOUNT = 'woow_paas_platform.llm_account'
KEY = 'woow_paas_platform.llm_api_key'
EVENT = 'woow_paas_platform.llm_event'
_COLUMNS = ('canary_generation', 'canary_started_at', 'canary_lease_until', 'canary_work',
            'canary_probe_result', 'canary_latency_ms', 'canary_failures',
            'last_canary_at', 'last_canary_result')


def _now(cr):
    cr.execute("SELECT clock_timestamp() AT TIME ZONE 'UTC'")
    return cr.fetchone()[0]


def _environment(cr):
    cr.execute('SELECT key, value FROM ir_config_parameter WHERE key = ANY(%s)',
               (list(common.LLM_CONFIG_PARAMETERS),))
    values = dict(cr.fetchall())
    fingerprint = hashlib.sha256('\n'.join(
        '%s=%s' % (key, (values.get(key) or '').strip())
        for key in sorted(common.LLM_CONFIG_PARAMETERS)).encode()).hexdigest()
    state, proxy = nac.proxy_url_state(values.get(common.PARAM_PROXY_URL))
    partner = common._parse_service_admin_id(values.get(common.PARAM_CANARY_PARTNER_ID))
    prefix = (values.get(common.PARAM_USERNAME_PREFIX) or nac.PROD_USERNAME_PREFIX).strip()
    block = common._parse_block(values.get(common.PARAM_IP_BLOCK))
    public = (values.get(common.PARAM_PUBLIC_BASE_URL) or '').strip()
    valid = state == 'usable' and bool(partner)
    try:
        nac.check_prefix_block(prefix, block)
    except nac.NewApiConfigError:
        valid = False
    expected = ('https://newapi.woowtech.io/v1' if proxy == nac.PROXY_URL_PROD
                else 'https://newapi-stg.woowtech.io/v1')
    valid = valid and public == expected
    return {'fingerprint': fingerprint, 'proxy_url': proxy, 'public_base_url': public,
            'partner_id': partner, 'prefix': prefix, 'block': block, 'valid': bool(valid)}


def _account(cr, partner_id):
    # A portal tenant can never receive the daily-key exemption just because
    # somebody accidentally put that partner ID in the canary setting.
    cr.execute('SELECT a.id, a.partner_id, a.newapi_username FROM '
               'woow_paas_platform_llm_account a WHERE a.partner_id=%s AND EXISTS '
               '(SELECT 1 FROM res_users u WHERE u.partner_id=a.partner_id '
               'AND u.active AND NOT u.share) '
               "ORDER BY (a.state = 'closed'), a.id DESC LIMIT 1", (partner_id,))
    return cr.dictfetchone()


class LlmCanary(models.Model):
    _inherit = 'woow_paas_platform.llm_runtime_state'

    canary_generation = common.LlmQuotaInteger(readonly=True, default=0)
    canary_started_at = fields.Datetime(readonly=True)
    canary_lease_until = fields.Datetime(readonly=True)
    canary_work = fields.Json(readonly=True)
    canary_probe_result = fields.Char(readonly=True)
    canary_latency_ms = fields.Integer(readonly=True, default=0)
    canary_failures = fields.Integer(readonly=True, default=0)

    @api.model
    def _cron_llm_canary(self):
        config = common.llm_config(self.env)
        if common.llm_unconfigured(self.env, config) or not config['canary']:
            return False
        # The worker must record failures even when the proxy circuit is open.
        # Its CRON client still enforces the normal circuit/admission guards.
        return common.run_llm_cron(self.env, CRON_XMLID, self._canary_tick,
                                   config=config, items=1, outbound=False)

    def _canary_row(self, cr, *, lock=False):
        cr.execute('SELECT %s FROM %s%s' % (', '.join(_COLUMNS), STATE_TABLE,
                   ' FOR UPDATE SKIP LOCKED' if lock else ''))
        return cr.dictfetchone()

    def _canary_claim(self, deadline):
        with deadline_cursor(self.env.registry.cursor, deadline) as cr:
            _begin(self.env, cr, read_committed=True)
            environment = _environment(cr)
            if not environment['partner_id']:
                return None
            self._llm_ensure_row(cr)
            row, now = self._canary_row(cr, lock=True), _now(cr)
            if not row or (row['canary_lease_until'] and row['canary_lease_until'] > now):
                return None
            work = row['canary_work']
            if work:
                # A previous owner may have died after POST or reveal. Only
                # cleanup may resume; the one-time key is never revealed again.
                work = dict(work, phase='cleanup')
                result = row['canary_probe_result'] or 'interrupted'
            else:
                if not policy.due(row['canary_started_at'], now):
                    return None
                account = _account(cr, environment['partner_id']) if environment['valid'] else None
                account_id = account['id'] if account else 0
                username = account['newapi_username'] if account else ''
                valid = account and username == nac.newapi_username(environment['prefix'], account_id)
                try:
                    ip = nac.tenant_ip(account_id, environment['block']) if valid else None
                except nac.NewApiError:
                    valid, ip = False, None
                work = dict(environment, request_id=str(uuid.uuid4()),
                            account_id=account_id, username=username,
                            phase='creating' if valid else 'cleanup',
                            expires_at=(now + timedelta(seconds=policy.KEY_LIFETIME_SECONDS)).isoformat(),
                            ip=ip)
                result = None if valid else 'account_unavailable'
                cr.execute('UPDATE %s SET canary_started_at=%%s, canary_latency_ms=0' % STATE_TABLE,
                           (now,))
            generation = (row['canary_generation'] or 0) + 1
            cr.execute('UPDATE %s SET canary_generation=%%s, canary_lease_until=%%s, '
                       'canary_work=%%s, canary_probe_result=%%s' % STATE_TABLE,
                       (generation, now + timedelta(seconds=policy.LEASE_SECONDS), json.dumps(work), result))
            return policy.issue_claim(generation, work), work

    def _canary_validate_claim_cr(self, cr, claim, *, lock=True):
        if not policy.authorized(claim):
            raise common.LlmError(common.ERR_LLM_UNAVAILABLE)
        row, now = self._canary_row(cr, lock=lock), _now(cr)
        environment = _environment(cr)
        account = _account(cr, claim.partner_id)
        if (not row or not policy.owns(row, claim, now)
                or row['canary_work'].get('phase') != 'creating'
                or not environment['valid'] or environment['fingerprint'] != claim.fingerprint
                or environment['partner_id'] != claim.partner_id
                or not account or account['id'] != claim.account_id
                or account['newapi_username'] != row['canary_work']['username']):
            raise common.LlmError(common.ERR_LLM_UNAVAILABLE)
        return dict(row['canary_work'])

    def _canary_save_probe(self, claim, result, latency, deadline):
        if result not in policy.RESULTS or type(latency) is not int or latency < 0:
            raise ValueError('invalid_canary_probe')
        with deadline_cursor(self.env.registry.cursor, deadline) as cr:
            _begin(self.env, cr, read_committed=True)
            row = self._canary_row(cr, lock=True)
            if not row or not policy.owns(row, claim, _now(cr)):
                return False
            cr.execute('UPDATE %s SET canary_work=%%s, canary_probe_result=%%s, '
                       'canary_latency_ms=%%s' % STATE_TABLE,
                       (json.dumps(dict(row['canary_work'], phase='cleanup')), result, latency))
            return True

    def _canary_tick(self, config, tick):
        claimed = self._canary_claim(tick.deadline)
        if not claimed:
            return False
        claim, work = claimed
        if work['phase'] == 'creating':
            result, latency = 'create_failed', 0
            try:
                # A fresh environment avoids the cron's old REPEATABLE READ
                # snapshot and contains no request-controlled context.
                with deadline_cursor(self.env.registry.cursor, tick.deadline - 8) as cr:
                    _begin(self.env, cr, read_committed=True)
                    env = _env_on(self.env.registry, cr)
                    account = env[ACCOUNT].sudo().browse(claim.account_id)
                    created = env[KEY]._llm_create_canary_key(
                        account, claim, min(time.monotonic() + 12, tick.deadline - 16))
                    try:
                        probe = llm_canary_probe.probe(work['public_base_url'], created['key'],
                                                      deadline=tick.deadline - 8)
                        result = 'ok' if probe.code == 'ok' else 'probe_failed'
                        latency = probe.latency_ms
                    finally:
                        created.clear()
            except Exception:  # noqa: BLE001 - never log credential-bearing exception objects
                _logger.warning('[LLM] canary create/probe did not complete')
            if not self._canary_save_probe(claim, result, latency, tick.deadline):
                return False
        cleaned, failure = self._canary_cleanup(claim, work, tick.deadline - 2)
        pending = self._canary_finish(claim, cleaned, failure, tick.deadline)
        if pending:
            tick.delay = 60
        return bool(pending)

    def _canary_cleanup(self, claim, work, deadline):
        if not claim.account_id:
            return True, None
        try:
            with deadline_cursor(self.env.registry.cursor, deadline) as cr:
                _begin(self.env, cr, read_committed=True)
                row, now = self._canary_row(cr), _now(cr)
                if not row or not policy.owns(row, claim, now):
                    return False, 'cleanup_pending'
                environment = _environment(cr)
                if environment['fingerprint'] != claim.fingerprint:
                    return False, 'config_changed'
                env = _env_on(self.env.registry, cr)
                account = env[ACCOUNT].sudo().browse(claim.account_id).exists()
                if (not account or account.partner_id.id != claim.partner_id
                        or account.newapi_username != work['username']):
                    return False, 'account_unavailable'
                key = env[KEY].sudo().search([
                    ('account_id', '=', claim.account_id), ('client_request_id', '=', claim.request_id)], limit=1)
                if not key:
                    # The creating mirror always commits before POST; no mirror
                    # means no external create was attempted for this request.
                    return True, None
                if not key.canary_generation:
                    return False, 'cleanup_pending'
                if key.state == 'deleted':
                    return True, None
                client = env[ACCOUNT]._llm_client(nac.ROLE_CRON)
                if client.base_url != work['proxy_url']:
                    return False, 'config_changed'
                up = account._llm_up_token(nac.ROLE_CRON)
                listed = client.user_list_tokens(work['ip'], up, 1, 100, deadline=deadline)
                if not isinstance(listed, nac.Ok) or not isinstance(listed.data, dict):
                    return False, 'cleanup_pending'
                data, token_id = listed.data, key.newapi_token_id
                items, total = data.get('items'), data.get('total')
                if (not isinstance(items, list) or type(total) is not int or total != len(items)
                        or any(not isinstance(item, dict) or type(item.get('id')) is not int
                               or item['id'] <= 0 or not isinstance(item.get('name'), str) for item in items)):
                    return False, 'cleanup_pending'
                matches = [item for item in items if item['name'] == key.newapi_name]
                if len(matches) > 1 or (matches and token_id and matches[0]['id'] != token_id):
                    return False, 'cleanup_pending'
                if matches:
                    deleted = client.user_delete_token(work['ip'], up, matches[0]['id'], deadline=deadline)
                    if not isinstance(deleted, nac.Ok):
                        return False, 'cleanup_pending'
                elif not token_id and not key.creation_rejected and now < datetime.fromisoformat(work['expires_at']):
                    # A timed-out create can still finish upstream. Keep its
                    # absolute expiry as a quarantine before accepting absence.
                    return False, 'cleanup_pending'
                common._llm_internal(key).write({'state': 'deleted', 'created_notice_pending': False})
                Event = env[EVENT]
                values = Event._llm_record_values('key_delete', account_id=claim.account_id,
                                                 target_model=KEY, target_id=key.id)
                Event._llm_insert_event_cr(cr, values, now)
                return True, None
        except Exception:  # noqa: BLE001 - durable work survives remote/DB uncertainty
            _logger.warning('[LLM] canary cleanup deferred')
            return False, 'cleanup_pending'

    def _canary_finish(self, claim, cleaned, failure, deadline):
        alert = False
        with deadline_cursor(self.env.registry.cursor, deadline) as cr:
            _begin(self.env, cr, read_committed=True)
            row, now = self._canary_row(cr, lock=True), _now(cr)
            if not row or not policy.owns(row, claim, now):
                return False
            result = ((row['canary_probe_result'] or 'interrupted') if cleaned
                      else (failure or 'cleanup_pending'))
            if _environment(cr)['fingerprint'] != claim.fingerprint:
                result = 'config_changed'
            sample = policy.outcome(row, result, now, completed=cleaned)
            if sample:
                cr.execute('UPDATE %s SET last_canary_at=%%s, last_canary_result=%%s, '
                           'canary_failures=%%s' % STATE_TABLE,
                           (sample['sample_at'], result, sample['failures']))
                env = _env_on(self.env.registry, cr)
                Event = env[EVENT]
                values = Event._llm_record_values(
                    'canary', account_id=claim.account_id or None,
                    result='ok' if result == 'ok' else 'failed',
                    error_code=None if result == 'ok' else 'llm_canary_failed',
                    details={'count': sample['failures'], 'latency_ms': row['canary_latency_ms'] or 0})
                Event._llm_insert_event_cr(cr, values, now)
                alert = sample['alert']
            cr.execute('UPDATE %s SET canary_lease_until=NULL, canary_work=%%s' % STATE_TABLE,
                       (json.dumps({} if cleaned else dict(row['canary_work'], phase='cleanup')),))
        if alert:
            try:
                self.env[EVENT]._llm_alert(LlmAlert.CANARY_FAILED, account_id=claim.account_id or None,
                                           error_code='llm_canary_failed', deadline=deadline)
            except Exception:  # noqa: BLE001 - receipt is already committed
                _logger.warning('[LLM] canary alert could not be queued')
        return not cleaned
