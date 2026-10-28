"""Deployable bounded HTTP origin gate for the REV01 manifest cohort.

This is deliberately not a generic reverse proxy.  Only GET/HEAD /manifest.json
is supported; WebSockets, request bodies, query strings and other paths deny.
"""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
from pathlib import Path
import ssl

from aiohttp import ClientSession, ClientTimeout, web
import jwt

from .latch import Conflict, Latch, validate_envelope

CAPABILITY_DIGEST = '16aaf107b2e6d73e0ae818424998d7738d3fa0c7d14bd18986dac7bd7362f76f'


def _policy_digest(payload):
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


class BoundedManifestGate:
    def __init__(self, binding, authority_reader):
        required = {'resource_identity', 'installation_id', 'state', 'issuer', 'audience',
                    'jwt_public_key', 'client_sha256', 'server_cert', 'server_key', 'ca',
                    'control_bind', 'control_port', 'data_bind', 'data_port', 'upstream'}
        if not isinstance(binding, dict) or set(binding) != required:
            raise ValueError('exact gate binding required')
        if binding['upstream'] != 'http://127.0.0.1:8123/manifest.json':
            raise ValueError('fixed loopback HA manifest upstream required')
        self.binding = binding
        self.latch = Latch(binding['state'], binding['resource_identity'], binding['installation_id'])
        self.authority_reader = authority_reader
        self.public_key = Path(binding['jwt_public_key']).read_text()
        self.activated = None
        self.closing = True
        self.active = {}
        self.lock = asyncio.Lock()

    async def current(self):
        current = await self.authority_reader()
        if current is None:
            raise ValueError('fresh committed authority unavailable')
        desired = current.get('desired_payload', {})
        if (current.get('committed') is not True
                or current.get('resource_identity') != self.binding['resource_identity']
                or desired.get('operation') != 'guarded_publish'
                or desired.get('capability_digest') != CAPABILITY_DIGEST
                or current.get('policy') != desired.get('origin_authorization', {}).get('policy')
                or _policy_digest(desired) != current.get('envelope', {}).get('desired_digest')):
            raise ValueError('fresh committed authority mismatch')
        policy = current['policy']
        if (set(policy) != {'mode', 'emails'} or policy['mode'] != 'email_allowlist'
                or not isinstance(policy['emails'], list) or not policy['emails']):
            raise ValueError('unsupported authority policy')
        return current

    @asynccontextmanager
    async def track(self, request):
        if self.closing or self.activated is None:
            raise web.HTTPServiceUnavailable(text='origin publication quarantined')
        task = asyncio.current_task()
        self.active[task] = request.transport
        try:
            yield
        finally:
            self.active.pop(task, None)

    async def close(self, envelope):
        validate_envelope(envelope, self.binding['resource_identity'])
        async with self.lock:
            self.closing = True
            self.activated = None
            await asyncio.to_thread(self.latch.close, envelope)
            pending = list(self.active)
            for task in pending:
                transport = self.active.get(task)
                if transport is not None:
                    transport.abort()
                task.cancel()
            if pending:
                _, unfinished = await asyncio.wait(pending, timeout=3)
                if unfinished or self.active:
                    raise RuntimeError('origin drain unproven')
            return {'contract': 'origin-deny-monotonic-v1', 'envelope': envelope,
                    'installation_id': self.binding['installation_id'],
                    'release_permitted': False, 'active_requests': 0,
                    'active_ws': 0, 'backend_links': 0}

    async def activate(self, envelope):
        validate_envelope(envelope, self.binding['resource_identity'])
        async with self.lock:
            current = await self.current()
            if current['envelope'] != envelope or not self.latch.matches(envelope) or self.active:
                raise ValueError('exact close/current authority required')
            self.activated = dict(envelope)
            self.closing = False
            return {'activated': envelope, 'normal_release_supported': 'bounded-http-only',
                    'capability_digest': CAPABILITY_DIGEST}

    def _authenticate_control(self, request):
        tls = request.transport.get_extra_info('ssl_object')
        certificate = tls and tls.getpeercert(binary_form=True)
        if not certificate or hashlib.sha256(certificate).hexdigest() != self.binding['client_sha256']:
            raise web.HTTPForbidden()

    async def close_http(self, request):
        self._authenticate_control(request)
        try:
            return web.json_response(await self.close(await request.json()))
        except Conflict:
            raise web.HTTPConflict(text='generation/effect conflict') from None
        except (ValueError, TypeError, KeyError):
            raise web.HTTPBadRequest(text='invalid envelope') from None

    async def activate_http(self, request):
        self._authenticate_control(request)
        try:
            return web.json_response(await self.activate(await request.json()))
        except Exception:
            raise web.HTTPConflict(text='activation rejected') from None

    async def denial_receipts_http(self, request):
        self._authenticate_control(request)
        try:
            envelope = await request.json()
            validate_envelope(envelope, self.binding['resource_identity'])
            return web.json_response({
                'contract': 'rev01-policy-denial-receipt-v1',
                'release_permitted': False,
                'receipts': await asyncio.to_thread(
                    self.latch.denial_receipts, envelope),
            })
        except (ValueError, TypeError, KeyError):
            raise web.HTTPBadRequest(text='invalid envelope') from None

    async def manifest(self, request):
        if (request.method not in ('GET', 'HEAD') or request.raw_path != '/manifest.json'
                or request.can_read_body or request.headers.get('Upgrade')):
            raise web.HTTPServiceUnavailable(text='unsupported request')
        envelope = self.activated
        try:
            current = await self.current()
            token = request.headers.get('Cf-Access-Jwt-Assertion', '')
            claims = jwt.decode(token, self.public_key, algorithms=['RS256'],
                                issuer=self.binding['issuer'], audience=self.binding['audience'],
                                options={'require': ['exp', 'iat', 'nbf', 'iss', 'aud', 'sub',
                                                     'email', 'type', 'identity_nonce']})
            if (self.closing or envelope is None or current['envelope'] != envelope
                    or claims['type'] != 'app'):
                raise ValueError('denied')
            if claims['email'] not in current['policy']['emails']:
                cohort = json.dumps({
                    'aud': self.binding['audience'],
                    'identity_nonce': claims['identity_nonce'],
                    'iss': claims['iss'],
                    'sub': claims['sub'],
                }, sort_keys=True, separators=(',', ':'))
                await asyncio.to_thread(
                    self.latch.record_denial, envelope,
                    hashlib.sha256(cohort.encode()).hexdigest())
                raise ValueError('denied')
        except Exception:
            raise web.HTTPForbidden(text='origin authorization denied') from None
        async with self.track(request):
            async with ClientSession(timeout=ClientTimeout(total=5)) as client:
                method = 'HEAD' if request.method == 'HEAD' else 'GET'
                async with client.request(method, self.binding['upstream']) as upstream:
                    body = await upstream.read()
                    if upstream.status != 200 or len(body) > 65536:
                        raise web.HTTPServiceUnavailable(text='unsupported upstream response')
            try:
                latest = await self.current()
            except Exception:
                # The upstream attempt has already happened.  Never return its body,
                # retry it, or fall back to the predecessor when fresh authority
                # disappears during post-forward revalidation.
                raise web.HTTPServiceUnavailable(
                    text='fresh authority unavailable after upstream') from None
            if self.closing or self.activated != envelope or latest['envelope'] != envelope:
                raise web.HTTPForbidden(text='policy changed during response')
            return web.Response(status=200, body=body, content_type='application/json',
                                headers={'Cache-Control': 'no-store, max-age=0',
                                         'Pragma': 'no-cache'})

    def tls_context(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(self.binding['server_cert'], self.binding['server_key'])
        context.load_verify_locations(self.binding['ca'])
        context.verify_mode = ssl.CERT_REQUIRED
        return context
