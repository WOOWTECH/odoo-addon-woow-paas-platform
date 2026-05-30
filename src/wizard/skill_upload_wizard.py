import base64
import hashlib

from odoo import fields, models
from odoo.exceptions import UserError


class SkillUploadWizard(models.TransientModel):
    _name = 'woow_paas_platform.skill_upload_wizard'
    _description = 'Upload Skill Bundle (system scope)'

    MAX_BUNDLE_SIZE = 10 * 1024 * 1024  # 10 MB

    name = fields.Char(
        string='Skill Name',
        required=True,
        help='Slug; must match the name in the bundle SKILL.md.',
    )
    version = fields.Char(string='Version', required=True, default='0.1.0')
    title = fields.Char(string='Display Title')
    description = fields.Text(string='Description')
    bundle_file = fields.Binary(string='Skill Bundle (.tar.gz)', required=True)
    bundle_filename = fields.Char(string='Filename')

    def action_upload(self):
        """Store the uploaded bundle bytes as a draft system skill.

        Odoo never parses the bundle: it decodes the binary, enforces a size
        cap, computes sha256 over the RAW bytes (the digest the pod verifies),
        and persists an anchored attachment + a draft scope='system' skill.
        """
        self.ensure_one()
        raw = base64.b64decode(self.bundle_file)
        if len(raw) > self.MAX_BUNDLE_SIZE:
            raise UserError("Bundle exceeds the 10 MB limit.")

        bundle_sha256 = hashlib.sha256(raw).hexdigest()
        bundle_size_kb = -(-len(raw) // 1024)  # ceil division

        Skill = self.env['woow_paas_platform.skill'].sudo()
        if Skill.search([
            ('scope', '=', 'system'),
            ('name', '=', self.name),
            ('version', '=', self.version),
        ], limit=1):
            raise UserError(
                "System skill '%s' v%s already exists; bump the version."
                % (self.name, self.version)
            )

        attachment = self.env['ir.attachment'].sudo().create({
            'name': self.bundle_filename or '%s-%s.tar.gz' % (
                self.name, self.version),
            'datas': self.bundle_file,
            'mimetype': 'application/gzip',
        })
        skill = Skill.create({
            'scope': 'system',
            'project_id': False,
            'state': 'draft',
            'name': self.name,
            'version': self.version,
            'title': self.title or self.name,
            'description': self.description or '',
            'bundle_sha256': bundle_sha256,
            'bundle_size_kb': bundle_size_kb,
            'bundle_attachment_id': attachment.id,
        })
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'woow_paas_platform.skill',
            'res_id': skill.id,
            'view_mode': 'form',
            'target': 'current',
        }
