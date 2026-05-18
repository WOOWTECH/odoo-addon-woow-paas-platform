"""18.0.1.0.24 — MCP cloud_service_id → project_id decouple.

Defensive backfill (D2) — idempotent so re-running ``-u woow_paas_platform``
is a noop on the second pass. Designed to be safe even when
``cloud_service_id`` column has already been dropped (introspect before SQL).

Five steps:

1. **Backfill** ``project_id`` from ``cloud_service_id`` for user-scope rows
   (only when the column still exists), walking through the bound
   ``project.project.cloud_service_id`` (LIMIT 1 because the project↔service
   relation has a UNIQUE constraint on the service side).
2. **Archive orphans** — user-scope rows with ``project_id IS NULL`` get
   ``active=False`` + log warning. Runs unconditionally (even after the
   ``cloud_service_id`` column is gone) so re-invocations and post-state DBs
   still catch any user-scope row that slipped through (e.g. inserted via
   raw SQL bypassing the new ``@api.constrains``). Resolved Q #4 forbids
   ``unlink()`` here — migration is irreversible; archive leaves the row as
   a rollback safety net. Idempotent: already-archived rows fail the
   ``active=true`` filter on the second pass.
3. **Drop column** ``cloud_service_id`` (D3=A one-shot — only runs when the
   column still exists).
4. **Noupdate flip** on the four new ir.rule xmlids so future ``-u``
   re-applies any XML domain edits (same pattern as 18.0.1.0.23). Always
   runs.
5. **Retire MAJOR 1 legacy rules** — DELETE the original dual-leg
   ``mcp_server_rule_workspace_member`` / ``mcp_tool_rule_workspace_member``
   rows and their ``ir_model_data`` entries. They were superseded by the
   four-rule split (system_readonly + workspace_user × server/tool); leaving
   them active would OR-combine with the new rules and re-open the
   create-direction leak (any base.group_user could create scope='system').
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

    has_cloud_service_id = _column_exists(
        cr, 'woow_paas_platform_mcp_server', 'cloud_service_id',
    )

    # Step 1 — backfill project_id from the bound project (user-scope only).
    # Only when the legacy column still exists; once dropped, there is nothing
    # left to backfill from.
    if has_cloud_service_id:
        cr.execute(
            """
            UPDATE woow_paas_platform_mcp_server mcp
            SET project_id = pp.id
            FROM project_project pp
            WHERE mcp.cloud_service_id IS NOT NULL
              AND mcp.scope = 'user'
              AND mcp.project_id IS NULL
              AND pp.cloud_service_id = mcp.cloud_service_id
            """
        )
        _logger.info(
            "Backfilled project_id for %s user-scope mcp_server row(s)",
            cr.rowcount,
        )
    else:
        _logger.info(
            "cloud_service_id column already absent — backfill skipped."
        )

    # Step 2 — archive user-scope orphans (project_id still NULL after the
    # backfill, or NULL because the column was already gone before this run).
    # Runs unconditionally so the orphan-cleanup invariant holds in both the
    # initial -u path and any subsequent invocations.
    cr.execute(
        """
        SELECT id, name
        FROM woow_paas_platform_mcp_server
        WHERE scope = 'user'
          AND project_id IS NULL
          AND active = true
        """
    )
    orphans = cr.fetchall()
    if orphans:
        cr.execute(
            """
            UPDATE woow_paas_platform_mcp_server
            SET active = false
            WHERE scope = 'user'
              AND project_id IS NULL
              AND active = true
            """
        )
        for row_id, name in orphans:
            _logger.warning(
                "Archived orphan user-scope mcp_server id=%s name=%r "
                "(no matching project — kept as rollback safety net).",
                row_id, name,
            )

    # Step 3 — drop the legacy column (D3=A: one-shot, no two-phase deprecate).
    if has_cloud_service_id:
        cr.execute(
            """
            ALTER TABLE woow_paas_platform_mcp_server
            DROP COLUMN cloud_service_id
            """
        )
        _logger.info(
            "Dropped woow_paas_platform_mcp_server.cloud_service_id column"
        )

    # Step 4 — flip noupdate on the six new ir.rule xmlids (4 split rules +
    # 2 admin-bypass rules) so future module updates re-apply XML-defined
    # domains (same pattern as 18.0.1.0.23).
    cr.execute(
        """
        UPDATE ir_model_data
        SET noupdate = false
        WHERE module = 'woow_paas_platform'
          AND model = 'ir.rule'
          AND name IN (
            'mcp_server_rule_system_readonly',
            'mcp_server_rule_workspace_user',
            'mcp_server_rule_admin_full',
            'mcp_tool_rule_system_readonly',
            'mcp_tool_rule_workspace_user',
            'mcp_tool_rule_admin_full'
          )
        """
    )
    _logger.info(
        "Cleared noupdate on new MCP ir.rule xmlids (%s data rows)",
        cr.rowcount,
    )

    # Step 5 — MAJOR 1 fix: retire the original dual-leg rules. Leaving them
    # active would OR-combine with the new split rules and re-open the
    # create-direction leak (any base.group_user could create scope='system'
    # because the ('scope', '=', 'system') leg evaluates True for new rows).
    # Two DELETEs because ir_model_data does NOT cascade to ir_rule on its
    # own — order: drop the rule rows first, then their xmlid pointers.
    cr.execute(
        """
        DELETE FROM ir_rule
        WHERE id IN (
            SELECT res_id FROM ir_model_data
            WHERE module = 'woow_paas_platform'
              AND model = 'ir.rule'
              AND name IN (
                'mcp_server_rule_workspace_member',
                'mcp_tool_rule_workspace_member'
              )
        )
        """
    )
    retired_rule_rows = cr.rowcount
    cr.execute(
        """
        DELETE FROM ir_model_data
        WHERE module = 'woow_paas_platform'
          AND model = 'ir.rule'
          AND name IN (
            'mcp_server_rule_workspace_member',
            'mcp_tool_rule_workspace_member'
          )
        """
    )
    if retired_rule_rows or cr.rowcount:
        _logger.info(
            "Retired legacy dual-leg MCP rules: %s ir_rule + %s ir_model_data rows",
            retired_rule_rows, cr.rowcount,
        )
