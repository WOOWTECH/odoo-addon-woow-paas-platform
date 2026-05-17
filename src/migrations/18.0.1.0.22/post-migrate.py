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

    # Odoo's project module ships `project_public_members_rule` for base.group_user
    # with domain ['|', ('privacy_visibility', '!=', 'followers'), follower-check].
    # Odoo evaluates per-group rules with OR, so any always-true leg of that rule
    # bypasses the workspace filter introduced by D3=A. Deactivate it so the
    # decouple-refactor's `project_rule_workspace_member` is the sole gate.
    cr.execute(
        """
        UPDATE ir_rule SET active = false
        WHERE id IN (
            SELECT res_id FROM ir_model_data
            WHERE module = 'project'
              AND name = 'project_public_members_rule'
              AND model = 'ir.rule'
        )
        """
    )
    _logger.info(
        "Deactivated project.project_public_members_rule (%s rows)",
        cr.rowcount,
    )
