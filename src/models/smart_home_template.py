from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class SmartHomeTemplate(models.Model):
    """Catalog entry that holds the subscription-plan whitelist for a Smart Home variant.

    Mirrors `cloud_app_template.subscription_plan_ids` so that the Smart Home
    flow discovers plans through a structured M2M relation instead of matching
    plan names with a string prefix. The previous `Plan.search([("name","ilike",
    "Smart Home%")])` path was both fragile and ACL-blocked for portal users —
    see `controllers/smart_home.py:api_smarthome_plans`.
    """

    _name = 'woow_paas_platform.smart_home_template'
    _description = 'Smart Home Template'
    _order = 'name'

    name = fields.Char(
        string='Name',
        required=True,
        help='Template display name (e.g., Home Assistant)',
    )
    slug = fields.Char(
        string='Slug',
        index=True,
        help='URL-safe identifier for the template',
    )
    icon = fields.Binary(
        string='Icon',
    )
    description = fields.Char(
        string='Description',
        help='Short description (~100 characters)',
    )
    full_description = fields.Text(
        string='Full Description',
        help='Detailed description with features and use cases',
    )

    # Subscription plans available for this template.
    # Same M2M shape as cloud_app_template.subscription_plan_ids; relation table
    # is intentionally separate so a plan can never accidentally appear under
    # both service types (see _check_no_cross_service_type_plans).
    subscription_plan_ids = fields.Many2many(
        'sh.subscription.plan',
        'woow_smart_home_template_plan_rel',
        'template_id',
        'plan_id',
        string='Subscription Plans',
        help='Available subscription plans for Smart Homes created from this template',
    )

    is_active = fields.Boolean(
        string='Active',
        default=True,
        help='Whether this template is offered to end users when creating a Smart Home',
    )

    @api.constrains('subscription_plan_ids')
    def _check_no_cross_service_type_plans(self):
        """A plan must belong to one service type only.

        Each plan binds to a single product (sh_subscription_plan_id on
        product.template), and that product's name is what shows up on the
        invoice line. Sharing a plan across service types would produce
        invoices whose product name contradicts the actual service the
        customer is using — an accounting trap, not a feature.

        Three-way symmetric check with cloud_app_template and
        security_access_template; all fire on write so the conflict is
        caught regardless of which side is edited last.
        """
        if not self:
            return
        for record in self:
            if not record.subscription_plan_ids:
                continue
            for other_model, label in [
                ('woow_paas_platform.cloud_app_template', 'Cloud App Template'),
                ('woow_paas_platform.security_access_template',
                 'Security Access Template'),
            ]:
                conflicts = self.env[other_model].search([
                    ('subscription_plan_ids', 'in',
                     record.subscription_plan_ids.ids),
                ])
                if conflicts:
                    shared = (record.subscription_plan_ids
                              & conflicts.subscription_plan_ids)
                    raise ValidationError(
                        _("Plan(s) %s are already linked to %s(s) %s. "
                          "A subscription plan must belong to a single service "
                          "type — its product determines the invoice line text.")
                        % (
                            ', '.join(shared.mapped('name')),
                            label,
                            ', '.join(conflicts.mapped('name')),
                        )
                    )
