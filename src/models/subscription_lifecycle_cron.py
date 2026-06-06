import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class SubscriptionLifecycleCron(models.AbstractModel):
    _name = 'woow_paas_platform.subscription_lifecycle_cron'
    _description = 'Subscription Lifecycle Enforcement'

    def _cron_enforce_subscription_lifecycle(self):
        """Daily cron: enforce subscription states on services and smart homes.

        Checks all entities with a subscription and takes action:
          - subscription renewed → follow renewal chain, resume if suspended
          - subscription in_progress + entity suspended → resume
          - subscription expired/closed > grace period → suspend
          - subscription cancelled → suspend + check cleanup threshold
        """
        grace_days = int(self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.grace_period_days', '7'
        ))
        cleanup_months = int(self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.cleanup_after_months', '6'
        ))
        today = fields.Date.today()
        _logger.info(
            "[SUB_CRON] _cron_enforce_subscription_lifecycle start grace_days=%s cleanup_months=%s today=%s",
            grace_days, cleanup_months, today,
        )

        total = 0
        for Model in [
            self.env['woow_paas_platform.cloud_service'],
            self.env['woow_paas_platform.smart_home'],
            self.env['woow_paas_platform.security_access'],
        ]:
            entities = Model.search([('subscription_id', '!=', False)])
            _logger.info(
                "[SUB_CRON] scanning model=%s count=%s",
                Model._name, len(entities),
            )
            for entity in entities:
                total += 1
                try:
                    with self.env.cr.savepoint():
                        self._enforce_single(entity, grace_days, cleanup_months, today)
                except Exception:
                    _logger.exception(
                        "[SUB_CRON] enforcement failed for %s (id=%s)",
                        entity._name, entity.id,
                    )
        _logger.info("[SUB_CRON] _cron_enforce_subscription_lifecycle done entities=%s", total)

    def _enforce_single(self, entity, grace_days, cleanup_months, today):
        sub = entity.subscription_id
        _logger.info(
            "[SUB_CRON] enforce entity=%s:%s entity_state=%s sub=%s sub_state=%s end_date=%s",
            entity._name, entity.id, entity.state, sub.id, sub.state, sub.sh_end_date,
        )

        # 1. Follow renewal chain
        if sub.state == 'renewed':
            new_sub = self.env['sh.subscription.subscription'].search([
                ('sh_subscription_id', '=', sub.id),
                ('state', '=', 'in_progress'),
            ], limit=1)
            if new_sub:
                entity.write({'subscription_id': new_sub.id})
                _logger.info(
                    "[SUB_CRON] renewal_chain %s %s → new sub=%s",
                    entity._name, entity.id, new_sub.id,
                )
                sub = new_sub

        # 2. Active subscription → resume if suspended
        if sub.state == 'in_progress':
            if entity.state == 'suspended':
                entity._do_resume()
                _logger.info(
                    "[SUB_CRON] resumed %s %s (subscription active)",
                    entity._name, entity.id,
                )
            return

        # 3. Expired/Closed → check grace period
        if sub.state in ('expire', 'close'):
            if not sub.sh_end_date:
                return
            days_expired = (today - sub.sh_end_date).days
            if days_expired > grace_days and entity.state != 'suspended':
                entity._do_suspend()
                _logger.info(
                    "[SUB_CRON] suspended %s %s (expired %d days, grace=%d)",
                    entity._name, entity.id, days_expired, grace_days,
                )
            return

        # 4. Cancelled → suspend + check cleanup
        if sub.state == 'cancel':
            if entity.state != 'suspended':
                entity._do_suspend()
                _logger.info(
                    "[SUB_CRON] suspended %s %s (cancelled)",
                    entity._name, entity.id,
                )

            cancel_date = sub.write_date.date() if sub.write_date else today
            months_since_cancel = (today - cancel_date).days / 30.0
            if months_since_cancel > cleanup_months:
                entity._do_cleanup()
                _logger.info(
                    "[SUB_CRON] cleaned up %s %s (cancelled %.1f months ago)",
                    entity._name, entity.id, months_since_cancel,
                )
