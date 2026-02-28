from odoo import models, fields, api, _
from odoo.tools import ormcache
from odoo.exceptions import UserError


class AIConfig(models.Model):
    _name = 'ai.config'
    _description = 'AI Configuration'

    active = fields.Boolean(string="Active", default=True)
    name = fields.Char(string="Name", required=True)
    type = fields.Selection([
        ('odooai', 'OdooAI'),
    ], string="Type", default="odooai", required=True)
    api_key = fields.Char(string="API Key", groups='base.group_system')
    model = fields.Char(string="Model", compute='_compute_model', store=True, readonly=False,
                        help="Name of the model to use.")
    temperature = fields.Float(string="Temperature", default=0.5,
                               help="Controls randomness in the output. Lower values make the "
                                    "output more focused and deterministic.")
    max_tokens = fields.Integer(string="Max Tokens", default=2000,
                                help="The maximum number of tokens to generate in the response.")
    allow_files = fields.Boolean(string="Allow Upload Files", compute='_compute_allow_files', store=True,
                                 help="Allow users to upload files when discussing with AI.")

    _sql_constraints = [
        (
            'check_temperature',
            'CHECK(temperature >= 0 AND temperature <= 2)',
            'Temperature must be between 0 and 2.'
        ),
        (
            'check_max_tokens',
            'CHECK(max_tokens > 0)',
            'Max Tokens must be a positive integer.'
        )
    ]

    @api.depends('type')
    def _compute_model(self):
        for r in self:
            r.model = r._get_default_model()

    def _get_default_model(self):
        self.ensure_one()
        return False

    @api.depends('type')
    def _compute_allow_files(self):
        for r in self:
            r.allow_files = r.type != 'odooai'

    @ormcache('self.type', 'self.api_key')
    def _get_ai_client(self):
        method = f'_get_{self.type}_client'
        if not hasattr(self, method):
            raise UserError(_("Method %s not found") % method)
        return getattr(self, method)()
