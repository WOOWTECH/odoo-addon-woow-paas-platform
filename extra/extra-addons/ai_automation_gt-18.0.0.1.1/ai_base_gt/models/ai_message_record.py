import re
import json
from odoo import models, fields, api
from odoo.osv import expression
from odoo.tools import json_default

RECORD_TAG_PATTERN = r'\$([a-zA-Z_.]+)(?:/([0-9]+))?'


class AIMessageRecord(models.Model):
    _name = 'ai.message.record'
    _description = 'AI Message Record Reference'
    _rec_name = 'tag_name'

    message_id = fields.Many2one('ai.message', string="Message", required=True, index=True, ondelete='cascade')
    thread_id = fields.Many2one('ai.thread', string="Thread", related='message_id.thread_id', precompute=True,
                                store=True, index=True, ondelete='cascade')
    res_model = fields.Char(string="Resource Model", required=True, index=True)
    res_id = fields.Integer(string="Resource ID", index=True)
    tag_name = fields.Char(string="Tag Name", compute='_compute_tag_name', precompute=True, store=True)
    accessible = fields.Boolean(string="Accessible", compute='_compute_accessible', precompute=True, store=True)
    record_name = fields.Char(string="Record Name", compute='_compute_record_name', precompute=True, store=True)
    record_data = fields.Text(string="Record Data", compute='_compute_record_data', precompute=True, store=True,
                              help="Record data at time of tagging")

    _sql_constraints = [
        ('unique_message_record', 'unique (message_id, res_model, res_id)',
         'Record reference must be unique per message')
    ]

    @api.depends('res_model', 'res_id')
    def _compute_tag_name(self):
        for record in self:
            record.tag_name = f"{record.res_model}{f'/{record.res_id}' if record.res_id else ''}"

    @api.depends('res_model', 'res_id')
    def _compute_accessible(self):
        """
        Compute accessible using AI thread access methods
        """
        # Disable the sudo mode in computing a stored field
        self = self.sudo(False)

        # Group records by AI thread for batch processing
        by_thread = {}
        for record in self:
            thread = record.message_id.thread_id
            thread_id = thread.id
            if thread_id not in by_thread:
                by_thread[thread_id] = []
            by_thread[thread_id].append(record)

        # Process each thread group
        for thread_id, records in by_thread.items():
            thread = self.env['ai.thread'].browse(thread_id)

            # Group by model within this thread
            by_model = {}
            for record in records:
                model_name = record.res_model
                if model_name not in by_model:
                    by_model[model_name] = []
                by_model[model_name].append(record)

            # Process each model group
            for model_name, model_records in by_model.items():
                try:
                    # Use AI thread method to check model access
                    thread.assistant_id._check_model_fields_access(model_name)

                    # Get domain from AI thread
                    domain = thread.assistant_id._get_model_domain_access(model_name)

                    # Add record IDs to domain for batch check
                    record_ids = [r.res_id for r in model_records if r.res_id]
                    if record_ids:
                        domain = expression.AND([domain, [('id', 'in', record_ids)]])

                    # Get accessible records
                    accessible_records = self.env[model_name].search(domain)
                    accessible_ids = set(accessible_records.ids)

                    # Set computed values
                    for record in model_records:
                        record.accessible = not record.res_id or record.res_id in accessible_ids

                except Exception:
                    # Model access denied or other error
                    for record in model_records:
                        record.accessible = False

    @api.depends('res_model', 'res_id', 'accessible')
    def _compute_record_name(self):
        """
        Compute record_name with batch optimization
        Only compute for accessible records
        """
        # Disable the sudo mode in computing a stored field
        self = self.sudo(False)

        # Group records by model, but only for accessible ones
        by_model = {}
        for record in self:
            if not record.accessible:
                record.record_name = f"Access denied: {record.tag_name}"
                continue

            model_name = record.res_model
            if model_name not in by_model:
                by_model[model_name] = []
            by_model[model_name].append(record)

        # Load model names used for model tags
        model_names = self.env['ir.model'].sudo().search([('model', 'in', list(by_model.keys()))]).read(['model', 'name'])
        model_names_map = {model['model']: model['name'] for model in model_names}

        # Batch load display names for accessible records
        for model_name, records in by_model.items():
            try:
                Model = self.env[model_name]
                record_ids = [r.res_id for r in records if r.res_id]
                target_records = Model.browse(record_ids).exists()

                # Create mapping of id -> display_name
                name_mapping = {rec.id: rec.display_name for rec in target_records}

                # Set computed values
                for record in records:
                    if not record.res_id:
                        record.record_name = model_names_map.get(model_name, model_name)
                    elif record.res_id in name_mapping:
                        record.record_name = name_mapping[record.res_id]
                    else:
                        record.record_name = f"Not found: {record.tag_name}"

            except Exception:
                for record in records:
                    record.record_name = f"Error loading: {record.tag_name}"

    @api.depends('res_model', 'res_id', 'record_name', 'accessible')
    def _compute_record_data(self):
        # Disable the sudo mode in computing a stored field
        self = self.sudo(False)

        for record in self:
            ai_thread = record.message_id.thread_id
            if not record.accessible:
                record_data = {'model': record.res_model}
                if record.res_id:
                    record_data['id'] = record.res_id
                record_data['error'] = record.record_name
            else:
                try:
                    if not record.res_id:
                        record_data = ai_thread._get_model_fields_spec([record.res_model])[0]
                    else:
                        record_data = ai_thread._model_read(record.res_model, [record.res_id], [])[0]
                except Exception as e:
                    # Fallback if AI thread access fails; do not raise to avoid system crash
                    record_data = {'model': record.res_model}
                    if record.res_id:
                        record_data['id'] = record.res_id
                    record_data['error'] = str(e)

            record.record_data = json.dumps(record_data, ensure_ascii=False, indent=2, default=json_default)

    def _get_record_url(self):
        """Get the URL to access this record"""
        self.ensure_one()
        if not self.accessible:
            return ""
        base_url = self.get_base_url()
        if self.res_id:
            return f"{base_url}/web#id={self.res_id}&model={self.res_model}"
        return ""

    @api.model
    def _create_from_message_content(self, message, content):
        """
        Parse message content and create record references
        """
        # Pattern to match $model/id format
        matches = re.findall(RECORD_TAG_PATTERN, content)

        if not matches:
            return self.browse()

        vals_list = []
        appeared = set()
        for model_name, record_id in matches:
            record_id = int(record_id) if record_id else None
            if (model_name, record_id) in appeared:
                continue
            appeared.add((model_name, record_id))
            vals_list.append({
                'message_id': message.id,
                'res_model': model_name,
                'res_id': record_id,
            })

        return self.create(vals_list)
