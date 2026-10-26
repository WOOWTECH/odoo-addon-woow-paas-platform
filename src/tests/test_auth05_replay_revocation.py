"""AUTH-05: a replayed authorization code / reused refresh token must not
leave a live session behind.

True HTTP controls against /oauth2/token. The product already denies the
replay itself; what these lock down is the second half of RFC 6749 10.5 and
RFC 9700 4.14.2 — the credential the replay proves leaked must not keep
working. Each case also asserts the inverse, because the revocation must not
become a way for someone holding only a spent code value to kill a healthy
session.
"""
import base64
import hashlib
import secrets
import uuid
from datetime import timedelta

from odoo import fields
from odoo.tests.common import HttpCase, new_test_user, tagged


REDIRECT_URI = 'http://localhost:8123/auth/callback'
OTHER_REDIRECT_URI = 'http://localhost:8123/auth/other'


@tagged('post_install', '-at_install', 'auth05_replay_revocation')
class TestAuth05ReplayRevocation(HttpCase):
    """Replay/reuse detection has to invalidate what the credential produced."""

    def setUp(self):
        super().setUp()
        suffix = uuid.uuid4().hex[:10]
        self.user = new_test_user(
            self.env,
            login=f'auth05_owner_{suffix}',
            password='auth05-sandbox-only',
            groups='base.group_user',
            context={'no_reset_password': True, 'mail_create_nosubscribe': True},
        )
        self.other_user = new_test_user(
            self.env,
            login=f'auth05_other_{suffix}',
            password='auth05-sandbox-only',
            groups='base.group_user',
            context={'no_reset_password': True, 'mail_create_nosubscribe': True},
        )
        self.Token = self.env['woow_paas_platform.oauth_token'].sudo()
        self.Code = self.env['woow_paas_platform.oauth_code'].sudo()
        self.public_client = self.env['woow_paas_platform.oauth_client'].sudo().create({
            'name': f'AUTH05 public client {suffix}',
            'client_id': f'auth05-public-{suffix}',
            'client_type': 'public',
            'require_pkce': True,
            'redirect_uris': f'{REDIRECT_URI}\n{OTHER_REDIRECT_URI}',
            'scopes': 'workspace:read smarthome:read',
            'grant_types': 'authorization_code,refresh_token',
        })
        self.secret = secrets.token_urlsafe(24)
        self.conf_client = self.env['woow_paas_platform.oauth_client'].sudo().create({
            'name': f'AUTH05 confidential client {suffix}',
            'client_id': f'auth05-conf-{suffix}',
            'client_type': 'confidential',
            'require_pkce': False,
            'redirect_uris': f'{REDIRECT_URI}\n{OTHER_REDIRECT_URI}',
            'scopes': 'workspace:read smarthome:read',
            'grant_types': 'authorization_code,refresh_token',
        })
        self.conf_client.set_secret(self.secret)

    # ------------------------------------------------------------ helpers
    def _pkce(self):
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode('ascii')).digest()
        ).rstrip(b'=').decode('ascii')
        return verifier, challenge

    def _code(self, client, challenge=None, redirect_uri=REDIRECT_URI,
              user=None):
        return self.Code.create({
            'code': secrets.token_urlsafe(32),
            'redirect_uri': redirect_uri,
            'scope': 'workspace:read',
            'expires_at': fields.Datetime.now() + timedelta(minutes=10),
            'user_id': (user or self.user).id,
            'client_id': client.id,
            'code_challenge': challenge or False,
            'code_challenge_method': 'S256' if challenge else False,
        })

    def _exchange(self, code, verifier=None, redirect_uri=REDIRECT_URI,
                  client=None, secret=None):
        client = client or self.public_client
        data = {
            'grant_type': 'authorization_code',
            'code': code.code,
            'redirect_uri': redirect_uri,
            'client_id': client.client_id,
        }
        if verifier is not None:
            data['code_verifier'] = verifier
        if secret is not None:
            data['client_secret'] = secret
        return self.url_open('/oauth2/token', data=data)

    def _refresh(self, refresh_token, client=None, secret=None):
        client = client or self.public_client
        data = {
            'grant_type': 'refresh_token',
            'refresh_token': refresh_token,
            'client_id': client.client_id,
        }
        if secret is not None:
            data['client_secret'] = secret
        return self.url_open('/oauth2/token', data=data)

    def _row(self, access_token):
        return self.Token.search([('access_token', '=', access_token)])

    def _assert_denied(self, response):
        self.assertEqual(response.status_code, 400, response.text)
        body = response.json()
        self.assertEqual(body['error'], 'invalid_grant')
        self.assertNotIn('access_token', body)
        self.assertNotIn('refresh_token', body)
        return body

    # ------------------------------------------------------------- code replay
    def test_replayed_code_revokes_the_session_it_already_minted(self):
        verifier, challenge = self._pkce()
        code = self._code(self.public_client, challenge)
        first = self._exchange(code, verifier)
        self.assertEqual(first.status_code, 200, first.text)
        issued = first.json()
        row = self._row(issued['access_token'])
        self.assertTrue(row)
        self.assertEqual(row.origin_code_id, code)
        self.assertFalse(row.is_revoked)
        rows_before = self.Token.search_count([])

        replay = self._exchange(code, verifier)

        self._assert_denied(replay)
        self.env.invalidate_all()
        self.assertTrue(row.is_revoked, 'the first exchange kept a live session')
        self.assertFalse(row.is_access_token_valid())
        self.assertFalse(row.is_refresh_token_valid())
        self.assertEqual(self.Token.search_count([]), rows_before,
                         'a denied replay must not create a token row')

    def test_replay_without_the_verifier_denies_without_killing_the_session(self):
        verifier, challenge = self._pkce()
        code = self._code(self.public_client, challenge)
        issued = self._exchange(code, verifier).json()
        row = self._row(issued['access_token'])

        replay = self._exchange(code, secrets.token_urlsafe(48))

        self._assert_denied(replay)
        self.env.invalidate_all()
        self.assertFalse(
            row.is_revoked,
            'knowing only a spent code must not revoke a healthy session',
        )
        self.assertTrue(row.is_access_token_valid())

    def test_replay_with_mismatched_redirect_uri_does_not_revoke(self):
        code = self._code(self.conf_client)
        issued = self._exchange(code, client=self.conf_client,
                                secret=self.secret).json()
        row = self._row(issued['access_token'])

        replay = self._exchange(code, redirect_uri=OTHER_REDIRECT_URI,
                                client=self.conf_client, secret=self.secret)

        self._assert_denied(replay)
        self.env.invalidate_all()
        self.assertFalse(row.is_revoked)

    def test_confidential_replay_with_full_binding_revokes(self):
        code = self._code(self.conf_client)
        issued = self._exchange(code, client=self.conf_client,
                                secret=self.secret).json()
        row = self._row(issued['access_token'])

        replay = self._exchange(code, client=self.conf_client,
                                secret=self.secret)

        self._assert_denied(replay)
        self.env.invalidate_all()
        self.assertTrue(row.is_revoked)

    def test_expired_unused_code_is_not_treated_as_a_replay(self):
        verifier, challenge = self._pkce()
        live_code = self._code(self.public_client, challenge)
        live = self._exchange(live_code, verifier).json()
        live_row = self._row(live['access_token'])

        verifier2, challenge2 = self._pkce()
        stale = self._code(self.public_client, challenge2)
        stale.expires_at = fields.Datetime.now() - timedelta(seconds=1)

        response = self._exchange(stale, verifier2)

        self._assert_denied(response)
        self.env.invalidate_all()
        self.assertFalse(stale.is_used, 'an expired code must not be consumed')
        self.assertFalse(live_row.is_revoked)

    def test_replaying_the_origin_code_reaches_tokens_rotated_since(self):
        verifier, challenge = self._pkce()
        code = self._code(self.public_client, challenge)
        first = self._exchange(code, verifier).json()
        second = self._refresh(first['refresh_token']).json()
        third = self._refresh(second['refresh_token']).json()
        current = self._row(third['access_token'])
        self.assertFalse(current.is_revoked)
        self.assertEqual(current.origin_code_id, code,
                         'the lineage must survive rotation')

        self._assert_denied(self._exchange(code, verifier))

        self.env.invalidate_all()
        self.assertTrue(current.is_revoked)

    def test_code_replay_does_not_revoke_another_users_lineage(self):
        """The sudo revocation domain stays anchored to the exact code."""
        verifier, challenge = self._pkce()
        code = self._code(self.public_client, challenge)
        owner_token = self._exchange(code, verifier).json()
        owner_row = self._row(owner_token['access_token'])

        other_verifier, other_challenge = self._pkce()
        other_code = self._code(
            self.public_client,
            other_challenge,
            user=self.other_user,
        )
        other_token = self._exchange(other_code, other_verifier).json()
        other_row = self._row(other_token['access_token'])
        self.assertEqual(other_row.user_id, self.other_user)
        self.assertFalse(other_row.is_revoked)

        self._assert_denied(self._exchange(code, verifier))

        self.env.invalidate_all()
        self.assertTrue(owner_row.is_revoked)
        self.assertFalse(
            other_row.is_revoked,
            'replaying one user code revoked another user token lineage',
        )
        self.assertTrue(other_row.is_access_token_valid())

    # ---------------------------------------------------------- refresh reuse
    def test_reused_refresh_token_revokes_its_rotation_family(self):
        verifier, challenge = self._pkce()
        code = self._code(self.public_client, challenge)
        first = self._exchange(code, verifier).json()
        rotated = self._refresh(first['refresh_token']).json()
        successor = self._row(rotated['access_token'])
        self.assertFalse(successor.is_revoked)
        self.assertEqual(successor.parent_token_id,
                         self._row(first['access_token']))

        reuse = self._refresh(first['refresh_token'])

        self._assert_denied(reuse)
        self.env.invalidate_all()
        self.assertTrue(successor.is_revoked,
                        'reuse left the pair minted by the first use alive')
        self.assertFalse(successor.is_refresh_token_valid())

    def test_reuse_of_an_old_link_revokes_the_whole_chain(self):
        verifier, challenge = self._pkce()
        code = self._code(self.public_client, challenge)
        gen1 = self._exchange(code, verifier).json()
        gen2 = self._refresh(gen1['refresh_token']).json()
        gen3 = self._refresh(gen2['refresh_token']).json()
        row2 = self._row(gen2['access_token'])
        row3 = self._row(gen3['access_token'])

        self._assert_denied(self._refresh(gen1['refresh_token']))

        self.env.invalidate_all()
        self.assertTrue(row2.is_revoked)
        self.assertTrue(row3.is_revoked)

    def test_expired_refresh_token_is_not_treated_as_reuse(self):
        verifier, challenge = self._pkce()
        code = self._code(self.public_client, challenge)
        first = self._exchange(code, verifier).json()
        row = self._row(first['access_token'])
        row.refresh_expires_at = fields.Datetime.now() - timedelta(seconds=1)

        response = self._refresh(first['refresh_token'])

        self._assert_denied(response)
        self.env.invalidate_all()
        self.assertFalse(row.is_revoked,
                         'expiry alone is no evidence of a leak')

    def test_normal_rotation_is_unaffected(self):
        verifier, challenge = self._pkce()
        code = self._code(self.public_client, challenge)
        first = self._exchange(code, verifier).json()
        rotated = self._refresh(first['refresh_token'])

        self.assertEqual(rotated.status_code, 200, rotated.text)
        body = rotated.json()
        self.assertNotEqual(body['refresh_token'], first['refresh_token'])
        self.env.invalidate_all()
        old = self._row(first['access_token'])
        new = self._row(body['access_token'])
        self.assertTrue(old.is_revoked)
        self.assertFalse(new.is_revoked)
        self.assertTrue(new.is_access_token_valid())
        self.assertEqual(new.user_id, self.user)
