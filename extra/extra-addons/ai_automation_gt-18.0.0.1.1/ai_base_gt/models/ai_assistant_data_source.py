from odoo import api, fields, models, _


class AIAssistantDataSource(models.Model):
    _name = 'ai.assistant.data.source'
    _description = 'AI Assistant Data Source'
    _order = 'sequence, id'

    sequence = fields.Integer(string="Sequence", default=10)
    assistant_id = fields.Many2one(
        'ai.assistant',
        string="Assistant",
        required=True,
        ondelete='cascade',
        index=True,
    )
    data_source_id = fields.Many2one(
        'ai.data.source',
        string="Data Source",
        required=True,
        ondelete='cascade',
        index=True,
    )
    data_source_type = fields.Selection(related='data_source_id.type')
    allow_read = fields.Boolean(
        string="Read",
        compute='_compute_allow_crud',
        store=True,
        precompute=True,
        readonly=False,
        help="Allow the assistant to read records from the data source.",
    )
    allow_create = fields.Boolean(
        string="Create",
        compute='_compute_allow_crud',
        store=True,
        precompute=True,
        readonly=False,
        help="Allow the assistant to create records in the data source.",
    )
    allow_write = fields.Boolean(
        string="Write",
        compute='_compute_allow_crud',
        store=True,
        precompute=True,
        readonly=False,
        help="Allow the assistant to update records in the data source.",
    )
    allow_unlink = fields.Boolean(
        string="Delete",
        compute='_compute_allow_crud',
        store=True,
        precompute=True,
        readonly=False,
        help="Allow the assistant to delete records in the data source.",
    )

    _sql_constraints = [
        (
            'assistant_data_source_uniq',
            'unique(assistant_id, data_source_id)',
            'This data source is already linked to this assistant.',
        ),
    ]

    @api.depends('data_source_id', 'data_source_id.type')
    def _compute_allow_crud(self):
        for rec in self:
            if rec.data_source_id.type != 'model':
                rec.allow_read = True
                rec.allow_create = False
                rec.allow_write = False
                rec.allow_unlink = False
            else:
                rec.allow_read = True
                rec.allow_create = True
                rec.allow_write = True
                rec.allow_unlink = True
