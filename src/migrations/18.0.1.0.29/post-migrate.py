import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Finalise skill dual-scope: drop the NULL-unsafe UNIQUE constraint,
    create the two partial unique indexes, and retire the legacy
    skill_rule_project_* ir.rules (two-DELETE — same pattern as 18.0.1.0.24)
    so they don't OR-combine with the new scope-split rules."""

    # 1. Drop the old UNIQUE(project_id, name, version) constraint.
    cr.execute("""
        ALTER TABLE woow_paas_platform_skill
        DROP CONSTRAINT IF EXISTS
            woow_paas_platform_skill_unique_name_version_per_project
    """)

    # 2. Create the NULL-aware partial unique indexes (idempotent; also created
    #    by skill.init() on every -u, but explicit here for upgrade clarity).
    cr.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS skill_uniq_user_proj_name_version
        ON woow_paas_platform_skill (project_id, name, version)
        WHERE project_id IS NOT NULL
    """)
    cr.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS skill_uniq_system_name_version
        ON woow_paas_platform_skill (name, version)
        WHERE project_id IS NULL
    """)

    # 3. Retire legacy skill rules. Their domains changed (added scope='user'
    #    leg) but they live in a noupdate block, so the XML can't overwrite them
    #    on -u. We replaced them with NEW xmlids (skill_rule_user_*) which load
    #    fresh; the old ones must be deleted or they OR-combine and re-open the
    #    create-direction leak. Two DELETEs (ir_model_data does not cascade).
    cr.execute("""
        DELETE FROM ir_rule
        WHERE id IN (
            SELECT res_id FROM ir_model_data
            WHERE module = 'woow_paas_platform'
              AND model = 'ir.rule'
              AND name IN (
                'skill_rule_project_admin',
                'skill_rule_project_user',
                'skill_rule_project_guest'
              )
        )
    """)
    retired = cr.rowcount
    cr.execute("""
        DELETE FROM ir_model_data
        WHERE module = 'woow_paas_platform'
          AND model = 'ir.rule'
          AND name IN (
            'skill_rule_project_admin',
            'skill_rule_project_user',
            'skill_rule_project_guest'
          )
    """)
    _logger.info(
        "Retired legacy skill rules: %s ir_rule + %s ir_model_data rows",
        retired, cr.rowcount,
    )
