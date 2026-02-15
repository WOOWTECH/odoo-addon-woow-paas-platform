import logging
import traceback
from markdownify import markdownify
from markupsafe import Markup, escape
from odoo import models, api
from odoo.tools import ormcache
from odoo.addons.ai_base_gt.models.tools import after_commit

_logger = logging.getLogger(__name__)


class MailAI(models.AbstractModel):
    _name = 'mail.ai'
    _description = 'Mail AI'

    @ormcache('self.env.cr.dbname')
    def _get_ai_partner_ids(self):
        return self.env['res.partner'].sudo().with_context(active_test=False).search([
            ('is_ai', '=', True)
        ]).ids

    @api.model
    def _clear_ai_partner_cache(self):
        """Clear the AI partner IDs cache. Call this when AI partners change."""
        self._get_ai_partner_ids.clear_cache(self)

    def _get_ai_threads_by_partner_ids(self, record, ai_partner_ids):
        AIThread = self.env['ai.thread']
        Partner = self.env['res.partner']
        threads = AIThread.search([
            ('res_model', '=', record._name),
            ('res_id', '=', record.id),
            ('ai_partner_id', 'in', ai_partner_ids),
        ])
        if missing_ids := (set(ai_partner_ids) - set(threads.ai_partner_id.ids)):
            for missing_id in missing_ids:
                assistant = Partner.sudo().browse(missing_id).ai_assistant_ids[:1]
                threads |= AIThread.create({
                    'name': record.display_name,
                    'assistant_id': assistant.id,
                    'context_id': assistant.context_id.id,
                    'res_model': record._name,
                    'res_id': record.id,
                })
        return threads

    def _apply_logic(self, record, message, values):
        if len(record) != 1 or values.get("author_id") in self._get_ai_partner_ids() or values.get("message_type") != "comment":
            return
        if ai_partner_ids := (self._is_ai_pinged(values) or self._is_ai_in_private_channel(record)):
            prompt = markdownify(message.body.replace(u'\xa0', u' ').strip())
            ai_threads = self._get_ai_threads_by_partner_ids(record, ai_partner_ids)
            for thread in ai_threads:
                context = {
                    'mail_thread_model': record._name,
                    'mail_thread_id': record.id,
                    'mail_message_id': message.id,
                }
                prompt_message = thread.with_context(**context)._create_prompt_message(prompt, msg_vals=values)
                self.with_context(**context)._ai_request_after_commit(thread.id, prompt, prompt_message.id, values)

    @after_commit(wait=True)
    def _ai_request_after_commit(self, thread_id, prompt, prompt_message_id=None, values=None):
        thread = self.env['ai.thread'].browse(thread_id)
        values = values or {}
        try:
            thread._send_request(prompt, prompt_message_id, msg_vals=values)
        except Exception as e:
            _logger.error(traceback.format_exc())
            self.env.cr.rollback()
            # Escape error message to prevent XSS
            err_msg = escape(str(e.args[0] if e.args else str(e))).replace('\n', Markup('<br/>'))
            error_html = Markup('<div class="alert alert-danger" role="alert">') + err_msg + Markup('</div>')
            record = self.env[thread.res_model].browse(thread.res_id)
            default_message_type = 'comment'
            default_subtype_id = self.env['ir.model.data']._xmlid_to_res_id('mail.mt_comment')
            record.with_context(mail_create_nosubscribe=True).sudo().message_post(
                body=error_html,
                author_id=thread.ai_partner_id.id,
                message_type=values.get('message_type', default_message_type),
                subtype_id=values.get('subtype_id', default_subtype_id),
                partner_ids=self.env.user.partner_id.ids,
            )

    def _is_ai_pinged(self, values):
        ai_partner_ids = self._get_ai_partner_ids()
        return list(set(ai_partner_ids) & set(values.get('partner_ids', [])))

    def _is_ai_in_private_channel(self, record):
        ai_partner_ids = self._get_ai_partner_ids()
        if record._name == 'discuss.channel' and record.channel_type in ('chat', 'ai_chat'):
            return list(set(ai_partner_ids) & set(record.with_context(active_test=False).channel_partner_ids.ids))
        return False
