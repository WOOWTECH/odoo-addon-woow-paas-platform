from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class SecurityAccessTemplate(models.Model):
    """Catalog entry holding the subscription-plan whitelist for Security Access.

    Mirrors smart_home_template / cloud_app_template.subscription_plan_ids.
    Separate relation table so a plan can never appear under two service
    types (see _check_no_cross_service_type_plans — invoice line text is
    derived from the plan's product, so sharing would mislabel invoices).
    """

    _name = 'woow_paas_platform.security_access_template'
    _description = 'Security Access Template'
    _order = 'name'

    name = fields.Char(
        string='Name',
        required=True,
        help='Template display name (e.g., Security Access)',
    )
    slug = fields.Char(
        string='Slug',
        index=True,
        help='URL-safe identifier for the template',
    )
    icon = fields.Binary(string='Icon')
    description = fields.Char(
        string='Description',
        help='Short description (~100 characters)',
    )
    full_description = fields.Text(
        string='Full Description',
        help='Detailed description with features and use cases',
    )
    subscription_plan_ids = fields.Many2many(
        'sh.subscription.plan',
        'woow_security_access_template_plan_rel',
        'template_id',
        'plan_id',
        string='Subscription Plans',
        help='Available subscription plans for Security Access instances '
             'created from this template',
    )
    is_active = fields.Boolean(
        string='Active',
        default=True,
        help='Whether this template is offered to end users',
    )

    @api.model
    def _ensure_default_template_links(self):
        """Heal the seed template's plan links on upgraded databases.

        Called by a ``<function>`` element in subscription_template_links.xml
        (function elements run on every -u regardless of noupdate). Why this
        exists: ``security_access_default`` was historically seeded with
        noupdate=1 — the ir.model.data noupdate flag is *sticky*, so on DBs
        that got the record under that flag, every later record-level update
        in -u (including the links file's m2m write) is silently skipped and
        the template ends up plan-less. Fresh installs never hit this.

        Idempotent and admin-respecting: flips the sticky noupdate flag to
        False (aligning with the smart_home/cloud_app noupdate=0 convention)
        and only fills in the four default plans when the template currently
        has none — an admin-curated plan list is never overwritten.
        """
        template = self.env.ref(
            'woow_paas_platform.security_access_default',
            raise_if_not_found=False,
        )
        if not template:
            return
        imd = self.env['ir.model.data'].sudo().search([
            ('module', '=', 'woow_paas_platform'),
            ('name', '=', 'security_access_default'),
            ('model', '=', self._name),
        ], limit=1)
        if imd and imd.noupdate:
            imd.noupdate = False
        if template.subscription_plan_ids:
            return
        plans = self.env['sh.subscription.plan']
        for xml_id in (
            'woow_paas_platform.plan_security_access_r1_monthly',
            'woow_paas_platform.plan_security_access_r1_yearly',
            'woow_paas_platform.plan_security_access_r3_monthly',
            'woow_paas_platform.plan_security_access_r3_yearly',
            'woow_paas_platform.plan_security_access_entry_monthly',
            'woow_paas_platform.plan_security_access_entry_yearly',
            'woow_paas_platform.plan_security_access_pro_monthly',
            'woow_paas_platform.plan_security_access_pro_yearly',
        ):
            plan = self.env.ref(xml_id, raise_if_not_found=False)
            if plan:
                plans |= plan
        if plans:
            template.subscription_plan_ids = [(6, 0, plans.ids)]

    @api.constrains('subscription_plan_ids')
    def _check_no_cross_service_type_plans(self):
        """A plan must belong to one service type only (three-way lockstep).

        Symmetric checks live on cloud_app_template and smart_home_template;
        all three fire on write so the conflict is caught regardless of
        which side is edited last.
        """
        if not self:
            return
        for record in self:
            if not record.subscription_plan_ids:
                continue
            for other_model, label in [
                ('woow_paas_platform.cloud_app_template', 'Cloud App Template'),
                ('woow_paas_platform.smart_home_template', 'Smart Home Template'),
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
