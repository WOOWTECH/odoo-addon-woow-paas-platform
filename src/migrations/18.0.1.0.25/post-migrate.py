"""18.0.1.0.25 — backfill ``sidecar_backed`` from legacy ``auto_created``.

``auto_created`` is kept as a deprecated column because the legacy test
suite still seeds rows through it; the new code paths read
``sidecar_backed`` exclusively. This migration only backfills — column
removal is deferred.

The UPDATE is intentionally **not** filtered on ``active`` so archived
orphan rows (from the .24 migration) get a consistent flag value;
``active`` and ``project_id`` stay untouched.

Idempotent: the WHERE clause skips already-backfilled rows, and the
``has_auto_created`` guard turns this into a clean noop after a future
drop-column migration.
"""
import logging

_logger = logging.getLogger(__name__)


def _column_exists(cr, table, column):
    cr.execute(
        """
        SELECT 1 FROM information_schema.columns
        WHERE table_name = %s AND column_name = %s
        """,
        (table, column),
    )
    return cr.fetchone() is not None


def migrate(cr, version):
    if not version:
        return

    has_auto_created = _column_exists(
        cr, 'woow_paas_platform_mcp_server', 'auto_created',
    )
    has_sidecar_backed = _column_exists(
        cr, 'woow_paas_platform_mcp_server', 'sidecar_backed',
    )

    if not has_sidecar_backed:
        # Field declaration must land before migration runs — the ORM
        # registry creates the column during module load (pre-migrate).
        # If we hit this branch the developer skipped the schema bump.
        _logger.warning(
            "Migration 18.0.1.0.25: sidecar_backed column missing — "
            "skipping backfill. Re-run -u woow_paas_platform after "
            "model registry refresh."
        )
        return

    if has_auto_created:
        # Step 1 — backfill sidecar_backed=TRUE where auto_created=TRUE
        # AND sidecar_backed is still default FALSE. The second clause
        # makes this idempotent against re-runs and against rows that
        # may have already been written via new code paths.
        cr.execute(
            """
            UPDATE woow_paas_platform_mcp_server
            SET sidecar_backed = TRUE
            WHERE auto_created = TRUE
              AND sidecar_backed = FALSE
            """
        )
        _logger.info(
            "Backfilled sidecar_backed=TRUE for %s mcp_server row(s) "
            "(based on legacy auto_created flag).",
            cr.rowcount,
        )
    else:
        # auto_created column dropped by a future PR — nothing to
        # backfill from, second-run noop.
        _logger.info(
            "Migration 18.0.1.0.25: auto_created column already absent "
            "— sidecar_backed backfill skipped (idempotent noop)."
        )
