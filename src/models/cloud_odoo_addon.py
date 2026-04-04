from odoo import fields, models


class CloudOdooAddon(models.Model):
    _name = 'woow_paas_platform.cloud_odoo_addon'
    _description = 'Cloud Odoo Addon'
    _order = 'category, name'

    name = fields.Char(
        string='Name',
        required=True,
        help='Display name (e.g., Subscription Management)',
    )
    technical_name = fields.Char(
        string='Technical Name',
        required=True,
        index=True,
        help='Odoo module technical name (e.g., sh_subscription)',
    )
    description = fields.Text(
        string='Description',
        help='Short description of the addon',
    )
    category = fields.Selection(
        selection=[
            ('accounting', 'Accounting'),
            ('crm', 'CRM'),
            ('ecommerce', 'eCommerce'),
            ('hr', 'Human Resources'),
            ('inventory', 'Inventory'),
            ('manufacturing', 'Manufacturing'),
            ('marketing', 'Marketing'),
            ('productivity', 'Productivity'),
            ('project', 'Project'),
            ('sales', 'Sales'),
            ('subscription', 'Subscription'),
            ('website', 'Website'),
            ('other', 'Other'),
        ],
        string='Category',
        default='other',
    )
    icon = fields.Char(
        string='Icon',
        help='Material Symbols icon name',
    )
    source_type = fields.Selection(
        selection=[
            ('built_in', 'Built-in (Odoo Core)'),
            ('third_party', 'Third-party'),
            ('custom', 'Custom'),
        ],
        string='Source Type',
        required=True,
        default='third_party',
        help='built_in: already in Odoo image, no initContainer copy needed. '
             'third_party/custom: stored in /all-addons/, requires initContainer copy.',
    )
    depends_on = fields.Many2many(
        comodel_name='woow_paas_platform.cloud_odoo_addon',
        relation='cloud_odoo_addon_dep_rel',
        column1='addon_id',
        column2='dependency_id',
        string='Dependencies',
        help='Other addons this addon depends on',
    )
    template_ids = fields.Many2many(
        comodel_name='woow_paas_platform.cloud_app_template',
        relation='cloud_odoo_addon_template_rel',
        column1='addon_id',
        column2='template_id',
        string='Compatible Templates',
        help='Application templates this addon is available for',
    )
    is_default = fields.Boolean(
        string='Default',
        default=False,
        help='Automatically selected when creating a new service',
    )
    active = fields.Boolean(
        string='Active',
        default=True,
    )
