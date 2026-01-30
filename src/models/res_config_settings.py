from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # 範例設定欄位（可依需求修改或刪除）
    # woow_api_key = fields.Char(
    #     string='API Key',
    #     config_parameter='woow_cloud_platform.api_key',
    # )
