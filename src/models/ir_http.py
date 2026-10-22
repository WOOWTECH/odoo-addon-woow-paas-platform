from odoo import models
from odoo.http import request

from ..services import rpc_error_redaction
from ..services.security_headers import (
    apply_hsts_header,
    apply_security_headers,
    wants_platform_hsts,
    wants_security_headers,
)

# X-SEC-006-1：JSON-RPC 錯誤不對非內部使用者外流 traceback。匿名的畸形 body／不存在的 model
# 在 ir.http 之外就出錯，覆寫 _handle_error 攔不到，所以包在 odoo.http.serialize_exception。
rpc_error_redaction.install()


class IrHttp(models.AbstractModel):
    """#1457：完整標頭依路徑；aiot.woowtech.io 的 HSTS 依主機套用。"""

    _inherit = 'ir.http'

    @classmethod
    def _post_dispatch(cls, response):
        super()._post_dispatch(response)
        httprequest = getattr(request, 'httprequest', None)
        headers = getattr(response, 'headers', None)
        if httprequest is not None and headers is not None:
            if wants_platform_hsts(getattr(httprequest, 'host', None)):
                apply_hsts_header(headers)
            if wants_security_headers(httprequest.path):
                apply_security_headers(headers)
