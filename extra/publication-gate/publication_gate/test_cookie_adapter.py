"""Test-only cookie continuity adapter for the real publication gate.

The adapter never reads policy or revocation state and never decides allow or
deny.  It only retains an opaque cookie in memory, translates that exact
cookie to a fixed signed test identity, and forwards the gate's response.
It is not a Cloudflare cookie/session substitute.
"""
import hashlib
import secrets
import time

from aiohttp import ClientSession, web
import jwt


class CookieIdentityAdapter:
    cookie_name = "rev01_test_continuity"

    def __init__(self, gate_url, issuer, audience, email, private_key):
        self.gate_url = gate_url
        self.issuer = issuer
        self.audience = audience
        self.email = email
        self.private_key = private_key
        self.sessions = set()

    @staticmethod
    def digest(cookie):
        return hashlib.sha256(cookie.encode()).hexdigest()

    async def establish(self, _request):
        cookie = secrets.token_urlsafe(32)
        self.sessions.add(cookie)
        response = web.json_response({"cookie_sha256": self.digest(cookie)})
        response.set_cookie(self.cookie_name, cookie, httponly=True, samesite="Strict")
        return response

    def _identity(self, cookie):
        now = int(time.time())
        return jwt.encode({
            "aud": [self.audience], "email": self.email,
            "exp": now + 300, "iat": now, "nbf": now - 1,
            "iss": self.issuer, "type": "app",
            "identity_nonce": self.digest(cookie), "sub": self.digest(cookie),
        }, self.private_key, algorithm="RS256", headers={"kid": "owned-test-only"})

    async def manifest(self, request):
        cookie = request.cookies.get(self.cookie_name, "")
        if not cookie or cookie not in self.sessions:
            raise web.HTTPUnauthorized(text="test continuity cookie absent")
        async with ClientSession() as client:
            async with client.get(
                    self.gate_url,
                    headers={"Cf-Access-Jwt-Assertion": self._identity(cookie)}) as upstream:
                body = await upstream.read()
                return web.Response(status=upstream.status, body=body,
                                    content_type=upstream.content_type,
                                    headers={"X-Test-Cookie-SHA256": self.digest(cookie)})


def build(adapter):
    app = web.Application(client_max_size=0)
    app.router.add_post("/test-only/session", adapter.establish)
    app.router.add_get("/manifest.json", adapter.manifest)
    return app
