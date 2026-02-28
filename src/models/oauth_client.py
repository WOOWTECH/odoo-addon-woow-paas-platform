import secrets

from odoo import api, fields, models
from werkzeug.security import check_password_hash, generate_password_hash


class OAuthClient(models.Model):
    _name = 'woow_paas_platform.oauth_client'
    _description = 'OAuth 2.0 Client Application'
    _order = 'create_date desc'

    name = fields.Char(
        string='Application Name',
        required=True,
        help='Display name of the OAuth client application',
    )
    client_id = fields.Char(
        string='Client ID',
        required=True,
        readonly=True,
        index=True,
        default=lambda self: secrets.token_urlsafe(32),
        help='Public identifier for the client application',
    )
    client_secret_hash = fields.Char(
        string='Client Secret (hashed)',
        help='Hashed client secret. The plain secret is only shown once at creation.',
    )
    redirect_uris = fields.Text(
        string='Redirect URIs',
        required=True,
        help='Allowed redirect URIs, one per line. '
             'Must match exactly during authorization.',
    )
    scopes = fields.Char(
        string='Allowed Scopes',
        default='smarthome:read smarthome:tunnel workspace:read',
        help='Space-separated list of scopes this client can request',
    )
    grant_types = fields.Char(
        string='Grant Types',
        default='authorization_code,refresh_token',
        help='Comma-separated list of allowed grant types: '
             'authorization_code, refresh_token, client_credentials',
    )
    is_active = fields.Boolean(
        string='Active',
        default=True,
        help='Whether this client is allowed to request tokens',
    )

    _sql_constraints = [
        (
            'unique_client_id',
            'UNIQUE(client_id)',
            'Client ID must be unique.',
        ),
    ]

    def set_secret(self, plain_secret: str) -> None:
        """Hash and store a client secret.

        Args:
            plain_secret: The plain-text secret to hash and store.
        """
        self.ensure_one()
        self.client_secret_hash = generate_password_hash(plain_secret)

    def verify_secret(self, plain_secret: str) -> bool:
        """Verify a plain-text secret against the stored hash.

        Args:
            plain_secret: The plain-text secret to verify.

        Returns:
            True if the secret matches, False otherwise.
        """
        self.ensure_one()
        if not self.client_secret_hash:
            return False
        return check_password_hash(self.client_secret_hash, plain_secret)

    def check_redirect_uri(self, uri: str) -> bool:
        """Check if a redirect URI is registered for this client.

        Args:
            uri: The redirect URI to validate (exact match).

        Returns:
            True if the URI is registered, False otherwise.
        """
        self.ensure_one()
        if not self.redirect_uris:
            return False
        registered = [
            u.strip() for u in self.redirect_uris.strip().splitlines() if u.strip()
        ]
        return uri in registered

    def check_grant_type(self, grant_type: str) -> bool:
        """Check if a grant type is allowed for this client.

        Args:
            grant_type: The grant type to check.

        Returns:
            True if allowed, False otherwise.
        """
        self.ensure_one()
        if not self.grant_types:
            return False
        allowed = [g.strip() for g in self.grant_types.split(',') if g.strip()]
        return grant_type in allowed

    def get_allowed_scopes(self) -> set[str]:
        """Return the set of scopes this client is allowed to request."""
        self.ensure_one()
        if not self.scopes:
            return set()
        return {s.strip() for s in self.scopes.split() if s.strip()}

    @api.model
    def generate_client_credentials(self) -> dict[str, str]:
        """Generate a new client_id and client_secret pair.

        Returns:
            dict with 'client_id' and 'client_secret' (plain text).
        """
        return {
            'client_id': secrets.token_urlsafe(32),
            'client_secret': secrets.token_urlsafe(48),
        }

    def action_regenerate_secret(self) -> dict:
        """Regenerate the client secret. Shows the new secret once.

        Returns:
            Action dict to display the new secret to the admin.
        """
        self.ensure_one()
        new_secret = secrets.token_urlsafe(48)
        self.set_secret(new_secret)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'New Client Secret Generated',
                'message': f'New secret (copy now, it will not be shown again): {new_secret}',
                'type': 'warning',
                'sticky': True,
            },
        }
