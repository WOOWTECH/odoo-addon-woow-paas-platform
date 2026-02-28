from odoo import fields, models


class OAuthCode(models.Model):
    _name = 'woow_paas_platform.oauth_code'
    _description = 'OAuth 2.0 Authorization Code'
    _order = 'create_date desc'

    code = fields.Char(
        string='Authorization Code',
        required=True,
        index=True,
        help='One-time-use authorization code',
    )
    redirect_uri = fields.Char(
        string='Redirect URI',
        required=True,
        help='The redirect URI used in the authorization request',
    )
    scope = fields.Char(
        string='Scope',
        help='Space-separated list of granted scopes',
    )
    expires_at = fields.Datetime(
        string='Expires At',
        required=True,
        help='Authorization code expiration time (10 minutes from creation)',
    )
    user_id = fields.Many2one(
        comodel_name='res.users',
        string='User',
        required=True,
        ondelete='cascade',
        index=True,
        help='The user who authorized this code',
    )
    client_id = fields.Many2one(
        comodel_name='woow_paas_platform.oauth_client',
        string='OAuth Client',
        required=True,
        ondelete='cascade',
        index=True,
        help='The client application this code was issued to',
    )
    is_used = fields.Boolean(
        string='Used',
        default=False,
        help='Whether this code has been exchanged for a token',
    )
    code_challenge = fields.Char(
        string='Code Challenge',
        help='PKCE code challenge (S256 or plain)',
    )
    code_challenge_method = fields.Char(
        string='Code Challenge Method',
        help='PKCE method: "S256" or "plain"',
    )

    def is_valid(self) -> bool:
        """Check if this authorization code is still valid.

        Returns:
            True if the code is not used and not expired.
        """
        self.ensure_one()
        if self.is_used:
            return False
        return fields.Datetime.now() < self.expires_at

    def mark_used(self) -> None:
        """Mark this authorization code as used (single-use)."""
        self.ensure_one()
        self.write({'is_used': True})
