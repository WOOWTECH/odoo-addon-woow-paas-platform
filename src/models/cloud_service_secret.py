from odoo import fields, models


class CloudServiceSecret(models.Model):
    _name = 'woow_paas_platform.cloud_service_secret'
    _description = 'Cloud Service Secret'
    _order = 'updated_at desc, id desc'

    service_id = fields.Many2one(
        comodel_name='woow_paas_platform.cloud_service',
        required=True,
        ondelete='cascade',
        index=True,
        help='Owning cloud service',
    )
    admin_password = fields.Char(
        string='MCP Admin Password',
        required=True,
        help='One-time plaintext admin password for MCP admin GUI',
    )
    # #530：本模型刻意維持「一個 service 一組憑證」的單記錄設計（service_id 帶
    # UNIQUE），第二把固定用途的憑證就是加一個欄位——改建成通用 key-value 表要
    # 另立 ACL、唯一鍵語意與清理路徑，換不到任何現有需求。
    #
    # 刻意**不設** required：存量服務（本欄位落地前建立的）讀回 False，那是合法
    # 狀態而非錯誤——它們走 Reset 換發（`_build_mcp_sensitive_values` 對空值整個
    # 鍵不送，讓 chart 沿用既有 Secret）。設 required 會讓 -u 對既有列直接炸。
    gateway_api_key = fields.Char(
        string='Gateway API Key',
        help='One-time plaintext gateway API key; supplied to the chart at '
             'auth.apiServerKey (hermes-agent gateway Bearer auth)',
    )
    updated_at = fields.Datetime(
        string='Updated At',
        required=True,
        default=fields.Datetime.now,
    )

    _sql_constraints = [
        (
            'unique_cloud_service_secret_service',
            'UNIQUE(service_id)',
            'Each cloud service can have only one secret record.',
        ),
    ]

    def write(self, vals):
        vals = dict(vals)
        vals.setdefault('updated_at', fields.Datetime.now())
        return super().write(vals)
