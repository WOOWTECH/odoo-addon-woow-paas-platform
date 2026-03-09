#!/usr/bin/python3
# @Time    : 2022-12-02
# @Author  : Kevin Kong (kfx2007@163.com)

from odoo import api, fields, models, _
from sf.api import SF
from autils.string import randomstr
from sf.order import QUERY_URL
from sf.model.contact import ContactInfo
from sf.model.cargo import CargoDetail
from sf.model.address import Address
from odoo.exceptions import UserError
import logging

_logger = logging.getLogger(__name__)

ORDER_TYPES = [
    ('picking', 'Using picking name as order Id.'),
    ('origin', 'Using picking origin as order Id.'),
    ('random', 'Generate random string as order Id.')
]

LANGUAGE_TYPES = [
    ('follow', 'Follow System'),
    ('fixed', 'Fixed')
]

LANGUAGES = [
    ('zh-CN', 'Chinese'),
    ('zh-TW', 'Chinese(TW)'),
    ('zh-HK', 'Chinese(HK)'),
    ('zh-MO', 'Chinese Tranditional'),
    ('en', 'English')
]

EXPRESS_TYPES = [(i, n) for i, t, n in SF.get_express_types()]

PAYMENT_METHODS = [
    ('1', '寄付现结'),
    ('2', '到付'),
    ('3', '寄付月结')
]


class delivery_carrier(models.Model):

    _inherit = "delivery.carrier"

    def _get_sf_client(self):
        """获取SFAPI"""
        if self.sf_language_type == 'fixed':
            language = self.sf_language
        else:
            language = self.env.lang
        if self.prod_environment:
            checkword = self.sf_checkword
        else:
            checkword = self.sf_sandbox_checkword
        return SF(self.sf_clientcode, checkword, sandbox=not self.prod_environment, language=language)

    def _get_order_id(self, picking):
        """获取订单号"""
        if self.sf_order_type == "picking":
            return picking.display_name
        elif self.sf_order_type == "origin":
            return picking.origin
        else:
            return f"{picking.name}-{randomstr.generate(4)}"

    @api.model
    def _get_sf_format_address(self, partner):
        """get format address"""
        args = {
            "state": partner.state_id.name or "",
            "city": partner.city or "",
            "street": partner.street or "",
            "street2": partner.street2 or "",
        }
        return self.sf_address_format % args

    def _get_cargo_detail(self):
        return self.sf_default_goods or _("Goods")

    delivery_type = fields.Selection(
        selection_add=[('sf', 'SF Express')], ondelete={"sf": "cascade"})
    sf_allow_manual = fields.Boolean("Allow Order Manually.", default=False)
    sf_clientcode = fields.Char("Client Code")
    sf_sandbox_checkword = fields.Char("Sandbox Check Word")
    sf_checkword = fields.Char("Check Word")
    sf_custid = fields.Char("Monthly Code")
    sf_language_type = fields.Selection(
        LANGUAGE_TYPES, string="Language Type", default="follow")
    sf_language = fields.Selection(
        LANGUAGES, string="Language", default='zh-CN')
    sf_templcate_code = fields.Many2one("mommy.sf.template",string="Template")
    sf_order_type = fields.Selection(
        ORDER_TYPES, string="Order Id Setting", default='origin')
    sf_sender_address = fields.Many2one("res.partner", string="Sender Address")
    sf_address_format = fields.Text(
        "Address Format", default="%(state) s%(city)s %(street)s %(street2)s")
    sf_default_cargo = fields.Many2one("sf.delivery.cargo", string="Cargo Description")
    sf_select_express_type = fields.Boolean(
        "Product Select", default=False, help="Select Express Product Type on Pickings")
    sf_default_express_type = fields.Selection(
        EXPRESS_TYPES, string="Product Type", default='1')
    sf_payment_select = fields.Boolean(
        "Payment Select", help="Select Payment Method on pickings")
    sf_default_payment = fields.Selection(
        PAYMENT_METHODS, string="Payment Method", default='1')
    sf_declared_value = fields.Float("Declared Value")
    sf_declared_currency = fields.Many2one('res.currency', string='currency')

    def action_sync_template(self):
        """
        action sync template
        """
        sf = self._get_sf_client()
        res = sf.sheet.get_custom_templates("")
        if res['success']:
            tmp_obj = self.env['mommy.sf.template'].sudo()
            tmplates = tmp_obj.search([])
            tmplates.unlink()
            # 
            data = [{
                "name": t['customTemplateName'],
                "code": t['customTemplateCode']
            }
                for t in res['obj']]
            tmp_obj.create(data)
            return self.show_message(_("Success"),_("Template Sync Success!"))


    def sf_send_shipping(self, pickings):
        """send package to service provider"""
        sf = self._get_sf_client()
        res = []
        for picking in pickings:
            receiver, sender = picking.partner_id, picking.get_sender()
            contact_info = []
            reciever_info = ContactInfo(
                f"{self._get_sf_format_address(receiver)}", contact=receiver.name, province=receiver.state_id.name, city=receiver.city, mobile=receiver.phone or receiver.mobile)
            sender_info = ContactInfo(
                f"{self._get_sf_format_address(sender)}", contact=sender.name, province=sender.state_id.name, city=sender.city,  contactType=1, mobile=sender.phone or sender.mobile)
            contact_info += [reciever_info, sender_info]
            cargos = picking._get_cargo_detail()
            order_id = self._get_order_id(picking)
            kwargs = {
                "cargoDesc": picking._get_cargo_desc(),
                "expressTypeId": int(picking.get_express_type()),
                "payMethod": int(picking.get_payment_method()),
                "monthlyCard": sender.sf_month_code or self.sf_custid
            }
            # check if customer's country == CN
            if receiver.country_id.code != 'CN':
                # international business.
                kwargs['customsInfo'] = {
                    "declaredValue": picking.sf_declared_value if picking.sf_declared_value else str(picking.carrier_id.sf_declared_value),
                    "declaredValueCurrency": picking.sf_declared_currency.name if picking.sf_declared_currency else self.sf_declared_currency.name
                }

            _logger.debug("[Mommy SF]create sf order:%s" % kwargs)
            result = sf.order.create_order(order_id, contact_info, cargos, **kwargs)
            if not result['success']:
                raise UserError(_("SF shippment failed :%s , origin:%s") %
                                (result['errorMsg'], order_id))
            else:
                tracking_number = result["msgData"]["waybillNoInfoList"][0]["waybillNo"]
                log_message = (
                    _("Shipment created into SF <br/> <b>Tracking Number : </b>%s") % (tracking_number))
                documents  = [{'masterWaybillNo': tracking_number}]
                template_code = f"{self.sf_templcate_code.code}_{self.sf_clientcode}"
                files = sf.sheet.sync_print(template_code, documents)
                pdfs = [
                    (f'LabelSF-{tracking_number}-{picking.display_name}', file) for file in files]
                picking.message_post(body=log_message, attachments=pdfs, body_is_html=True)
                tracking = {
                    'exact_price': 0,
                    'tracking_number': tracking_number
                }
                res += [tracking]

        return res

    def sf_get_tracking_link(self, picking):
        return f"{QUERY_URL}{picking.carrier_tracking_ref}"

    def sf_cancel_shipment(self, picking):
        sf = self._get_sf_client()
        order_id = self._get_order_id(picking)
        res = sf.order.confirm_order(
            order_id, mailno=picking.carrier_tracking_ref, dealType=2)
        if res["success"]:
            # picking.message_post(
            #     body=_(f"{picking.origin}'s delivery order:{picking.carrier_tracking_ref} has been canceled."))
            picking.write({'carrier_tracking_ref': '',
                           'carrier_price': 0.0})

    def sf_get_return_label(self, picking, tracking_number=None, origin_date=None):
        shipping_data = {
            'exact_price': 0,
            'tracking_number': picking.carrier_tracking_ref,
        }
        return shipping_data

    def sf_rate_shipment(self, order):
        sf = self._get_sf_client()
        receiver, sender = order.partner_shipping_id, order.warehouse_id.partner_id
        dest_address = Address(receiver.state_id.name,
                               receiver.city, receiver.street, receiver.street2)
        src_address = Address(sender.state_id.name,
                              sender.city, sender.street, sender.street2)
        weight = round(sum(line.product_id.weight * line.product_uom_qty for line in order.order_line),2)
        res = sf.order.query_delivery(
            self.sf_default_express_type, dest_address, src_address, searchPrice=1, weight=weight)
        _logger.debug(f"[Mommy SF]getting shipment rate result:{res},source:{src_address.to_dict()}, dest:{dest_address.to_dict()}")
        if res['success']:
            data = {
                "success": True,
                "price": res['msgData']['deliverTmDto'][0]['fee'],
                'error_message': False,
                'warning_message': False
            }
        else:
            data = {
                "success": False,
                "price": False,
                "error_message": res['errorMsg'],
                'warning_message': False
            }
        return data
