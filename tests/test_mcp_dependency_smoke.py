"""Exercise installed adapter + SDK, including the guarded HTTP factory."""
import asyncio
import importlib.util
from pathlib import Path
import unittest

import httpx
import httpcore
from langchain_mcp_adapters.sessions import create_session

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    'mcp_url_guard_smoke', ROOT / 'src/services/mcp_url_guard.py')
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def leaves(exc):
    children = getattr(exc, 'exceptions', None)
    return [leaf for child in children for leaf in leaves(child)] if children else [exc]


class TestInstalledMcpTransport(unittest.TestCase):
    def test_streamable_http_uses_guard_with_other_http_stacks_installed(self):
        self.assertEqual(guard._http_modules(), (httpx, httpcore))
        calls = []
        guarded = guard.guarded_http_client_factory(
            resolver=lambda host: {'10.43.88.10'})

        def factory(**kwargs):
            calls.append(kwargs)
            client = guarded(**kwargs)
            self.assertIsInstance(client, httpx.AsyncClient)
            return client

        async def connect():
            async with create_session({
                'transport': 'streamable_http',
                'url': 'http://forbidden.example.invalid/mcp',
                'httpx_client_factory': factory,
            }) as session:
                await session.initialize()

        with self.assertRaises(BaseException) as raised:
            asyncio.run(asyncio.wait_for(connect(), timeout=5))
        self.assertEqual(len(calls), 1)
        self.assertTrue(any(isinstance(exc, guard.McpUrlRejected)
                            for exc in leaves(raised.exception)), repr(raised.exception))


if __name__ == '__main__':
    unittest.main()
