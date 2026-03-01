# -*- coding: utf-8 -*-
# from odoo import http


# class MommyProduct(http.Controller):
#     @http.route('/mommy_product/mommy_product', auth='public')
#     def index(self, **kw):
#         return "Hello, world"

#     @http.route('/mommy_product/mommy_product/objects', auth='public')
#     def list(self, **kw):
#         return http.request.render('mommy_product.listing', {
#             'root': '/mommy_product/mommy_product',
#             'objects': http.request.env['mommy_product.mommy_product'].search([]),
#         })

#     @http.route('/mommy_product/mommy_product/objects/<model("mommy_product.mommy_product"):obj>', auth='public')
#     def object(self, obj, **kw):
#         return http.request.render('mommy_product.object', {
#             'object': obj
#         })
