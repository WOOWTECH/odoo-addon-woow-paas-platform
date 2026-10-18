"""#1678: opt-in managed quota observation, through the public HTTP endpoint."""
import json
import subprocess
from unittest.mock import patch

from fastapi.testclient import TestClient
import pytest

from src.config import settings
from src.main import app

NS = 'paas-ws-test1678'
HARD = {'requests.cpu': '12', 'limits.cpu': '12', 'requests.memory': '24Gi',
        'limits.memory': '24Gi', 'requests.storage': '100Gi'}
USED = {'requests.cpu': '2500m', 'limits.cpu': '9500m', 'requests.memory': '6Gi',
        'limits.memory': '18Gi', 'requests.storage': '60Gi'}


def managed_quota():
    return {'metadata': {'namespace': NS, 'name': NS + '-quota',
                         'uid': 'uid-1678', 'resourceVersion': '1678'},
            'spec': {'hard': dict(HARD)},
            'status': {'hard': dict(HARD), 'used': dict(USED)}}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, 'api_key', 'isolated-test-key')
    monkeypatch.setattr(settings, 'namespace_prefix', 'paas-ws-')
    return TestClient(app, headers={'X-API-Key': 'isolated-test-key'})


def get_report(client, items, include_managed=True, kubectl_calls=1):
    with patch('src.services.helm.subprocess.run', return_value=subprocess.CompletedProcess(
            [], 0, stdout=json.dumps({'items': items}), stderr='')) as kubectl:
        response = client.get('/api/namespaces/%s/quota' % NS,
                              params={'include_managed': include_managed})
    assert response.status_code == 200, response.text
    assert len(kubectl.call_args_list) == kubectl_calls
    assert 'resourcequota' in kubectl.call_args_list[0].args[0]
    return response.json()


def test_admin_projection_preserves_one_managed_object_not_the_tightest_summary(client):
    managed = managed_quota()
    other = {'metadata': {'name': 'other'},
             'status': {'hard': {'limits.cpu': '1'}, 'used': {'limits.cpu': '900m'}}}
    default = get_report(client, [managed, other], include_managed=False)
    assert 'managed_quota' not in default
    assert default['resources']['limits.cpu']['quota'] == 'other'
    report = get_report(client, [managed, other])
    observation = report.pop('managed_quota')
    assert report == default
    assert observation['namespace'] == NS
    assert observation['name'] == NS + '-quota'
    assert observation['uid'] == 'uid-1678'
    assert observation['resource_version'] == '1678'
    assert observation['available'] is True
    assert observation['hard'] == HARD
    assert observation['used'] == USED
    assert observation['observed_at']


@pytest.mark.parametrize('change,reason', [
    ('scopes', 'managed_quota_scoped'),
    ('scopeSelector', 'managed_quota_scoped'),
    ('uid', 'managed_quota_identity_incomplete'),
    ('resourceVersion', 'managed_quota_identity_incomplete'),
    ('namespace', 'managed_quota_identity_incomplete'),
    ('foreign_namespace', 'managed_quota_namespace_mismatch'),
    ('hard', 'managed_quota_incomplete'),
    ('used', 'managed_quota_incomplete'),
    ('invalid', 'managed_quota_invalid_quantity'),
    ('negative', 'managed_quota_invalid_quantity'),
    ('spec_changed', 'managed_quota_status_pending'),
])
def test_incomplete_or_scoped_observation_is_unknown_not_zero(client, change, reason):
    item = managed_quota()
    if change in ('scopes', 'scopeSelector'):
        item['spec'][change] = ['BestEffort'] if change == 'scopes' else {
            'matchExpressions': [{'scopeName': 'PriorityClass', 'operator': 'Exists'}]}
    elif change in ('uid', 'resourceVersion', 'namespace'):
        item['metadata'].pop(change)
    elif change == 'foreign_namespace':
        item['metadata']['namespace'] = 'paas-ws-foreign'
    elif change in ('hard', 'used'):
        item['status'][change].pop('requests.cpu')
    elif change in ('invalid', 'negative'):
        item['status']['used']['requests.cpu'] = 'NaN' if change == 'invalid' else '-1'
    else:
        item['spec']['hard']['limits.cpu'] = '18'
    observation = get_report(client, [item])['managed_quota']
    assert observation['available'] is False
    assert observation['reason'] == reason
    if change in ('hard', 'used'):
        assert 'requests.cpu' not in observation[change]


@pytest.mark.parametrize('items', [[], [{'metadata': {'name': 'other'}}]])
def test_missing_managed_quota_does_not_prove_namespace_is_absent(client, items):
    # #1679：空 list（或只有別的 quota）本身不下結論；namespace 存在性只由第二次的
    # 明確探測決定。這裡探測成功 ⇒ namespace 存在，只是沒有受管 quota。
    # NotFound／Forbidden／逾時的對照見 tests/test_namespace_exists_1679.py。
    observation = get_report(client, items, kubectl_calls=2)['managed_quota']
    assert observation['available'] is False
    assert observation['reason'] == 'managed_quota_not_found'
    assert observation['namespace_exists'] is True
    assert observation['hard'] == observation['used'] == {}


def test_explicit_zero_is_known_but_status_hard_never_falls_back_to_spec(client):
    item = managed_quota()
    item['status']['used']['requests.cpu'] = '0'
    observation = get_report(client, [item])['managed_quota']
    assert observation['available'] is True
    assert observation['used']['requests.cpu'] == '0'
    item.pop('status')
    observation = get_report(client, [item])['managed_quota']
    assert observation['available'] is False
    assert observation['reason'] == 'managed_quota_incomplete'
    assert observation['hard'] == observation['used'] == {}


@pytest.mark.parametrize('failure', [subprocess.TimeoutExpired(['kubectl'], 5),
                                    subprocess.CompletedProcess([], 1, stdout='', stderr='Forbidden')])
def test_read_failure_is_not_successful_empty_list(client, failure):
    behavior = {'side_effect': failure} if isinstance(failure, Exception) else {'return_value': failure}
    with patch('src.services.helm.subprocess.run', **behavior):
        response = client.get('/api/namespaces/%s/quota?include_managed=true' % NS)
    assert response.status_code == 200
    observation = response.json()['managed_quota']
    assert observation['available'] is False
    assert observation['reason'] == 'quota_unreadable'
    assert observation['hard'] == observation['used'] == {}
