from odoo import models, api
from odoo.osv import expression


class ResPartner(models.Model):
    _inherit = 'res.partner'

    @api.model
    def search_for_channel_invite(self, search_term, channel_id=None, limit=30):
        # Override to add assistant partners with inactive users to the result
        result = super().search_for_channel_invite(search_term, channel_id, limit)
        result_ids = [partner['id'] for partner in result['partners']]
        inactive_assistants_domain = [
            '|',
            ('name', 'ilike', search_term),
            ('email', 'ilike', search_term),
            ('is_ai', '=', True),
            ('active', '=', True),
            ('user_ids', '!=', False),
            ('user_ids.active', '=', False),
        ]
        if channel_id:
            channel = self.env['discuss.channel'].search([('id', '=', int(channel_id))])
            inactive_assistants_domain = expression.AND([
                inactive_assistants_domain,
                [('channel_ids', 'not in', channel.id)]
            ])
            if channel.group_public_id:
                inactive_assistants_domain = expression.AND([
                    inactive_assistants_domain,
                    [('user_ids.groups_id', 'in', channel.group_public_id.id)]
                ])
        domain = expression.OR([
            inactive_assistants_domain,
            [('id', 'in', result_ids)],
        ])
        query = self.env['res.partner'].with_context(active_test=False)._search(domain, order='name, id')
        query.order = 'LOWER("res_partner"."name"), "res_partner"."id"'
        query.limit = int(limit)
        return {
            'count': self.env['res.partner'].search_count(domain),
            'partners': list(self.env['res.partner'].browse(query).mail_partner_format().values()),
        }

    @api.model
    def _search_mention_suggestions(self, domain, limit):
        # Override to add assistant partners with inactive users to the result
        active_domain = [('active', '=', True)]
        inactive_assistants_domain = [
            ('is_ai', '=', True),
            ('user_ids', '!=', False),
            ('user_ids.active', '=', False),
        ]
        new_domain = expression.AND([
            domain,
            expression.OR([
                active_domain,
                inactive_assistants_domain,
            ]),
        ])
        return super(ResPartner, self.with_context(active_test=False))._search_mention_suggestions(new_domain, limit)
