"""#1431：從未正常運作過的服務刪除時，孤兒 PVC／DataVolume 只保留 1 天。

刪除服務後，PVC 依 #317 標 ``paas.woowtech.io/orphaned-at`` 保留（正式站 90 天），
期間照樣佔用 Longhorn 排程額度（#1415 事件）。從沒 running 過的服務沒有租戶資料要保護，
平台刪除時帶 ``?grace_period_days=1``，operator 用它標 ``grace-period-days``；chart 的
pvc-cleanup cron 讀每顆 PVC／DV 自己的標籤，不必改。

守的事：

* 帶合法值（``1 ≤ n ≤ PVC_GRACE_PERIOD_DAYS``）⇒ PVC 與 DataVolume 都標那個值；
* 沒帶（舊平台）⇒ 照舊用預設，行為完全不變；
* 非整數／0／負數／超過預設 ⇒ 忽略、用預設，**不回 422**（只能縮短、不能延長；
  刪除不能因為參數寫壞而刪不掉）；
* 純函式 ``resolve_orphan_grace_period_days`` 的邊界。
"""
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.api.releases import resolve_orphan_grace_period_days
from src.main import app

NS = "paas-ws-test1431"
GRACE_LABEL = "paas.woowtech.io/grace-period-days"


@pytest.fixture
def client():
    with patch("src.main.settings") as mock_settings:
        mock_settings.api_key = "test-api-key"
        mock_settings.namespace_prefix = "paas-ws-"
        yield TestClient(app, headers={"X-API-Key": "test-api-key"})


@pytest.fixture
def default_90():
    """正式站的預設（chart config.pvcGracePeriodDays＝90）。"""
    with patch("src.api.releases.settings.pvc_grace_period_days", 90):
        yield 90


@pytest.fixture
def teardown_mocks():
    with patch("src.api.releases.helm_service") as helm, \
            patch("src.api.releases.k8s_service") as k8s, \
            patch("src.api.releases.cloudflare_service") as cf:
        helm.uninstall.return_value = {"message": "Release uninstalled"}
        k8s.list_pvcs.return_value = ["svc-x-data"]
        k8s.has_datavolume_crd.return_value = True
        k8s.list_datavolumes.return_value = ["svc-x-root"]
        k8s.label_datavolumes.side_effect = lambda ns, names, labels: names
        cf.delete_route = AsyncMock(return_value=True)
        yield k8s


def _labels(k8s):
    pvc_labels = k8s.label_pvcs.call_args[0][2]
    dv_labels = k8s.label_datavolumes.call_args[0][2]
    return pvc_labels, dv_labels


def test_never_running_service_gets_one_day(client, default_90, teardown_mocks):
    r = client.delete(f"/api/releases/{NS}/svc-x?subdomain=paas-cs-x&grace_period_days=1")
    assert r.status_code == 200, r.text
    pvc, dv = _labels(teardown_mocks)
    assert pvc[GRACE_LABEL] == "1"
    assert dv[GRACE_LABEL] == "1", "VM 的磁碟在 DataVolume 上，要跟 PVC 同一個保留期"
    assert pvc["paas.woowtech.io/orphaned-at"].isdigit()


def test_without_the_parameter_the_default_is_unchanged(client, default_90, teardown_mocks):
    """舊平台不帶參數：行為與 #1431 之前完全相同。"""
    r = client.delete(f"/api/releases/{NS}/svc-x?subdomain=paas-cs-x")
    assert r.status_code == 200, r.text
    pvc, dv = _labels(teardown_mocks)
    assert pvc[GRACE_LABEL] == "90"
    assert dv[GRACE_LABEL] == "90"


@pytest.mark.parametrize("raw", ["0", "-1", "91", "365", "abc", "1.5", "", " "])
def test_invalid_or_out_of_range_values_fall_back_without_422(
    client, default_90, teardown_mocks, raw
):
    r = client.delete(f"/api/releases/{NS}/svc-x", params={"grace_period_days": raw})
    assert r.status_code == 200, f"參數寫壞不能讓刪除失敗：{raw!r} → {r.status_code} {r.text}"
    pvc, _dv = _labels(teardown_mocks)
    assert pvc[GRACE_LABEL] == "90", f"{raw!r} 必須退回預設"


def test_a_value_equal_to_the_default_is_accepted(client, default_90, teardown_mocks):
    r = client.delete(f"/api/releases/{NS}/svc-x?grace_period_days=90")
    assert r.status_code == 200
    assert _labels(teardown_mocks)[0][GRACE_LABEL] == "90"


def test_release_not_found_is_still_404(client, default_90):
    """參數不改變既有錯誤語意（平台把 404 當「已經不在」）。"""
    from src.services.helm import HelmException

    with patch("src.api.releases.helm_service") as helm:
        helm.uninstall.side_effect = HelmException(
            message="uninstall failed", command="helm uninstall",
            stderr="Error: uninstall: Release not loaded: svc-x: release: not found")
        r = client.delete(f"/api/releases/{NS}/svc-x?grace_period_days=1")
    assert r.status_code == 404


@pytest.mark.parametrize("raw, expected", [
    (None, 90), ("", 90), ("1", 1), ("7", 7), ("90", 90), ("91", 90), ("0", 90),
    ("-3", 90), ("x", 90), (" 2 ", 2),
])
def test_resolve_orphan_grace_period_days(default_90, raw, expected):
    assert resolve_orphan_grace_period_days(raw, NS, "svc-x") == expected


def test_a_dev_default_shorter_than_one_request_is_respected():
    """stg／dev 預設 7 天：1 仍合法；8 超過預設 ⇒ 用 7（不能延長）。"""
    with patch("src.api.releases.settings.pvc_grace_period_days", 7):
        assert resolve_orphan_grace_period_days("1", NS, "svc-x") == 1
        assert resolve_orphan_grace_period_days("8", NS, "svc-x") == 7
