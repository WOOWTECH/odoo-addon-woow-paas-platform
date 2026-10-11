"""Response factories matching the operator tunnel publication wire contract.

Only tests import this file. No global model monkeypatch or test-only context.
"""
def configure_receipt(tunnel_id, hostname, service_url, **kwargs):
    return {'message': 'Tunnel %s configuration updated' % tunnel_id,
            'hostname': hostname, 'service_url': service_url}

def routes_receipt(tunnel_id, routes, domain='woowtech.io', sample=None, **kwargs):
    samples=(sample or {}).get('routes', [])
    dns={row['subdomain']: row.get('dns_record_id') for row in samples}
    return {'tunnel_id': tunnel_id, 'routes': [
        {'subdomain':row['subdomain'], 'hostname':row['subdomain']+'.'+domain,
         'dns_record_id':dns.get(row['subdomain'])} for row in routes]}
