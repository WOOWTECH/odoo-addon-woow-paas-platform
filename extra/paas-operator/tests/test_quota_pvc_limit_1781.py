"""#1781：ResourceQuota 的 ``persistentvolumeclaims`` 上限改由平台送來（選填 ``pvc_limit``）。

在 #1189（依 template 宣告計算 PVC 數）之前的止血：平台送 ``max(10, 叢集現值)``，operator 照寫。

* 帶 ``pvc_limit`` ⇒ manifest 用它，回應原樣回報 ``pvc_limit``（平台據此確認真的寫進去了——
  operator 滾動更新時 GET 與 PUT 可能落在不同版本的 pod）；
* 不帶 ⇒ 與 #1781 之前完全相同（``"10"``，回應沒有 ``pvc_limit``），舊平台不受影響；
* GET ``managed_quota`` 一律帶 ``supports_pvc_limit: True``：舊 operator 會靜默忽略未知欄位、
  照樣寫 10，平台只能靠這個鍵判斷能不能送（比照 #1680 的 ``sync_token`` 鍵）。
"""
import json
from unittest.mock import patch

import pytest

from src.services.helm import KubernetesService
from tests.test_quota_guarded_write_1680 import (  # noqa: F401 — client 是 pytest fixture
    BODY, CREATE_BODY, CONFLICT, NS, Kubectl, _fail, _ok, _post, _put, _quota_item, client,
)


def _hard(manifest):
    return manifest['spec']['hard']


def test_manifest_uses_the_given_pvc_limit_and_defaults_to_ten():
    given = KubernetesService._build_quota_manifest(NS, '8', '8Gi', '100Gi', pvc_limit='12')
    assert _hard(given)['persistentvolumeclaims'] == '12'
    default = KubernetesService._build_quota_manifest(NS, '8', '8Gi', '100Gi')
    assert _hard(default)['persistentvolumeclaims'] == '10'


def test_put_with_pvc_limit_writes_it_and_echoes_it(client):
    kubectl = Kubectl()
    response = _put(client, kubectl, dict(BODY, pvc_limit='12', sync_token='0123456789abcdef',
                                          expected_resource_version='41'))
    assert response.status_code == 200, response.text
    assert response.json()['pvc_limit'] == '12'
    [(_verb, manifest)] = kubectl.quota_writes()
    assert _hard(manifest)['persistentvolumeclaims'] == '12'
    # ephemeral 與版本防護欄位照舊
    assert _hard(manifest)['limits.ephemeral-storage'] == '80Gi'
    assert manifest['metadata']['resourceVersion'] == '41'


def test_put_without_pvc_limit_is_unchanged(client):
    kubectl = Kubectl()
    response = _put(client, kubectl, dict(BODY))
    assert response.status_code == 200, response.text
    assert 'pvc_limit' not in response.json()
    [(verb, manifest)] = kubectl.quota_writes()
    assert verb == 'apply'
    assert _hard(manifest)['persistentvolumeclaims'] == '10'


def test_unguarded_put_with_pvc_limit_also_writes_and_echoes_it(client):
    kubectl = Kubectl()
    response = _put(client, kubectl, dict(BODY, pvc_limit='12'))
    assert response.status_code == 200, response.text
    assert response.json()['pvc_limit'] == '12'
    [(_verb, manifest)] = kubectl.quota_writes()
    assert _hard(manifest)['persistentvolumeclaims'] == '12'


def test_namespace_create_with_pvc_limit_writes_it_and_echoes_it(client):
    kubectl = Kubectl()
    response = _post(client, kubectl, dict(CREATE_BODY, pvc_limit='12',
                                           sync_token='0123456789abcdef',
                                           expected_resource_version='41'))
    assert response.status_code == 201, response.text
    assert response.json()['pvc_limit'] == '12'
    [(_verb, manifest)] = kubectl.quota_writes()
    assert _hard(manifest)['persistentvolumeclaims'] == '12'


def test_namespace_create_without_pvc_limit_is_unchanged(client):
    kubectl = Kubectl()
    response = _post(client, kubectl, dict(CREATE_BODY))
    assert response.status_code == 201, response.text
    assert 'pvc_limit' not in response.json()
    [(_verb, manifest)] = kubectl.quota_writes()
    assert _hard(manifest)['persistentvolumeclaims'] == '10'


def test_namespace_create_quota_conflict_does_not_claim_the_pvc_limit(client):
    """quota 沒寫（前提不成立）就不能回報 pvc_limit，否則平台會以為已套用。"""
    kubectl = Kubectl({('replace', 'ResourceQuota'): _fail(CONFLICT)})
    response = _post(client, kubectl, dict(CREATE_BODY, pvc_limit='12',
                                           sync_token='0123456789abcdef',
                                           expected_resource_version='41'))
    assert response.status_code == 201, response.text
    assert response.json()['quota'] == 'conflict'
    assert 'pvc_limit' not in response.json()


@pytest.mark.parametrize('value', ['', '-1', '12; rm -rf', '1 2', '9' * 40, 12.5, 12])
def test_malformed_pvc_limit_is_rejected_before_any_write(client, value):
    kubectl = Kubectl()
    response = _put(client, kubectl, dict(BODY, pvc_limit=value))
    assert response.status_code == 422, response.text
    assert kubectl.quota_writes() == []


def _managed(client, run):
    with patch('src.services.helm.subprocess.run', side_effect=run):
        response = client.get('/api/namespaces/%s/quota' % NS, params={'include_managed': True})
    assert response.status_code == 200, response.text
    return response.json()['managed_quota']


def test_managed_quota_always_reports_pvc_limit_support(client):
    available = _managed(client, lambda *a, **k: _ok(json.dumps({'items': [_quota_item()]})))
    assert available['available'] is True
    assert available['supports_pvc_limit'] is True

    def absent(cmd, *a, **k):
        if 'namespace' in cmd:
            return _fail('Error from server (NotFound): namespaces "%s" not found' % NS)
        return _ok(json.dumps({'items': []}))
    not_found = _managed(client, absent)
    assert not_found['reason'] == 'managed_quota_not_found'
    assert not_found['supports_pvc_limit'] is True

    unreadable = _managed(client, lambda *a, **k: _fail('Error from server (Forbidden): nope'))
    assert unreadable['available'] is False
    assert unreadable['supports_pvc_limit'] is True


def test_kubectl_failure_on_a_pvc_write_stays_500(client):
    kubectl = Kubectl({('apply', 'ResourceQuota'): _fail('Error from server (Forbidden): no')})
    response = _put(client, kubectl, dict(BODY, pvc_limit='12'))
    assert response.status_code == 500
