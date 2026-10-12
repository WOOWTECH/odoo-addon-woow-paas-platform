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


def access_receipt(payload, app_id=None, policy_ids=None, allowed_idps=None,
                   orphaned_policy_ids=None):
    """Build a complete Access sync receipt for the supplied request.

    This factory deliberately derives cardinalities from the request.  It is
    suitable only for positive publication fixtures; malformed-receipt tests
    must continue to spell out their intentionally invalid responses.
    """
    if not payload['enabled']:
        return {
            'app_id': None,
            'policy_ids': [],
            'allowed_idps': [],
            'unresolved_idp_types': [],
            'orphaned_policy_ids': list(orphaned_policy_ids or []),
        }

    policies = payload.get('policies') or []
    selected_types = list(dict.fromkeys(payload.get('login_idp_types') or []))
    if policy_ids is None:
        policy_ids = ['pol-%d' % (index + 1)
                      for index in range(len(policies))]
    if allowed_idps is None:
        allowed_idps = ['idp-%s' % idp_type
                        for idp_type in selected_types]
    return {
        'app_id': app_id or payload.get('existing_app_id') or 'app-1',
        'policy_ids': list(policy_ids),
        'allowed_idps': list(allowed_idps),
        'unresolved_idp_types': [],
        'orphaned_policy_ids': list(orphaned_policy_ids or []),
    }


def install_publication_receipts(client, domain='woowtech.io'):
    """Attach production-shaped positive publication responses to a mock."""
    client.configure_tunnel.side_effect = configure_receipt
    client.sync_tunnel_routes.side_effect = (
        lambda tunnel_id, routes, **kwargs: routes_receipt(
            tunnel_id, routes, domain=domain, **kwargs))
    client.sync_access_app.side_effect = access_receipt
    client.park_release_route.return_value = {'status': 'parked'}
    client.unpark_release_route.return_value = {'status': 'unparked'}
    return client
