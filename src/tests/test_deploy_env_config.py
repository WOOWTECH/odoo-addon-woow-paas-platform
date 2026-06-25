"""L3: deploy_env config_parameter wiring + uninstall cleanup list."""
from odoo.tests.common import TransactionCase

from odoo.addons.woow_paas_platform import hooks


class TestDeployEnvConfig(TransactionCase):
    """woow_deploy_env Settings 欄位 ↔ ir.config_parameter 接線 + 卸載清理。"""

    def test_deploy_env_config_parameter_roundtrip(self):
        """Settings.woow_deploy_env 經 execute() 寫入正確的 config_parameter key。

        斷言硬寫字面 key 'woow_paas_platform.deploy_env'（非反讀欄位定義），
        避免 key 打錯卻假綠。
        """
        settings = self.env['res.config.settings'].create(
            {'woow_deploy_env': 'stg'})
        settings.execute()
        self.assertEqual(
            self.env['ir.config_parameter'].sudo().get_param(
                'woow_paas_platform.deploy_env'),
            'stg',
        )

    def test_deploy_env_in_config_parameters_cleanup_list(self):
        """卸載清理：deploy_env 須在 hooks._CONFIG_PARAMETERS（用 in，不依賴 len/index）。"""
        self.assertIn(
            'woow_paas_platform.deploy_env', hooks._CONFIG_PARAMETERS)
