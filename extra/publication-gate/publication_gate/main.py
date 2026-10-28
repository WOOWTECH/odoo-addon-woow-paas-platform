"""Entrypoint for the owned bounded manifest gate."""
import asyncio
import json
import os
from pathlib import Path
import sys

from aiohttp import web

from .gate import BoundedManifestGate
from .remote import read_committed


def load(path):
    value = json.loads(Path(path).read_text())
    if not isinstance(value, dict):
        raise ValueError('JSON object required')
    return value


async def serve(binding_path, authority_path):
    binding = load(binding_path)
    authority = load(authority_path)
    gate = BoundedManifestGate(
        binding, lambda: asyncio.to_thread(read_committed, authority))

    control = web.Application(client_max_size=16384)
    control.router.add_post('/close', gate.close_http)
    control.router.add_post('/activate', gate.activate_http)
    control.router.add_post('/denial-receipts', gate.denial_receipts_http)
    control_runner = web.AppRunner(control, access_log=None)
    await control_runner.setup()
    control_site = web.TCPSite(
        control_runner, binding['control_bind'], binding['control_port'],
        ssl_context=gate.tls_context())

    data = web.Application(client_max_size=0)
    data.router.add_route('*', '/manifest.json', gate.manifest)
    data_runner = web.AppRunner(data, access_log=None)
    await data_runner.setup()
    data_site = web.TCPSite(data_runner, binding['data_bind'], binding['data_port'])

    await control_site.start()
    await data_site.start()
    try:
        await asyncio.Event().wait()
    finally:
        await data_runner.cleanup()
        await control_runner.cleanup()


def main():
    binding_path = os.environ.get('PUBLICATION_GATE_BINDING', '/etc/woow-publication/gate.json')
    authority_path = os.environ.get('PUBLICATION_AUTHORITY_BINDING', '/etc/woow-publication/authority.json')
    binding = load(binding_path)
    if len(sys.argv) == 2 and sys.argv[1] == 'initialize':
        BoundedManifestGate(binding, lambda: None).latch.install()
        return
    if len(sys.argv) != 1:
        raise SystemExit('usage: python -m publication_gate.main [initialize]')
    asyncio.run(serve(binding_path, authority_path))


if __name__ == '__main__':
    main()
