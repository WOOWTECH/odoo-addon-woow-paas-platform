import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4
from datetime import datetime, timedelta, timezone
import ipaddress
import ssl
import time

from aiohttp import ClientSession, TCPConnector, web
import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from publication_gate.gate import BoundedManifestGate, CAPABILITY_DIGEST
from publication_gate.latch import Conflict


class GateContractTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        (root / 'jwt.pem').write_text('unused-in-control-tests')
        self.scope = f"cf-tunnel:v1:{'a' * 32}:{uuid4()}"
        self.installation = str(uuid4())
        self.binding = {
            'resource_identity': self.scope,
            'installation_id': self.installation,
            'state': str(root / 'latch.sqlite'),
            'issuer': 'https://owned.cloudflareaccess.test',
            'audience': 'b' * 64,
            'jwt_public_key': str(root / 'jwt.pem'),
            'client_sha256': 'c' * 64,
            'server_cert': str(root / 'server.pem'),
            'server_key': str(root / 'server.key'),
            'ca': str(root / 'ca.pem'),
            'control_bind': '127.0.0.1',
            'control_port': 18443,
            'data_bind': '127.0.0.1',
            'data_port': 18124,
            'upstream': 'http://127.0.0.1:8123/manifest.json',
        }
        self.policy = {'mode': 'email_allowlist', 'emails': ['a@example.test']}
        self.desired = {
            'operation': 'guarded_publish',
            'origin_authorization': {'policy': self.policy},
            'origin_identity': self.installation,
            'origin_set_digest': 'd' * 64,
            'expected_predecessor': None,
            'capability_digest': CAPABILITY_DIGEST,
        }
        encoded = json.dumps(self.desired, sort_keys=True, separators=(',', ':'), allow_nan=False)
        self.envelope = {
            'resource_identity': self.scope,
            'effect_id': str(uuid4()),
            'desired_generation': 1,
            'desired_digest': hashlib.sha256(encoded.encode()).hexdigest(),
        }
        self.current = {
            'resource_identity': self.scope,
            'committed': True,
            'envelope': dict(self.envelope),
            'desired_payload': self.desired,
            'policy': self.policy,
        }

    def tearDown(self):
        self.temp.cleanup()

    def _certificate(self, name, issuer_cert=None, issuer_key=None, server=False):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
        now = datetime.now(timezone.utc)
        builder = (x509.CertificateBuilder().subject_name(subject)
                   .issuer_name(issuer_cert.subject if issuer_cert else subject)
                   .public_key(key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now - timedelta(minutes=1))
                   .not_valid_after(now + timedelta(days=1))
                   .add_extension(x509.BasicConstraints(ca=issuer_cert is None, path_length=None), True))
        if server:
            builder = builder.add_extension(
                x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), False)
        cert = builder.sign(issuer_key or key, hashes.SHA256())
        return key, cert

    def _write_identity(self, stem, key, cert):
        root = Path(self.temp.name)
        cert_path, key_path = root / f'{stem}.pem', root / f'{stem}.key'
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()))
        return cert_path, key_path

    async def test_real_latch_close_activate_and_restart_deny(self):
        gate = BoundedManifestGate(self.binding, lambda: asyncio.sleep(0, result=self.current))
        gate.latch.install()
        receipt = await gate.close(self.envelope)
        self.assertEqual(receipt['contract'], 'origin-deny-monotonic-v1')
        self.assertFalse(receipt['release_permitted'])
        self.assertEqual(receipt['active_requests'], 0)
        activation = await gate.activate(self.envelope)
        self.assertEqual(activation['activated'], self.envelope)
        self.assertFalse(gate.closing)

        restarted = BoundedManifestGate(
            self.binding, lambda: asyncio.sleep(0, result=self.current))
        self.assertIsNone(restarted.activated)
        self.assertTrue(restarted.closing)

    async def test_stale_conflict_and_changed_authority_deny(self):
        gate = BoundedManifestGate(self.binding, lambda: asyncio.sleep(0, result=self.current))
        gate.latch.install()
        await gate.close(self.envelope)
        newer = dict(self.envelope, effect_id=str(uuid4()), desired_generation=2)
        await gate.close(newer)
        with self.assertRaises(Conflict):
            await gate.close(self.envelope)

        changed = json.loads(json.dumps(self.current))
        changed['policy']['emails'] = ['other@example.test']
        denied = BoundedManifestGate(self.binding, lambda: asyncio.sleep(0, result=changed))
        with self.assertRaises(ValueError):
            await denied.current()

    async def test_missing_state_never_recreated_on_normal_start(self):
        gate = BoundedManifestGate(self.binding, lambda: asyncio.sleep(0, result=self.current))
        with self.assertRaises(Exception):
            await gate.close(self.envelope)

    async def test_denial_receipt_requires_exact_current_envelope_and_is_idempotent(self):
        gate = BoundedManifestGate(
            self.binding, lambda: asyncio.sleep(0, result=self.current))
        gate.latch.install()
        await gate.close(self.envelope)
        session = 'e' * 64
        first = gate.latch.record_denial(self.envelope, session)
        second = gate.latch.record_denial(self.envelope, session)
        self.assertEqual(first, second)
        self.assertEqual(gate.latch.denial_receipts(self.envelope), [first])

        newer = dict(self.envelope, effect_id=str(uuid4()), desired_generation=2)
        await gate.close(newer)
        with self.assertRaises(Conflict):
            gate.latch.record_denial(self.envelope, 'f' * 64)
        self.assertEqual(gate.latch.denial_receipts(newer), [])

    async def test_real_mtls_control_accepts_pinned_client_and_rejects_rogue(self):
        ca_key, ca_cert = self._certificate('owned-ca')
        server_key, server_cert = self._certificate(
            'owned-gate', ca_cert, ca_key, server=True)
        client_key, client_cert = self._certificate('candidate-odoo', ca_cert, ca_key)
        rogue_key, rogue_cert = self._certificate('rogue', ca_cert, ca_key)
        root = Path(self.temp.name)
        ca_path = root / 'ca.pem'
        ca_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
        server_cert_path, server_key_path = self._write_identity(
            'server', server_key, server_cert)
        client_cert_path, client_key_path = self._write_identity(
            'client', client_key, client_cert)
        rogue_cert_path, rogue_key_path = self._write_identity(
            'rogue', rogue_key, rogue_cert)
        self.binding.update({
            'server_cert': str(server_cert_path), 'server_key': str(server_key_path),
            'ca': str(ca_path),
            'client_sha256': hashlib.sha256(
                client_cert.public_bytes(serialization.Encoding.DER)).hexdigest(),
        })
        gate = BoundedManifestGate(self.binding, lambda: asyncio.sleep(0, result=self.current))
        gate.latch.install()
        app = web.Application(client_max_size=16384)
        app.router.add_post('/close', gate.close_http)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0, ssl_context=gate.tls_context())
        await site.start()
        port = site._server.sockets[0].getsockname()[1]

        async def post(cert_path, key_path, envelope):
            context = ssl.create_default_context(cafile=str(ca_path))
            context.load_cert_chain(cert_path, key_path)
            async with ClientSession(connector=TCPConnector(ssl=context)) as client:
                async with client.post(
                        f'https://127.0.0.1:{port}/close', json=envelope) as response:
                    return response.status, await response.text()
        try:
            status, body = await post(client_cert_path, client_key_path, self.envelope)
            self.assertEqual(status, 200, body)
            newer = dict(self.envelope, effect_id=str(uuid4()), desired_generation=2)
            status, _ = await post(rogue_cert_path, rogue_key_path, newer)
            self.assertEqual(status, 403)
            self.assertFalse(gate.latch.matches(newer))
        finally:
            await runner.cleanup()

    async def test_real_http_backend_bytes_and_cloudflare_application_claim_shape(self):
        """Use actual aiohttp backend/proxy bytes, not a gate-owned response."""
        jwt_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        Path(self.binding['jwt_public_key']).write_bytes(jwt_key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
        backend_body = b'{"marker":"actual-ha-backend-byte-control"}'
        backend_methods = []

        async def backend(request):
            backend_methods.append(request.method)
            return web.Response(body=backend_body, content_type='application/json')

        backend_app = web.Application()
        backend_app.router.add_route('*', '/manifest.json', backend)
        backend_runner = web.AppRunner(backend_app, access_log=None)
        await backend_runner.setup()
        backend_site = web.TCPSite(backend_runner, '127.0.0.1', 8123)
        await backend_site.start()

        gate = BoundedManifestGate(self.binding, lambda: asyncio.sleep(0, result=self.current))
        gate.latch.install()
        await gate.close(self.envelope)
        await gate.activate(self.envelope)
        gate_app = web.Application(client_max_size=0)
        gate_app.router.add_route('*', '/manifest.json', gate.manifest)
        gate_runner = web.AppRunner(gate_app, access_log=None)
        await gate_runner.setup()
        gate_site = web.TCPSite(gate_runner, '127.0.0.1', 0)
        await gate_site.start()
        port = gate_site._server.sockets[0].getsockname()[1]

        now = int(time.time())
        claims = {'aud': [self.binding['audience']], 'email': 'a@example.test',
                  'exp': now + 300, 'iat': now, 'nbf': now - 1,
                  'iss': self.binding['issuer'], 'type': 'app',
                  'identity_nonce': 'opaque-cache-key', 'sub': str(uuid4())}
        token = jwt.encode(claims, jwt_key, algorithm='RS256', headers={'kid': 'owned-test'})
        headers = {'Cf-Access-Jwt-Assertion': token}
        try:
            async with ClientSession() as client:
                async with client.get(f'http://127.0.0.1:{port}/manifest.json',
                                      headers=headers) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(await response.read(), backend_body)
                    self.assertEqual(response.headers['Cache-Control'], 'no-store, max-age=0')
                async with client.head(f'http://127.0.0.1:{port}/manifest.json',
                                       headers=headers) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(await response.read(), b'')
                reads = []
                async def authority_disappears_after_forward():
                    reads.append(True)
                    return self.current if len(reads) == 1 else None
                gate.authority_reader = authority_disappears_after_forward
                async with client.get(f'http://127.0.0.1:{port}/manifest.json',
                                      headers=headers) as response:
                    self.assertEqual(response.status, 503)
                    self.assertNotIn(backend_body, await response.read())
                self.assertEqual(len(reads), 2)
                self.assertEqual(backend_methods[-1], 'GET')
                gate.authority_reader = lambda: asyncio.sleep(0, result=self.current)
                async with client.get(f'http://127.0.0.1:{port}/manifest.json?nonce=1',
                                      headers=headers) as response:
                    self.assertEqual(response.status, 503)
                bad = dict(claims, type='org')
                bad_token = jwt.encode(bad, jwt_key, algorithm='RS256')
                async with client.get(f'http://127.0.0.1:{port}/manifest.json',
                                      headers={'Cf-Access-Jwt-Assertion': bad_token}) as response:
                    self.assertEqual(response.status, 403)
            self.assertEqual(backend_methods, ['GET', 'HEAD', 'GET'])
        finally:
            await gate_runner.cleanup()
            await backend_runner.cleanup()
