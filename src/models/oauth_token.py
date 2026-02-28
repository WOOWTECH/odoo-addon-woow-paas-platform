from odoo import fields, models


class OAuthToken(models.Model):
    _name = 'woow_paas_platform.oauth_token'
    _description = 'OAuth 2.0 Token'
    _order = 'create_date desc'

    access_token = fields.Char(
        string='Access Token',
        required=True,
        index=True,
        help='Bearer access token',
    )
    refresh_token = fields.Char(
        string='Refresh Token',
        index=True,
        help='Token used to obtain a new access token',
    )
    token_type = fields.Char(
        string='Token Type',
        default='bearer',
        help='Type of token (always "bearer")',
    )
    scope = fields.Char(
        string='Scope',
        help='Space-separated list of granted scopes',
    )
    expires_at = fields.Datetime(
        string='Access Token Expires At',
        required=True,
        help='When the access token expires',
    )
    refresh_expires_at = fields.Datetime(
        string='Refresh Token Expires At',
        help='When the refresh token expires',
    )
    user_id = fields.Many2one(
        comodel_name='res.users',
        string='User',
        required=True,
        ondelete='cascade',
        index=True,
        help='The user who authorized this token',
    )
    client_id = fields.Many2one(
        comodel_name='woow_paas_platform.oauth_client',
        string='OAuth Client',
        required=True,
        ondelete='cascade',
        index=True,
        help='The client application this token was issued to',
    )
    is_revoked = fields.Boolean(
        string='Revoked',
        default=False,
        help='Whether this token has been revoked',
    )

    def revoke(self) -> None:
        """Mark this token (and its refresh token) as revoked."""
        self.write({'is_revoked': True})

    def is_access_token_valid(self) -> bool:
        """Check if the access token is still valid.

        Returns:
            True if the token is not revoked and not expired.
        """
        self.ensure_one()
        if self.is_revoked:
            return False
        return fields.Datetime.now() < self.expires_at

    def is_refresh_token_valid(self) -> bool:
        """Check if the refresh token is still valid.

        Returns:
            True if not revoked and not expired.
        """
        self.ensure_one()
        if self.is_revoked:
            return False
        if not self.refresh_token or not self.refresh_expires_at:
            return False
        return fields.Datetime.now() < self.refresh_expires_at

    def has_scope(self, required_scope: str) -> bool:
        """Check if this token grants a specific scope.

        Args:
            required_scope: The scope to check for.

        Returns:
            True if the scope is granted.
        """
        self.ensure_one()
        if not self.scope:
            return False
        granted = {s.strip() for s in self.scope.split() if s.strip()}
        return required_scope in granted

    def has_all_scopes(self, required_scopes: list[str]) -> bool:
        """Check if this token grants all specified scopes.

        Args:
            required_scopes: List of scopes to check.

        Returns:
            True if all scopes are granted.
        """
        self.ensure_one()
        if not self.scope:
            return not required_scopes
        granted = {s.strip() for s in self.scope.split() if s.strip()}
        return all(s in granted for s in required_scopes)
