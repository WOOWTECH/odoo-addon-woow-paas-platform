"""Backfill is_latest invariant for all skills (idempotent).

Spec: docs/plans/2026-Q2/2026-05-28-skill-e2e-and-is-latest-drift-fix-design.md §②

幂等：對每個 (project_id, name) group 重算 is_latest，保證不變式
（at most one is_latest=True per group, must be the most-recently-published
published skill, by published_at desc + id desc tiebreak）。

跑兩次結果一致（所有 skill 已 desired，內部 if-guard 不寫 → no-op）。
無資料 → early return no-op。

Rollback safety: is_latest 是 derived 欄位，重算即修正；不需 down migration。
若 rollback model code（移除 create/write recompute），舊資料仍正確直到
下次 transition 自動重算。
"""
from odoo import api, SUPERUSER_ID, fields as o_fields


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    Skill = env['woow_paas_platform.skill'].sudo()
    all_skills = Skill.search([])
    if not all_skills:
        return  # 無資料 no-op
    epoch = o_fields.Datetime.to_datetime('1970-01-01 00:00:00')
    groups = {}
    for s in all_skills:
        groups.setdefault((s.project_id.id, s.name), []).append(s)
    for (project_id, name), members in groups.items():
        published = [s for s in members if s.state == 'published']
        winner = sorted(
            published,
            key=lambda s: (s.published_at or epoch, s.id),
            reverse=True,
        )[:1]
        winner_id = winner[0].id if winner else None
        for s in members:
            desired = s.id == winner_id
            if s.is_latest != desired:
                s.is_latest = desired
