"""#1679 integration：找不到受管 quota 時，回報 namespace 是否存在（真 kubectl）。

單元測試餵的是手寫的 kubectl 錯誤字串，看不到真叢集的兩件事：

1. ``kubectl get namespace`` 對不存在的 namespace 回的 stderr 形狀，``is_not_found_error``
   要真的認得它，才會回 ``namespace_exists: false``（平台據此判斷「確認尚無 namespace」）；
2. 存在、但沒有受管 ResourceQuota 的 namespace 要回 ``true``，不能和不存在混為一談。

權限不足（Forbidden）回 ``null`` 的分支需要另一個身分，由 #1679 的真 operator pod
（namespace-scoped ServiceAccount）另外驗證。namespace 由 fixture 收掉。
"""
import uuid

import pytest

from tests.namespace_scope import integration_namespace_prefix

pytestmark = pytest.mark.integration


def _managed(app_client, namespace):
    r = app_client.get(f"/api/namespaces/{namespace}/quota",
                       params={"include_managed": "true"})
    assert r.status_code == 200, r.text
    return r.json()["managed_quota"]


def test_missing_namespace_reports_namespace_absent(app_client):
    namespace = f"{integration_namespace_prefix()}nons{uuid.uuid4().hex[:8]}"

    managed = _managed(app_client, namespace)

    assert managed["available"] is False
    assert managed["reason"] == "managed_quota_not_found"
    assert managed["namespace_exists"] is False


def test_namespace_without_managed_quota_reports_namespace_present(app_client, test_namespace):
    managed = _managed(app_client, test_namespace)

    assert managed["available"] is False
    assert managed["reason"] == "managed_quota_not_found"
    assert managed["namespace_exists"] is True
