# -*- coding: utf-8 -*-
from odoo import http
from odoo.http import request
import base64
from werkzeug.wrappers import Response

class AIAvatarController(http.Controller):

    @http.route('/ai/avatar/user/<int:user_id>', type='http', auth='public', website=True)
    def user_avatar(self, user_id):
        user = request.env['res.users'].sudo().browse(user_id)
        if user and user.avatar_128:
            image_bytes = base64.b64decode(user.avatar_128)
            return Response(
                image_bytes,
                content_type='image/png',
                headers=[
                    ('Content-Length', str(len(image_bytes))),
                    ('Cache-Control', 'public, max-age=86400'),
                ],
            )
        return request.redirect('/web/static/src/img/avatar.png')

    @http.route('/ai/avatar/llm/<int:llm_id>', type='http', auth='public', website=True)
    def llm_avatar(self, llm_id):
        llm = request.env['sh.ai.llm'].sudo().browse(llm_id)
        if llm and llm.image:
            image_bytes = base64.b64decode(llm.image)
            return Response(
                image_bytes,
                content_type='image/png',
                headers=[
                    ('Content-Length', str(len(image_bytes))),
                    ('Cache-Control', 'public, max-age=86400'),
                ],
            )
        return request.redirect('/sh_ai_assistance/static/description/icon.png')
