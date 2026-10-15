"""#1679：找不到受管 quota 時，以明確的 namespace 探測回報 ``namespace_exists``。

空的 quota list 本身不能證明 namespace 不存在（#1678）。平台要「確認尚無 namespace 才保存
政策待首次開通」，所以 operator 另外做一次 ``kubectl get namespace``：只有明確的 NotFound
回 ``False``；Forbidden、逾時或其他失敗一律 ``None``（未知，不得據此跳過任何安全檢查）。
"""
import json
import subprocess
from unittest.mock import patch

from fastapi.testclient import TestClient
import pytest

from src.config import settings
from src.main import app

NS = 'paas-ws-test1679'
HARD = {'requests.cpu': '12', 'limits.cpu': '12', 'requests.memory': '24Gi',
        'limits.memory': '24Gi', 'requests.storage': '100Gi'}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, 'api_key', 'isolated-test-key')
    monkeypatch.setattr(settings, 'namespace_prefix', 'paas-ws-')
    return TestClient(app, headers={'X-API-Key': 'isolated-test-key'})


def _ok(stdout=''):
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr='')


def _fail(stderr):
    return subprocess.CompletedProcess([], 1, stdout='', stderr=stderr)


def _get(client, responses, include_managed=True):
    calls = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        result = responses[len(calls) - 1]
        if isinstance(result, Exception):
            raise result
        return result

    with patch('src.services.helm.subprocess.run', side_effect=fake_run):
        response = client.get('/api/namespaces/%s/quota' % NS,
                              params={'include_managed': include_managed})
    assert response.status_code == 200, response.text
    return response.json(), calls


@pytest.mark.parametrize('probe, expected', [
    (_ok('namespace/%s\n' % NS), True),
    (_fail('Error from server (NotFound): namespaces "%s" not found' % NS), False),
    (_fail('Error from server (Forbidden): namespaces "%s" is forbidden: User cannot get '
           'resource "namespaces"' % NS), None),
    (subprocess.TimeoutExpired(['kubectl'], 5), None),
])
def test_missing_managed_quota_probes_namespace_existence(client, probe, expected):
    report, calls = _get(client, [_ok(json.dumps({'items': []})), probe])
    managed = report['managed_quota']
    assert managed['available'] is False
    assert managed['reason'] == 'managed_quota_not_found'
    assert managed['namespace_exists'] is expected
    assert len(calls) == 2
    assert 'resourcequota' in calls[0]
    assert 'namespace' in calls[1] and NS in calls[1]


def test_found_managed_quota_means_namespace_exists_without_extra_probe(client):
    item = {'metadata': {'namespace': NS, 'name': NS + '-quota', 'uid': 'u', 'resourceVersion': '1'},
            'spec': {'hard': dict(HARD)}, 'status': {'hard': dict(HARD), 'used': dict(HARD)}}
    report, calls = _get(client, [_ok(json.dumps({'items': [item]}))])
    assert report['managed_quota']['available'] is True
    assert report['managed_quota']['namespace_exists'] is True
    assert len(calls) == 1


def test_unreadable_quota_does_not_probe_and_stays_unknown(client):
    report, calls = _get(client, [_fail('Error from server (Forbidden): resourcequotas')])
    assert report['managed_quota']['reason'] == 'quota_unreadable'
    assert report['managed_quota']['namespace_exists'] is None
    assert len(calls) == 1


def test_legacy_summary_without_include_managed_is_unchanged(client):
    report, calls = _get(client, [_ok(json.dumps({'items': []}))], include_managed=False)
    assert 'managed_quota' not in report
    assert len(calls) == 1
