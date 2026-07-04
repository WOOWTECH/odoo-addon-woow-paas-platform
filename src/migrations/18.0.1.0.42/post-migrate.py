"""18.0.1.0.42 — #279 P5 one-time tenant-isolation backfill.

Namespaces created before O4/O1 lack the baseline NetworkPolicy bundle and the
``woow.tw/workspace-id`` / ``paas.woowtech.io/env`` labels the #279 selectors now
depend on. Once #280 removes the agent-runtime chart's from-less allows, an
un-backfilled CS ns (no baseline NP + no workspace-id label) or a legacy agent ns
(no env label → fails NP4's env predicate) silently loses reach — a fail-closed
outage. This migration re-applies the baseline NP bundle + re-stamps managed labels
on every existing CloudService ns, and re-stamps env (+ reach-ws) on every live
agent-runtime ns.

Idempotent + re-runnable + best-effort (per-ns logged; one failure never aborts the
sweep). MUST run before #280. Delegated to a model method so it is unit-testable.
"""

from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['woow_paas_platform.cloud_service']._backfill_reach_isolation_p5()
