# -*- coding: utf-8 -*-
from odoo import http

# class StockSfConnector(http.Controller):
#     @http.route('/stock_sf_connector/stock_sf_connector/', auth='public')
#     def index(self, **kw):
#         return "Hello, world"

#     @http.route('/stock_sf_connector/stock_sf_connector/objects/', auth='public')
#     def list(self, **kw):
#         return http.request.render('stock_sf_connector.listing', {
#             'root': '/stock_sf_connector/stock_sf_connector',
#             'objects': http.request.env['stock_sf_connector.stock_sf_connector'].search([]),
#         })

#     @http.route('/stock_sf_connector/stock_sf_connector/objects/<model("stock_sf_connector.stock_sf_connector"):obj>/', auth='public')
#     def object(self, obj, **kw):
#         return http.request.render('stock_sf_connector.object', {
#             'object': obj
#         })