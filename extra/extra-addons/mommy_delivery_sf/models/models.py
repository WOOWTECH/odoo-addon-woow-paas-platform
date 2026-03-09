#!/usr/bin/python3
# @Time    : 2019-08-20
# @Author  : Kevin Kong (kfx2007@163.com)

from odoo import api, fields, models, _
from sf.api import SF
from sf.model.cargo import CargoDetail
from odoo.exceptions import UserError
from .delivery import EXPRESS_TYPES, PAYMENT_METHODS
import logging

_logger = logging.getLogger(__name__)


class stock_picking(models.Model):

    _inherit = "stock.picking"

    @api.depends("carrier_id", "carrier_tracking_ref")
    def _get_route(self):
        """
        getting the tracking detail.
        """
        sf_id = self.env.ref("mommy_delivery_sf.carrier_sf").id
        if self.carrier_id.id != sf_id:
            self.carrier_tracking_detail = ""
            return
        sf = self.carrier_id._get_sf_client()
        if not self.carrier_tracking_ref:
            self.carrier_tracking_detail = None
            return
        res = sf.order.get_route_info(self.carrier_tracking_ref)
        _logger.debug(f"[SF]result of getting the tracking detail:{res}")
        if not res.get("success"):
            # something went wrong
            self.carrier_tracking_detail = res.get('msg', "")
        else:
            # format data
            routes = res["msgData"]["routeResps"][0]['routes']
            if type(routes) is list:
                result = "\r\n".join(
                    '{}: {}({})'.format(route["acceptTime"], route["remark"], route["acceptAddress"]) for route in routes)
            else:
                result = '{}: {}({})'.format(
                    routes["acceptTime"], routes["remark"], routes["acceptAddress"])
            self.carrier_tracking_detail = result

    def _get_cargo_desc(self):
        """"
        get cargo description
        """
        cargo = self.sf_cargo_detail or self.carrier_id.sf_default_cargo
        if not cargo:
            raise UserError(_('You have to select at least one cargo.'))
        return cargo.name

    def _get_cargo_detail(self):
        """"get cargo detail"""
        cargos = [CargoDetail(move.product_id.name, cargoDeclaredValue=move.product_uom_qty * move.product_id.list_price)
            for move in self.move_ids_without_package
        ]
        return cargos

    carrier_tracking_detail = fields.Text(
        "Tracking Detail", compute="_get_route")
    sf_allow_manual = fields.Boolean("Allow Order Manually.", related="carrier_id.sf_allow_manual")
    sf_express_type_select = fields.Boolean(related="carrier_id.sf_select_express_type")
    sf_payment_select = fields.Boolean(related="carrier_id.sf_payment_select")
    sf_express_type = fields.Selection(
        EXPRESS_TYPES, string="SF Product Type", default="1")
    sf_payment_method = fields.Selection(
        PAYMENT_METHODS, string="SF Payment Method", default='1')
    sf_cargo_detail = fields.Many2one("sf.delivery.cargo",string="Cargo Desc")
    sf_declared_value = fields.Float("Declared Value")
    sf_declared_currency = fields.Many2one('res.currency',string='Currency')

    def btn_sf(self):
        """
        make an order manually.
        """
        res = self.carrier_id.sf_send_shipping(self)
        if res:
            self.carrier_tracking_ref = res[0]['tracking_number']

    def update_tracking_detail(self):
        self._get_route()

    def get_express_type(self):
        if self.carrier_id.sf_select_express_type:
            return self.sf_express_type
        return self.carrier_id.sf_default_express_type

    def get_payment_method(self):
        if self.carrier_id.sf_payment_select:
            return self.sf_payment_method
        return self.carrier_id.sf_default_payment
