"""Current upstream Cloudflare400 service against an isolated persistent fake account.

This fixture loads only the exact upstream IdP-fail-closed source, without any recovery
verifier, signing key, owner registry or future API ownership claims.
"""
import asyncio
from copy import deepcopy
import importlib.util
import json
import hashlib
import os
from pathlib import Path
import sys
import types
from unittest.mock import patch

import httpx


class Account:
    def __init__(self):
        self.apps = {}
        self.policies = {}
        self.tags = {}
        self.calls = []
        self.next_app = 0
        self.next_policy = 0

    def handler(self, request):
        body = json.loads(request.content) if request.content else None
        method, path = request.method, request.url.path
        self.calls.append((method, path, deepcopy(body)))
        if path.endswith('/identity_providers') and method == 'GET':
            return self.ok([{'id': 'native-google', 'type': 'google'}])
        for name, rows in [('apps', self.apps), ('policies', self.policies),
                           ('tags', self.tags)]:
            base = '/access/' + name
            if path.endswith(base):
                if method == 'GET':
                    return self.ok(list(rows.values()))
                if method == 'POST':
                    if name == 'apps':
                        self.next_app += 1
                        item_id = 'native-app-%d' % self.next_app
                    elif name == 'policies':
                        self.next_policy += 1
                        item_id = 'native-policy-%d' % self.next_policy
                    else:
                        item_id = body.get('name') or body.get('id')
                    item = dict(deepcopy(body), id=item_id)
                    rows[item_id] = item
                    return self.ok(item, 201)
            if base + '/' in path:
                item_id = path.rsplit('/', 1)[-1]
                if method == 'GET':
                    if item_id not in rows:
                        return httpx.Response(404, json={'success': False})
                    return self.ok(rows[item_id])
                if method == 'PUT':
                    if item_id not in rows:
                        return httpx.Response(404, json={'success': False})
                    rows[item_id] = dict(deepcopy(body), id=item_id)
                    return self.ok(rows[item_id])
                if method == 'DELETE':
                    rows.pop(item_id, None)
                    return self.ok({})
        raise AssertionError('Unmodelled isolated Cloudflare endpoint: %s %s' %
                             (method, path))

    @staticmethod
    def ok(result, status=200):
        return httpx.Response(status, json={
            'success': True, 'result': deepcopy(result),
            'result_info': {'page': 1, 'per_page': 1000,
                            'total_pages': 1,
                            'count': len(result) if isinstance(result, list) else 1},
        })


def load_cloudflare():
    path = Path(os.environ.get(
        'AC1011_OPERATOR_SOURCE', '/tmp/ac1011-operator/cloudflare.py'))
    # ⚠ 這個指紋釘住的是 CI 複製進來的真 operator 原始碼
    # （`extra/paas-operator/src/services/cloudflare.py`，見
    # `.gitea/workflows/odoo-tests.yml` 的 docker cp）——這個 fixture 直接 exec 它，
    # 所以上游一改，這裡載到的行為就跟著改，指紋強迫改的人回來看一眼。
    # **改了那個檔就要同步更新這一行**（`sha256sum` 取新值），否則 full-suite 會以
    # 「Unexpected operator source」整組紅掉——#1766 給 connections 補 client_id
    # 時就是這樣紅的（PR CI 不含 Odoo 測試，只在 merge 進 develop 後才跑）。
    assert hashlib.sha256(path.read_bytes()).hexdigest() == '50dd0ab9e4f3319730579bf21b8ba1d1d5795dc637d1dee00e1898ccf4daa8b6', 'Unexpected operator source'
    config = types.ModuleType('src.config')
    config.settings = types.SimpleNamespace(
        cloudflare_account_id='native-isolated-account',
        cloudflare_tunnel_id='native-isolated-shared-tunnel',
        cloudflare_zone_id='native-isolated-zone',
        cloudflare_api_token='fake-isolated-token',
        cloudflare_domain='example.test', cloudflare_enabled=True,
    )
    with patch.dict(sys.modules, {'src.config': config}):
        spec = importlib.util.spec_from_file_location('ac1011_native_cf', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class ActualCloudflareContract:
    """Sync bridge: actual service code, real JSON requests, no live sockets."""
    def __init__(self, cf):
        self.cf = cf
        self.account = Account()
        self.service = cf.CloudflareService()

    def sync(self, payload):
        data = deepcopy(payload)
        client_class = httpx.AsyncClient

        def client_factory(*args, **kwargs):
            return client_class(transport=httpx.MockTransport(
                self.account.handler))

        async def forbid_real_transport(self, request, *args, **kwargs):
            raise AssertionError('AC1011 real external transport forbidden')

        with patch.object(self.cf.httpx, 'AsyncClient', client_factory), \
                patch.object(httpx.AsyncHTTPTransport, 'handle_async_request',
                             forbid_real_transport):
            return asyncio.run(self.service.sync_access_app(**data))
