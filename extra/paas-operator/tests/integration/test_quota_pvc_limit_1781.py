"""#1781 integration：``pvc_limit`` 真的寫進 ResourceQuota、真的被 admission 執行（真 kubectl、真 apiserver）。

單元測試只看得到 operator 組出來的 manifest；這裡驗三件只有真叢集才有的事：

1. apiserver 收下平台送的字串（``"12"``），quota controller 把 status.hard 追上 spec，GET 回報
   ``supports_pvc_limit``；
2. 不帶 ``pvc_limit`` 的請求（舊平台）照舊寫 10——也就是平台不送時一定會降，這正是平台
   必須先看能力鍵、且 #1781 之前要整筆擋下的原因；
3. 寫進去的值真的被 quota admission 執行（超過上限的 PVC 建不起來）。

namespace 由 fixture 收掉。
"""
import json
import subprocess
import uuid

import pytest

from tests.integration.test_quota_guarded_write_1680 import (
    BODY, _managed, _put, _spec_hard, _token, _wait_available,
)
from tests.namespace_scope import integration_namespace_prefix, integration_storage_class

pytestmark = pytest.mark.integration


def test_pvc_limit_round_trips_through_the_apiserver(app_client, test_namespace):
    r = _put(app_client, test_namespace, pvc_limit="12", sync_token=_token(), expect_absent=True)
    assert r.status_code == 200, r.text
    assert r.json()["pvc_limit"] == "12"
    managed = _wait_available(app_client, test_namespace)
    assert managed["supports_pvc_limit"] is True
    assert managed["spec_hard"]["persistentvolumeclaims"] == "12"
    assert managed["hard"]["persistentvolumeclaims"] == "12"

    # 之後的推送照樣帶它就維持 12（不降額）
    r = _put(app_client, test_namespace, cpu_limit="12", pvc_limit="12", sync_token=_token(),
             expected_resource_version=managed["resource_version"])
    assert r.status_code == 200, r.text
    assert _spec_hard(test_namespace)["persistentvolumeclaims"] == "12"
    assert _spec_hard(test_namespace)["limits.cpu"] == "12"


def test_request_without_pvc_limit_still_writes_ten(app_client, test_namespace):
    """舊平台不送 ⇒ 與 #1781 之前相同：寫 10（叢集原本是 12 也一樣會被蓋成 10）。"""
    assert _put(app_client, test_namespace, pvc_limit="12", sync_token=_token(),
                expect_absent=True).status_code == 200
    planned = _wait_available(app_client, test_namespace)["resource_version"]
    r = _put(app_client, test_namespace, sync_token=_token(), expected_resource_version=planned)
    assert r.status_code == 200, r.text
    assert "pvc_limit" not in r.json()
    assert _spec_hard(test_namespace)["persistentvolumeclaims"] == "10"


def _create_pvc(namespace, name):
    spec = {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "1Gi"}},
            **integration_storage_class()}
    manifest = {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
                "metadata": {"name": name, "namespace": namespace}, "spec": spec}
    return subprocess.run(["kubectl", "apply", "-f", "-"], input=json.dumps(manifest),
                          capture_output=True, text=True)


def test_written_pvc_limit_is_enforced_by_quota_admission(app_client, test_namespace):
    assert _put(app_client, test_namespace, pvc_limit="1", sync_token=_token(),
                expect_absent=True).status_code == 200
    _wait_available(app_client, test_namespace)
    first = _create_pvc(test_namespace, "pvc-1781-a")
    assert first.returncode == 0, first.stderr
    second = _create_pvc(test_namespace, "pvc-1781-b")
    assert second.returncode != 0
    assert "exceeded quota" in second.stderr and "persistentvolumeclaims" in second.stderr


def test_namespace_create_path_writes_the_pvc_limit(app_client):
    namespace = f"{integration_namespace_prefix()}{uuid.uuid4().hex[:8]}"
    try:
        r = app_client.post("/api/namespaces", json=dict(
            BODY, name=namespace, pvc_limit="12", sync_token=_token(), expect_absent=True))
        assert r.status_code == 201, r.text
        assert r.json()["quota"] == "applied"
        assert r.json()["pvc_limit"] == "12"
        managed = _wait_available(app_client, namespace)
        assert managed["supports_pvc_limit"] is True
        assert managed["hard"]["persistentvolumeclaims"] == "12"

        # 前提不成立（重跑開通、quota 已存在）：沒有寫，就不回報 pvc_limit。
        stale = app_client.post("/api/namespaces", json=dict(
            BODY, name=namespace, pvc_limit="20", sync_token=_token(), expect_absent=True))
        assert stale.status_code == 201, stale.text
        assert stale.json()["quota"] == "conflict"
        assert "pvc_limit" not in stale.json()
        assert _spec_hard(namespace)["persistentvolumeclaims"] == "12"
        assert _managed(app_client, namespace)["supports_pvc_limit"] is True
    finally:
        subprocess.run(["kubectl", "delete", "namespace", namespace, "--wait=false"],
                       capture_output=True)
