from odoo import models
from odoo.http import request

from ..services.security_headers import apply_security_headers, wants_security_headers


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
