from odoo import fields, models


class ResPartnerInvoice(models.Model):
    _inherit = 'res.partner'

    woow_invoice_carrier_type = fields.Selection(
        [
            ('email', 'Email carrier'),
            ('mobile', 'Mobile barcode'),
            ('citizen', 'Citizen digital cert'),
            ('triplicate', 'Triplicate (B2B)'),
            ('paper', 'Paper'),
            ('donation', 'Donation'),
        ],
        default='email',
    )
    woow_invoice_carrier = fields.Char()
    woow_invoice_tax_id = fields.Char()
    woow_invoice_title = fields.Char()
    woow_invoice_lovecode = fields.Char()

    def _woow_partner_invoice_vals(self):
        """Map this partner's invoice preference to the field combination
        ecpay_invoice_tw expects on `account.move`.

        Mirrors `wallet_topup._map_carrier_to_ecpay_vals`. Used by the
        subscription cycle invoice creation path so the issuing cron has
        carrier info available before posting.
        """
        self.ensure_one()
        vals = {}
        ct = self.woow_invoice_carrier_type
        if ct == 'email':
            vals['carrierType'] = '1'
        elif ct == 'mobile':
            vals.update({
                'carrierType': '3',
                'input_carrier_num': self.woow_invoice_carrier or '',
            })
        elif ct == 'citizen':
            vals.update({
                'carrierType': '2',
                'input_carrier_num': self.woow_invoice_carrier or '',
            })
        elif ct == 'triplicate':
            vals.update({
                'ecpay_CustomerIdentifier': self.woow_invoice_tax_id or '',
                'ec_ident_name': self.woow_invoice_title or self.name or '',
            })
        elif ct == 'paper':
            vals['is_print'] = True
        elif ct == 'donation':
            vals.update({
                'is_donation': True,
                'lovecode': self.woow_invoice_lovecode or '',
            })
        return vals
