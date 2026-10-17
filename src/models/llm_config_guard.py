"""AI 模型設定的寫入守衛（DESIGN §2.11，〔REV〕15）。

``newapi_proxy_url`` 決定 PaaS 把簽過章的請求（含 UP 與開戶密碼）送到哪裡：寫成外部主機
就等於把憑證交出去。所以只接受兩個叢集內代理位址與 neutralize 的黑洞值，其他值在
``ir.config_parameter`` 的 ORM 寫入當下就拒絕（``set_param`` 走 create／write）。
SQL 直改擋不住，但 ``llm_common.llm_config`` 讀取時會再驗一次、不合法就當「未設定」。

只看本票的 key（``llm_common.LLM_CONFIG_PARAMETERS``），其他設定的行為完全不變。
"""
from odoo import api, models

from .llm_common import LLM_CONFIG_PARAMETERS, validate_llm_param


class IrConfigParameter(models.Model):
    _inherit = 'ir.config_parameter'

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('key') in LLM_CONFIG_PARAMETERS:
                validate_llm_param(vals.get('key'), vals.get('value'))
        return super().create(vals_list)

    def write(self, vals):
        if 'key' in vals or 'value' in vals:
            for record in self:
                key = vals.get('key', record.key)
                if key in LLM_CONFIG_PARAMETERS:
                    validate_llm_param(key, vals.get('value', record.value))
        return super().write(vals)
