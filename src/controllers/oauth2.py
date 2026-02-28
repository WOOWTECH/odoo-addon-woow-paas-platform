from __future__ import annotations

import hashlib
import json
import logging
import secrets
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode, urlparse, urlunparse, parse_qs

from odoo import http
from odoo.http import request, route, Controller

_logger = logging.getLogger(__name__)

# Token lifetimes
ACCESS_TOKEN_LIFETIME = timedelta(hours=1)
REFRESH_TOKEN_LIFETIME = timedelta(days=30)
AUTH_CODE_LIFETIME = timedelta(minutes=10)


def _json_error(error: str, description: str, status: int = 400) -> http.Response:
    """Return a JSON error response per RFC 6749 Section 5.2."""
    body = json.dumps({
        'error': error,
        'error_description': description,
    })
    return http.Response(
        body,
        status=status,
        content_type='application/json',
    )


def _json_response(data: dict, status: int = 200) -> http.Response:
    """Return a JSON success response."""
    body = json.dumps(data)
    return http.Response(
        body,
        status=status,
        content_type='application/json',
        headers=[('Cache-Control', 'no-store'), ('Pragma', 'no-cache')],
    )


def _build_redirect_uri(base_uri: str, params: dict) -> str:
    """Append query parameters to a redirect URI."""
    parsed = urlparse(base_uri)
    existing_params = parse_qs(parsed.query)
    existing_params.update({k: [v] for k, v in params.items()})
    flat_params = {k: v[0] if len(v) == 1 else v for k, v in existing_params.items()}
    new_query = urlencode(flat_params)
    return urlunparse(parsed._replace(query=new_query))


def _verify_pkce(code_verifier: str, code_challenge: str, method: str) -> bool:
    """Verify PKCE code_verifier against the stored code_challenge.

    Args:
        code_verifier: The verifier sent with the token request.
        code_challenge: The challenge stored from the authorization request.
        method: Either 'S256' or 'plain'.

    Returns:
        True if verification passes.
    """
    if method == 'S256':
        digest = hashlib.sha256(code_verifier.encode('ascii')).digest()
        import base64
        computed = base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')
        return computed == code_challenge
    elif method == 'plain':
        return code_verifier == code_challenge
    return False


def verify_oauth_token(
    req: Any,
    required_scopes: list[str] | None = None,
) -> tuple[Any, Any]:
    """Verify an OAuth 2.0 Bearer token from the Authorization header.

    This helper is intended for use by other controllers to protect
    API endpoints with OAuth tokens.

    Args:
        req: The current HTTP request object (odoo.http.request).
        required_scopes: Optional list of scopes that must all be present.

    Returns:
        Tuple of (user record, token record) if valid.

    Raises:
        werkzeug.exceptions.Unauthorized: If the token is missing or invalid.
        werkzeug.exceptions.Forbidden: If the token lacks required scopes.
    """
    from werkzeug.exceptions import Unauthorized, Forbidden

    auth_header = req.httprequest.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        raise Unauthorized('Missing or invalid Authorization header. '
                           'Expected: Authorization: Bearer <token>')

    access_token = auth_header[7:].strip()
    if not access_token:
        raise Unauthorized('Empty bearer token')

    token = req.env['woow_paas_platform.oauth_token'].sudo().search([
        ('access_token', '=', access_token),
        ('is_revoked', '=', False),
    ], limit=1)

    if not token:
        raise Unauthorized('Invalid or expired token')

    if not token.is_access_token_valid():
        raise Unauthorized('Token has expired')

    if required_scopes:
        if not token.has_all_scopes(required_scopes):
            raise Forbidden(
                f'Insufficient scope. Required: {" ".join(required_scopes)}')

    return token.user_id, token


class OAuth2Controller(Controller):
    """OAuth 2.0 Provider endpoints (RFC 6749)."""

    # ==================== Authorization Endpoint ====================

    @route('/oauth2/authorize', type='http', auth='user', methods=['GET'], website=False)
    def authorize_get(self, **kwargs: Any) -> Any:
        """Display the OAuth authorization page.

        Query params (RFC 6749 Section 4.1.1):
            response_type: Must be 'code'.
            client_id: The client's public identifier.
            redirect_uri: Where to redirect after authorization.
            scope: Space-separated list of requested scopes.
            state: Opaque value for CSRF protection.
            code_challenge: PKCE challenge (optional).
            code_challenge_method: PKCE method, 'S256' or 'plain' (optional).
        """
        response_type = kwargs.get('response_type', '')
        client_id_param = kwargs.get('client_id', '')
        redirect_uri = kwargs.get('redirect_uri', '')
        scope = kwargs.get('scope', '')
        state = kwargs.get('state', '')
        code_challenge = kwargs.get('code_challenge', '')
        code_challenge_method = kwargs.get('code_challenge_method', '')

        # Validate response_type
        if response_type != 'code':
            return request.render('woow_paas_platform.oauth2_error', {
                'error_title': 'Invalid Request',
                'error_message': 'Only response_type=code is supported.',
            })

        # Validate client_id
        client = request.env['woow_paas_platform.oauth_client'].sudo().search([
            ('client_id', '=', client_id_param),
            ('is_active', '=', True),
        ], limit=1)

        if not client:
            return request.render('woow_paas_platform.oauth2_error', {
                'error_title': 'Invalid Client',
                'error_message': 'The client_id is invalid or the application is inactive.',
            })

        # Validate redirect_uri
        if not redirect_uri or not client.check_redirect_uri(redirect_uri):
            return request.render('woow_paas_platform.oauth2_error', {
                'error_title': 'Invalid Redirect URI',
                'error_message': 'The redirect_uri is not registered for this application.',
            })

        # Validate scopes
        requested_scopes = scope.split() if scope else []
        allowed_scopes = client.get_allowed_scopes()
        invalid_scopes = [s for s in requested_scopes if s not in allowed_scopes]
        if invalid_scopes:
            return request.render('woow_paas_platform.oauth2_error', {
                'error_title': 'Invalid Scope',
                'error_message': f'Invalid scope(s): {", ".join(invalid_scopes)}',
            })

        # Use all allowed scopes if none specified
        if not requested_scopes:
            requested_scopes = sorted(allowed_scopes)

        # Scope descriptions for display
        scope_descriptions = {
            'smarthome:read': 'Read your Smart Home and Workspace data',
            'smarthome:tunnel': 'Access tunnel connections to your devices',
            'workspace:read': 'Read your Workspace information',
        }

        scopes_display = [
            {
                'name': s,
                'description': scope_descriptions.get(s, s),
            }
            for s in requested_scopes
        ]

        return request.render('woow_paas_platform.oauth2_authorize', {
            'client_name': client.name,
            'scopes': scopes_display,
            'client_id': client_id_param,
            'redirect_uri': redirect_uri,
            'scope': ' '.join(requested_scopes),
            'state': state,
            'response_type': response_type,
            'code_challenge': code_challenge,
            'code_challenge_method': code_challenge_method,
        })

    @route('/oauth2/authorize', type='http', auth='user', methods=['POST'],
           website=False, csrf=True)
    def authorize_post(self, **kwargs: Any) -> Any:
        """Process the user's authorization decision.

        Form params:
            approve: Present if user clicked 'Allow'.
            deny: Present if user clicked 'Deny'.
            client_id, redirect_uri, scope, state: Echoed from the form.
        """
        client_id_param = kwargs.get('client_id', '')
        redirect_uri = kwargs.get('redirect_uri', '')
        scope = kwargs.get('scope', '')
        state = kwargs.get('state', '')
        code_challenge = kwargs.get('code_challenge', '')
        code_challenge_method = kwargs.get('code_challenge_method', '')

        # Validate client
        client = request.env['woow_paas_platform.oauth_client'].sudo().search([
            ('client_id', '=', client_id_param),
            ('is_active', '=', True),
        ], limit=1)

        if not client or not client.check_redirect_uri(redirect_uri):
            return request.render('woow_paas_platform.oauth2_error', {
                'error_title': 'Invalid Request',
                'error_message': 'Invalid client or redirect URI.',
            })

        # User denied
        if 'deny' in kwargs:
            params = {'error': 'access_denied', 'error_description': 'User denied access'}
            if state:
                params['state'] = state
            return request.redirect(_build_redirect_uri(redirect_uri, params))

        # User approved - generate authorization code
        now = http.request.env.cr.now()
        code = secrets.token_urlsafe(32)
        from datetime import datetime
        expires = datetime.utcnow() + AUTH_CODE_LIFETIME

        request.env['woow_paas_platform.oauth_code'].sudo().create({
            'code': code,
            'redirect_uri': redirect_uri,
            'scope': scope,
            'expires_at': expires,
            'user_id': request.env.user.id,
            'client_id': client.id,
            'code_challenge': code_challenge or False,
            'code_challenge_method': code_challenge_method or False,
        })

        params = {'code': code}
        if state:
            params['state'] = state

        _logger.info(
            'OAuth2 authorization code issued for client=%s user=%s',
            client.name, request.env.user.login,
        )

        return request.redirect(_build_redirect_uri(redirect_uri, params))

    # ==================== Token Endpoint ====================

    @route('/oauth2/token', type='http', auth='none', methods=['POST'],
           csrf=False, save_session=False)
    def token(self, **kwargs: Any) -> http.Response:
        """Exchange credentials for tokens (RFC 6749 Section 4.1.3, 4.3, 6).

        Supports grant_type:
            - authorization_code: Exchange auth code for tokens.
            - refresh_token: Refresh an expired access token.
            - client_credentials: Machine-to-machine token (no user context).
        """
        grant_type = kwargs.get('grant_type', '')

        if grant_type == 'authorization_code':
            return self._handle_authorization_code(kwargs)
        elif grant_type == 'refresh_token':
            return self._handle_refresh_token(kwargs)
        elif grant_type == 'client_credentials':
            return self._handle_client_credentials(kwargs)
        else:
            return _json_error(
                'unsupported_grant_type',
                f'Grant type "{grant_type}" is not supported. '
                'Use: authorization_code, refresh_token, or client_credentials.',
            )

    def _authenticate_client(self, kwargs: dict) -> Any | None:
        """Authenticate a client using client_id and client_secret.

        Returns the client record or None.
        """
        client_id_param = kwargs.get('client_id', '')
        client_secret = kwargs.get('client_secret', '')

        if not client_id_param or not client_secret:
            return None

        client = request.env['woow_paas_platform.oauth_client'].sudo().search([
            ('client_id', '=', client_id_param),
            ('is_active', '=', True),
        ], limit=1)

        if not client or not client.verify_secret(client_secret):
            return None

        return client

    def _issue_tokens(
        self,
        client: Any,
        user: Any,
        scope: str,
        include_refresh: bool = True,
    ) -> http.Response:
        """Create and return access (and optionally refresh) tokens.

        Args:
            client: The OAuth client record.
            user: The res.users record.
            scope: Space-separated granted scopes.
            include_refresh: Whether to include a refresh token.

        Returns:
            JSON response with token data.
        """
        from datetime import datetime

        now = datetime.utcnow()
        access_token = secrets.token_urlsafe(48)
        access_expires = now + ACCESS_TOKEN_LIFETIME

        token_vals = {
            'access_token': access_token,
            'token_type': 'bearer',
            'scope': scope,
            'expires_at': access_expires,
            'user_id': user.id,
            'client_id': client.id,
        }

        refresh_token_str = None
        if include_refresh:
            refresh_token_str = secrets.token_urlsafe(48)
            token_vals['refresh_token'] = refresh_token_str
            token_vals['refresh_expires_at'] = now + REFRESH_TOKEN_LIFETIME

        request.env['woow_paas_platform.oauth_token'].sudo().create(token_vals)

        response_data = {
            'access_token': access_token,
            'token_type': 'bearer',
            'expires_in': int(ACCESS_TOKEN_LIFETIME.total_seconds()),
            'scope': scope,
        }
        if refresh_token_str:
            response_data['refresh_token'] = refresh_token_str

        _logger.info(
            'OAuth2 token issued: client=%s user=%s scope=%s',
            client.name, user.login if user else 'N/A', scope,
        )

        return _json_response(response_data)

    def _handle_authorization_code(self, kwargs: dict) -> http.Response:
        """Handle grant_type=authorization_code."""
        code_str = kwargs.get('code', '')
        redirect_uri = kwargs.get('redirect_uri', '')
        code_verifier = kwargs.get('code_verifier', '')

        # Authenticate client
        client = self._authenticate_client(kwargs)
        if not client:
            return _json_error('invalid_client', 'Client authentication failed.', 401)

        if not client.check_grant_type('authorization_code'):
            return _json_error(
                'unauthorized_client',
                'This client is not authorized for the authorization_code grant type.',
            )

        # Find and validate authorization code
        auth_code = request.env['woow_paas_platform.oauth_code'].sudo().search([
            ('code', '=', code_str),
            ('client_id', '=', client.id),
        ], limit=1)

        if not auth_code:
            return _json_error('invalid_grant', 'Authorization code not found.')

        if not auth_code.is_valid():
            return _json_error('invalid_grant', 'Authorization code expired or already used.')

        if auth_code.redirect_uri != redirect_uri:
            return _json_error('invalid_grant', 'Redirect URI mismatch.')

        # PKCE verification
        if auth_code.code_challenge:
            if not code_verifier:
                return _json_error('invalid_grant', 'PKCE code_verifier is required.')
            if not _verify_pkce(
                code_verifier, auth_code.code_challenge, auth_code.code_challenge_method or 'plain'
            ):
                return _json_error('invalid_grant', 'PKCE verification failed.')

        # Mark code as used (single-use)
        auth_code.mark_used()

        return self._issue_tokens(
            client=client,
            user=auth_code.user_id,
            scope=auth_code.scope or '',
            include_refresh=True,
        )

    def _handle_refresh_token(self, kwargs: dict) -> http.Response:
        """Handle grant_type=refresh_token."""
        refresh_token_str = kwargs.get('refresh_token', '')

        # Authenticate client
        client = self._authenticate_client(kwargs)
        if not client:
            return _json_error('invalid_client', 'Client authentication failed.', 401)

        if not client.check_grant_type('refresh_token'):
            return _json_error(
                'unauthorized_client',
                'This client is not authorized for the refresh_token grant type.',
            )

        if not refresh_token_str:
            return _json_error('invalid_grant', 'Missing refresh_token parameter.')

        # Find the token record
        token = request.env['woow_paas_platform.oauth_token'].sudo().search([
            ('refresh_token', '=', refresh_token_str),
            ('client_id', '=', client.id),
        ], limit=1)

        if not token:
            return _json_error('invalid_grant', 'Refresh token not found.')

        if not token.is_refresh_token_valid():
            return _json_error('invalid_grant', 'Refresh token expired or revoked.')

        # Revoke the old token
        token.revoke()

        # Issue new tokens with the same scope and user
        return self._issue_tokens(
            client=client,
            user=token.user_id,
            scope=token.scope or '',
            include_refresh=True,
        )

    def _handle_client_credentials(self, kwargs: dict) -> http.Response:
        """Handle grant_type=client_credentials (machine-to-machine)."""
        client = self._authenticate_client(kwargs)
        if not client:
            return _json_error('invalid_client', 'Client authentication failed.', 401)

        if not client.check_grant_type('client_credentials'):
            return _json_error(
                'unauthorized_client',
                'This client is not authorized for the client_credentials grant type.',
            )

        scope = kwargs.get('scope', '')
        if scope:
            requested = set(scope.split())
            allowed = client.get_allowed_scopes()
            invalid = requested - allowed
            if invalid:
                return _json_error(
                    'invalid_scope',
                    f'Invalid scope(s): {", ".join(sorted(invalid))}',
                )
        else:
            scope = client.scopes or ''

        # For client_credentials, use a system/service user.
        # We use the SUPERUSER to represent the client itself.
        from odoo import SUPERUSER_ID
        service_user = request.env['res.users'].sudo().browse(SUPERUSER_ID)

        return self._issue_tokens(
            client=client,
            user=service_user,
            scope=scope,
            include_refresh=False,
        )

    # ==================== Introspection Endpoint ====================

    @route('/oauth2/introspect', type='http', auth='none', methods=['POST'],
           csrf=False, save_session=False)
    def introspect(self, **kwargs: Any) -> http.Response:
        """Token introspection (RFC 7662).

        Allows resource servers to verify token validity.

        Form params:
            token: The token to introspect.
            client_id, client_secret: Client credentials for authentication.
        """
        client = self._authenticate_client(kwargs)
        if not client:
            return _json_error('invalid_client', 'Client authentication failed.', 401)

        token_str = kwargs.get('token', '')
        if not token_str:
            return _json_response({'active': False})

        # Search by access_token first, then by refresh_token
        token = request.env['woow_paas_platform.oauth_token'].sudo().search([
            ('access_token', '=', token_str),
        ], limit=1)

        if not token:
            token = request.env['woow_paas_platform.oauth_token'].sudo().search([
                ('refresh_token', '=', token_str),
            ], limit=1)

        if not token or not token.is_access_token_valid():
            return _json_response({'active': False})

        from odoo import fields as odoo_fields
        exp_dt = odoo_fields.Datetime.to_datetime(token.expires_at)
        exp_timestamp = int(exp_dt.timestamp()) if exp_dt else 0

        return _json_response({
            'active': True,
            'scope': token.scope or '',
            'client_id': token.client_id.client_id,
            'username': token.user_id.login,
            'token_type': 'bearer',
            'exp': exp_timestamp,
            'sub': str(token.user_id.id),
        })

    # ==================== Revocation Endpoint ====================

    @route('/oauth2/revoke', type='http', auth='none', methods=['POST'],
           csrf=False, save_session=False)
    def revoke(self, **kwargs: Any) -> http.Response:
        """Token revocation (RFC 7009).

        Form params:
            token: The token to revoke (access_token or refresh_token).
            client_id, client_secret: Client credentials for authentication.
        """
        client = self._authenticate_client(kwargs)
        if not client:
            return _json_error('invalid_client', 'Client authentication failed.', 401)

        token_str = kwargs.get('token', '')
        if not token_str:
            # Per RFC 7009, always return 200 even if token is invalid
            return _json_response({})

        # Search by access_token or refresh_token
        token = request.env['woow_paas_platform.oauth_token'].sudo().search([
            '|',
            ('access_token', '=', token_str),
            ('refresh_token', '=', token_str),
            ('client_id', '=', client.id),
        ], limit=1)

        if token:
            token.revoke()
            _logger.info(
                'OAuth2 token revoked: client=%s user=%s',
                client.name, token.user_id.login,
            )

        # Always return 200 per RFC 7009
        return _json_response({})
