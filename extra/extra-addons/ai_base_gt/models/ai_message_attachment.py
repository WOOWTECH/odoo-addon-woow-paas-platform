from odoo import models, fields


class AIMessageAttachment(models.Model):
    _name = 'ai.message.attachment'
    _description = 'AI Message Attachment'

    attachment_id = fields.Many2one('ir.attachment', string="Attachment", required=True, index=True, ondelete='cascade')
    name = fields.Char(string="Name", related='attachment_id.name', related_sudo=True)
    datas = fields.Binary(string="Data", related='attachment_id.datas', related_sudo=True)
    mimetype = fields.Char(string="MIME Type", related='attachment_id.mimetype', related_sudo=True)
    checksum = fields.Char(string="Checksum", related='attachment_id.checksum', related_sudo=True)
    message_id = fields.Many2one('ai.message', string="Message", required=True, index=True, ondelete='cascade')
    thread_id = fields.Many2one('ai.thread', string="Thread", related='message_id.thread_id', precompute=True,
                                store=True, index=True, ondelete='cascade')
