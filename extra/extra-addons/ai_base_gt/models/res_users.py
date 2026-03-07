from odoo import models
from odoo.tools import ormcache


class ResUsers(models.Model):
    _inherit = 'res.users'

    @ormcache('self.id')
    def _get_type(self):
        self.ensure_one()
        if self.has_group('base.group_user'):
            return 'internal'
        elif self.has_group('base.group_portal'):
            return 'portal'
        return 'public'
