import json
import logging
import os

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

CATALOG_PATH = '/all-addons/catalog.json'

# Map Odoo addon categories to our selection values
_CATEGORY_MAP = {
    'Accounting': 'accounting',
    'Accounting/Localizations': 'accounting',
    'CRM': 'crm',
    'Sales': 'sales',
    'Sales/Sales': 'sales',
    'eCommerce': 'ecommerce',
    'Human Resources': 'hr',
    'Human Resources/Recruitment': 'hr',
    'Inventory': 'inventory',
    'Inventory/Inventory': 'inventory',
    'Manufacturing': 'manufacturing',
    'Marketing': 'marketing',
    'Productivity': 'productivity',
    'Project': 'project',
    'Website': 'website',
    'Extra Tools': 'other',
    'Hidden': 'other',
}


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

    @api.model
    def sync_from_catalog(self, catalog_path=None):
        """Sync CloudOdooAddon records from addon-catalog.json.

        Reads the catalog file (baked into the Docker image at build time)
        and creates or updates addon records accordingly.

        Args:
            catalog_path: Override path for testing. Defaults to CATALOG_PATH.

        Returns:
            dict with created/updated/total counts.
        """
        path = catalog_path or CATALOG_PATH
        if not os.path.isfile(path):
            _logger.info("Addon catalog not found at %s, skipping sync", path)
            return {'created': 0, 'updated': 0, 'total': 0}

        with open(path) as f:
            catalog = json.load(f)

        created = 0
        updated = 0

        for technical_name, meta in catalog.items():
            existing = self.search([('technical_name', '=', technical_name)], limit=1)
            category = _CATEGORY_MAP.get(meta.get('category', ''), 'other')
            vals = {
                'name': meta.get('name', technical_name),
                'technical_name': technical_name,
                'description': meta.get('summary', ''),
                'category': category,
                'source_type': 'third_party',
            }

            if existing:
                existing.write(vals)
                updated += 1
            else:
                self.create(vals)
                created += 1

        # Deactivate addons not in catalog
        catalog_names = list(catalog.keys())
        stale = self.with_context(active_test=False).search([
            ('technical_name', 'not in', catalog_names),
        ])
        deactivated = 0
        if stale:
            stale.write({'active': False})
            deactivated = len(stale)

        # Resolve depends_on after all records exist
        for technical_name, meta in catalog.items():
            depends = meta.get('depends', [])
            if not depends:
                continue
            addon = self.search([('technical_name', '=', technical_name)], limit=1)
            if not addon:
                continue
            dep_records = self.search([('technical_name', 'in', depends)])
            if dep_records:
                addon.write({'depends_on': [(6, 0, dep_records.ids)]})

        total = self.search_count([])
        _logger.info(
            "Addon catalog sync: %d created, %d updated, %d deactivated, %d total",
            created, updated, deactivated, total,
        )
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Sync Complete',
                'message': f'{created} created, {updated} updated, {deactivated} deactivated ({total} total)',
                'type': 'success',
                'sticky': False,
            },
        }
