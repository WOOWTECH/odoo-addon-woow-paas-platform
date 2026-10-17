from odoo import models
from odoo.http import request

from ..services import rpc_error_redaction
from ..services.security_headers import apply_security_headers, wants_security_headers

# X-SEC-006-1：JSON-RPC 錯誤不對非內部使用者外流 traceback。匿名的畸形 body／不存在的 model
# 在 ir.http 之外就出錯，覆寫 _handle_error 攔不到，所以包在 odoo.http.serialize_exception。
rpc_error_redaction.install()


class IrHttp(models.AbstractModel):
    """#1457：只對本 addon 的頁面路徑補安全標頭（判準見 services/security_headers.py）。"""

    _inherit = 'ir.http'

    @classmethod
    def _post_dispatch(cls, response):
        super()._post_dispatch(response)
        httprequest = getattr(request, 'httprequest', None)
        headers = getattr(response, 'headers', None)
        if httprequest is not None and headers is not None and wants_security_headers(httprequest.path):
            apply_security_headers(headers)
