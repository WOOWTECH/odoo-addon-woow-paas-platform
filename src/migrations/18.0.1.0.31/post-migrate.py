"""18.0.1.0.31 — re-pack demo hello-world skill bundle to root-level SKILL.md.

The seeded demo bundle (demo_system_skill.xml, ``<data noupdate="1">``) was
packed with a wrapper dir (``hello-world/SKILL.md``), but agent-runtime
``cache.py`` reads ``dest/SKILL.md`` after extraction — so the pod raised
``No such file: .../SKILL.md`` on every invoke (re-fetch loop, never cached).
The bundle is now re-packed root-level (``SKILL.md`` at tar root, per
``agent-runtime-pod-design.md`` §bundle structure + ``skill-dual-scope-design``).

Because the record is ``noupdate``, a module upgrade does NOT overwrite the
existing attachment, so this migration updates the existing
``demo_skill_hello_world`` attachment bytes + ``bundle_sha256`` in place from
the on-disk bundle that ships with this version. Idempotent: no-op when the
skill is absent (admin-deleted, or fresh install which seeds the new bundle via
XML) or already carries the new sha.
"""
import base64
import hashlib
import logging

from odoo import SUPERUSER_ID, api
from odoo.tools import file_open

_logger = logging.getLogger(__name__)

_BUNDLE_RESOURCE = (
    "woow_paas_platform/data/demo_skills/hello-world-0.1.0.tar.gz"
)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    skill = env.ref(
        "woow_paas_platform.demo_skill_hello_world", raise_if_not_found=False
    )
    if not skill:
        _logger.info(
            "18.0.1.0.31 demo-bundle re-pack: demo_skill_hello_world absent "
            "(admin-deleted or fresh install) — nothing to migrate."
        )
        return

    with file_open(_BUNDLE_RESOURCE, "rb") as fh:
        raw = fh.read()
    new_sha = hashlib.sha256(raw).hexdigest()

    if skill.bundle_sha256 == new_sha:
        _logger.info(
            "18.0.1.0.31 demo-bundle re-pack: already root-level "
            "(sha %s) — no-op.",
            new_sha,
        )
        return

    old_sha = skill.bundle_sha256
    attachment = skill.bundle_attachment_id
    if attachment:
        attachment.write({"datas": base64.b64encode(raw).decode("ascii")})
    skill.write({"bundle_sha256": new_sha})
    _logger.info(
        "18.0.1.0.31 demo-bundle re-pack: updated demo_skill_hello_world "
        "bundle to root-level SKILL.md (sha %s -> %s).",
        old_sha,
        new_sha,
    )
