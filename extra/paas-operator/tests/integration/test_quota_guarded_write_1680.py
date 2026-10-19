"""#1680 integration：ResourceQuota 的版本防護寫入（真 kubectl、真 apiserver）。

單元測試餵的是手寫的 kubectl stderr，看不到只有真叢集才有的三件事：

1. ``kubectl replace`` 帶 ``metadata.resourceVersion`` 時，apiserver 真的把它當 optimistic
   concurrency 前提——讀取之後被改過就 409，``(Conflict)`` 的 stderr 形狀要被 operator 認得；
2. 「舊請求晚到」：拿同一個 resourceVersion 規劃的兩筆請求，後送到的那筆一定落不下；
3. annotation 的 token 能被 GET 讀回，且 status 還在追 spec 時也讀得到。

namespace 由 fixture 收掉。
"""
from concurrent.futures import ThreadPoolExecutor
import json
import subprocess
import time
import uuid

import pytest

from tests.namespace_scope import integration_namespace_prefix

pytestmark = pytest.mark.integration

BODY = {'cpu_limit': '8', 'memory_limit': '8Gi', 'storage_limit': '100Gi'}


def _token():
    return uuid.uuid4().hex


def _managed(app_client, namespace):
    r = app_client.get(f"/api/namespaces/{namespace}/quota", params={"include_managed": "true"})
    assert r.status_code == 200, r.text
    return r.json()["managed_quota"]


def _spec_hard(namespace):
    out = subprocess.run(
        ["kubectl", "get", "resourcequota", f"{namespace}-quota", "-n", namespace, "-o", "json"],
        check=True, capture_output=True, text=True).stdout
    return json.loads(out)["spec"]["hard"]


def _put(app_client, namespace, **body):
    return app_client.put(f"/api/namespaces/{namespace}/quota", json=dict(BODY, **body))


def _wait_available(app_client, namespace, timeout=60):
    """quota controller 非同步把 status.hard 追上 spec；追上前 GET 回 status_pending。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        managed = _managed(app_client, namespace)
        if managed["available"]:
            return managed
        time.sleep(1)
    pytest.fail(f"managed quota 沒有收斂（最後 {managed!r}）")


def test_guarded_create_then_replace_round_trips_the_token(app_client, test_namespace):
    first = _token()
    r = _put(app_client, test_namespace, sync_token=first, expect_absent=True)
    assert r.status_code == 200, r.text
    assert r.json()["quota"] == "applied"
    managed = _wait_available(app_client, test_namespace)
    assert managed["sync_token"] == first

    second = _token()
    r = _put(app_client, test_namespace, cpu_limit="12", sync_token=second,
             expected_resource_version=managed["resource_version"])
    assert r.status_code == 200, r.text
    # status 還沒追上 spec 時（status_pending）token 也已讀得到
    assert _managed(app_client, test_namespace)["sync_token"] == second
    assert _spec_hard(test_namespace)["limits.cpu"] == "12"
    assert _spec_hard(test_namespace)["persistentvolumeclaims"] == "10"


def test_stale_resource_version_is_a_conflict_and_does_not_overwrite(app_client, test_namespace):
    assert _put(app_client, test_namespace, sync_token=_token(), expect_absent=True).status_code == 200
    planned = _wait_available(app_client, test_namespace)["resource_version"]

    # 新請求先落地（例如另一筆同步），之後以舊版本規劃的請求才晚到。
    newer = _put(app_client, test_namespace, cpu_limit="16", sync_token=_token(),
                 expected_resource_version=planned)
    assert newer.status_code == 200, newer.text
    late = _put(app_client, test_namespace, cpu_limit="10", sync_token=_token(),
                expected_resource_version=planned)
    assert late.status_code == 409, late.text
    assert late.json()["detail"] == "quota_conflict"
    assert _spec_hard(test_namespace)["limits.cpu"] == "16"


def test_two_requests_planned_on_the_same_version_only_one_lands(app_client, test_namespace):
    assert _put(app_client, test_namespace, sync_token=_token(), expect_absent=True).status_code == 200
    planned = _wait_available(app_client, test_namespace)["resource_version"]
    tokens = {"12": _token(), "14": _token()}

    def push(cpu):
        return cpu, _put(app_client, test_namespace, cpu_limit=cpu, sync_token=tokens[cpu],
                         expected_resource_version=planned)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = dict((cpu, r.status_code) for cpu, r in pool.map(push, tokens))
    assert sorted(results.values()) == [200, 409], results
    winner = next(cpu for cpu, code in results.items() if code == 200)
    assert _spec_hard(test_namespace)["limits.cpu"] == winner
    assert _managed(app_client, test_namespace)["sync_token"] == tokens[winner]


def test_expect_absent_on_an_existing_quota_is_a_conflict(app_client, test_namespace):
    assert _put(app_client, test_namespace, sync_token=_token(), expect_absent=True).status_code == 200
    r = _put(app_client, test_namespace, cpu_limit="4", sync_token=_token(), expect_absent=True)
    assert r.status_code == 409, r.text
    assert _spec_hard(test_namespace)["limits.cpu"] == "8"


def test_guarded_write_on_a_missing_quota_is_a_conflict(app_client, test_namespace):
    r = _put(app_client, test_namespace, sync_token=_token(), expected_resource_version="1")
    assert r.status_code == 409, r.text
    assert _managed(app_client, test_namespace)["reason"] == "managed_quota_not_found"


def test_legacy_unguarded_put_still_overwrites_after_a_guarded_write(app_client, test_namespace):
    """舊平台（不帶防護欄位）在新 operator 上照舊可用。"""
    assert _put(app_client, test_namespace, sync_token=_token(), expect_absent=True).status_code == 200
    r = _put(app_client, test_namespace, cpu_limit="20")
    assert r.status_code == 200, r.text
    assert "quota" not in r.json()
    assert _spec_hard(test_namespace)["limits.cpu"] == "20"


def test_namespace_create_path_uses_the_same_guarded_quota_write(app_client):
    namespace = f"{integration_namespace_prefix()}{uuid.uuid4().hex[:8]}"
    try:
        token = _token()
        r = app_client.post("/api/namespaces", json=dict(
            BODY, name=namespace, sync_token=token, expect_absent=True))
        assert r.status_code == 201, r.text
        assert r.json()["quota"] == "applied"
        managed = _wait_available(app_client, namespace)
        assert managed["sync_token"] == token

        # 重跑開通時拿到過期版本：基線照做（201），quota 不被覆寫、回報 conflict。
        stale = app_client.post("/api/namespaces", json=dict(
            BODY, name=namespace, cpu_limit="2", sync_token=_token(), expect_absent=True))
        assert stale.status_code == 201, stale.text
        assert stale.json()["quota"] == "conflict"
        assert _spec_hard(namespace)["limits.cpu"] == "8"
        assert _managed(app_client, namespace)["sync_token"] == token
    finally:
        subprocess.run(["kubectl", "delete", "namespace", namespace, "--wait=false"],
                       capture_output=True)
