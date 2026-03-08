import logging
from odoo import models, api
from odoo.tools.safe_eval import safe_eval

_logger = logging.getLogger(__name__)


class AIAssistant(models.Model):
    _inherit = 'ai.assistant'

    @api.model
    def get_assistants_for_record(self, res_model, res_id):
        """
        Get assistants that can access the record.
        """
        record = self.env[res_model].browse(res_id)
        assistants = self.browse()

        # Super assistants
        if super_assistants := self.search([('is_super_assistant', '=', True)]):
            assistants |= super_assistants

        # Assistants with data sources that can access the record
        for data_source in self.env['ai.data.source'].search([('model', '=', res_model)]):
            try:
                model_domain = data_source.model_domain and safe_eval(data_source.model_domain, {'env': self.env}) or []
            except Exception as e:
                _logger.warning("Invalid domain in data source %s: %s", data_source.id, e)
                model_domain = []
            if not model_domain or record.filtered_domain(model_domain):
                assistants |= data_source.assistant_ids

        result = []
        for assistant in assistants.sorted():
            result.append({
                'id': assistant.id,
                'name': assistant.name,
                'description': assistant.description,
                'partner_id': assistant.partner_id.id,
            })
        return result
