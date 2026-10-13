"""Server-only, per-invitation mailbox challenge before workspace grants.

Odoo remains responsible for account signup, activation and authentication.
This ledger proves possession of a particular delivered invitation; it does
not turn mutable billing email or an unverified login into a verified IdP.
"""
from datetime import timedelta
import hashlib
import re
import secrets
from urllib.parse import urlsplit

from odoo import _, api, fields, models, SUPERUSER_ID
from odoo.exceptions import UserError
from odoo.tools import escape_psql

from .workspace_access import ASSIGNABLE_ROLES, ROLE_ADMIN
from ..services.paas_capability import _PaaSCapability

INVITATION_TTL = timedelta(hours=24)
_EMAIL_RE = re.compile(r'^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$')
_TOKEN_RE = re.compile(r'^[A-Za-z0-9_-]{43}$')


class InvitationRejected(UserError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


class WorkspaceInvitation(models.Model):
    _name = 'woow_paas_platform.workspace_invitation'
    _description = 'Pending Workspace Mailbox Challenge'
    _order = 'create_date desc, id desc'

    workspace_id = fields.Many2one('woow_paas_platform.workspace', required=True, index=True, ondelete='cascade')
    recipient_user_id = fields.Many2one('res.users', required=True, index=True, ondelete='cascade')
    recipient_email = fields.Char(required=True, readonly=True)
    invited_by_id = fields.Many2one('res.users', required=True, ondelete='cascade')
    role = fields.Selection([(role, role.title()) for role in ASSIGNABLE_ROLES], required=True, readonly=True)
    token_digest = fields.Char(required=True, readonly=True, copy=False, index=True, groups='base.group_system')
    expires_at = fields.Datetime(required=True, readonly=True)
    state = fields.Selection([('pending', 'Pending'), ('accepted', 'Accepted'), ('revoked', 'Revoked')], required=True, default='pending', readonly=True)
    accepted_at = fields.Datetime(readonly=True)
    accepted_by_id = fields.Many2one('res.users', readonly=True, ondelete='set null')
    revoked_at = fields.Datetime(readonly=True)
    revoked_by_id = fields.Many2one('res.users', readonly=True, ondelete='set null')

    _sql_constraints = [('unique_workspace_invitation_digest', 'UNIQUE(token_digest)', 'Invitation token must be unique.')]

    @api.model
    def _lock_workspaces(self, workspace_ids):
        ids = sorted(set(workspace_ids))
        if ids:
            self.env.flush_all()
            self.env.cr.execute(
                'SELECT id FROM woow_paas_platform_workspace WHERE id = ANY(%s) ORDER BY id FOR UPDATE', [ids])
            self.env.invalidate_all()

    @api.model
    def _authorized_workspace(self, workspace_id):
        workspace = _PaaSCapability(self.env).workspace(workspace_id, ROLE_ADMIN)
        if not workspace or workspace.state != 'active':
            raise InvitationRejected('workspace_access_denied', _('Workspace not found or access denied'))
        return workspace

    @api.model
    def _issue(self, workspace_id, email, role):
        """Private server entry point: authenticate grantor before any sudo."""
        with self.env.cr.savepoint():
            self._authorized_workspace(workspace_id)
            self._lock_workspaces([workspace_id])
            workspace = self._authorized_workspace(workspace_id)
            email = email.strip().lower() if isinstance(email, str) else ''
            if not _EMAIL_RE.fullmatch(email):
                raise InvitationRejected('invite_email_invalid', _('A valid account login email is required'))
            role = role or 'user'
            if role not in ASSIGNABLE_ROLES:
                raise InvitationRejected('invite_role_invalid', _('Invalid role'))
            users = self.env['res.users'].with_context({}).sudo().search([
                ('login', '=ilike', escape_psql(email)), ('active', '=', True),
            ]).filtered(lambda user: not user._is_public())
            if not users:
                raise InvitationRejected('invite_identity_not_found', _('No active account found with this login email'))
            if len(users) != 1:
                raise InvitationRejected('invite_identity_ambiguous', _('This login email identifies multiple active accounts'))
            recipient = users
            if workspace.env['woow_paas_platform.workspace_access'].search_count([
                    ('workspace_id', '=', workspace.id), ('user_id', '=', recipient.id)]):
                raise InvitationRejected('invite_already_member', _('User is already a member of this workspace'))

            sender = self.env.user.company_id.email_formatted
            if not sender:
                raise InvitationRejected('invite_sender_unconfigured', _('The platform mail sender is not configured'))
            base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url', '')
            parsed_url = urlsplit(base_url)
            if parsed_url.scheme not in ('http', 'https') or not parsed_url.netloc:
                raise InvitationRejected('invite_url_unconfigured', _('The platform URL is not configured'))
            template = self.env.ref('woow_paas_platform.mail_template_workspace_invitation', raise_if_not_found=False)
            if not template or template._name != 'mail.template':
                raise InvitationRejected('invite_delivery_unconfigured', _('The invitation mail template is not configured'))

            ledger = self.with_context({}).sudo()
            ledger.search([('workspace_id', '=', workspace.id),
                           ('recipient_user_id', '=', recipient.id), ('state', '=', 'pending')])._mark_revoked()
            token = secrets.token_urlsafe(32)
            invitation = ledger.create({
                'workspace_id': workspace.id, 'recipient_user_id': recipient.id,
                'recipient_email': email, 'invited_by_id': self.env.uid,
                'role': role, 'token_digest': hashlib.sha256(token.encode('ascii')).hexdigest(),
                'expires_at': fields.Datetime.now() + INVITATION_TTL,
            })
            # The nonce lives only in this server context and the secured
            # outbound email. It is never stored in plaintext on the ledger,
            # returned by a product API or attached to recipient chatter.
            # A separate system principal creates the outbox/message: Odoo
            # lets message authors and creators read bodies even without
            # permission on the linked ledger model.
            accept_url = base_url.rstrip('/') + '/woow#/workspace-invitation/accept/' + token
            template.with_context({
                'lang': recipient.lang or 'en_US', 'workspace_invite_accept_url': accept_url,
            }).with_user(SUPERUSER_ID).sudo().send_mail(invitation.id, force_send=False, raise_exception=True, email_values={
                'email_to': email, 'email_cc': False, 'email_from': sender,
                'recipient_ids': [], 'partner_ids': [], 'auto_delete': True,
                'author_id': self.env['res.users'].sudo().browse(SUPERUSER_ID).partner_id.id,
                'message_type': 'user_notification',
            })
            # The native trigger and outbox commit together; worker wake-up is
            # post-commit, without SMTP here or edits to the cron's cadence.
            self.env.ref('mail.ir_cron_mail_scheduler_action')._trigger()
            return invitation

    def _dto(self):
        self.ensure_one()
        state = self.state
        if state == 'pending' and self.expires_at <= fields.Datetime.now():
            state = 'expired'
        return {
            'id': self.id, 'workspace_id': self.workspace_id.id,
            'user_id': self.recipient_user_id.id, 'name': self.recipient_user_id.name,
            'email': self.recipient_email, 'role': self.role, 'state': state,
            'expires_at': self.expires_at.isoformat(),
        }

    @api.model
    def _list_for_workspace(self, workspace_id):
        self._authorized_workspace(workspace_id)
        return [invitation._dto() for invitation in self.sudo().search([
            ('workspace_id', '=', workspace_id), ('state', '=', 'pending')])]

    def _mark_revoked(self):
        pending = self.filtered(lambda invitation: invitation.state == 'pending')
        if pending:
            pending.sudo().write({'state': 'revoked', 'revoked_at': fields.Datetime.now(), 'revoked_by_id': self.env.uid})

    @api.model
    def _revoke(self, workspace_id, invitation_id):
        with self.env.cr.savepoint():
            self._authorized_workspace(workspace_id)
            self._lock_workspaces([workspace_id])
            self._authorized_workspace(workspace_id)
            if not isinstance(invitation_id, int) or isinstance(invitation_id, bool):
                raise InvitationRejected('invite_not_found', _('Invitation not found'))
            invitation = self.sudo().search([
                ('id', '=', invitation_id), ('workspace_id', '=', workspace_id), ('state', '=', 'pending')], limit=1)
            if not invitation:
                raise InvitationRejected('invite_not_found', _('Invitation not found'))
            invitation._mark_revoked()
            return invitation._dto()

    @api.model
    def _accept(self, token):
        """Validate authenticated UID and a delivered single-use challenge."""
        invalid = lambda: InvitationRejected('invite_invalid', _('Invitation is invalid or no longer available'))
        user = self.env.user
        if not user.active or user._is_public() or not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
            raise invalid()
        digest = hashlib.sha256(token.encode('ascii')).hexdigest()
        with self.env.cr.savepoint():
            invitation = self.with_context({}).sudo().search([('token_digest', '=', digest)], limit=1)
            if not invitation:
                raise invalid()
            self._lock_workspaces([invitation.workspace_id.id])
            self.env.cr.execute('SELECT id FROM woow_paas_platform_workspace_invitation WHERE id=%s FOR UPDATE', [invitation.id])
            self.env.invalidate_all()
            # Keep identity changes/deactivation from racing the final checks.
            identity_ids = sorted({self.env.uid, invitation.invited_by_id.id})
            self.env.cr.execute('SELECT id FROM res_users WHERE id=ANY(%s) ORDER BY id FOR SHARE', [identity_ids])
            self.env.invalidate_all()
            if (invitation.state != 'pending' or invitation.expires_at <= fields.Datetime.now()
                    or invitation.recipient_user_id.id != self.env.uid
                    or not self.env.user.active or self.env.user._is_public()
                    or self.env.user.login.strip().lower() != invitation.recipient_email
                    or invitation.workspace_id.state != 'active'
                    or invitation.role not in ASSIGNABLE_ROLES):
                raise invalid()
            inviter = invitation.invited_by_id
            if not inviter.active or inviter._is_public():
                raise invalid()
            grant_env = self.env(user=inviter.id, su=False, context={})
            workspace = _PaaSCapability(grant_env).workspace(invitation.workspace_id.id, ROLE_ADMIN)
            if not workspace or workspace.state != 'active':
                raise invalid()
            access_model = workspace.env['woow_paas_platform.workspace_access']
            if access_model.search_count([('workspace_id', '=', workspace.id), ('user_id', '=', self.env.uid)]):
                raise invalid()
            access = access_model.create({
                'workspace_id': workspace.id, 'user_id': self.env.uid,
                'role': invitation.role, 'invited_by_id': inviter.id,
            })
            invitation.write({'state': 'accepted', 'accepted_at': fields.Datetime.now(), 'accepted_by_id': self.env.uid})
            return {'id': access.id, 'workspace_id': workspace.id, 'user_id': self.env.uid, 'role': access.role}


class WorkspaceAccessInvitationRevocation(models.Model):
    _inherit = 'woow_paas_platform.workspace_access'

    def write(self, vals):
        ledger = self.env['woow_paas_platform.workspace_invitation']
        snapshots = [(access.workspace_id.id, access.user_id.id) for access in self]
        if 'role' in vals:
            ledger._lock_workspaces([workspace_id for workspace_id, _uid in snapshots])
        result = super().write(vals)
        if 'role' in vals and vals['role'] not in ('owner', 'admin'):
            for workspace_id, uid in snapshots:
                ledger.sudo().search([('workspace_id', '=', workspace_id),
                                     ('invited_by_id', '=', uid), ('state', '=', 'pending')])._mark_revoked()
        return result

    def unlink(self):
        ledger = self.env['woow_paas_platform.workspace_invitation']
        snapshots = [(access.workspace_id.id, access.user_id.id) for access in self]
        ledger._lock_workspaces([workspace_id for workspace_id, _uid in snapshots])
        result = super().unlink()
        for workspace_id, uid in snapshots:
            ledger.sudo().search([('workspace_id', '=', workspace_id), ('state', '=', 'pending'),
                                 '|', ('recipient_user_id', '=', uid), ('invited_by_id', '=', uid)])._mark_revoked()
        return result


class WorkspaceInvitationArchiveRevocation(models.Model):
    _inherit = 'woow_paas_platform.workspace'

    def write(self, vals):
        ledger = self.env['woow_paas_platform.workspace_invitation']
        if 'state' in vals and vals['state'] != 'active':
            ledger._lock_workspaces(self.ids)
        result = super().write(vals)
        if 'state' in vals and vals['state'] != 'active':
            ledger.sudo().search([('workspace_id', 'in', self.ids), ('state', '=', 'pending')])._mark_revoked()
        return result


class ResUsersWorkspaceInvitationRevocation(models.Model):
    _inherit = 'res.users'

    def write(self, vals):
        # A changed/re-enabled login must not revive an old mailbox challenge.
        # Scope this hook to already pending PaaS invitations; ordinary billing
        # contact updates and upstream account signup behavior are preserved.
        relevant = set(vals) & {'login', 'active'}
        pending = self.env['woow_paas_platform.workspace_invitation'].browse()
        if relevant:
            self.check_access('write')
            changed = self.sudo().filtered(lambda user: any(
                vals[field] != user[field] for field in relevant))
            if changed:
                ledger = self.env['woow_paas_platform.workspace_invitation']
                pending = ledger.sudo().search([
                    ('state', '=', 'pending'), '|',
                    ('recipient_user_id', 'in', changed.ids), ('invited_by_id', 'in', changed.ids),
                ])
                # Use workspace -> identity ordering, matching acceptance, so
                # account updates cannot leave a grant/revocation race window.
                ledger._lock_workspaces(pending.mapped('workspace_id').ids)
        result = super().write(vals)
        pending._mark_revoked()
        return result
