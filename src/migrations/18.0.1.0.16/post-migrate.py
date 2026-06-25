"""G3 hotfix: clear stale 'BEST VALUE' labels on topup_plan rows.

The `is_best_value` field (added in this version) is now the source of truth
for the "best value" badge. The legacy hardcoded `label='BEST VALUE'` on the
100-credit pack is no longer correct (1000-credit has the highest bonus ratio)
but `topup_plans.xml` uses `noupdate="1"` so existing DB rows are not cleared
by ``-u`` alone. Clear them here so the frontend's ``t-elif`` fallback never
double-renders the badge.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    cr.execute(
        """
        UPDATE woow_paas_platform_topup_plan
        SET label = NULL
        WHERE label = 'BEST VALUE'
        """
    )
    _logger.info(
        "G3 hotfix: cleared stale 'BEST VALUE' label from %s topup_plan rows",
        cr.rowcount,
    )
