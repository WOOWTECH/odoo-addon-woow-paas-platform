"""#1680：ResourceQuota 的版本防護寫入（resourceVersion 條件寫入＋sync token）。

平台在同一個 workspace 鎖內先 GET 受管 quota、再 PUT／POST。只靠平台端序列化擋不住
「舊請求晚到」：前一筆請求逾時後，operator 端的 kubectl 可能晚一點才真的寫入，蓋掉後來
的新值。所以平台送出時帶 ``expected_resource_version``（GET 讀到的版本）與這次推送的
``sync_token``：

* operator 以 ``kubectl replace``（帶 ``metadata.resourceVersion``）寫入，物件在這之間被
  任何人改過就回 409 ``quota_conflict``，不覆寫；
* ``expect_absent``（物件尚不存在）改用 ``kubectl create``，已存在就是 409；
* token 寫進 annotation，GET 原樣回報 ``sync_token``——平台據此讀回「這次推送有沒有落地」，
  也據此判斷 operator 支不支援防護（舊 operator 的 GET 沒有這個鍵；舊 operator 會靜默忽略
  PUT 的未知欄位，所以不能用 PUT 有沒有報錯來判斷）。

沒帶這些欄位的請求維持原本的 ``kubectl apply``（舊平台相容）。
"""
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
import pytest
import yaml

from src.config import settings
from src.main import app

NS = 'paas-ws-test1680'
TOKEN_KEY = 'woow.tw/quota-sync-token'
HARD = {'requests.cpu': '12', 'limits.cpu': '12', 'requests.memory': '24Gi',
        'limits.memory': '24Gi', 'requests.storage': '100Gi'}
BODY = {'cpu_limit': '16', 'memory_limit': '24Gi', 'storage_limit': '100Gi',
        'ephemeral_storage_request': '4Gi', 'ephemeral_storage_limit': '80Gi'}
CONFLICT = ('Error from server (Conflict): error when replacing "q.yaml": Operation cannot be '
            'fulfilled on resourcequotas "%s-quota": the object has been modified; please apply '
            'your changes to the latest version and try again' % NS)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, 'api_key', 'isolated-test-key')
    monkeypatch.setattr(settings, 'namespace_prefix', 'paas-ws-')
    return TestClient(app, headers={'X-API-Key': 'isolated-test-key'})


def _ok(stdout=''):
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr='')


def _fail(stderr):
    return subprocess.CompletedProcess([], 1, stdout='', stderr=stderr)


class Kubectl:
    """kubectl 邊界的假實作：記錄每個指令與 ``-f`` 檔案內容，依（動詞, kind）回應。"""

    def __init__(self, answers=None):
        self.calls = []
        self.answers = answers or {}

    def __call__(self, cmd, *args, **kwargs):
        cmd = list(cmd)
        manifest = None
        if '-f' in cmd:
            manifest = yaml.safe_load(Path(cmd[cmd.index('-f') + 1]).read_text())
        self.calls.append((cmd, manifest))
        kind = (manifest or {}).get('kind')
        verb = cmd[1] if len(cmd) > 1 else ''
        answer = self.answers.get((verb, kind))
        if answer is not None:
            return answer
        return _ok('{}' if verb == 'get' else '')

    def quota_writes(self):
        return [(cmd[1], manifest) for cmd, manifest in self.calls
                if manifest and manifest.get('kind') == 'ResourceQuota']


def _quota_item(annotations=None, resource_version='41'):
    metadata = {'namespace': NS, 'name': NS + '-quota', 'uid': 'u', 'resourceVersion': resource_version}
    if annotations is not None:
        metadata['annotations'] = annotations
    return {'metadata': metadata, 'spec': {'hard': dict(HARD)},
            'status': {'hard': dict(HARD), 'used': dict(HARD)}}


def _get_managed(client, item):
    with patch('src.services.helm.subprocess.run',
               side_effect=lambda *a, **k: _ok(json.dumps({'items': [item]}))):
        response = client.get('/api/namespaces/%s/quota' % NS, params={'include_managed': True})
    assert response.status_code == 200, response.text
    return response.json()['managed_quota']


def test_managed_quota_reports_sync_token_so_the_platform_can_detect_the_guard(client):
    """鍵一定存在（舊 operator 沒有這個鍵）；沒有 annotation 時值為 None。"""
    managed = _get_managed(client, _quota_item())
    assert 'sync_token' in managed and managed['sync_token'] is None
    managed = _get_managed(client, _quota_item({TOKEN_KEY: 'a1b2c3d4e5f60718'}))
    assert managed['sync_token'] == 'a1b2c3d4e5f60718'
    assert managed['resource_version'] == '41'


def test_sync_token_is_reported_even_while_quota_status_lags_the_spec(client):
    """推送後 status.hard 追上 spec 前（status_pending）也要讀得到 token：讀回靠它。"""
    item = _quota_item({TOKEN_KEY: 'feedface00000001'})
    item['spec']['hard']['limits.cpu'] = '16'
    managed = _get_managed(client, item)
    assert managed['available'] is False
    assert managed['reason'] == 'managed_quota_status_pending'
    assert managed['sync_token'] == 'feedface00000001'


def _put(client, kubectl, body):
    with patch('src.services.helm.subprocess.run', side_effect=kubectl):
        return client.put('/api/namespaces/%s/quota' % NS, json=body)


def test_guarded_put_replaces_only_the_expected_version_and_stamps_the_token(client):
    kubectl = Kubectl()
    response = _put(client, kubectl, dict(BODY, sync_token='0123456789abcdef',
                                          expected_resource_version='41'))
    assert response.status_code == 200, response.text
    assert response.json()['quota'] == 'applied'
    [(verb, manifest)] = kubectl.quota_writes()
    assert verb == 'replace'
    assert manifest['metadata']['resourceVersion'] == '41'
    assert manifest['metadata']['annotations'][TOKEN_KEY] == '0123456789abcdef'
    assert manifest['spec']['hard']['limits.cpu'] == '16'
    assert manifest['spec']['hard']['persistentvolumeclaims'] == '10'


@pytest.mark.parametrize('stderr', [
    CONFLICT,
    'Error from server (NotFound): error when replacing "q.yaml": resourcequotas '
    '"%s-quota" not found' % NS,
])
def test_guarded_put_on_a_changed_or_missing_object_is_a_conflict_not_an_overwrite(client, stderr):
    kubectl = Kubectl({('replace', 'ResourceQuota'): _fail(stderr)})
    response = _put(client, kubectl, dict(BODY, sync_token='0123456789abcdef',
                                          expected_resource_version='41'))
    assert response.status_code == 409, response.text
    assert response.json()['detail'] == 'quota_conflict'
    assert [verb for verb, _m in kubectl.quota_writes()] == ['replace']


def test_guarded_put_expecting_absence_creates_and_conflicts_if_it_exists(client):
    kubectl = Kubectl()
    response = _put(client, kubectl, dict(BODY, sync_token='0123456789abcdef', expect_absent=True))
    assert response.status_code == 200, response.text
    [(verb, manifest)] = kubectl.quota_writes()
    assert verb == 'create'
    assert 'resourceVersion' not in manifest['metadata']

    kubectl = Kubectl({('create', 'ResourceQuota'): _fail(
        'Error from server (AlreadyExists): error when creating "q.yaml": resourcequotas '
        '"%s-quota" already exists' % NS)})
    response = _put(client, kubectl, dict(BODY, sync_token='0123456789abcdef', expect_absent=True))
    assert response.status_code == 409
    assert response.json()['detail'] == 'quota_conflict'


def test_other_guarded_write_failures_stay_500(client):
    kubectl = Kubectl({('replace', 'ResourceQuota'): _fail(
        'Error from server (Forbidden): resourcequotas is forbidden')})
    response = _put(client, kubectl, dict(BODY, sync_token='0123456789abcdef',
                                          expected_resource_version='41'))
    assert response.status_code == 500


def test_unguarded_put_keeps_the_legacy_apply(client):
    kubectl = Kubectl()
    response = _put(client, kubectl, dict(BODY))
    assert response.status_code == 200, response.text
    [(verb, manifest)] = kubectl.quota_writes()
    assert verb == 'apply'
    assert 'annotations' not in manifest['metadata']
    assert 'resourceVersion' not in manifest['metadata']


@pytest.mark.parametrize('extra', [
    {'sync_token': '0123456789abcdef'},  # 有 token 卻沒有前提
    {'expected_resource_version': '41'},  # 有前提卻沒有 token
    {'sync_token': '0123456789abcdef', 'expected_resource_version': '41', 'expect_absent': True},
    {'sync_token': 'NOT A TOKEN!', 'expected_resource_version': '41'},
    {'sync_token': '0123456789abcdef', 'expected_resource_version': '41; rm -rf'},
])
def test_incomplete_or_malformed_guard_is_rejected_before_any_write(client, extra):
    kubectl = Kubectl()
    response = _put(client, kubectl, dict(BODY, **extra))
    assert response.status_code == 422, response.text
    assert kubectl.quota_writes() == []


BASELINE_STEPS = ('ensure_registry_secret', 'ensure_default_sa_image_pull_secret',
                  'apply_pod_security', '_apply_baseline_netpol')


def _post(client, kubectl, body):
    """namespace 基線（pull secret、SA、PSA、NetworkPolicy）各有自己的測試；這裡只記錄有沒有做。"""
    from contextlib import ExitStack
    from src.services.helm import KubernetesService
    kubectl.baseline = []
    with ExitStack() as stack:
        stack.enter_context(patch('src.services.helm.subprocess.run', side_effect=kubectl))
        for step in BASELINE_STEPS:
            stack.enter_context(patch.object(
                KubernetesService, step,
                lambda self, *a, _step=step, **k: kubectl.baseline.append(_step)))
        return client.post('/api/namespaces', json=body)


CREATE_BODY = {'name': NS, 'workspace_id': 7, **BODY}


def test_guarded_namespace_create_uses_the_same_conditional_quota_write(client):
    kubectl = Kubectl()
    response = _post(client, kubectl, dict(CREATE_BODY, sync_token='0123456789abcdef',
                                           expected_resource_version='41'))
    assert response.status_code == 201, response.text
    assert response.json()['quota'] == 'applied'
    [(verb, manifest)] = kubectl.quota_writes()
    assert verb == 'replace'
    assert manifest['metadata']['annotations'][TOKEN_KEY] == '0123456789abcdef'


def test_namespace_create_quota_conflict_still_finishes_the_namespace_baseline(client):
    """quota 衝突不能讓 namespace 少了 pull secret／NetworkPolicy：基線照做，回報 conflict。"""
    kubectl = Kubectl({('replace', 'ResourceQuota'): _fail(CONFLICT)})
    response = _post(client, kubectl, dict(CREATE_BODY, sync_token='0123456789abcdef',
                                           expected_resource_version='41'))
    assert response.status_code == 201, response.text
    assert response.json()['quota'] == 'conflict'
    assert kubectl.baseline == list(BASELINE_STEPS)


def test_unguarded_namespace_create_keeps_the_legacy_apply(client):
    kubectl = Kubectl()
    response = _post(client, kubectl, dict(CREATE_BODY))
    assert response.status_code == 201, response.text
    assert 'quota' not in response.json()
    [(verb, _manifest)] = kubectl.quota_writes()
    assert verb == 'apply'
