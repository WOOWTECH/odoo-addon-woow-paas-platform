"""The service Information tab must get a guide, not marketplace copy."""

from odoo.tests.common import TransactionCase


class TestCloudAppDocumentationSeed(TransactionCase):
    _TUTORIAL_REPOS = {
        'cloud_app_code_server': 'Woow_code_server_tutorial_site',
        'cloud_app_emqx': 'Woow_emqx_tutorial_site',
        'cloud_app_ha_full': 'Woow_ha_tutorial_site',
        'cloud_app_haos': 'Woow_ha_tutorial_site',
        'cloud_app_headscale': 'Woow_headscale_tutorial_site',
        'cloud_app_n8n': 'Woow_n8n_tutorial_site',
        'cloud_app_nextcloud': 'Woow_nextcloud_tutorial_site',
        'cloud_app_omnigent': 'Woow_omnigent_tutorial_site',
        'cloud_app_opendesign': 'Woow_opendesign_tutorial_site',
    }
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
                self.assertIn('## 跟著做', template.documentation)
                self.assertIn('## 延伸學習', template.documentation)
                self.assertNotEqual(template.documentation, template.full_description)
                if xmlid in self._TUTORIAL_REPOS:
                    self.assertTrue(template.tutorial_url.startswith('https://'))
                    self.assertIn('https://github.com/WOOWTECH/' + self._TUTORIAL_REPOS[xmlid],
                                  template.documentation)
                else:
                    self.assertFalse(template.tutorial_url)
