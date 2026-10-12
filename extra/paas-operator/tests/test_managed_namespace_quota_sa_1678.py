"""#1678 opt-in, read-only HTTP contract against a real operator ServiceAccount.

Fixtures are supplied by an authorized operator in an owned dev-sandbox namespace.
This test creates no namespaces/RBAC/PVCs and does not use the broad integration
suite's cluster-admin setup/cleanup fixtures. Run with -m integration and the
QUOTA1678_* environment variables documented in the operator README.
"""
import os
import subprocess

import httpx
import pytest

pytestmark = pytest.mark.integration


def test_managed_quota_http_uses_real_readonly_service_account():
    url = os.environ.get('QUOTA1678_OPERATOR_URL')
    namespace = os.environ.get('QUOTA1678_NAMESPACE')
    if not url or not namespace:
        pytest.skip('Explicit isolated quota fixture and real operator URL required')
    assert namespace.startswith('dev-sandbox-quota1678-')
    headers = {'X-API-Key': os.environ['QUOTA1678_API_KEY']}
    endpoint = '%s/api/namespaces/%s/quota' % (url, namespace)
    with httpx.Client(headers=headers, timeout=15) as client:
        assert client.get(endpoint, headers={'X-API-Key': 'wrong-test-key'}).status_code == 401
        default = client.get(endpoint).json()
        observation_response = client.get(endpoint, params={'include_managed': True})
        assert observation_response.status_code == 200
        report = observation_response.json()
        observation = report.pop('managed_quota')
        assert report == default
        assert default['resources']['limits.cpu']['quota'] == 'diagnostic-other'
        assert observation['available'] is True
        assert observation['namespace'] == namespace
        assert observation['name'] == namespace + '-quota'
        assert observation['uid'] == os.environ['QUOTA1678_QUOTA_UID']
        assert observation['resource_version'] == os.environ['QUOTA1678_QUOTA_VERSION']
        assert observation['hard'] == {
            'requests.cpu': '2', 'limits.cpu': '7', 'requests.memory': '3Gi',
            'limits.memory': '5Gi', 'requests.storage': '11Gi',
        }
        assert observation['used'] == {key: '0' for key in observation['hard']}
        assert observation['observed_at']
        # This namespace has no RoleBinding for the reader, and is not created.
        denied = client.get('%s/api/namespaces/%s-unbound/quota' % (url, namespace),
                            params={'include_managed': True}).json()['managed_quota']
        assert denied['available'] is False
        assert denied['reason'] == 'quota_unreadable'
    whoami = subprocess.run(['kubectl', '--context', 'woow-k3s', 'auth', 'whoami'],
                            capture_output=True, text=True, check=True, timeout=10)
    assert 'system:serviceaccount:dev-sandbox-quota1678:quota1678-reader' in whoami.stdout
    # Server dry-run cannot mutate the fixture even if the test detects bad RBAC.
    denied_write = subprocess.run([
        'kubectl', '--context', 'woow-k3s', 'patch', 'resourcequota', namespace + '-quota',
        '-n', namespace, '--type=merge', '--dry-run=server',
        '-p', '{"spec":{"hard":{"limits.cpu":"8"}}}',
    ], capture_output=True, text=True, timeout=10)
    assert denied_write.returncode != 0
    assert 'Forbidden' in denied_write.stderr
