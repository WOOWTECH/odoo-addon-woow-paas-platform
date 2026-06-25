"""18.0.1.0.33 — re-pack demo hello-world skill bundle: executable entrypoint.

The 18.0.1.0.31 re-pack put SKILL.md at the tar root (so the pod could find it),
but left the bundle's script unrunnable: it shipped ``src/main.py`` while the
agent-runtime loader defaults ``entrypoint`` to ``scripts/main.py``
(``loader.py``), and that script only ``def run()`` — it never read the stdin
JSON args nor wrote to stdout, which is the actual subprocess execution contract
(``subprocess_runner.py``: ``python3 <entrypoint>``, input_args on stdin, stdout
== tool_result). So every invoke returned ``SKILL_NOT_FOUND`` (and would have
returned empty even with the path fixed). See issue #139.

The bundle is now re-packed at the canonical ``scripts/main.py`` (design default)
with a ``__main__`` harness that reads ``json.load(sys.stdin)`` and writes the
greeting to stdout; SKILL.md frontmatter declares ``entrypoint: scripts/main.py``
explicitly.

Because the record is ``noupdate``, a module upgrade does NOT overwrite the
existing attachment, so this migration updates the existing
``demo_skill_hello_world`` attachment bytes + ``bundle_sha256`` in place from the
on-disk bundle that ships with this version. Idempotent: no-op when the skill is
absent (admin-deleted, or fresh install which seeds the new bundle via XML) or
already carries the new sha.
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
            "18.0.1.0.33 demo-bundle re-pack: demo_skill_hello_world absent "
            "(admin-deleted or fresh install) — nothing to migrate."
        )
        return

    with file_open(_BUNDLE_RESOURCE, "rb") as fh:
        raw = fh.read()
    new_sha = hashlib.sha256(raw).hexdigest()

    if skill.bundle_sha256 == new_sha:
        _logger.info(
            "18.0.1.0.33 demo-bundle re-pack: already executable bundle "
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
        "18.0.1.0.33 demo-bundle re-pack: updated demo_skill_hello_world "
        "bundle to executable scripts/main.py entrypoint (sha %s -> %s).",
        old_sha,
        new_sha,
    )
