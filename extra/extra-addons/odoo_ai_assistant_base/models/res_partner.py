from odoo import models, api
from odoo.osv import expression
from odoo.tools import SQL
from odoo.addons.mail.tools.discuss import Store


class ResPartner(models.Model):
    _inherit = 'res.partner'

    @api.readonly
    @api.model
    def search_for_channel_invite(self, search_term, channel_id=None, limit=30):
        # Override to add assistant partners with inactive users to the result
        # Build domain for regular active users
        domain = expression.AND([
            expression.OR([
                [('name', 'ilike', search_term)],
                [('email', 'ilike', search_term)],
            ]),
            [('active', '=', True)],
            [('user_ids', '!=', False)],
            [('user_ids.active', '=', True)],
            [('user_ids.share', '=', False)],
        ])
        # Build domain for AI assistants (which may have inactive users)
        inactive_assistants_domain = expression.AND([
            expression.OR([
                [('name', 'ilike', search_term)],
                [('email', 'ilike', search_term)],
            ]),
            [('is_ai', '=', True)],
            [('active', '=', True)],
            [('user_ids', '!=', False)],
            [('user_ids.active', '=', False)],
        ])
        channel = self.env['discuss.channel']
        if channel_id:
            channel = self.env['discuss.channel'].search([('id', '=', int(channel_id))])
            domain = expression.AND([domain, [('channel_ids', 'not in', channel.id)]])
            inactive_assistants_domain = expression.AND([
                inactive_assistants_domain,
                [('channel_ids', 'not in', channel.id)]
            ])
            if channel.group_public_id:
                domain = expression.AND([
                    domain,
                    [('user_ids.groups_id', 'in', channel.group_public_id.id)]
                ])
                inactive_assistants_domain = expression.AND([
                    inactive_assistants_domain,
                    [('user_ids.groups_id', 'in', channel.group_public_id.id)]
                ])
        # Combine domains: regular users OR AI assistants
        combined_domain = expression.OR([domain, inactive_assistants_domain])
        query = self.with_context(active_test=False)._search(combined_domain, limit=limit)
        query.order = SQL('LOWER(%s), "res_partner"."id"', self._field_to_sql(self._table, 'name'))
        store = Store()
        self.env['res.partner'].browse(query)._search_for_channel_invite_to_store(store, channel)
        return {
            'count': self.env['res.partner'].with_context(active_test=False).search_count(combined_domain),
            'data': store.get_result(),
        }

    @api.model
    def _search_mention_suggestions(self, domain, limit, extra_domain=None):
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
        return super(ResPartner, self.with_context(active_test=False))._search_mention_suggestions(new_domain, limit, extra_domain)
