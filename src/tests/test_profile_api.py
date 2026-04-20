"""Tests for /api/me profile endpoints."""
from odoo.tests.common import HttpCase


class TestProfileApi(HttpCase):

    def setUp(self):
        super().setUp()
        self.authenticate('admin', 'admin')
        # self.env.user is __system__ (partner=OdooBot) in HttpCase, but the HTTP
        # session authenticated as admin (partner=Administrator). Use the admin
        # partner explicitly; see test_payment_methods_api.py for context.
        self.partner = self.env.ref('base.partner_admin')

    def _post(self, url, params_json):
        return self.url_open(
            url,
            data='{"jsonrpc":"2.0","method":"call","params":' + params_json + '}',
            headers={'Content-Type': 'application/json'},
        )

    def test_me_returns_current_partner(self):
        response = self._post('/api/me', '{}')
        body = response.json()['result']
        self.assertTrue(body['success'], f"Unexpected: {body}")
        self.assertEqual(body['data']['id'], self.partner.id)
        self.assertEqual(body['data']['name'], self.partner.name)
        self.assertEqual(body['data']['email'], self.partner.email or '')
        self.assertEqual(
            body['data']['avatar_url'],
            f'/web/image/res.partner/{self.partner.id}/image_128',
        )

    def test_update_saves_fields(self):
        response = self._post(
            '/api/me/update',
            '{"name":"New Name","email":"new@example.com","phone":"+886-912-000-000"}',
        )
        body = response.json()['result']
        self.assertTrue(body['success'], f"Unexpected: {body}")
        self.partner.invalidate_recordset()
        self.assertEqual(self.partner.name, 'New Name')
        self.assertEqual(self.partner.email, 'new@example.com')
        self.assertEqual(self.partner.phone, '+886-912-000-000')

    def test_update_rejects_empty_name(self):
        response = self._post('/api/me/update', '{"name":""}')
        body = response.json()['result']
        self.assertFalse(body['success'])
        self.assertEqual(body.get('code'), 'name_required')

    def test_update_rejects_whitespace_only_name(self):
        response = self._post('/api/me/update', '{"name":"   "}')
        body = response.json()['result']
        self.assertFalse(body['success'])
        self.assertEqual(body.get('code'), 'name_required')

    def test_update_rejects_invalid_email(self):
        response = self._post(
            '/api/me/update', '{"name":"OK","email":"not-an-email"}',
        )
        body = response.json()['result']
        self.assertFalse(body['success'])
        self.assertEqual(body.get('code'), 'invalid_email')

    def test_update_accepts_empty_email(self):
        response = self._post('/api/me/update', '{"name":"OK","email":""}')
        body = response.json()['result']
        self.assertTrue(body['success'], f"Unexpected: {body}")

    def test_update_ignores_foreign_partner_id(self):
        other = self.env['res.partner'].sudo().create({'name': 'Other Person'})
        original_name = other.name
        response = self._post(
            '/api/me/update',
            f'{{"partner_id":{other.id},"name":"Hijacked"}}',
        )
        body = response.json()['result']
        self.assertTrue(body['success'], f"Unexpected: {body}")
        other.invalidate_recordset()
        self.assertEqual(other.name, original_name,
                         "Endpoint must ignore partner_id from body")
        self.partner.invalidate_recordset()
        self.assertEqual(self.partner.name, 'Hijacked')

    def test_update_partial_payload_clears_omitted_fields(self):
        """Document full-replace contract: omitted fields are cleared, not preserved.

        Pre-populate email/phone, then send an update with only `name`.
        Expected: email and phone become False (cleared).
        """
        self.partner.sudo().write({
            'email': 'before@example.com',
            'phone': '+886-900-000-000',
        })
        response = self._post('/api/me/update', '{"name":"Just Name"}')
        body = response.json()['result']
        self.assertTrue(body['success'], f"Unexpected: {body}")
        self.partner.invalidate_recordset()
        self.assertEqual(self.partner.name, 'Just Name')
        self.assertFalse(self.partner.email,
                         "Full-replace semantics: omitted email must be cleared")
        self.assertFalse(self.partner.phone,
                         "Full-replace semantics: omitted phone must be cleared")

    def test_update_phone_whitespace_clears_value(self):
        """Document phone semantics: whitespace-only = clear (like empty string)."""
        self.partner.sudo().write({'phone': '+886-900-000-000'})
        response = self._post(
            '/api/me/update', '{"name":"OK","phone":"   "}',
        )
        body = response.json()['result']
        self.assertTrue(body['success'], f"Unexpected: {body}")
        self.partner.invalidate_recordset()
        self.assertFalse(self.partner.phone,
                         "Whitespace-only phone must be cleared (stored as False)")
