"""Pinned mTLS client for the private Odoo authority endpoint."""
import hashlib
import http.client
import json
import ssl


def read_committed(binding):
    required = {'host', 'port', 'server_sha256', 'ca', 'client_cert', 'client_key'}
    if not isinstance(binding, dict) or set(binding) != required:
        raise ValueError('exact authority binding required')
    context = ssl.create_default_context(cafile=binding['ca'])
    context.load_cert_chain(binding['client_cert'], binding['client_key'])
    connection = http.client.HTTPSConnection(
        binding['host'], binding['port'], context=context, timeout=5)
    try:
        connection.connect()
        certificate = connection.sock.getpeercert(binary_form=True)
        if hashlib.sha256(certificate).hexdigest() != binding['server_sha256']:
            raise ValueError('authority certificate mismatch')
        connection.request('GET', '/committed', headers={'Accept': 'application/json'})
        response = connection.getresponse()
        body = response.read(16385)
        if response.status != 200 or len(body) > 16384:
            raise RuntimeError('authority denied or unavailable')
        value = json.loads(body)
        if not isinstance(value, dict):
            raise ValueError('authority object required')
        return value
    finally:
        connection.close()
