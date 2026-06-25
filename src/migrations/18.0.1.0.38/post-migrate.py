"""18.0.1.0.38 — Security Access 軸2: remove the auto primary route entirely.

軸2 deletes the "auto primary route" concept (see
docs/decisions/refactor/security-access-bare-tunnel-zero-route.md): a fresh
Security Access now provisions a BARE Cloudflare Tunnel (no hostname, no auto
route, zero DNS); routes are added on demand by the user.

This migration converges existing data to the new model. The product owner
confirmed **no tenant currently uses Security Access**, so there is no data to
preserve — we remove the primary concept in one step (no two-phase deferral):

1. DELETE the auto-created primary route records (the vestigial
   ``localhost:80`` default routes, prefix = canonical_name). Raw SQL: in
   practice this matches 0 rows; if any stray test row exists it is removed so
   the SA becomes a true bare tunnel like a freshly-provisioned one.
2. DROP the now-unused ``is_primary`` COLUMN. The field declaration was removed
   from ``security_access_route``; Odoo leaves the orphan column behind, so we
   drop it here. (The .37 migration's ORM ``is_primary`` reference was patched
   to no longer touch the field, keeping the先裝-後升 upgrade path safe.)

No quota grandfather is needed: with no existing routes, nothing can be over the
new all-routes quota, and the deleted primary route never counted anyway.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    # The is_primary column may already be gone (e.g. re-run). Guard so the
    # migration is idempotent and never aborts the upgrade.
    cr.execute(
        """
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'woow_paas_platform_security_access_route'
           AND column_name = 'is_primary'
        """
    )
    if not cr.fetchone():
        _logger.info(
            "18.0.1.0.38 軸2: is_primary column already absent — nothing to do."
        )
        return

    # 1. Delete the vestigial auto primary routes (0 rows in practice).
    cr.execute(
        """
        DELETE FROM woow_paas_platform_security_access_route
         WHERE is_primary = TRUE
        """
    )
    deleted = cr.rowcount

    # 2. Drop the now-unused column (field declaration already removed in code).
    cr.execute(
        """
        ALTER TABLE woow_paas_platform_security_access_route
        DROP COLUMN is_primary
        """
    )

    _logger.info(
        "18.0.1.0.38 軸2: deleted %s auto primary route(s) and dropped the "
        "is_primary column — Security Access is now a bare tunnel with only "
        "user-managed routes.",
        deleted,
    )
