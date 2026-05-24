"""TDD red phase — Phase D WS3: admin enablement surface (UI toggle + seed).

Plan : docs/plans/2026-Q2/agent-runtime-pod-plan.md WS3 (code-only; nothing is
       bound or flipped — Phase D只準備啟用面).

Acceptance criteria covered:

UI.1  The ``project.project`` FORM view exposes the ``use_agent_runtime``
      toggle. The field shipped in Phase A (project_project.py:63) but NO view
      surfaces it, so an admin cannot enable a pilot project from the UI. Red:
      the resolved form arch does not contain ``use_agent_runtime``. Fix: a new
      ``src/views/project_views.xml`` inherited form view adds JUST this field
      (NOT neighbouring fields like external_mcp_allowed — out of scope).

SEED.1 An UNPUBLISHED demo ``agent_template`` record is seeded by the module
      (new ``src/data/agent_template_demo.xml`` + manifest data entry), not
      bound to a real project. Red: the frozen xmlid does not resolve. Fix:
      add the seed data file.

      FROZEN CONTRACT for impl — the seed record xmlid must be:
          woow_paas_platform.agent_template_demo
      and it must be unpublished (state != 'published').

Red expectation today (impl NOT started):
* UI.1  → RED: no project form view in the module (only project_task_views.xml).
* SEED.1 → RED: env.ref(...agent_template_demo) returns empty.

Boundary (intentionally NOT here):
* Actually flipping use_agent_runtime / binding a pilot project → Phase D
  runbook (user-run), not this code-prep session.
* Neighbouring project fields' UI → explicitly out of scope (plan WS3 note).
"""
import uuid

from odoo.tests.common import TransactionCase, tagged

_DEMO_TEMPLATE_XMLID = 'woow_paas_platform.agent_template_demo'


@tagged('post_install', '-at_install')
class TestPhaseDAdminToggleView(TransactionCase):
    """UI.1 — project form view must surface the use_agent_runtime toggle."""

    def test_UI1_project_form_arch_exposes_use_agent_runtime(self):
        Project = self.env['project.project']
        # precondition: the Phase A field exists, so a red here is "view does
        # not surface it", not "field missing".
        self.assertIn(
            'use_agent_runtime', Project._fields,
            "precondition: project.use_agent_runtime field must exist "
            "(Phase A).",
        )
        arch = Project.get_view(view_type='form')['arch']
        self.assertIn(
            'use_agent_runtime', arch,
            "UI.1: the project FORM view must expose the use_agent_runtime "
            "toggle so an admin can enable a pilot project. Today no view in "
            "the module references it (only project_task_views.xml exists) — "
            "add an inherited project form view (src/views/project_views.xml) "
            "that surfaces JUST this field.",
        )


@tagged('post_install', '-at_install')
class TestPhaseDDemoTemplateSeed(TransactionCase):
    """SEED.1 — an unpublished demo agent_template must be seeded."""

    def test_SEED1_demo_agent_template_exists_unpublished(self):
        template = self.env.ref(_DEMO_TEMPLATE_XMLID, raise_if_not_found=False)
        self.assertTrue(
            template,
            "SEED.1: the demo agent_template seed (%s) is missing — add "
            "src/data/agent_template_demo.xml seeding an UNPUBLISHED demo "
            "template (not bound to a real project) and register it in "
            "__manifest__.py's data list." % _DEMO_TEMPLATE_XMLID,
        )
        self.assertEqual(
            template._name, 'woow_paas_platform.agent_template',
            "SEED.1: %s must be an agent_template record." % _DEMO_TEMPLATE_XMLID,
        )
        self.assertNotEqual(
            template.state, 'published',
            "SEED.1: the demo template must be UNPUBLISHED (state != "
            "'published') — it is a code-prep placeholder, not a live "
            "published template.",
        )
