# -*- coding: utf-8 -*-
# Part of Softhealer Technologies.

from odoo import fields, models
import logging

_logger = logging.getLogger(__name__)

class SubscriptionProductWizard(models.TransientModel):
    _name = 'sh.subscription.product.wizard'
    _description = 'Subscription Product Wizard'

    # Define a Many2one field to select a product that is allowed for subscription
    sh_product_id = fields.Many2one(
        comodel_name="product.product", 
        string="Product", 
        required=True, 
        domain="[('sale_ok', '=', True), ('sh_product_subscribe', '=', True)]"
    )

    def sh_add_product(self):
        """Add selected subscription product to the active Sale Order."""
        try:
            so_id = self.env['sale.order'].sudo().browse(self.env.context.get('active_id'))
            if not so_id:
                return  # Exit if no Sale Order is found

            # Prepare the line values to add the product to the sale order
            line_vals = {
                'product_id': self.sh_product_id.id,
                'name': self.sh_product_id.display_name,
                'price_unit': self.sh_product_id.lst_price,
                'product_uom_qty': 1.0,
            }
            
            # Directly assign the line values to the sale order
            so_id.order_line = [(0, 0, line_vals)]

        except Exception as e:
            # Handle any exceptions that occur during the process
            _logger.error(f"Error adding subscription product: {e}")
            # Optionally, add more specific error handling if necessary
