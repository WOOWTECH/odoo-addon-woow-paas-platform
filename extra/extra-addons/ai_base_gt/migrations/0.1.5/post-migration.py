from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    """
    Archive all active users of assistants
    """
    env = api.Environment(cr, SUPERUSER_ID, {})
    users = env['res.users'].search([('is_ai', '=', True), ('active', '=', True)])
    users.action_archive()
