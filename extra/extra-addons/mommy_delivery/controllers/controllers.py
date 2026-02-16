# -*- coding: utf-8 -*-
# from odoo import http


# class MommyDelivery(http.Controller):
#     @http.route('/mommy_delivery/mommy_delivery', auth='public')
#     def index(self, **kw):
#         return "Hello, world"

#     @http.route('/mommy_delivery/mommy_delivery/objects', auth='public')
#     def list(self, **kw):
#         return http.request.render('mommy_delivery.listing', {
#             'root': '/mommy_delivery/mommy_delivery',
#             'objects': http.request.env['mommy_delivery.mommy_delivery'].search([]),
#         })

#     @http.route('/mommy_delivery/mommy_delivery/objects/<model("mommy_delivery.mommy_delivery"):obj>', auth='public')
#     def object(self, obj, **kw):
#         return http.request.render('mommy_delivery.object', {
#             'object': obj
#         })
