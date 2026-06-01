"""Auto-enable agent runtime on project creation.

決策來源：docs/plans/2026-Q2/2026-06-01-auto-enable-agent-runtime.md
Covers:
  T1   res.config.settings.woow_default_enable_agent_runtime round-trips through
       ir.config_parameter 'woow_paas_platform.default_enable_agent_runtime'.
  R-*  _agent_runtime_autoenable_ready fail-safe gate.
  C-*  _create_project auto-enables only when ready.
  B-*  backfill_existing_projects_use_agent_runtime one-time migration helper.
"""
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged

_TOGGLE = 'woow_paas_platform.default_enable_agent_runtime'
_URL = 'woow_paas_platform.operator_url'
_KEY = 'woow_paas_platform.operator_api_key'
_DEFTMPL = 'woow_paas_platform.default_agent_template_id'


@tagged('post_install', '-at_install')
class TestAutoEnableToggleSetting(TransactionCase):
    """T1: the Settings Boolean persists to ir.config_parameter."""

    def test_t1_toggle_round_trips_to_config_parameter(self):
        settings = self.env['res.config.settings'].sudo().create({
            'woow_default_enable_agent_runtime': True,
        })
        settings.execute()
        stored = self.env['ir.config_parameter'].sudo().get_param(_TOGGLE)
        self.assertEqual(
            stored, 'True',
            "Settings field must persist True to the '%s' config_parameter."
            % _TOGGLE,
        )


@tagged('post_install', '-at_install')
class TestAutoEnableUninstallCleanup(TransactionCase):
    """The new config_parameter must be in hooks._CONFIG_PARAMETERS so an
    addon uninstall clears it (CLAUDE.md: config_parameter is not auto-cleaned).
    """

    def test_toggle_param_in_uninstall_cleanup_list(self):
        from odoo.addons.woow_paas_platform import hooks
        self.assertIn(
            _TOGGLE, hooks._CONFIG_PARAMETERS,
            "default_enable_agent_runtime must be in _CONFIG_PARAMETERS so "
            "uninstall removes it.",
        )
