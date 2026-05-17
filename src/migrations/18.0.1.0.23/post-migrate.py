"""18.0.1.0.18 — CloudService ↔ Project decouple (D3=A / D8=A).

Forces the rewritten `project_rule_workspace_member` domain to take effect on
DBs that loaded the legacy rule under ``noupdate="1"`` (so a plain ``-u`` does
not refresh the domain). Also marks the ir_model_data row noupdate=0 so future
updates re-apply the XML-defined domain.

The companion rule ``cloud_service_rule_workspace_member`` is new — XML load
handles its create — but we still flip its noupdate flag for the same reason.
"""
import logging

_logger = logging.getLogger(__name__)


_PROJECT_RULE_DOMAIN = (
    "['|', "
    "('cloud_service_id.workspace_id.access_ids.user_id', '=', user.id), "
    "'&', "
    "('cloud_service_id', '=', False), "
    "'|', "
    "('message_partner_ids', 'in', [user.partner_id.id]), "
    "('create_uid', '=', user.id)"
    "]"
)
_PROJECT_RULE_NAME = "Project: workspace member (bound) or project member (unbound)"


def migrate(cr, version):
    if not version:
        return

    cr.execute(
        """
        UPDATE ir_rule
        SET domain_force = %s, name = %s
        WHERE id IN (
            SELECT res_id FROM ir_model_data
            WHERE module = 'woow_paas_platform'
              AND name = 'project_rule_workspace_member'
              AND model = 'ir.rule'
        )
        """,
        [_PROJECT_RULE_DOMAIN, _PROJECT_RULE_NAME],
    )
    _logger.info(
        "Refreshed project_rule_workspace_member domain (%s rule rows updated)",
        cr.rowcount,
    )

    cr.execute(
        """
        UPDATE ir_model_data
        SET noupdate = false
        WHERE module = 'woow_paas_platform'
          AND name IN (
            'project_rule_workspace_member',
            'cloud_service_rule_workspace_member'
          )
        """
    )
    _logger.info(
        "Cleared noupdate on refactored ir.rule xmlids (%s data rows)",
        cr.rowcount,
    )

    # Odoo's project module ships rules whose always-true legs OR-bypass the
    # workspace filter introduced by D3=A (see Odoo `ir_rule.py:140-168` —
    # per-group rules combine with OR, so a single permissive leg defeats all
    # stricter rules in the same group). Deactivate the symmetric pair on both
    # project.project and project.task:
    #
    #   - project.project_public_members_rule (base.group_user, project.project)
    #         leg: privacy_visibility != 'followers' → matches Odoo's default
    #              privacy ('portal'/'employees') → universal project read
    #   - project.task_visibility_rule (base.group_user, project.task)
    #         leg: project.privacy_visibility != 'followers' → same leak shape,
    #              perm_read implicit
    #   - project.task_visibility_rule_project_user (project.group_project_user,
    #         project.task) — same domain, perm_read=False so it grants write /
    #         create / unlink to any group_project_user on every non-followers
    #         project's tasks
    #
    # CAQA B1 caught the asymmetry: project rule was deactivated in 18.0.1.0.22
    # but the two task counterparts were left active, so cross-workspace task
    # read still leaked via the std rule's always-true leg. Listing all three
    # in one UPDATE keeps them in lockstep on future migrations.
    cr.execute(
        """
        UPDATE ir_rule SET active = false
        WHERE id IN (
            SELECT res_id FROM ir_model_data
            WHERE module = 'project'
              AND model = 'ir.rule'
              AND name IN (
                'project_public_members_rule',
                'task_visibility_rule',
                'task_visibility_rule_project_user'
              )
        )
        """
    )
    _logger.info(
        "Deactivated 3 Odoo std rules that OR-bypass the workspace filter "
        "(project_public_members_rule + task_visibility_rule + "
        "task_visibility_rule_project_user) — %s rows",
        cr.rowcount,
    )
