"""Daily free-trial cap (LLM-10): Taipei days, durable log receipts and bounded ticks.

Admin logs are newest first. Advancing the global watermark after just one page
would drop older pages after a crash. Each page atomically records log receipts,
counts and its scan cursor; only the completed scan advances the watermark.
Receipts contain IDs/dates only and expire with the 90-day daily counters.
"""
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from odoo import SUPERUSER_ID, _, api, fields, models
from odoo.exceptions import AccessError

from ..services import newapi_client as nac
from .billing_incident import _is_odoo_module_uninstall
from .llm_common import (
    ERR_LLM_FREE_CAPPED_TODAY, ERR_LLM_FREE_KEY_DAILY_LIMIT, FREE_DAILY_LIMIT,
    LlmCronTick, LlmError, LlmQuotaInteger, _is_llm_internal, _without_llm_internal,
    llm_config, llm_unconfigured, run_llm_cron,
)
from .llm_event import LlmAlert, _begin
from .llm_runtime_state import STATE_TABLE

USAGE_MODEL = 'woow_paas_platform.llm_free_usage'
USAGE_TABLE = 'woow_paas_platform_llm_free_usage'
RECEIPT_TABLE = 'woow_paas_platform_llm_free_log_receipt'
ACCOUNT_TABLE = 'woow_paas_platform_llm_account'
KEY_TABLE = 'woow_paas_platform_llm_api_key'
METER_CRON = 'woow_paas_platform.ir_cron_llm_free_tier_meter'
RESET_CRON = 'woow_paas_platform.ir_cron_llm_free_tier_reset'
CLEANUP_CRON = 'woow_paas_platform.ir_cron_llm_free_usage_cleanup'
BG_CRON = 'woow_paas_platform.ir_cron_llm_free_group_action'
FREE_KEYS_PER_DAY = 3
TAIPEI = ZoneInfo('Asia/Taipei')


def taipei_day(timestamp=None):
    value = time.time() if timestamp is None else timestamp
    return datetime.fromtimestamp(value, timezone.utc).astimezone(TAIPEI).date()


def meter_window(now, last_ts):
    """Overlap five minutes / watermark minus one minute; recover at most 24h."""
    return max(now - 86400, min(now - 300, last_ts - 60))


class LlmFreeUsage(models.Model):
    _name = 'woow_paas_platform.llm_free_usage'
    _description = 'AI Models Free Trial Daily Usage'
    _order = 'day_tpe desc, id desc'

    account_id = fields.Many2one('woow_paas_platform.llm_account', required=True,
                                  readonly=True, index=True, ondelete='restrict')
    day_tpe = fields.Date(required=True, readonly=True, index=True)
    success_count = LlmQuotaInteger(readonly=True, default=0)
    trial_keys_created = fields.Integer(readonly=True, default=0)
    capped_at = fields.Datetime(readonly=True)

    _sql_constraints = [
        ('account_day_uniq', 'UNIQUE(account_id, day_tpe)', 'One free trial counter per day.'),
        ('counts_nonnegative', 'CHECK(success_count >= 0 AND trial_keys_created >= 0)',
         'Free trial counters cannot be negative.'),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        if not _is_llm_internal(self.env):
            raise AccessError(_('Free trial usage is maintained by the platform.'))
        return _without_llm_internal(super().create(vals_list))

    def write(self, vals):
        if not _is_llm_internal(self.env):
            raise AccessError(_('Free trial usage is maintained by the platform.'))
        return super().write(vals)

    def unlink(self):
        if _is_odoo_module_uninstall(self.env):
            return super().unlink()
        raise AccessError(_('Free trial usage is maintained by the platform.'))

    @api.model
    def _llm_daily_limit(self):
        # Only trusted staging shell/cron context can lower the cap. RPC context
        # alone cannot change production enforcement or raise its ceiling.
        override = self.env.context.get('llm_free_daily_limit')
        if (self.env.su and llm_config(self.env).get('proxy_url') in (
                nac.PROXY_URL_STG, nac.PROXY_URL_LLMDEV)
                and type(override) is int and 0 < override <= FREE_DAILY_LIMIT):
            return override
        return FREE_DAILY_LIMIT

    @api.model
    def _llm_today(self, account_id):
        day = taipei_day()
        row = {}
        if account_id:
            with self.env.registry.cursor() as cr:
                cr.execute('SELECT success_count, trial_keys_created, capped_at FROM %s '
                           'WHERE account_id=%%s AND day_tpe=%%s' % USAGE_TABLE,
                           (account_id, day))
                row = cr.dictfetchone() or {}
        return {'day_tpe': day.isoformat(), 'today_count': row.get('success_count') or 0,
                'daily_limit': self._llm_daily_limit(), 'capped': bool(row.get('capped_at')),
                'keys_created_today': row.get('trial_keys_created') or 0}

    @staticmethod
    def _llm_check_row(row):
        if row.get('capped_at'):
            raise LlmError(ERR_LLM_FREE_CAPPED_TODAY)
        if (row.get('trial_keys_created') or 0) >= FREE_KEYS_PER_DAY:
            raise LlmError(ERR_LLM_FREE_KEY_DAILY_LIMIT)

    @api.model
    def _llm_assert_can_create(self, account_id):
        today = self._llm_today(account_id)
        self._llm_check_row({'capped_at': today['capped'],
                             'trial_keys_created': today['keys_created_today']})

    @api.model
    def _llm_reserve_key_cr(self, cr, account_id, day):
        """Same READ COMMITTED transaction as the key mirror insert; serializes creates."""
        cr.execute('INSERT INTO %s (account_id, day_tpe, success_count, trial_keys_created) '
                   'VALUES (%%s, %%s, 0, 0) ON CONFLICT DO NOTHING' % USAGE_TABLE,
                   (account_id, day))
        cr.execute('SELECT capped_at, trial_keys_created FROM %s '
                   'WHERE account_id=%%s AND day_tpe=%%s FOR UPDATE' % USAGE_TABLE,
                   (account_id, day))
        self._llm_check_row(cr.dictfetchone())
        cr.execute('UPDATE %s SET trial_keys_created=trial_keys_created+1 '
                   'WHERE account_id=%%s AND day_tpe=%%s' % USAGE_TABLE, (account_id, day))

    @api.model
    def _llm_alert(self, alert, code, account_id=None):
        self.env['woow_paas_platform.llm_event']._llm_alert(
            alert, account_id=account_id, error_code=code)

    @api.model
    def _cron_llm_free_tier_meter(self):
        config = llm_config(self.env)
        if llm_unconfigured(self.env, config):
            return False
        # Report lag even while circuit breaker prevents the actual meter work.
        state = self.env['woow_paas_platform.llm_runtime_state']._llm_read()
        last = state['free_meter_last_ts']
        if last and time.time() - last > 300:
            self._llm_alert(LlmAlert.FREE_METER_LAG, 'llm_free_meter_lag')
        return run_llm_cron(self.env, METER_CRON, self._llm_meter_tick, items=20, config=config)

    @api.model
    def _llm_meter_tick(self, config, tick):
        # Session advisory lock spans HTTP without locking the shared runtime row.
        # It also prevents reset from racing a cap operation across cron workers.
        with self.env.registry.cursor() as lock_cr:
            lock_cr.execute("SELECT pg_try_advisory_lock(hashtextextended('llm_free_meter', 0))")
            if not lock_cr.fetchone()[0]:
                return False
            try:
                # Reserve most of the tick for log progress. Invalid UPs rotate
                # by last attempt instead of starving every subsequent account.
                cap_tick = LlmCronTick(min(10, max(0, tick.remaining())), 5)
                self._llm_enforce_caps(cap_tick, enabled=False)
                tick.took(cap_tick.done)
                client = self.env['woow_paas_platform.llm_account']._llm_client(nac.ROLE_CRON)
                while tick.can_take(1):
                    state = self.env['woow_paas_platform.llm_runtime_state']._llm_read()
                    end = state['free_meter_scan_end'] or int(time.time()) - 1
                    start = state['free_meter_scan_start'] or meter_window(
                        end, state['free_meter_last_ts'])
                    if (state['free_meter_last_ts'] and
                            end - state['free_meter_last_ts'] > 86400):
                        self._llm_alert(LlmAlert.FREE_METER_LAG, 'llm_free_meter_gap')
                    page = state['free_meter_scan_page'] or 1
                    result = client.ap_log(
                        {'type': 2, 'group': 'free', 'start_timestamp': start,
                         'end_timestamp': end, 'page_size': 100, 'p': page},
                        priority=nac.AP_PRIORITY_FREE_METER, deadline=tick.deadline)
                    tick.took()
                    if not result.ok:
                        tick.more = True
                        tick.delay = 60
                        return True
                    complete = self._llm_apply_page(state, result.data, start, end, page,
                                                    config['prefix'])
                    self._llm_enforce_caps(tick, enabled=False)
                    if complete:
                        return False
                return True
            finally:
                lock_cr.execute("SELECT pg_advisory_unlock(hashtextextended('llm_free_meter', 0))")

    @api.model
    def _llm_apply_page(self, state, data, start, end, page, prefix):
        if not isinstance(data, dict) or not isinstance(data.get('items'), list):
            raise ValueError('llm_free_log_shape')
        items = data['items']
        total = data.get('total')
        if type(total) is not int or total < 0 or len(items) > 100:
            raise ValueError('llm_free_log_shape')
        if not items and (page - 1) * 100 < total:
            raise ValueError('llm_free_log_incomplete')
        maximum = state['free_meter_scan_max_id'] or state['free_meter_last_log_id']
        accepted = []
        for item in items:
            if (not isinstance(item, dict) or type(item.get('id')) is not int
                    or item['id'] <= 0 or type(item.get('created_at')) is not int
                    or not isinstance(item.get('username'), str)
                    or item.get('type') != 2 or item.get('group') != 'free'
                    or not start <= item['created_at'] <= end):
                raise ValueError('llm_free_log_shape')
            maximum = max(maximum, item['id'])
            if item['id'] > state['free_meter_last_log_id']:
                accepted.append(item)
        complete = page * 100 >= total
        now = fields.Datetime.now()
        limit = self._llm_daily_limit()
        with self.env.registry.cursor() as cr:
            _begin(self.env, cr, read_committed=True)
            for item in accepted:
                # Exact local username match plus environment prefix; no tenant-supplied ID.
                if not re.fullmatch(re.escape(prefix) + r'\d{10}', item['username'], re.ASCII):
                    continue
                cr.execute('SELECT id FROM %s WHERE newapi_username=%%s' % ACCOUNT_TABLE,
                           (item['username'],))
                account = cr.fetchone()
                if not account:
                    continue
                day = taipei_day(item['created_at'])
                cr.execute('INSERT INTO %s (newapi_log_id, day_tpe) VALUES (%%s, %%s) '
                           'ON CONFLICT DO NOTHING RETURNING id' % RECEIPT_TABLE,
                           (item['id'], day))
                if not cr.fetchone():
                    continue
                cr.execute(
                    'INSERT INTO {t} AS u (account_id, day_tpe, success_count, '
                    'trial_keys_created, capped_at) VALUES (%s,%s,1,0,%s) '
                    'ON CONFLICT (account_id, day_tpe) DO UPDATE SET '
                    'success_count=u.success_count+1, capped_at=CASE '
                    'WHEN u.success_count+1 >= %s THEN COALESCE(u.capped_at,%s) '
                    'ELSE u.capped_at END'.format(t=USAGE_TABLE),
                    (account[0], day, now if limit <= 1 else None, limit, now))
            if complete:
                cr.execute('UPDATE %s SET free_meter_last_log_id=%%s, free_meter_last_ts=%%s, '
                           'free_meter_scan_start=0, free_meter_scan_end=0, '
                           'free_meter_scan_page=0, free_meter_scan_max_id=0' % STATE_TABLE,
                           (maximum, end))
            else:
                cr.execute('UPDATE %s SET free_meter_scan_start=%%s, free_meter_scan_end=%%s, '
                           'free_meter_scan_page=%%s, free_meter_scan_max_id=%%s' % STATE_TABLE,
                           (start, end, page + 1, maximum))
        return complete

    @api.model
    def _llm_cap_candidates(self, enabled):
        day = taipei_day()
        with self.env.registry.cursor() as cr:
            cr.execute(
                'SELECT k.id, k.account_id, k.newapi_token_id, k.newapi_name, k.state, k.disabled_by '
                'FROM {keys} k JOIN {accounts} a ON a.id=k.account_id '
                'WHERE k.kind=\'free_trial\' AND '.format(keys=KEY_TABLE, accounts=ACCOUNT_TABLE)
                + ("(k.state='disabled' OR (k.state='active' AND k.free_cap_pending)) "
                   "AND k.disabled_by='free_daily_cap' AND a.state='active' "
                   "AND k.free_capped_day < %s AND (k.expires_at IS NULL OR k.expires_at > %s) "
                   "AND NOT EXISTS (SELECT 1 FROM {usage} u WHERE u.account_id=a.id "
                   "AND u.day_tpe=%s AND u.capped_at IS NOT NULL) " if enabled else
                   "k.state IN ('active','failed_reveal','disabled') "
                   "AND (k.state!='disabled' OR k.disabled_by='free_daily_cap') "
                   "AND (k.free_cap_pending OR NOT (COALESCE(k.disabled_by,'')='free_daily_cap' "
                   "AND COALESCE(k.free_capped_day, DATE '1970-01-01')=%s)) "
                   "AND EXISTS (SELECT 1 FROM {usage} u "
                   "WHERE u.account_id=a.id AND u.day_tpe=%s AND u.capped_at IS NOT NULL) ")
                .format(usage=USAGE_TABLE)
                + "AND k.disabled_by IS DISTINCT FROM 'ops' "
                + 'ORDER BY k.free_cap_attempted_at NULLS FIRST, k.id LIMIT 100',
                (day, fields.Datetime.now(), day) if enabled else (day, day))
            rows = cr.dictfetchall()
        for row in rows:
            # The request can finish after midnight. Keep the day that triggered
            # this operation, so tomorrow's reset never treats it as a new cap.
            row['cap_day'] = day
        return rows

    @api.model
    def _llm_enforce_caps(self, tick, enabled):
        rows = self._llm_cap_candidates(enabled)
        client = None
        for row in rows:
            if not tick.can_take(1):
                return
            tick.took()
            with self.env.registry.cursor() as cr:
                _begin(self.env, cr, read_committed=True)
                cr.execute('UPDATE %s SET free_cap_attempted_at=%%s WHERE id=%%s' % KEY_TABLE,
                           (fields.Datetime.now(), row['id']))
            account = self.env['woow_paas_platform.llm_account'].sudo().browse(row['account_id'])
            account.invalidate_recordset()
            try:
                ip, up = account._llm_tenant_ip(), account._llm_up_token(role=nac.ROLE_CRON)
                client = client or account._llm_client(nac.ROLE_CRON)
                token_id = row['newapi_token_id']
                if not token_id:
                    # Interrupted create may have committed remotely before returning its ID.
                    result = client.user_list_tokens(ip, up, 1, 100, deadline=tick.deadline,
                                                     timeout=nac.TIMEOUT_CRON_READ)
                    tick.took()
                    if not result.ok:
                        raise LlmError('llm_free_up_failed')
                    matches = [t for t in result.data.get('items', [])
                               if t.get('name') == row['newapi_name'] and t.get('group') == 'free']
                    if not matches:
                        continue
                    token_id = matches[0]['id']
                if not tick.can_take(1):
                    return
                if not enabled:
                    # Persist intent before the remote side effect. A lost disable
                    # response near midnight must still be recoverable by reset.
                    with self.env.registry.cursor() as cr:
                        _begin(self.env, cr, read_committed=True)
                        cr.execute(
                            'UPDATE %s SET free_cap_pending=true, free_capped_day=%%s, '
                            "disabled_by='free_daily_cap' WHERE id=%%s AND state=%%s "
                            'AND disabled_by IS NOT DISTINCT FROM %%s '
                            "AND disabled_by IS DISTINCT FROM 'ops'" % KEY_TABLE,
                            (row['cap_day'], row['id'], row['state'], row['disabled_by']))
                        if not cr.rowcount:
                            continue
                with self.env.registry.cursor() as cr:
                    _begin(self.env, cr, read_committed=True)
                    if enabled:
                        # Lock before the remote enable; account suspension and key
                        # disable changes serialize with this last eligibility check.
                        # Credentials are decrypted before locking (error handlers
                        # may need a separate cursor to mark UP invalid).
                        reset_day = taipei_day()
                        cr.execute(
                            'SELECT k.id FROM {keys} k JOIN {accounts} a ON a.id=k.account_id '
                            "WHERE k.id=%s AND a.state='active' "
                            "AND (k.state='disabled' OR (k.state='active' AND k.free_cap_pending)) "
                            "AND k.disabled_by='free_daily_cap' AND k.free_capped_day < %s "
                            'AND (k.expires_at IS NULL OR k.expires_at > %s) '
                            'AND NOT EXISTS (SELECT 1 FROM {usage} u WHERE u.account_id=a.id '
                            'AND u.day_tpe=%s AND u.capped_at IS NOT NULL) '
                            'FOR UPDATE OF a,k'.format(keys=KEY_TABLE, accounts=ACCOUNT_TABLE,
                                                      usage=USAGE_TABLE),
                            (row['id'], reset_day, fields.Datetime.now(), reset_day))
                        if not cr.fetchone():
                            continue
                    result = client.user_set_token_status(ip, up, token_id, enabled,
                                                          timeout=nac.TIMEOUT_CRON_WRITE,
                                                          deadline=tick.deadline)
                    tick.took()
                    if not result.ok:
                        raise LlmError('llm_free_up_failed')
                    cr.execute('UPDATE %s SET state=%%s, disabled_by=%%s, newapi_token_id=%%s, free_capped_day=%%s, '
                               'free_cap_pending=false, '
                               "write_uid=%%s, write_date=%%s WHERE id=%%s AND state=%%s AND disabled_by='free_daily_cap'" % KEY_TABLE,
                               ('active' if enabled else ('failed_reveal' if row['state'] == 'failed_reveal'
                                                         else 'disabled'), None if enabled else
                                'free_daily_cap', token_id, None if enabled else row['cap_day'],
                                SUPERUSER_ID, fields.Datetime.now(),
                                row['id'], row['state']))
            except LlmError:
                self._llm_alert(LlmAlert.FREE_UP_FAILED, 'llm_free_up_failed', account.id)
        if len(rows) == 100:
            tick.more = True

    @api.model
    def _cron_llm_free_tier_reset(self):
        return run_llm_cron(self.env, RESET_CRON, self._llm_reset_tick,
                            items=20, config=llm_config(self.env))

    @api.model
    def _llm_reset_tick(self, config, tick):
        with self.env.registry.cursor() as cr:
            cr.execute("SELECT pg_try_advisory_lock(hashtextextended('llm_free_meter', 0))")
            if not cr.fetchone()[0]:
                return False
            try:
                self._llm_enforce_caps(tick, enabled=True)
            finally:
                cr.execute("SELECT pg_advisory_unlock(hashtextextended('llm_free_meter', 0))")

    @api.model
    def _cron_llm_free_group_action(self):
        return run_llm_cron(self.env, BG_CRON,
                            self.env['woow_paas_platform.llm_account']._llm_free_group_tick,
                            items=10, config=llm_config(self.env), outbound=False)

    @api.model
    def _cron_llm_free_usage_cleanup(self):
        return run_llm_cron(self.env, CLEANUP_CRON, self._llm_cleanup_tick, items=4,
                            config=llm_config(self.env), outbound=False)

    @api.model
    def _llm_cleanup_tick(self, config, tick):
        cutoff = taipei_day() - timedelta(days=90)
        for table in (RECEIPT_TABLE, USAGE_TABLE):
            while tick.can_take(1):
                with self.env.registry.cursor() as cr:
                    cr.execute('DELETE FROM {t} WHERE id IN (SELECT id FROM {t} '
                               'WHERE day_tpe < %s ORDER BY day_tpe LIMIT 5000)'.format(t=table),
                               (cutoff,))
                    count = cr.rowcount
                tick.took()
                if count < 5000:
                    break


class LlmFreeLogReceipt(models.Model):
    _name = 'woow_paas_platform.llm_free_log_receipt'
    _description = 'AI Models Free Trial Log Receipt'
    _log_access = False

    newapi_log_id = LlmQuotaInteger(required=True, readonly=True)
    day_tpe = fields.Date(required=True, readonly=True, index=True)
    _sql_constraints = [('log_uniq', 'UNIQUE(newapi_log_id)', 'Log already metered.')]

    @api.model_create_multi
    def create(self, vals_list):
        raise AccessError(_('Free trial usage is maintained by the platform.'))

    def write(self, vals):
        raise AccessError(_('Free trial usage is maintained by the platform.'))

    def unlink(self):
        if _is_odoo_module_uninstall(self.env):
            return super().unlink()
        raise AccessError(_('Free trial usage is maintained by the platform.'))


class LlmAccountFreeGroup(models.Model):
    _inherit = 'woow_paas_platform.llm_account'

    free_group_requested = fields.Selection(
        [('free_blocked', 'Block free trial'), ('default', 'Restore default group')],
        readonly=True, copy=False)

    def _llm_queue_free_group(self, group):
        if not self.env.user.has_group('base.group_system'):
            raise AccessError(_('Only administrators can request the free trial fallback.'))
        if llm_unconfigured(self.env):
            raise LlmError('llm_unavailable')
        self.ensure_one()
        with self.env.registry.cursor() as cr:
            cr.execute('UPDATE %s SET free_group_requested=%%s WHERE id=%%s '
                       'AND newapi_user_id > 0' % ACCOUNT_TABLE, (group, self.id))
        self.env.ref(BG_CRON).sudo()._trigger()
        return {'type': 'ir.actions.client', 'tag': 'display_notification', 'params': {
            'type': 'info', 'message': _(
                'Break-glass request queued for one attempt. If it fails, confirm break-glass '
                'is enabled and press the button again.'),
            'sticky': False}}

    def action_llm_block_free_trial(self):
        return self._llm_queue_free_group('free_blocked')

    def action_llm_restore_free_group(self):
        return self._llm_queue_free_group('default')

    @api.model
    def _llm_free_group_tick(self, config, tick):
        # Claim and consume atomically before outbound: multiple workers cannot
        # duplicate a request, and an unavailable client cannot replay stale
        # intent when an owner enables bg later. Any failure needs manual requeue.
        while tick.can_take(1):
            with self.env.registry.cursor() as cr:
                _begin(self.env, cr, read_committed=True)
                cr.execute(
                    'WITH request AS (SELECT id, newapi_user_id, free_group_requested '
                    'FROM {t} WHERE free_group_requested IS NOT NULL '
                    'ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED) '
                    'UPDATE {t} a SET free_group_requested=NULL FROM request r '
                    'WHERE a.id=r.id RETURNING r.id, r.newapi_user_id, '
                    'r.free_group_requested'.format(t=ACCOUNT_TABLE))
                row = cr.dictfetchone()
            if not row:
                return False
            tick.took()
            try:
                result = self._llm_client(nac.ROLE_CRON).bg_set_user_group(
                    row['newapi_user_id'], row['free_group_requested'], deadline=tick.deadline)
                failed = not result.ok
            except Exception:  # Client/config failures also consume the explicit request.
                failed = True
            if failed:
                self.env[USAGE_MODEL]._llm_alert(
                    LlmAlert.BREAK_GLASS_USED, 'llm_break_glass_failed', row['id'])
