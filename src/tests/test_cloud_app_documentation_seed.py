"""The service Information tab must get a guide, not marketplace copy."""

from odoo.tests.common import TransactionCase


class TestCloudAppDocumentationSeed(TransactionCase):
    _GUIDE_XMLIDS = (
        'cloud_app_code_server',
        'cloud_app_emqx',
        'cloud_app_ha_full',
        'cloud_app_haos',
        'cloud_app_headscale',
        'cloud_app_hermes_agent',
        'cloud_app_litellm',
        'cloud_app_mcp_hermes',
        'cloud_app_mcp_litellm',
        'cloud_app_mcp_n8n',
        'cloud_app_mcp_nextcloud',
        'cloud_app_mcp_odoo',
        'cloud_app_n8n',
        'cloud_app_nextcloud',
        'cloud_app_odoo',
        'cloud_app_omnigent',
        'cloud_app_opendesign',
        'cloud_app_pi_agent',
        'cloud_app_tailscale',
    )

    def test_service_guides_are_loaded_for_current_templates(self):
        for xmlid in self._GUIDE_XMLIDS:
            with self.subTest(xmlid=xmlid):
                template = self.env.ref('woow_paas_platform.' + xmlid)
                self.assertTrue(template.documentation)
                self.assertTrue(template.documentation.startswith('# '))
                self.assertIn('## 開始使用', template.documentation)
                self.assertNotEqual(template.documentation, template.full_description)
