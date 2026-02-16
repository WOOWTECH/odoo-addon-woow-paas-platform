import mistune
import re
from markupsafe import Markup
from odoo import models, fields, api

from .ai_message_record import RECORD_TAG_PATTERN


class AIMessage(models.Model):
    _name = 'ai.message'
    _description = 'AI Thread Message'

    thread_id = fields.Many2one('ai.thread', string="Thread", required=True, index=True, ondelete='cascade')
    message_type = fields.Selection([
        ('system', 'System Prompt'),
        ('prompt', 'User Prompt'),
        ('response', 'AI Response'),
    ], string="Message Type", required=True)
    author_id = fields.Many2one('res.partner', string="Author", required=True)
    response = fields.Text(string="Raw Response", readonly=True)
    content = fields.Text(string="Text Content", readonly=True)
    content_html = fields.Html(string="HTML Content", compute='_compute_content_html', sanitize=False)
    message_record_ids = fields.One2many('ai.message.record', 'message_id', string="Record References")
    message_attachment_ids = fields.One2many('ai.message.attachment', 'message_id', string="Attachments")
    legit_attachment_ids = fields.Many2many('ai.message.attachment', string="Legit Attachments",
                                            compute='_compute_legit_attachment_ids', compute_sudo=True,
                                            help="Attachments that are legit to send to AI.")
    content_full = fields.Text(string="Full Content", compute='_compute_content_full',
                               help="Content including record references data context.")
    func_call = fields.Text(string="Function Call Data", readonly=True)
    func_result = fields.Text(string="Function Call Result", readonly=True)

    @api.depends('content')
    def _compute_content_html(self):
        for r in self:
            if r.content:
                content_html = Markup(mistune.html(r.content))
                r.content_html = r._replace_record_tag(content_html, r.message_record_ids)
            else:
                r.content_html = False

    @api.depends('content', 'message_record_ids')
    def _compute_content_full(self):
        for r in self:
            if not r.content or not r.message_record_ids:
                r.content_full = r.content
            else:
                record_data_context = ["--- Referenced models/records data at time of tagging ---"]
                for ref in r.message_record_ids:
                    record_data_context.append(f"{'Model' if not ref.res_id else 'Record'} ${ref.tag_name}:")
                    record_data_context.append(ref.record_data)
                record_data_context.append("--- End referenced models/records data ---")
                r.content_full = r.content + "\n\n" + "\n".join(record_data_context)

    @api.model
    def _replace_record_tag(self, content_html, message_record_ids):
        """Replace $model/id tags with HTML links"""
        if not content_html or not message_record_ids:
            return content_html

        ref_map = {(ref.res_model, ref.res_id): ref for ref in message_record_ids}

        def replace_tag(match):
            """Replace record tag with link to record"""
            full_tag = match.group()
            model, rec_id = match.groups()
            rec_id = int(rec_id) if rec_id else False
            ref = ref_map.get((model, rec_id))
            if not ref:
                return full_tag
            if ref and ref.accessible:
                url = ref._get_record_url()
                if url:
                    return f"<a href=\"{url}\" data-oe-model=\"{model}\" data-oe-id=\"{rec_id}\" target=\"_blank\" class=\"o_record_tag\">{ref.record_name}</a>"
                else:
                    return f"<a href=\"#\" class=\"o_record_tag\">{ref.record_name}</a>"
            else:
                return f"<a href=\"#\" class=\"o_record_tag text-danger\">{full_tag}</a>"

        if isinstance(content_html, str):
            content_html = re.sub(RECORD_TAG_PATTERN, replace_tag, content_html)
        return Markup(content_html) if isinstance(content_html, str) else content_html

    @api.depends('thread_id.assistant_id.allow_files', 'message_attachment_ids')
    def _compute_legit_attachment_ids(self):
        for r in self:
            if r.thread_id.assistant_id.allow_files:
                r.legit_attachment_ids = r._filter_legit_attachments()
            else:
                r.legit_attachment_ids = r.message_attachment_ids.browse()

    def _filter_legit_attachments(self):
        # By default, OdooAI does not allow to send files.
        return self.message_attachment_ids.browse()

    @api.model_create_multi
    def create(self, vals_list):
        messages = super().create(vals_list)
        messages._post_create_hook()
        self.env.cr.commit()
        return messages

    def _post_create_hook(self):
        """
        For other modules to override.
        """
        for message in self:
            message._parse_and_store_record_references()

    def _parse_and_store_record_references(self):
        """Parse $model/id patterns from message content and store as record references"""
        self.ensure_one()
        if not self.content:
            return self.env['ai.message.record']
        return self.env['ai.message.record']._create_from_message_content(self, self.content)
