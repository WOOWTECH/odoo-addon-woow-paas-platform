from odoo import api, fields, models


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
    origin_code_id = fields.Many2one(
        comodel_name='woow_paas_platform.oauth_code',
        string='Origin Authorization Code',
        ondelete='set null',
        index=True,
        help='The authorization code this token lineage came from. Carried '
             'forward across refresh rotations so that a replay of that code '
             'can invalidate everything it produced (RFC 6749 10.5). '
             'set null, not cascade: deleting a spent code must never delete '
             'the audit row of a token that is still live.',
    )
    parent_token_id = fields.Many2one(
        comodel_name='woow_paas_platform.oauth_token',
        string='Rotated From',
        ondelete='set null',
        index=True,
        help='The token this one replaced during a refresh rotation. Lets a '
             'detected refresh-token reuse invalidate the whole rotation '
             'family (RFC 9700 4.14.2).',
    )

    def revoke(self) -> None:
        """Mark this token (and its refresh token) as revoked."""
        self.write({'is_revoked': True})

    def revoke_family(self):
        """Revoke these tokens and everything they were rotated into.

        Walks `parent_token_id` downwards, so a reused refresh token takes its
        successors with it instead of leaving the attacker's freshly rotated
        pair alive (RFC 9700 4.14.2). Returns the full revoked recordset.

        The walk is breadth-first over committed rows and guards against a
        cyclic chain (impossible via the controller, but a data fix could
        create one) by never re-visiting an id.
        """
        family = self.browse()
        frontier = self
        while frontier:
            family |= frontier
            frontier = self.search([
                ('parent_token_id', 'in', frontier.ids),
                ('id', 'not in', family.ids),
            ])
        family.revoke()
        return family

    def revoke_issued_from_code(self, code):
        """Revoke every token lineage issued from one authorization code.

        Called when that code is replayed: the authorization server cannot tell
        the legitimate client from the party that replayed it, so RFC 6749 10.5
        has it invalidate what the code already produced. Returns the revoked
        recordset (possibly empty).
        """
        if not code:
            return self.browse()
        roots = self.search([
            ('origin_code_id', '=', code.id),
            ('is_revoked', '=', False),
        ])
        return roots.revoke_family() if roots else self.browse()

    @api.model
    def is_issuance_identity_valid(self, client, user) -> bool:
        """Return whether a client may issue a token for this subject."""
        return bool(
            client
            and client.is_active
            and user
            and user.active
            and not user._is_public()
        )

    def _has_valid_issuance_identity(self) -> bool:
        self.ensure_one()
        return self.is_issuance_identity_valid(self.client_id, self.user_id)

    def is_access_token_valid(self) -> bool:
        """Check if the access token is still valid.

        Returns:
            True if the token is not revoked and not expired.
        """
        self.ensure_one()
        if self.is_revoked or not self._has_valid_issuance_identity():
            return False
        return fields.Datetime.now() < self.expires_at

    def is_refresh_token_valid(self) -> bool:
        """Check if the refresh token is still valid.

        Returns:
            True if not revoked and not expired.
        """
        self.ensure_one()
        if self.is_revoked or not self._has_valid_issuance_identity():
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
