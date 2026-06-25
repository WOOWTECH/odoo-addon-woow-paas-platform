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

from ..models.oauth_device_code import DEVICE_CODE_LIFETIME, MAX_VERIFICATION_ATTEMPTS

_logger = logging.getLogger(__name__)

# Token lifetimes
ACCESS_TOKEN_LIFETIME = timedelta(hours=1)
REFRESH_TOKEN_LIFETIME = timedelta(days=30)
AUTH_CODE_LIFETIME = timedelta(minutes=10)

# RFC 8628 device authorization grant
DEVICE_GRANT = 'urn:ietf:params:oauth:grant-type:device_code'


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

        # Enforce PKCE for public / require_pkce clients
        pkce_required = client.client_type == 'public' or client.require_pkce
        if pkce_required:
            if not code_challenge:
                _logger.warning(
                    'OAuth2 PKCE enforcement reject (authorize GET): client=%s missing code_challenge',
                    client.name,
                )
                return request.render('woow_paas_platform.oauth2_error', {
                    'error_title': 'PKCE Required',
                    'error_message': 'This client requires PKCE. '
                                     'Include code_challenge and '
                                     'code_challenge_method=S256.',
                })
            if code_challenge_method != 'S256':
                _logger.warning(
                    'OAuth2 PKCE enforcement reject (authorize GET): client=%s method=%r (S256 required)',
                    client.name, code_challenge_method,
                )
                return request.render('woow_paas_platform.oauth2_error', {
                    'error_title': 'Invalid PKCE Method',
                    'error_message': 'Only code_challenge_method=S256 is '
                                     'accepted for this client.',
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
            'security-access:read':
                'Read your Security Access services and subscription',
            'security-access:tunnel':
                'Access the Cloudflare Tunnel token for your Security '
                'Access connector',
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

        # Re-enforce PKCE on POST (defense against bypassing the GET step).
        pkce_required = client.client_type == 'public' or client.require_pkce
        if pkce_required:
            if not code_challenge or code_challenge_method != 'S256':
                _logger.warning(
                    'OAuth2 PKCE enforcement reject (authorize POST): '
                    'client=%s challenge_present=%s method=%r',
                    client.name, bool(code_challenge), code_challenge_method,
                )
                params = {
                    'error': 'invalid_request',
                    'error_description': 'PKCE with S256 is required for this client.',
                }
                if state:
                    params['state'] = state
                return request.redirect(_build_redirect_uri(redirect_uri, params), local=False)

        # User denied
        if 'deny' in kwargs:
            params = {'error': 'access_denied', 'error_description': 'User denied access'}
            if state:
                params['state'] = state
            return request.redirect(_build_redirect_uri(redirect_uri, params), local=False)

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

        return request.redirect(_build_redirect_uri(redirect_uri, params), local=False)

    # ==================== Device Authorization Endpoint (RFC 8628) ====================

    @route('/oauth2/device_authorization', type='http', auth='none',
           methods=['POST'], csrf=False, save_session=False)
    def device_authorization(self, **kwargs: Any) -> http.Response:
        """RFC 8628 §3.1-3.2: issue a device_code + user_code for a client."""
        client = self._authenticate_client(kwargs)
        if not client:
            return _json_error('invalid_client', 'Client authentication failed.', 401)
        if not client.check_grant_type(DEVICE_GRANT):
            return _json_error(
                'unauthorized_client',
                'This client is not authorized for the device_code grant type.',
            )

        scope = kwargs.get('scope', '')
        if scope:
            invalid = set(scope.split()) - client.get_allowed_scopes()
            if invalid:
                return _json_error(
                    'invalid_scope', f'Invalid scope(s): {", ".join(sorted(invalid))}',
                )
        else:
            # No scope requested: resolve to the client's full allowed set NOW
            # and store it, so the consent page shows exactly what the token
            # will carry (mirrors authorize_get's write-back).
            scope = ' '.join(sorted(client.get_allowed_scopes()))

        device = request.env['woow_paas_platform.oauth_device_code'].create_for_client(
            client, scope,
        )

        # verification_uri MUST be absolute https. Use admin-controlled
        # web.base.url ONLY — never derive a security-relevant URL from the
        # client-controlled Host header (host-header injection -> phishing).
        base = (request.env['ir.config_parameter'].sudo()
                .get_param('web.base.url') or '').rstrip('/')
        if not base:
            return _json_error(
                'server_error', 'Server misconfigured: web.base.url is unset.', 500,
            )
        verification_uri = f'{base}/device'

        _logger.info(
            'OAuth2 device authorization issued: client=%s user_code=%s',
            client.name, device.user_code,
        )
        return _json_response({
            'device_code': device.device_code,
            'user_code': device.user_code,
            'verification_uri': verification_uri,
            'verification_uri_complete': f'{verification_uri}?user_code={device.user_code}',
            'expires_in': int(DEVICE_CODE_LIFETIME.total_seconds()),
            'interval': device.interval,
        })

    @route('/device', type='http', auth='user', methods=['GET'], website=False)
    def device_verify_get(self, **kwargs: Any) -> Any:
        """RFC 8628 §3.3: verification page. Optional ?user_code= pre-fill."""
        user_code = (kwargs.get('user_code') or '').strip().upper()
        return request.render('woow_paas_platform.device_verify', {
            'user_code': user_code, 'error': None,
        })

    @route('/device', type='http', auth='user', methods=['POST'],
           website=False, csrf=True)
    def device_verify_post(self, **kwargs: Any) -> Any:
        """Validate user_code, then show consent or process approve/deny."""
        user_code = (kwargs.get('user_code') or '').strip().upper()

        # Per-session brute-force throttle — the REAL guessing defense. A wrong
        # guess finds no record, so any per-RECORD counter never fires; the only
        # place to rate-limit guessing is per-actor here. (Persistent per-IP
        # version is F-DEV-1; session-based is the MVP floor, on top of 34.6-bit
        # entropy + 15-min lifetime + the auth='user' session requirement.)
        fails = request.session.get('device_verify_fails', 0)
        if fails >= MAX_VERIFICATION_ATTEMPTS:
            return request.render('woow_paas_platform.device_verify', {
                'user_code': '', 'error': 'Too many attempts. Please wait and try again.',
            })

        DeviceCode = request.env['woow_paas_platform.oauth_device_code'].sudo()
        device = DeviceCode.search([
            ('user_code', '=', user_code), ('status', '=', 'pending'),
        ], limit=1)

        # Invalid / expired user_code — generic error (no oracle on which codes
        # exist), and count the failed guess against this session.
        if not device or device.is_expired():
            request.session['device_verify_fails'] = fails + 1
            return request.render('woow_paas_platform.device_verify', {
                'user_code': '', 'error': 'Invalid or expired code. Please try again.',
            })

        # Valid code entered — reset the session failure counter.
        request.session['device_verify_fails'] = 0

        # Stage 2: approve / deny.
        if 'approve' in kwargs:
            device.mark_approved(request.env.user)
            return request.render('woow_paas_platform.device_result', {'approved': True})
        if 'deny' in kwargs:
            device.mark_denied()
            return request.render('woow_paas_platform.device_result',
                                  {'approved': False, 'message': None})

        # Stage 1 (verify pressed): show consent with scopes + possession check.
        scopes = [s for s in (device.scope or '').split() if s]
        return request.render('woow_paas_platform.device_consent', {
            'client_name': device.client_id.name,
            'user_code': device.user_code,
            'scopes': scopes or list(device.client_id.get_allowed_scopes()),
        })

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
        elif grant_type == DEVICE_GRANT:
            return self._handle_device_code(kwargs)
        else:
            return _json_error(
                'unsupported_grant_type',
                f'Grant type "{grant_type}" is not supported. '
                'Use: authorization_code, refresh_token, client_credentials, '
                'or urn:ietf:params:oauth:grant-type:device_code.',
            )

    def _authenticate_client(self, kwargs: dict) -> Any | None:
        """Authenticate by client_id (+ client_secret for confidential).

        Public clients are rejected if they carry any client_secret —
        defense against misconfigured clients populating the field.
        """
        client_id_param = kwargs.get('client_id', '')
        client_secret = kwargs.get('client_secret', '')

        if not client_id_param:
            return None

        client = request.env['woow_paas_platform.oauth_client'].sudo().search([
            ('client_id', '=', client_id_param),
            ('is_active', '=', True),
        ], limit=1)

        if not client:
            _logger.info(
                'OAuth2 token auth failed: unknown client_id=%r',
                client_id_param[:32],
            )
            return None

        if client.client_type == 'public':
            if client_secret:
                _logger.warning(
                    'Public client "%s" attempted token auth with a client_secret — rejected.',
                    client.name,
                )
                return None
            return client

        if not client_secret or not client.verify_secret(client_secret):
            _logger.info(
                'OAuth2 token auth failed: bad secret for client=%s',
                client.name,
            )
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

        # PKCE enforcement: public / require_pkce clients must have issued
        # the code with a challenge, blocking any downgrade attack where a
        # public client somehow obtained a non-PKCE auth_code.
        pkce_required = client.client_type == 'public' or client.require_pkce
        if pkce_required and not auth_code.code_challenge:
            _logger.warning(
                'OAuth2 PKCE downgrade reject: client=%s auth_code id=%s has no challenge',
                client.name, auth_code.id,
            )
            return _json_error(
                'invalid_grant',
                'Authorization code was not issued with PKCE; '
                'this client requires PKCE.',
            )

        # PKCE verification
        if auth_code.code_challenge:
            if not code_verifier:
                _logger.info(
                    'OAuth2 PKCE reject: client=%s auth_code id=%s missing code_verifier',
                    client.name, auth_code.id,
                )
                return _json_error('invalid_grant', 'PKCE code_verifier is required.')
            if pkce_required and auth_code.code_challenge_method != 'S256':
                _logger.warning(
                    'OAuth2 PKCE downgrade reject: client=%s auth_code id=%s method=%r (S256 required)',
                    client.name, auth_code.id, auth_code.code_challenge_method,
                )
                return _json_error(
                    'invalid_grant',
                    'This client requires code_challenge_method=S256.',
                )
            if not _verify_pkce(
                code_verifier, auth_code.code_challenge, auth_code.code_challenge_method or 'plain'
            ):
                _logger.info(
                    'OAuth2 PKCE verify failed: client=%s auth_code id=%s',
                    client.name, auth_code.id,
                )
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

    def _handle_device_code(self, kwargs: dict) -> http.Response:
        """Handle grant_type=urn:ietf:params:oauth:grant-type:device_code.

        RFC 8628 §3.4-3.5 polling state machine. No PKCE (device flow has no
        authorize redirect channel). The user identity comes from the /device
        approval, not from this machine-to-machine poll.
        """
        device_code_str = kwargs.get('device_code', '')

        client = self._authenticate_client(kwargs)
        if not client:
            return _json_error('invalid_client', 'Client authentication failed.', 401)
        if not client.check_grant_type(DEVICE_GRANT):
            return _json_error(
                'unauthorized_client',
                'This client is not authorized for the device_code grant type.',
            )
        if not device_code_str:
            return _json_error('invalid_request', 'Missing device_code parameter.')

        device = request.env['woow_paas_platform.oauth_device_code'].sudo().search([
            ('device_code', '=', device_code_str),
            ('client_id', '=', client.id),
        ], limit=1)
        if not device:
            return _json_error('invalid_grant', 'Unknown device_code.')

        # RFC 8628 §3.5: terminal states (expired / denied) must beat the
        # slow_down "pending variant" — a user who DENIED must get access_denied
        # even if the device is polling too fast. register_poll() runs ONLY in
        # the pending branch: never on the approved path (else the very poll
        # that should mint the token returns slow_down and delays success one
        # interval), and never preempting a terminal decision.
        if device.is_expired():
            return _json_error('expired_token', 'The device_code has expired.')
        if device.status == 'denied':
            return _json_error('access_denied', 'The user denied the request.')
        if device.status == 'consumed':
            return _json_error('invalid_grant', 'Authorization already used.')
        if device.status == 'pending':
            if device.register_poll():
                return _json_error('slow_down', 'Polling too frequently.')
            return _json_error('authorization_pending', 'Authorization pending.')

        # approved -> atomically consume (compare-and-swap). Only the race
        # winner issues tokens; concurrent polls cannot double-issue.
        if not device.consume():
            return _json_error('invalid_grant', 'Authorization already used.')
        return self._issue_tokens(
            client=client, user=device.user_id, scope=device.scope or '',
            include_refresh=True,
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
