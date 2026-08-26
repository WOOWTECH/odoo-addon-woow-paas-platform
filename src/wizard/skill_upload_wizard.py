import base64
import hashlib

from odoo import _, fields, models
from odoo.exceptions import UserError

from .skill_bundle_validation import validate_skill_bundle


class SkillUploadWizard(models.TransientModel):
    _name = 'woow_paas_platform.skill_upload_wizard'
    _description = 'Upload Skill Bundle'

    MAX_BUNDLE_SIZE = 10 * 1024 * 1024  # 10 MB

    name = fields.Char(
        string='Skill Name',
        required=True,
        help='Slug; must match the name in the bundle SKILL.md.',
    )
    version = fields.Char(string='Version', required=True, default='0.1.0')
    title = fields.Char(string='Display Title')
    description = fields.Text(string='Description')
    # default 'system' keeps the Configuration-menu entry behaviour; the
    # project-form button overrides via default_scope/default_project_id
    # context (spec §2.2).
    scope = fields.Selection(
        selection=[('system', 'System'), ('user', 'User')],
        string='Scope',
        required=True,
        default='system',
    )
    project_id = fields.Many2one(
        'project.project',
        string='Project',
        help='Required for user-scope skills; must stay empty for system.',
    )
    bundle_file = fields.Binary(string='Skill Bundle (.tar.gz)', required=True)
    bundle_filename = fields.Char(string='Filename')

    def action_upload(self):
        """Store the uploaded bundle bytes as a draft skill (system or
        user scope). Validate the tar structure up front (fail-fast) so a
        broken bundle is rejected at upload time instead of surfacing as a
        cryptic tool_error on the pod's first invoke. sha256 is still
        computed over the RAW bytes (pod fetch contract is byte-exact).

        Validation rules mirror the paas-agent-runtime Claude Code skill bundle
        contract (SKILL.md + optional files, no required entrypoint); see
        ``skill_bundle_validation``.
        """
        self.ensure_one()
        if self.scope == 'user' and not self.project_id:
            raise UserError(_("User-scope skills must be linked to a project."))
        if self.scope == 'system' and self.project_id:
            raise UserError(
                _("System-scope skills must not be linked to a project."))
        raw = base64.b64decode(self.bundle_file)
        if len(raw) > self.MAX_BUNDLE_SIZE:
            raise UserError(_("Bundle exceeds the 10 MB limit."))

        # Fail-fast structural validation BEFORE creating any attachment /
        # skill row, so a broken bundle never leaves an orphan attachment.
        # Hard violations are reported all at once (not just the first).
        validation = validate_skill_bundle(raw, wizard_name=self.name)
        if not validation.ok:
            raise UserError(_(
                "This skill bundle was rejected:\n- %(errors)s",
                errors="\n- ".join(validation.errors),
            ))

        bundle_sha256 = hashlib.sha256(raw).hexdigest()
        bundle_size_kb = -(-len(raw) // 1024)  # ceil division

        Skill = self.env['woow_paas_platform.skill'].sudo()
        # Per-scope dedupe mirrors the two partial unique indexes
        # (skill.py init): system → (name, version) WHERE project NULL;
        # user → (project_id, name, version).
        dedupe_domain = [
            ('name', '=', self.name),
            ('version', '=', self.version),
            ('project_id', '=',
             self.project_id.id if self.scope == 'user' else False),
        ]
        if Skill.search(dedupe_domain, limit=1):
            raise UserError(_(
                "%(scope)s skill '%(name)s' v%(version)s already exists; "
                "bump the version.",
                scope=self.scope.capitalize(),
                name=self.name,
                version=self.version,
            ))

        attachment = self.env['ir.attachment'].sudo().create({
            'name': self.bundle_filename or '%s-%s.tar.gz' % (
                self.name, self.version),
            'datas': self.bundle_file,
            'mimetype': 'application/gzip',
        })
        skill = Skill.create({
            'scope': self.scope,
            'project_id': self.project_id.id if self.scope == 'user' else False,
            'state': 'draft',
            'name': self.name,
            'version': self.version,
            'title': self.title or self.name,
            'description': self.description or '',
            'bundle_sha256': bundle_sha256,
            'bundle_size_kb': bundle_size_kb,
            'bundle_attachment_id': attachment.id,
        })
        act_window = {
            'type': 'ir.actions.act_window',
            'res_model': 'woow_paas_platform.skill',
            'res_id': skill.id,
            'view_mode': 'form',
            'target': 'current',
        }
        # Soft warnings: the skill is still created, but surface a sticky
        # notification so the uploader can fix the bundle before publishing.
        if validation.warnings:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': "Skill uploaded with warnings",
                    'message': "\n".join("• %s" % w
                                         for w in validation.warnings),
                    'type': 'warning',
                    'sticky': True,
                    'next': act_window,
                },
            }
        return act_window
