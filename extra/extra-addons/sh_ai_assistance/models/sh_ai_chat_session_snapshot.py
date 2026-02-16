# -*- coding: utf-8 -*-
from odoo import fields, models
import uuid

class ShAiChatSessionSnapshot(models.Model):
    _name = 'sh.ai.chat.session.snapshot'
    _description = 'AI Chat Session Snapshot (Filestore)'

    snapshot_uuid = fields.Char(
        string="Snapshot UUID", required=True, index=True,
        default=lambda self: str(uuid.uuid4()), copy=False, readonly=True
    )
    
    # Stored in filestore
    messages_json = fields.Binary(
        string="Messages JSON", readonly=True
    )
    
    name = fields.Char(string="Session Title", required=True, readonly=True)
    
    original_session_id = fields.Many2one(
        'sh.ai.chat.session', string="Original Session",
        on_delete='set null', readonly=True
    )
    
    create_date = fields.Datetime(string="Snapshot Date", readonly=True, default=fields.Datetime.now)
