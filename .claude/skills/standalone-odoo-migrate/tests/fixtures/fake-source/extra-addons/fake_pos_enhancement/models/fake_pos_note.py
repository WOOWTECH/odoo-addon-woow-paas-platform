from Cryptodome.Hash import SHA256  # pycryptodomex：harumi 主容器 command 執行時 pip install 的那一個

from odoo import api, fields, models


class FakePosNote(models.Model):
    _name = 'fake.pos.note'
    _description = 'Fake POS note（測試資料一致性用）'

    name = fields.Char(required=True)
    body_md = fields.Text()
    body_digest = fields.Char(compute='_compute_body_digest', store=True)
    amount = fields.Float(digits=(16, 4))
    partner_id = fields.Many2one('res.partner')
    attachment_ids = fields.Many2many('ir.attachment')

    @api.depends('body_md')
    def _compute_body_digest(self):
        for rec in self:
            rec.body_digest = SHA256.new((rec.body_md or '').encode()).hexdigest()
