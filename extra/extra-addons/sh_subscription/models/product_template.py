# -*- coding: utf-8 -*-
# Part of Softhealer Technologies.

from odoo import api, fields, models
from odoo.exceptions import UserError

class ProductTemplate(models.Model):
    _inherit = "product.template"

    sh_product_subscribe = fields.Boolean(
        string="Is Subscription Type",
        help="Click false to hide.",
        compute="_compute_sh_product_subscribe",
        inverse="_set_sh_product_subscribe",
        store=True,
        search="_search_sh_product_subscribe",
    )
    sh_subscription_plan_id = fields.Many2one(
        comodel_name="sh.subscription.plan",
        string="Subscription Plan",
        compute="_compute_sh_subscription_plan_id",
        inverse="_set_sh_subscription_plan_id",
        store=True,
        search="_search_sh_subscription_plan_id",
    )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            # Check if product is subscription type and validate attributes
            if vals.get("sh_product_subscribe") and "attribute_line_ids" in vals:
                attribute_line_ids = vals["attribute_line_ids"]
                
                if len(attribute_line_ids) > 1:
                    raise UserError(
                        "You can't take more than one attribute in the subscription type product."
                    )

                subscription_attribute = self.env.ref("sh_subscription.product_attribute_subscription")
                if not any(att[2]["attribute_id"] == subscription_attribute.id for att in attribute_line_ids):
                    raise UserError(
                        "You must select the subscription attribute in the subscription type product."
                    )
        return super(ProductTemplate, self).create(vals_list)

    def write(self, vals):
        if vals.get("sh_product_subscribe") and "attribute_line_ids" in vals:
            attribute_line_ids = vals["attribute_line_ids"]

            if len(attribute_line_ids) > 1:
                raise UserError(
                    "You can't take more than one attribute in the subscription type product."
                )

            subscription_attribute = self.env.ref("sh_subscription.product_attribute_subscription")
            if not any(att[2]["attribute_id"] == subscription_attribute.id for att in attribute_line_ids):
                raise UserError(
                    "You must select the subscription attribute in the subscription type product."
                )

        return super(ProductTemplate, self).write(vals)

    @api.depends("product_variant_ids.sh_product_subscribe")
    def _compute_sh_product_subscribe(self):
        """Compute the subscription flag based on the first product variant."""
        for template in self:
            template.sh_product_subscribe = (
                len(template.product_variant_ids) == 1 and template.product_variant_ids.sh_product_subscribe
            )

    def _search_sh_product_subscribe(self, operator, value):
        """Search for templates based on the subscription flag."""
        templates = self.with_context(active_test=False).search(
            [("product_variant_ids.sh_product_subscribe", operator, value)]
        )
        return [("id", "in", templates.ids)]

    def _set_sh_product_subscribe(self):
        """Set the subscription flag on product variants."""
        if len(self.product_variant_ids) == 1:
            self.product_variant_ids.sh_product_subscribe = self.sh_product_subscribe

    @api.depends("product_variant_ids.sh_subscription_plan_id")
    def _compute_sh_subscription_plan_id(self):
        """Compute the subscription plan based on the first product variant."""
        for template in self:
            template.sh_subscription_plan_id = (
                len(template.product_variant_ids) == 1 and template.product_variant_ids.sh_subscription_plan_id.id
            )

    def _search_sh_subscription_plan_id(self, operator, value):
        """Search for templates based on the subscription plan."""
        templates = self.with_context(active_test=False).search(
            [("product_variant_ids.sh_subscription_plan_id", operator, value)]
        )
        return [("id", "in", templates.ids)]

    def _set_sh_subscription_plan_id(self):
        """Set the subscription plan on product variants."""
        if len(self.product_variant_ids) == 1:
            self.product_variant_ids.sh_subscription_plan_id = self.sh_subscription_plan_id.id

    @api.onchange("sh_product_subscribe")
    def _onchange_sh_product_subscribe(self):
        """Set product type to 'service' if it is a subscription."""
        if self.sh_product_subscribe:
            self.type = "service"

    @api.onchange("sh_subscription_plan_id")
    def _onchange_sh_subscription_plan_id_product(self):
        """Update the product price if the subscription plan overrides the product price."""
        if self.sh_subscription_plan_id and self.sh_subscription_plan_id.sh_override_product:
            self.list_price = self.sh_subscription_plan_id.sh_plan_price


class Product(models.Model):
    _inherit = "product.product"

    sh_product_subscribe = fields.Boolean(
        string="Is Subscription Type", help="Click false to hide."
    )
    sh_subscription_plan_id = fields.Many2one(
        comodel_name="sh.subscription.plan", string="Subscription Plan"
    )

    @api.onchange("sh_product_subscribe")
    def _onchange_sh_product_subscribe(self):
        """Set product type to 'service' if it is a subscription."""
        if self.sh_product_subscribe:
            self.type = "service"

    @api.onchange("sh_subscription_plan_id")
    def _onchange_sh_subscription_plan_id_product(self):
        """Update the price extra of subscription attribute based on the selected plan."""
        if self.sh_subscription_plan_id and self.sh_subscription_plan_id.sh_override_product:
            subscription_attribute = self.env.ref("sh_subscription.product_attribute_subscription")
            if subscription_attribute:
                variant_value = self.product_template_variant_value_ids.filtered(
                    lambda x: x.attribute_id == subscription_attribute
                )
                if variant_value:
                    origin = getattr(variant_value[0], '_origin', variant_value[0])
                    origin.price_extra = self.sh_subscription_plan_id.sh_plan_price

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            template_id = self.env["product.template"].sudo().browse(vals.get("product_tmpl_id"))
            if template_id:
                vals.update(
                    {
                        "sh_product_subscribe": template_id.sh_product_subscribe,
                        "sh_subscription_plan_id": template_id.sh_subscription_plan_id.id,
                    }
                )
        return super(Product, self).create(vals_list)
