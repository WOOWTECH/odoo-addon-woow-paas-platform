import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from uuid import uuid4

from aiohttp import ClientSession, CookieJar, web
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from publication_gate.gate import BoundedManifestGate, CAPABILITY_DIGEST
from publication_gate.test_cookie_adapter import CookieIdentityAdapter, build


class CookieContinuityAdapterTest(unittest.IsolatedAsyncioTestCase):
    async def test_same_memory_cookie_real_gate_before_positive_after_deny(self):
        temp = tempfile.TemporaryDirectory(); root = Path(temp.name)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public = key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        private = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption())
        (root / "jwt.pem").write_bytes(public)
        scope = f"cf-tunnel:v1:{'a' * 32}:{uuid4()}"
        installation = str(uuid4())
        binding = {
            "resource_identity": scope, "installation_id": installation,
            "state": str(root / "latch.sqlite"),
            "issuer": "https://owned.cloudflareaccess.test", "audience": "b" * 64,
            "jwt_public_key": str(root / "jwt.pem"), "client_sha256": "c" * 64,
            "server_cert": str(root / "server.pem"), "server_key": str(root / "server.key"),
            "ca": str(root / "ca.pem"), "control_bind": "127.0.0.1", "control_port": 18443,
            "data_bind": "127.0.0.1", "data_port": 18303,
            "upstream": "http://127.0.0.1:8123/manifest.json",
        }
        policy = {"mode":"email_allowlist", "emails":["toypark@example.test"]}
        desired = {"operation":"guarded_publish", "origin_authorization":{"policy":policy},
                   "origin_identity":installation, "origin_set_digest":"d"*64,
                   "expected_predecessor":None, "capability_digest":CAPABILITY_DIGEST}
        envelope = {"resource_identity":scope, "effect_id":str(uuid4()),
                    "desired_generation":1,
                    "desired_digest":hashlib.sha256(json.dumps(
                        desired, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}
        authority = {"resource_identity":scope, "committed":True, "envelope":envelope,
                     "desired_payload":desired, "policy":policy}

        async def backend(_request):
            return web.json_response({"marker":"actual-backend"})
        backend_app = web.Application(); backend_app.router.add_get("/manifest.json", backend)
        backend_runner = web.AppRunner(backend_app); await backend_runner.setup()
        backend_site = web.TCPSite(backend_runner, "127.0.0.1", 8123); await backend_site.start()
        gate = BoundedManifestGate(binding, lambda: asyncio.sleep(0, result=authority))
        gate.latch.install(); await gate.close(envelope); await gate.activate(envelope)
        gate_app = web.Application(); gate_app.router.add_get("/manifest.json", gate.manifest)
        gate_runner = web.AppRunner(gate_app); await gate_runner.setup()
        gate_site = web.TCPSite(gate_runner, "127.0.0.1", 0); await gate_site.start()
        gate_port = gate_site._server.sockets[0].getsockname()[1]
        adapter = CookieIdentityAdapter(
            f"http://127.0.0.1:{gate_port}/manifest.json", binding["issuer"],
            binding["audience"], "toypark@example.test", private)
        client = TestClient(TestServer(build(adapter)), cookie_jar=CookieJar(unsafe=True))
        await client.start_server()
        try:
            login = await client.post("/test-only/session"); cookie_hash = (await login.json())["cookie_sha256"]
            before = await client.get("/manifest.json")
            self.assertEqual(before.status, 200)
            self.assertEqual(before.headers["X-Test-Cookie-SHA256"], cookie_hash)
            self.assertEqual((await before.json())["marker"], "actual-backend")

            # The adapter is unchanged.  The real gate closes and fresh
            # product-authority input no longer authorizes toypark.
            denied_policy = {"mode":"email_allowlist", "emails":["control@example.test"]}
            next_desired = dict(desired, origin_authorization={"policy": denied_policy},
                                expected_predecessor=envelope)
            next_envelope = {"resource_identity":scope, "effect_id":str(uuid4()),
                             "desired_generation":2,
                             "desired_digest":hashlib.sha256(json.dumps(
                                 next_desired, sort_keys=True,
                                 separators=(",", ":")).encode()).hexdigest()}
            authority.clear(); authority.update({
                "resource_identity":scope, "committed":True, "envelope":next_envelope,
                "desired_payload":next_desired, "policy":denied_policy})
            await gate.close(next_envelope)
            await gate.activate(next_envelope)
            after = await client.get("/manifest.json")
            self.assertEqual(after.status, 403)
            self.assertEqual(after.headers["X-Test-Cookie-SHA256"], cookie_hash)
            self.assertEqual(len(adapter.sessions), 1)
            # Timed reuse of the untouched cookie remains denied.  Repeated
            # observations are idempotent and the redacted receipt survives a
            # fresh gate object reading the same durable latch.
            for _ in range(3):
                repeated = await client.get("/manifest.json")
                self.assertEqual(repeated.status, 403)
                self.assertEqual(repeated.headers["X-Test-Cookie-SHA256"], cookie_hash)
            cohort = json.dumps({
                "aud": binding["audience"],
                "identity_nonce": cookie_hash,
                "iss": binding["issuer"],
                "sub": cookie_hash,
            }, sort_keys=True, separators=(",", ":"))
            session_sha256 = hashlib.sha256(cohort.encode()).hexdigest()
            receipts = gate.latch.denial_receipts(next_envelope)
            self.assertEqual(len(receipts), 1)
            self.assertEqual(receipts[0]["session_sha256"], session_sha256)
            restarted = BoundedManifestGate(
                binding, lambda: asyncio.sleep(0, result=authority))
            self.assertEqual(
                restarted.latch.denial_receipts(next_envelope), receipts)
            self.assertTrue(restarted.closing)
            self.assertIsNone(restarted.activated)

            control = CookieIdentityAdapter(
                f"http://127.0.0.1:{gate_port}/manifest.json", binding["issuer"],
                binding["audience"], "control@example.test", private)
            control_client = TestClient(
                TestServer(build(control)), cookie_jar=CookieJar(unsafe=True))
            await control_client.start_server()
            try:
                await control_client.post("/test-only/session")
                control_response = await control_client.get("/manifest.json")
                self.assertEqual(control_response.status, 200)
                self.assertEqual((await control_response.json())["marker"], "actual-backend")
                print(json.dumps({
                    "classification": "test-only-cookie-not-cloudflare",
                    "cookie_sha256": cookie_hash,
                    "before_status": before.status,
                    "after_policy_change_status": after.status,
                    "repeated_denial_statuses": [403, 403, 403],
                    "durable_denial_receipt_count": len(receipts),
                    "denial_receipt_session_sha256": session_sha256,
                    "restart_default_closed": restarted.closing,
                    "authorized_control_status": control_response.status,
                    "same_adapter_session_count": len(adapter.sessions),
                    "decision_maker": "BoundedManifestGate",
                }, sort_keys=True), flush=True)
            finally:
                await control_client.close()
        finally:
            await client.close(); await gate_runner.cleanup(); await backend_runner.cleanup(); temp.cleanup()
