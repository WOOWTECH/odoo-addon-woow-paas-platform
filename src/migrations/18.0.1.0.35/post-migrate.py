"""18.0.1.0.35 — re-pack demo hello-world skill bundle: SKILL.md-only.

Claude Code model (skill execution redesign): a skill no longer needs an
executable entrypoint. The demo hello-world bundle becomes SKILL.md-only — the
SKILL.md body is the skill's instructions, injected into the agent context via
``load_skill``; there is no ``scripts/main.py``. The 18.0.1.0.33 re-pack had
shipped an executable ``scripts/main.py`` for the (now removed) subprocess
contract.

``demo_skill_hello_world`` is ``noupdate="1"``, so a module upgrade does NOT
overwrite the existing attachment via the XML data file. This migration updates
the attachment bytes + ``bundle_sha256`` in place via a DIRECT ORM write (noupdate
only blocks XML data reload, not direct ORM writes — same mechanism as the
18.0.1.0.33 migration). fresh install seeds the new bytes via XML; an upgrade
path repairs the existing DB row here.

Idempotent: no-op when the skill is absent (admin-deleted, or fresh install which
seeds the new bundle via XML) or already carries the new sha.
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
            "18.0.1.0.35 demo-bundle re-pack: demo_skill_hello_world absent "
            "(admin-deleted or fresh install) — nothing to migrate."
        )
        return

    with file_open(_BUNDLE_RESOURCE, "rb") as fh:
        raw = fh.read()
    new_sha = hashlib.sha256(raw).hexdigest()

    if skill.bundle_sha256 == new_sha:
        _logger.info(
            "18.0.1.0.35 demo-bundle re-pack: already SKILL.md-only bundle "
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
        "18.0.1.0.35 demo-bundle re-pack: updated demo_skill_hello_world to "
        "SKILL.md-only bundle (sha %s -> %s).",
        old_sha,
        new_sha,
    )
