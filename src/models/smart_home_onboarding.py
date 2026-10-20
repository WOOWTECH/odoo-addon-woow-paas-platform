"""HA onboarding trials require an explicit paid-plan selection.

Only the HA onboarding endpoint opts in. Existing subscriptions retain their
billing policy. A trial-only subscription never produces an invoice: activation
closes it and creates a separate paid subscription through change-plan.
"""
import logging

from psycopg2 import OperationalError

from odoo import SUPERUSER_ID, api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class SmartHomeTrialSubscription(models.Model):
    _inherit = 'sh.subscription.subscription'

    woow_trial_only = fields.Boolean(
        string='Requires paid plan activation', default=False, copy=False,
        readonly=True, groups='base.group_system', index=True,
        help='HA onboarding trial. Never invoice or auto-charge this subscription.',
    )

    def _woow_trial_expired(self, today=None):
        self.ensure_one()
        return bool(self.woow_trial_only and (
            not self.sh_trial_end_date
            or self.sh_trial_end_date < (today or fields.Date.today())))

    def _generate_invoice_vals(self):
        if any(sub.woow_trial_only for sub in self):
            raise UserError(_('試用不會自動收費，請先選擇付費方案。'))
        return super()._generate_invoice_vals()

    def _process_subscription_billing(self, subscription, threshold,
                                      advance_on_success=True):
        if subscription.woow_trial_only:
            return
        return super()._process_subscription_billing(
            subscription, threshold, advance_on_success=advance_on_success)


class SmartHomeOnboarding(models.Model):
    _inherit = 'woow_paas_platform.smart_home'

    # DB uniqueness also protects against requests with an older transaction
    # snapshot after an advisory lock. NULL preserves ordinary multi-home orders.
    ha_onboarding_key = fields.Char(
        readonly=True, copy=False, groups='base.group_system', index=True,
    )
    ha_setup_requested = fields.Boolean(
        readonly=True, copy=False, groups='base.group_system', index=True,
    )
    _sql_constraints = [
        ('ha_onboarding_key_unique', 'unique(ha_onboarding_key)',
         '此試用服務已建立，請重新整理後繼續。'),
    ]

    def _lock_trial_transition(self):
        self.ensure_one()
        # Both expiry and activation lock the home, then subscription, in that
        # order. A concurrent subscription replacement gets an Odoo retry.
        self.flush_recordset(['subscription_id', 'state'])
        self.env.cr.execute(
            'SELECT id FROM woow_paas_platform_smart_home WHERE id = %s FOR UPDATE',
            (self.id,))
        self.invalidate_recordset(['subscription_id', 'state'])
        sub = self.sudo().subscription_id
        if not sub:
            return
        self.env.cr.execute(
            'SELECT id FROM sh_subscription_subscription WHERE id = %s FOR UPDATE',
            (sub.id,))
        sub.invalidate_recordset()

    def _request_ha_setup(self):
        self.ensure_one()
        self._check_operation_allowed()
        if self.state in ('pending', 'error'):
            self.write({'ha_setup_requested': True})
            self.env.ref('woow_paas_platform.ir_cron_ha_onboarding_setup')._trigger()

    @api.model
    def _cron_ha_setup(self):
        # Each committed intent gets its own transaction. A later home's error
        # must not roll back an earlier home's external-resource anchor.
        home_ids = self.search([('ha_setup_requested', '=', True)], limit=10).ids
        for home_id in home_ids:
            try:
                with self.env.registry.cursor() as cr:
                    env = api.Environment(cr, SUPERUSER_ID, {'lang': 'zh_TW'})
                    home = env[self._name].browse(home_id).exists()
                    if not home:
                        continue
                    operation = env['woow_paas_platform.smart_home_provision_operation']._for_home(home)
                    if operation:
                        # The existing worker always locks operation -> home.
                        if not operation._lock():
                            continue
                    else:
                        cr.execute('SELECT id FROM woow_paas_platform_smart_home '
                                   'WHERE id = %s FOR UPDATE SKIP LOCKED', (home_id,))
                        if not cr.fetchone():
                            continue
                    home.invalidate_recordset()
                    if not home.ha_setup_requested:
                        continue
                    home.write({'ha_setup_requested': False})
                    home.flush_recordset()
                    try:
                        if home.workspace_id.state != 'active':
                            raise UserError(_('工作區已停用，無法開通服務。'))
                        if home.state in ('pending', 'error'):
                            home.action_provision()
                    except OperationalError:
                        raise
                    except UserError as exc:
                        home.write({'state': 'error', 'error_message': str(exc)})
            except Exception:
                _logger.exception('HA background setup failed for home=%s', home_id)

    def to_dict(self, include_details=False):
        data = super().to_dict(include_details=include_details)
        data['setup_requested'] = self.sudo().ha_setup_requested
        return data

    def _check_operation_allowed(self, operation='write'):
        self.ensure_one()
        sub = self._sub()
        if sub and sub._woow_trial_expired():
            if operation in ('read', 'delete'):
                return
            raise UserError(_('七天試用已結束，請到 woowtech 選擇付費方案後繼續使用。'))
        return super()._check_operation_allowed(operation=operation)

    def action_provision(self):
        self._check_operation_allowed()
        return super().action_provision()

    def _get_mcp_integration(self):
        sub = self._sub()
        if sub and sub._woow_trial_expired():
            return ''
        return super()._get_mcp_integration()

    def _get_subscription_info(self):
        info = super()._get_subscription_info()
        sub = self._sub()
        if info and sub.woow_trial_only:
            info.update({
                'requires_paid_activation': True,
                'trial_expired': sub._woow_trial_expired(),
                'is_trial': sub.state == 'in_progress' and not sub._woow_trial_expired(),
                'next_payment': None,
                'monthly_estimate': 0.0,
            })
        return info

    def _skip_trial(self, before_mutation=None):
        if self._sub().woow_trial_only:
            raise UserError(_('請選擇付費方案；試用服務不會自動轉為付費。'))
        return super()._skip_trial(before_mutation=before_mutation)

    @api.model
    def _cron_ha_trial_lifecycle(self):
        """Five-minute reconciliation; no billing or trial extension here."""
        homes = self.search([
            ('ha_onboarding_key', '!=', False),
            ('state', '!=', 'deleting'),
        ])
        lifecycle = self.env['woow_paas_platform.subscription_lifecycle_cron']
        grace, cleanup = lifecycle._lifecycle_params()
        for home in homes:
            try:
                with self.env.cr.savepoint():
                    if (not home._sub().woow_trial_only and home.state == 'error'
                            and home.pre_suspend_state):
                        home._lock_trial_transition()
                        home.write({'state': 'suspended'})
                    lifecycle._enforce_single(home, grace, cleanup, fields.Date.today())
                    if not home._sub().woow_trial_only and home.state == 'pending':
                        home._request_ha_setup()
            except OperationalError:
                raise
            except Exception:
                _logger.exception('HA trial lifecycle failed for home=%s', home.id)


class SmartHomeTrialLifecycle(models.AbstractModel):
    _inherit = 'woow_paas_platform.subscription_lifecycle_cron'

    def _enforce_single(self, entity, grace_days, cleanup_months, today,
                        resume_only=False):
        if entity._name == 'woow_paas_platform.smart_home':
            sub = entity.sudo().subscription_id
            if sub.woow_trial_only:
                if sub.state in ('in_progress', 'close', 'expire'):
                    if not resume_only and sub._woow_trial_expired(today):
                        entity._lock_trial_transition()
                        # A paid activation may have replaced the subscription.
                        sub = entity.sudo().subscription_id
                        if sub._woow_trial_expired(today):
                            if sub.state != 'close':
                                sub.write({'state': 'close',
                                           'sh_end_date': sub.sh_trial_end_date,
                                           'sh_reason': '七天試用結束，等待選擇付費方案'})
                            if entity.state not in ('suspended', 'deleting'):
                                entity._do_suspend()
                                if entity.state == 'suspended' and not entity.tunnel_id:
                                    entity.write({'pre_suspend_state': 'pending'})
                    return  # Never auto-resume a trial awaiting paid activation.
        return super()._enforce_single(
            entity, grace_days, cleanup_months, today, resume_only=resume_only)
