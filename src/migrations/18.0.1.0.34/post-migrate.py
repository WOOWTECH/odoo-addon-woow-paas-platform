"""18.0.1.0.34 — clean up legacy same-name skill dual-binds (issue #156).

``agent_template._check_no_duplicate_skill_name`` lands in this version: a
template may no longer bind two skills sharing a ``name`` (a pod loads skills
by name, so two same-named bindings collide). Stock data created before the
guard can still carry such pairs, and the constraint would block any later
ORM write to those templates. This migration clears the conservative,
unambiguous case so the guard can go live.

Conservative scope (issue #156 D5):

1. (user, system) same-name pair on one template → DELETE the system rel row,
   keep the user row. This mirrors the snapshot dedup precedence
   (``agent_session._build_skill_snapshot``: user-scope wins over system) so
   the template ends in the state the pod was already effectively running.

2. same-scope same-name pair (user+user or system+system) on one template →
   AMBIGUOUS (no defensible automatic winner): only SELECT + log so an admin
   can unbind one by hand. We do NOT delete — guessing here could silently
   drop the version the admin wanted.

Raw SQL on ``agent_template_skill_rel`` deliberately bypasses the ORM so the
DELETE does not itself trip the constraint mid-migration. Idempotent: re-running
finds no remaining (user, system) pair to delete (case 2 only logs).
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    # --- Case 1: (user, system) same-name pair → delete the system rel row. --
    # Join the rel table to the skill rows twice (one user leg, one system leg)
    # on matching template_id + skill.name; delete only the system leg's rel.
    cr.execute(
        """
        DELETE FROM agent_template_skill_rel sys_rel
        USING woow_paas_platform_skill sys_sk,
              agent_template_skill_rel usr_rel,
              woow_paas_platform_skill usr_sk
        WHERE sys_rel.skill_id = sys_sk.id
          AND sys_sk.scope = 'system'
          AND usr_rel.template_id = sys_rel.template_id
          AND usr_rel.skill_id = usr_sk.id
          AND usr_sk.scope = 'user'
          AND usr_sk.name = sys_sk.name
        """
    )
    deleted = cr.rowcount
    if deleted:
        _logger.info(
            "18.0.1.0.34 skill dual-bind cleanup: deleted %s system rel "
            "row(s) where a same-name user-scope binding exists on the same "
            "template (user-scope kept, mirroring snapshot dedup precedence).",
            deleted,
        )
    else:
        _logger.info(
            "18.0.1.0.34 skill dual-bind cleanup: no (user, system) same-name "
            "pairs found — nothing to delete."
        )

    # --- Case 2: same-scope same-name pair → log only (ambiguous, no delete).-
    cr.execute(
        """
        SELECT rel.template_id, sk.name, sk.scope,
               array_agg(sk.id ORDER BY sk.id) AS skill_ids
        FROM agent_template_skill_rel rel
        JOIN woow_paas_platform_skill sk ON sk.id = rel.skill_id
        GROUP BY rel.template_id, sk.name, sk.scope
        HAVING count(*) > 1
        ORDER BY rel.template_id, sk.name
        """
    )
    ambiguous = cr.fetchall()
    for template_id, skill_name, scope, skill_ids in ambiguous:
        _logger.warning(
            "18.0.1.0.34 skill dual-bind cleanup: template %s binds multiple "
            "'%s' skills in the SAME scope (%s), skill_ids=%s — ambiguous, "
            "left in place. Unbind all but one manually so the duplicate-name "
            "guard can pass on the next edit.",
            template_id, skill_name, scope, list(skill_ids),
        )
    if not ambiguous:
        _logger.info(
            "18.0.1.0.34 skill dual-bind cleanup: no same-scope same-name "
            "pairs remain."
        )
