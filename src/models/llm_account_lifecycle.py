"""Guarded suspension and resumable key restoration (LLM-13).

Requests, local gates and their audit commit together. The existing bounded
suspension cron performs all remote work with UP/ROLE_CRON, never admin proof.
The account stays suspended until restoration finishes. Generation changes
invalidate old disable receipts, including interrupted key creation recovery.
"""
from datetime import timedelta

from odoo import SUPERUSER_ID, _, fields, models
from odoo.exceptions import AccessError, UserError

from ..services import newapi_client as nac
from .llm_account import ACCOUNT_TABLE, PROVISION_CRON_XMLID
from .llm_backoffice import _named_system_operator
from .llm_common import QUOTA_PER_POINT, _llm_internal, llm_unconfigured

ACCOUNT = 'woow_paas_platform.llm_account'
KEY = 'woow_paas_platform.llm_api_key'
NOTICE = 'woow_paas_platform.llm_notification'
_REASON_CODES = {
    'ops': 'llm_suspend_ops', 'policy': 'llm_suspend_policy',
    'account_inactive': 'llm_suspend_account_inactive', 'not_me': 'llm_suspend_not_me',
}


def _utcnow():
    return fields.Datetime.now()


class LlmAccountLifecycle(models.Model):
    _inherit = ACCOUNT

    suspend_generation = fields.Integer(readonly=True, default=0, copy=False)
    resume_pending = fields.Boolean(readonly=True, default=False, copy=False, index=True)
    suspend_return_state = fields.Selection(
        [('active', 'Active'), ('provisioning', 'Provisioning'), ('error', 'Error')],
        readonly=True, copy=False)
    lifecycle_operator_id = fields.Many2one(
        'res.users', readonly=True, copy=False, ondelete='restrict', groups='base.group_system')

    def _llm_lifecycle_lock(self):
        self.ensure_one()
        self.flush_recordset()
        self.env.cr.execute(
            'SELECT id FROM ' + ACCOUNT_TABLE + ' WHERE id=%s FOR NO KEY UPDATE', (self.id,))
        if not self.env.cr.fetchone():
            raise UserError(_('The AI account no longer exists.'))
        self.invalidate_recordset()

    def _llm_lifecycle_event(self, action, *, result='ok', key=None, uid=None):
        """Validated closed codes only; audit failure rolls back the local transition."""
        self.ensure_one()
        Event = self.env['woow_paas_platform.llm_event']
        values = Event._llm_record_values(
            action, account_id=self.id, partner_id=self.partner_id.id,
            uid=uid or self.lifecycle_operator_id.id or SUPERUSER_ID,
            target_model=KEY if key else ACCOUNT, target_id=key.id if key else self.id,
            result=result, details={'reason': _REASON_CODES[self.suspend_reason],
                                    'count': self.suspend_generation or self.not_me_notice_id})
        Event._llm_insert_event_cr(self.env.cr, values, _utcnow())

    def _llm_suspend(self, reason, operator_uid):
        """Trusted server seam, shared by named actions and res.users.write()."""
        if reason not in _REASON_CODES:
            raise UserError(_('Unsupported AI account suspension reason.'))
        account = self.sudo()
        account._llm_lifecycle_lock()
        if account.state == 'closed':
            return False
        if (account.state == 'suspended' and account.suspend_reason == reason
                and not account.resume_pending):
            return False
        values = {
            'state': 'suspended', 'suspend_reason': reason, 'resume_pending': False,
            'suspend_generation': max(account.suspend_generation, account.not_me_notice_id) + 1,
            'lifecycle_operator_id': operator_uid,
            # Fence an in-flight provision job before it can publish active.
            'provision_lease_token': False, 'provision_lease_until': False,
        }
        if account.state != 'suspended':
            values['suspend_return_state'] = account.state
        _llm_internal(account).write(values)
        account._llm_lifecycle_event('suspend', uid=operator_uid)
        self.env[NOTICE]._llm_queue_account_disable(account.id)
        return True

    def _llm_ops_suspend(self, reason):
        operator = _named_system_operator(self.env)
        self.ensure_one()
        if llm_unconfigured(self.env):
            raise UserError(_('AI Models are not configured.'))
        account = self.sudo()
        account._llm_lifecycle_lock()
        account._llm_require_not_closed()
        account._llm_suspend(reason, operator.id)
        return {'type': 'ir.actions.client', 'tag': 'reload'}

    def action_llm_suspend_ops(self):
        return self._llm_ops_suspend('ops')

    def action_llm_suspend_policy(self):
        return self._llm_ops_suspend('policy')

    def _llm_owner_inactive(self):
        return bool(self.env['res.users'].sudo().with_context(active_test=False).search_count([
            ('partner_id', '=', self.partner_id.id), ('active', '=', False),
        ], limit=1))

    def action_llm_resume(self):
        operator = _named_system_operator(self.env)
        self.ensure_one()
        if llm_unconfigured(self.env):
            raise UserError(_('AI Models are not configured.'))
        account = self.sudo()
        account._llm_lifecycle_lock()
        account._llm_require_not_closed()
        if account.state != 'suspended':
            raise UserError(_('Only a suspended AI account can be restored.'))
        if account._llm_owner_inactive():
            raise UserError(_('Reactivate the PaaS user before restoring AI service.'))
        if account.resume_pending:
            return {'type': 'ir.actions.client', 'tag': 'reload'}
        generation = account.suspend_generation or account.not_me_notice_id
        keys = account.api_key_ids.filtered(lambda k: k.state not in ('deleted', 'lost'))
        if any(k.state in ('active', 'creating')
               or k.not_me_disabled_notice_id != generation
               or not (k.not_me_disable_plan or {}).get('complete') for k in keys):
            raise UserError(_('API key suspension is still converging. Retry after it completes.'))
        # Older workers overwrote every disable reason with 'suspend'. A
        # completed legacy receipt cannot prove that a key was user-enabled.
        # Never guess its provenance when deciding to re-enable credentials.
        if any(k.disabled_by == 'suspend'
               and 'disabled_by_before_suspend' not in (k.not_me_disable_plan or {})
               for k in keys):
            raise UserError(_('Legacy API key suspension needs operator review before restoration.'))
        _llm_internal(account).write({'resume_pending': True, 'lifecycle_operator_id': operator.id})
        account._llm_lifecycle_event('resume', result='started')
        self.env[NOTICE]._llm_queue_account_disable(account.id)
        return {'type': 'ir.actions.client', 'tag': 'reload'}

    def _llm_resume_tick(self, tick, *, account_ids=None):
        """One idempotent enable per tick; lock before I/O, keep local gate closed."""
        if not tick.can_take(nac.MIN_CALL_SECONDS):
            return False
        self.flush_model()
        domain = [('state', '=', 'suspended'), ('resume_pending', '=', True)]
        if account_ids is not None:
            domain.append(('id', 'in', list(account_ids)))
        for account in self.sudo().search(domain, order='write_date,id', limit=20):
            self.env.cr.execute(
                'SELECT id FROM ' + ACCOUNT_TABLE + ' WHERE id=%s FOR NO KEY UPDATE SKIP LOCKED',
                (account.id,))
            if not self.env.cr.fetchone():
                continue
            account.invalidate_recordset()
            if account.state != 'suspended' or not account.resume_pending:
                continue
            try:
                _named_system_operator(self.env, account.lifecycle_operator_id)
            except AccessError:
                account._llm_lifecycle_event('resume', result='failed')
                # Some keys may already have been enabled. Re-quarantine them.
                account._llm_suspend(account.suspend_reason, account.lifecycle_operator_id.id)
                tick.took()
                return True
            if account._llm_owner_inactive():
                account._llm_lifecycle_event('resume', result='failed')
                account._llm_suspend('account_inactive', SUPERUSER_ID)
                tick.took()
                return True
            Key = self.env[KEY].sudo()
            Key.flush_model()
            pending = Key.search([
                ('account_id', '=', account.id), ('state', '=', 'disabled'),
                ('disabled_by', '=', 'suspend'),
            ], order='not_me_disable_next_at,id')
            now = _utcnow()
            due = pending.filtered(lambda k, stamp=now: not k.not_me_disable_next_at
                                   or k.not_me_disable_next_at <= stamp)
            if pending and not due:
                continue
            if not pending:
                account._llm_lifecycle_event('resume')
                state = account.suspend_return_state or 'active'
                _llm_internal(account).write({
                    'state': state, 'resume_pending': False, 'suspend_reason': False,
                    'suspend_return_state': False,
                })
                if state == 'provisioning':
                    cron = self.env.ref(PROVISION_CRON_XMLID, raise_if_not_found=False)
                    if cron:
                        cron.sudo()._trigger()
                tick.took()
                return True
            key = due[0]
            self.env.cr.execute('SELECT id FROM woow_paas_platform_llm_api_key '
                                'WHERE id=%s FOR UPDATE SKIP LOCKED', (key.id,))
            if not self.env.cr.fetchone():
                continue
            key.invalidate_recordset()
            if key.state != 'disabled' or key.disabled_by != 'suspend':
                continue
            tick.took()
            original_reason = (key.not_me_disable_plan or {}).get('disabled_by_before_suspend')
            if original_reason and original_reason != 'suspend':
                _llm_internal(key).write({'disabled_by': original_reason})
                account._llm_lifecycle_event('key_status', key=key, result='not_executed')
                return True
            # Expired/exhausted credentials remain disabled, even if suspension
            # was their most recent explicit status change.
            expired = key.expires_at and key.expires_at <= now
            exhausted = (key.quota_limit_points is not None
                         and key.used_quota_cached >= key.quota_limit_points * QUOTA_PER_POINT)
            free_usage = (self.env['woow_paas_platform.llm_free_usage']._llm_today(account.id)
                          if key.kind == 'free_trial' else None)
            capped = free_usage and free_usage['capped']
            if expired or exhausted or capped:
                values = {'disabled_by': 'user'}
                if capped:
                    values.update(disabled_by='free_daily_cap',
                                  free_capped_day=free_usage['day_tpe'], free_cap_pending=False)
                _llm_internal(key).write(values)
                account._llm_lifecycle_event('key_status', key=key, result='not_executed')
                return True
            success = False
            try:
                token_id = Key._llm_management_token_id(key)
                result = account._llm_client(nac.ROLE_CRON).user_set_token_status(
                    account._llm_tenant_ip(), account._llm_up_token(role=nac.ROLE_CRON),
                    token_id, True, deadline=tick.deadline)
                success = result.ok
            except Exception:  # noqa: BLE001 - durable, secret-free failure audit below
                success = False
            account._llm_lifecycle_event('key_status', key=key,
                                         result='ok' if success else 'unknown')
            if success:
                _llm_internal(key).write({'state': 'active', 'disabled_by': False,
                                          'not_me_disable_next_at': False,
                                          'not_me_disable_attempts': 0})
            else:
                attempts = key.not_me_disable_attempts + 1
                _llm_internal(key).write({
                    'not_me_disable_attempts': attempts,
                    'not_me_disable_next_at': now + timedelta(seconds=min(3600, 60 * 2 ** min(attempts, 6))),
                })
            return True
        return False


class LlmUserLifecycle(models.Model):
    _inherit = 'res.users'

    def write(self, vals):
        deactivating = self.filtered('active').partner_id if 'active' in vals and not vals['active'] else None
        result = super().write(vals)
        if deactivating:
            partners = deactivating | self.partner_id
            accounts = self.env[ACCOUNT].sudo().search([
                ('partner_id', 'in', partners.ids), ('state', '!=', 'closed'),
            ], order='id')
            for account in accounts:
                account._llm_suspend('account_inactive', self.env.uid)
        return result
