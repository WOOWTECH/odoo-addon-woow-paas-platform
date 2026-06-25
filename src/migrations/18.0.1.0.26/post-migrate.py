"""18.0.1.0.26 — Phase A: M:N migration steps 2/3/4 + additive bootstraps.

Mirrors design §7.0.2 (M:N project.cloud_service_ids rollout) + §7.0.7
(additive: external_mcp_allowed default + project_access bootstrap).

Step 2  Backfill ``project_cloud_service_rel`` from existing
         ``project_project.cloud_service_id`` 1:1 bindings (ON CONFLICT
         DO NOTHING — idempotent + collision-safe).
Step 3  Drop the legacy SQL UNIQUE constraint on
         ``project_project.cloud_service_id`` (M:N forbids it).
Step 4  Keep the ``cloud_service_id`` column — model declares a stored
         compute Many2one for backward-compat read; the column persists
         the computed primary so existing SQL walks still resolve.
Step 8  Backfill ``external_mcp_allowed`` to FALSE for any pre-existing
         row whose value is NULL (conservative closed-egress default).
Step 9  Bootstrap one ``role='owner'`` ``project_access`` row per existing
         project, derived from the bound cloud_service's workspace_access
         (fallback: project.create_uid partner). Skipped when an owner row
         is already present (idempotent + plays nice with
         ``project.create()`` hook that bootstraps on its own).
Step 10 Stub — design §7.0.7 keeps the ``default_ai_assistant_id`` fallback
         alive until Phase F (destructive cleanup). Recorded as log so the
         step accounting is explicit.
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


def _table_exists(cr, table):
    cr.execute(
        """
        SELECT 1 FROM information_schema.tables
        WHERE table_name = %s
        """,
        (table,),
    )
    return cr.fetchone() is not None


def migrate(cr, version):
    _step2_backfill_relation_table(cr)
    _step3_drop_unique_constraint(cr)
    _step8_external_mcp_allowed_default(cr)
    _step9_project_access_bootstrap(cr)
    _step10_default_ai_assistant_fallback_kept(cr)


# ---------------------------------------------------------------------- #
# Step 2 — relation table backfill                                        #
# ---------------------------------------------------------------------- #
def _step2_backfill_relation_table(cr):
    # The relation table is created by pre-migrate.py; defensive ensure
    # so that a stray bare post-migrate invocation still works.
    if not _table_exists(cr, 'project_cloud_service_rel'):
        cr.execute(
            """
            CREATE TABLE project_cloud_service_rel (
                project_id INTEGER NOT NULL
                    REFERENCES project_project(id) ON DELETE CASCADE,
                cloud_service_id INTEGER NOT NULL
                    REFERENCES woow_paas_platform_cloud_service(id) ON DELETE CASCADE,
                PRIMARY KEY (project_id, cloud_service_id)
            )
            """
        )
        _logger.info("Step 2 helper: created project_cloud_service_rel.")

    if not _column_exists(cr, 'project_project', 'cloud_service_id'):
        _logger.info(
            "Step 2: project_project.cloud_service_id column already absent — "
            "backfill skipped (idempotent noop).",
        )
        return

    cr.execute(
        """
        INSERT INTO project_cloud_service_rel (project_id, cloud_service_id)
        SELECT id, cloud_service_id
          FROM project_project
         WHERE cloud_service_id IS NOT NULL
        ON CONFLICT DO NOTHING
        """
    )
    _logger.info(
        "Step 2: backfilled %s rows into project_cloud_service_rel "
        "(1:1 → M:N seed).",
        cr.rowcount,
    )


# ---------------------------------------------------------------------- #
# Step 3 — drop UNIQUE constraint                                         #
# ---------------------------------------------------------------------- #
def _step3_drop_unique_constraint(cr):
    cr.execute(
        """
        SELECT constraint_name
          FROM information_schema.table_constraints
         WHERE table_name = 'project_project'
           AND constraint_type = 'UNIQUE'
           AND constraint_name LIKE %s
        """,
        ('%cloud_service%',),
    )
    rows = cr.fetchall()
    for (cname,) in rows:
        cr.execute(
            'ALTER TABLE project_project DROP CONSTRAINT IF EXISTS "%s"' % cname
        )
        _logger.info(
            "Step 3: dropped UNIQUE constraint %r on project_project.", cname,
        )


# ---------------------------------------------------------------------- #
# Step 8 — external_mcp_allowed default backfill                          #
# ---------------------------------------------------------------------- #
def _step8_external_mcp_allowed_default(cr):
    if not _column_exists(cr, 'project_project', 'external_mcp_allowed'):
        _logger.warning(
            "Step 8: project_project.external_mcp_allowed column missing — "
            "model registry has not added the field yet."
        )
        return
    cr.execute(
        """
        UPDATE project_project
           SET external_mcp_allowed = FALSE
         WHERE external_mcp_allowed IS NULL
        """
    )
    _logger.info(
        "Step 8: external_mcp_allowed backfilled FALSE for %s row(s).",
        cr.rowcount,
    )


# ---------------------------------------------------------------------- #
# Step 9 — project_access bootstrap                                       #
# ---------------------------------------------------------------------- #
def _step9_project_access_bootstrap(cr):
    if not _table_exists(cr, 'woow_paas_platform_project_access'):
        _logger.info(
            "Step 9: woow_paas_platform_project_access table missing — "
            "model not loaded; bootstrap skipped (idempotent on re-run)."
        )
        return

    # Skip projects that already have an owner row (idempotent).
    cr.execute(
        """
        SELECT pp.id,
               COALESCE(
                   (
                       SELECT wa.user_id
                         FROM woow_paas_platform_workspace_access wa
                         JOIN woow_paas_platform_cloud_service cs
                              ON cs.workspace_id = wa.workspace_id
                        WHERE cs.id = pp.cloud_service_id
                          AND wa.role = 'owner'
                        ORDER BY wa.create_date ASC
                        LIMIT 1
                   ),
                   pp.create_uid
               ) AS bootstrap_uid
          FROM project_project pp
         WHERE NOT EXISTS (
             SELECT 1 FROM woow_paas_platform_project_access pa
              WHERE pa.project_id = pp.id
                AND pa.role = 'owner'
         )
        """
    )
    rows = cr.fetchall()
    if not rows:
        _logger.info("Step 9: every project already has an owner row.")
        return

    seeded = 0
    for project_id, uid in rows:
        if not uid:
            _logger.warning(
                "Step 9: project_id=%s has no resolvable owner "
                "(unbound workspace + no create_uid) — skipped.",
                project_id,
            )
            continue
        cr.execute(
            """
            SELECT partner_id FROM res_users WHERE id = %s
            """,
            (uid,),
        )
        row = cr.fetchone()
        if not row or not row[0]:
            continue
        partner_id = row[0]
        cr.execute(
            """
            INSERT INTO woow_paas_platform_project_access
                (project_id, partner_id, user_id, role,
                 invited_by_uid, invited_at, create_uid, create_date,
                 write_uid, write_date)
            VALUES (%s, %s, %s, 'owner', %s, NOW(), %s, NOW(), %s, NOW())
            ON CONFLICT (project_id, partner_id) DO NOTHING
            """,
            (project_id, partner_id, uid, uid, uid, uid),
        )
        seeded += cr.rowcount
    _logger.info(
        "Step 9: bootstrapped %s project_access owner row(s) for "
        "pre-existing projects.",
        seeded,
    )


# ---------------------------------------------------------------------- #
# Step 10 — default ai.assistant fallback kept (no-op in Phase A)         #
# ---------------------------------------------------------------------- #
def _step10_default_ai_assistant_fallback_kept(cr):
    _logger.info(
        "Step 10: default_ai_assistant_id config_parameter kept as fallback; "
        "agent_template auto-bootstrap is deferred to Phase F (design §7.5)."
    )
