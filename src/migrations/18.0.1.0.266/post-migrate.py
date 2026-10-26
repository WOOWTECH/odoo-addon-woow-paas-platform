"""Patch the database-backed public website views reported in #2004.

This migration is paired with addon version ``18.0.1.0.266``.

The views were created with Odoo's website editor and therefore have no addon
XML source to update.  Their stable ``ir.ui.view.key`` values are used here;
missing views are a supported no-op (staging does not host the public website),
while a present but drifted view fails closed before any partial write.
"""

from __future__ import annotations

import json
import logging

from odoo import SUPERUSER_ID, api

from odoo.addons.woow_paas_platform.services.public_website_hardening import (
    PATCHERS,
    patch_arch_db,
)


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    env = api.Environment(cr, SUPERUSER_ID, {})
    total_views = 0
    total_languages = 0
    for key, patcher in PATCHERS.items():
        cr.execute(
            """
            SELECT id, arch_db
              FROM ir_ui_view
             WHERE key = %s
             ORDER BY id
            """,
            [key],
        )
        rows = cr.fetchall()
        if not rows:
            _logger.info("[WEBSITE_2004] view key %s absent; skipping", key)
            continue

        pending = []
        for view_id, arch_db in rows:
            if isinstance(arch_db, str):
                arch_db = json.loads(arch_db)
            patched, changed_languages = patch_arch_db(arch_db, patcher)
            pending.append((view_id, patched, changed_languages))

        # Validate every matching view before the first write, so one drifted
        # website cannot leave the migration half-applied.
        for view_id, patched, changed_languages in pending:
            if changed_languages:
                cr.execute(
                    """
                    UPDATE ir_ui_view
                       SET arch_db = %s::jsonb,
                           write_date = NOW()
                     WHERE id = %s
                    """,
                    [json.dumps(patched, ensure_ascii=False), view_id],
                )
                total_views += 1
                total_languages += changed_languages

    if total_views:
        env["ir.ui.view"].invalidate_model(["arch_db"])
        env.registry.clear_cache()
    _logger.info(
        "[WEBSITE_2004] migration complete: views=%s languages=%s",
        total_views,
        total_languages,
    )
