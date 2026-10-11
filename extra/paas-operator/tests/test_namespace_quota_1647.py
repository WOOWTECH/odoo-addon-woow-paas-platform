"""#1647：讀 namespace ResourceQuota 的上限與已佔用量（``GET /api/namespaces/{name}/quota``）。

2026-10-10 正式站事故：一台服務的 CPU limit 被調到超過工作區 namespace 的 ``limits.cpu``
上限，新 pod 被 quota 拒絕、停了近六小時，而平台沒有任何地方看得到上限與已佔用量。
operator 原本只有建立／覆寫 quota 的端點；這裡守新增的「讀」：

* ``tightest_quota_usage``：從 ``summarize()`` 搬出來的迴圈——搬完後 #1348 的儲存配額檢查
  拿到的東西必須一字不差（``exhausted_resources`` 讀它的 ``_headroom``）；
* ``quota_report``：quantity → 數字與單位、ratio、帶 scope 的 quota 不算工作區上限；
* ``get_resource_quotas``：一次唯讀 kubectl、timeout 有轉傳；
* 端點：一律 200＋``available``／``reason``，只有 namespace 前綴不合才 400；讀不到（kubectl
  失敗、逾時、輸出不是 JSON）不得變成 4xx／5xx——工作區頁開頁時同步呼叫它。
"""
import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.services.helm import KubectlException, KubernetesService
from src.services.namespace_quota import (
    QUOTA_NOT_FOUND,
    QUOTA_UNREADABLE,
    quota_report,
    resource_unit,
    unavailable_report,
)
from src.services.storage_quota import (
    STORAGE_QUOTA_RESOURCES,
    exhausted_resources,
    parse_quantity,
    summarize,
    tightest_quota_usage,
)

NS = "paas-ws-test1647"
QUOTA_NAME = f"{NS}-quota"
CELL_KEYS = {"hard", "used", "hard_value", "used_value", "unit", "ratio", "quota"}


@pytest.fixture(autouse=True)
def _default_namespace_prefix(monkeypatch):
    """``validate_namespace`` 讀的是真的 settings；釘成預設值，不受執行環境的 env 影響。"""
    monkeypatch.setattr("src.config.settings.namespace_prefix", "paas-ws-")


def _quota(name, hard, used=None, **spec_extra):
    """一筆 ResourceQuota item；``used=None`` 表示 quota controller 還沒寫 status。"""
    item = {"metadata": {"name": name}, "spec": {"hard": dict(hard), **spec_extra}}
    if used is not None:
        item["status"] = {"hard": dict(hard), "used": dict(used)}
    return item


# operator ``_build_quota_manifest`` 寫進叢集的六個鍵（requests 與 limits 的 hard 同值）。
PLATFORM_QUOTA = _quota(
    QUOTA_NAME,
    {"requests.cpu": "12", "limits.cpu": "12", "requests.memory": "24Gi",
     "limits.memory": "24Gi", "requests.storage": "100Gi", "persistentvolumeclaims": "10"},
    {"requests.cpu": "2500m", "limits.cpu": "9500m", "requests.memory": "6Gi",
     "limits.memory": "18Gi", "requests.storage": "60Gi", "persistentvolumeclaims": "4"},
)


def _legacy_summarize_quotas(quota_items):
    """重構前 ``summarize()`` 裡的迴圈，逐字留在這裡當對照組（#1348 當時的寫法）。"""
    quotas = {}
    for item in quota_items or []:
        status = item.get("status") or {}
        hard = status.get("hard") or (item.get("spec") or {}).get("hard") or {}
        used = status.get("used") or {}
        name = (item.get("metadata") or {}).get("name", "")
        for resource in STORAGE_QUOTA_RESOURCES:
            hard_q = parse_quantity(hard.get(resource))
            if hard_q is None:
                continue
            used_q = parse_quantity(used.get(resource, "0")) or Decimal(0)
            current = quotas.get(resource)
            if current is None or hard_q - used_q < current["_headroom"]:
                quotas[resource] = {
                    "used": str(used.get(resource, "0")),
                    "hard": str(hard.get(resource)),
                    "quota": name,
                    "_headroom": hard_q - used_q,
                }
    return quotas


class TestTightestQuotaUsage:
    def test_none_returns_every_declared_resource(self):
        usage = tightest_quota_usage([PLATFORM_QUOTA])
        assert set(usage) == {
            "requests.cpu", "limits.cpu", "requests.memory", "limits.memory",
            "requests.storage", "persistentvolumeclaims",
        }
        assert usage["limits.cpu"] == {
            "used": "9500m", "hard": "12", "quota": QUOTA_NAME,
            "_headroom": Decimal("2.5"),
        }

    def test_resource_list_limits_the_result(self):
        usage = tightest_quota_usage([PLATFORM_QUOTA], ["limits.cpu", "pods"])
        assert set(usage) == {"limits.cpu"}, "清單外的鍵不回、清單內但 quota 沒宣告的也不回"

    def test_resources_may_be_a_one_shot_iterable(self):
        """給 generator 也要對每一筆 quota 都生效（不能第一筆就把它耗盡）。"""
        loose = _quota("loose", {"limits.cpu": "50"}, {"limits.cpu": "1"})
        tight = _quota("tight", {"limits.cpu": "4"}, {"limits.cpu": "3"})
        usage = tightest_quota_usage([loose, tight], (r for r in ["limits.cpu"]))
        assert usage["limits.cpu"]["quota"] == "tight"

    def test_tightest_quota_wins_per_resource(self):
        """admission 每個 quota 都要過 ⇒ 每個資源各自取剩餘最少的那一筆。"""
        a = _quota("a", {"limits.cpu": "50", "limits.memory": "8Gi"},
                   {"limits.cpu": "10", "limits.memory": "7Gi"})
        b = _quota("b", {"limits.cpu": "12", "limits.memory": "64Gi"},
                   {"limits.cpu": "10", "limits.memory": "7Gi"})
        usage = tightest_quota_usage([a, b])
        assert usage["limits.cpu"]["quota"] == "b"
        assert usage["limits.cpu"]["hard"] == "12"
        assert usage["limits.memory"]["quota"] == "a"
        assert usage["limits.memory"]["hard"] == "8Gi"

    def test_unparseable_hard_is_skipped(self):
        """看不懂＝不知道：略過那個鍵，不得當成 0（0 會被讀成「已用完」）。"""
        q = _quota("q", {"limits.cpu": "lots", "limits.memory": "8Gi"},
                   {"limits.cpu": "1", "limits.memory": "1Gi"})
        assert set(tightest_quota_usage([q])) == {"limits.memory"}

    def test_missing_status_used_counts_as_zero(self):
        fresh = _quota("q", {"limits.cpu": "8"})
        usage = tightest_quota_usage([fresh])
        assert usage["limits.cpu"]["used"] == "0"
        assert usage["limits.cpu"]["_headroom"] == Decimal(8)

    def test_empty_input(self):
        assert tightest_quota_usage([]) == {}
        assert tightest_quota_usage(None) == {}

    @pytest.mark.parametrize("items", [
        [],
        [PLATFORM_QUOTA],
        [_quota("fresh", {"persistentvolumeclaims": "10", "requests.storage": "100Gi"})],
        [_quota("loose", {"persistentvolumeclaims": "50"}, {"persistentvolumeclaims": "10"}),
         _quota("tight", {"persistentvolumeclaims": "10"}, {"persistentvolumeclaims": "10"})],
        [_quota("cpu-only", {"requests.cpu": "1"}, {"requests.cpu": "1"})],
        [_quota("odd", {"persistentvolumeclaims": "many", "requests.storage": "1Gi"},
                {"requests.storage": "2Gi"})],
    ])
    def test_summarize_quotas_unchanged_by_the_refactor(self, items):
        """#1348 的檢查吃 ``summarize()["quotas"]``（含 ``_headroom``）——搬迴圈不得改它。"""
        got = summarize(items, [])["quotas"]
        expected = _legacy_summarize_quotas(items)
        assert got == expected
        assert list(got) == list(expected), "鍵的順序也要相同"
        assert set(got) <= set(STORAGE_QUOTA_RESOURCES), "summarize 只看儲存那兩個資源"

    def test_summarize_literal_shape_and_exhaustion(self):
        full = _quota("q", {"persistentvolumeclaims": "10", "requests.storage": "100Gi",
                            "limits.cpu": "4"},
                      {"persistentvolumeclaims": "10", "requests.storage": "95Gi",
                       "limits.cpu": "4"})
        summary = summarize([full], [])
        assert summary["quotas"] == {
            "persistentvolumeclaims": {"used": "10", "hard": "10", "quota": "q",
                                       "_headroom": Decimal(0)},
            "requests.storage": {"used": "95Gi", "hard": "100Gi", "quota": "q",
                                 "_headroom": Decimal(5 * 1024 ** 3)},
        }
        assert exhausted_resources(summary) == ["persistentvolumeclaims"]


class TestQuotaReport:
    def test_values_and_units(self):
        report = quota_report(NS, [PLATFORM_QUOTA])
        assert report["namespace"] == NS
        assert report["available"] is True
        assert report["reason"] is None
        assert report["quotas"] == [QUOTA_NAME]
        res = report["resources"]

        cpu = res["limits.cpu"]
        assert cpu == {"hard": "12", "used": "9500m", "hard_value": 12.0, "used_value": 9.5,
                       "unit": "cores", "ratio": 0.7917, "quota": QUOTA_NAME}
        assert isinstance(cpu["hard_value"], float) and isinstance(cpu["used_value"], float)

        memory = res["limits.memory"]
        assert memory["hard_value"] == 25769803776
        assert memory["used_value"] == 19327352832
        assert isinstance(memory["hard_value"], int) and isinstance(memory["used_value"], int)
        assert (memory["unit"], memory["ratio"]) == ("bytes", 0.75)

        storage = res["requests.storage"]
        assert (storage["hard_value"], storage["used_value"]) == (107374182400, 64424509440)
        assert (storage["unit"], storage["ratio"]) == ("bytes", 0.6)

        pvcs = res["persistentvolumeclaims"]
        assert (pvcs["hard_value"], pvcs["used_value"]) == (10, 4)
        assert isinstance(pvcs["hard_value"], int) and isinstance(pvcs["used_value"], int)
        assert (pvcs["unit"], pvcs["ratio"]) == ("count", 0.4)

    def test_requests_and_limits_are_reported_separately(self):
        """#1639 起容器 requests 小於 limits：兩組鍵的 used 不同，不可互相代用。"""
        res = quota_report(NS, [PLATFORM_QUOTA])["resources"]
        assert res["requests.cpu"]["used_value"] == 2.5
        assert res["limits.cpu"]["used_value"] == 9.5

    def test_every_cell_has_exactly_the_contract_keys(self):
        report = quota_report(NS, [PLATFORM_QUOTA])
        assert report["resources"], "陽性對照：至少有一格"
        for resource, cell in report["resources"].items():
            assert set(cell) == CELL_KEYS, resource

    def test_report_does_not_leak_headroom_and_is_json_serialisable(self):
        report = quota_report(NS, [PLATFORM_QUOTA])
        text = json.dumps(report)  # Decimal 混進去這裡就會 raise
        assert "_headroom" not in text

    def test_resources_are_sorted_by_key(self):
        keys = list(quota_report(NS, [PLATFORM_QUOTA])["resources"])
        assert keys == sorted(keys)

    def test_ratio_is_rounded_to_four_places(self):
        q = _quota("q", {"limits.cpu": "3"}, {"limits.cpu": "1"})
        assert quota_report(NS, [q])["resources"]["limits.cpu"]["ratio"] == 0.3333

    def test_over_quota_ratio_is_reported_above_one(self):
        """調降上限後 used 可以大於 hard（k8s 不會回收既有 pod）——照實回，不夾到 1。"""
        q = _quota("q", {"limits.cpu": "12"}, {"limits.cpu": "16"})
        cell = quota_report(NS, [q])["resources"]["limits.cpu"]
        assert cell["ratio"] == 1.3333
        assert (cell["hard_value"], cell["used_value"]) == (12.0, 16.0)

    def test_zero_hard_has_no_ratio(self):
        q = _quota("q", {"persistentvolumeclaims": "0", "limits.cpu": "0"},
                   {"persistentvolumeclaims": "0", "limits.cpu": "0"})
        res = quota_report(NS, [q])["resources"]
        assert res["persistentvolumeclaims"]["ratio"] is None
        assert res["persistentvolumeclaims"]["hard_value"] == 0
        assert res["limits.cpu"]["ratio"] is None

    def test_millicores_keep_three_decimals(self):
        q = _quota("q", {"limits.cpu": "1500m"}, {"limits.cpu": "333m"})
        cell = quota_report(NS, [q])["resources"]["limits.cpu"]
        assert (cell["hard_value"], cell["used_value"]) == (1.5, 0.333)

    @pytest.mark.parametrize("resource,unit", [
        ("limits.cpu", "cores"),
        ("requests.cpu", "cores"),
        ("cpu", "cores"),
        ("requests.memory", "bytes"),
        ("limits.memory", "bytes"),
        ("requests.storage", "bytes"),
        ("limits.ephemeral-storage", "bytes"),
        ("requests.ephemeral-storage", "bytes"),
        ("longhorn.storageclass.storage.k8s.io/requests.storage", "bytes"),
        ("requests.hugepages-2Mi", "bytes"),
        ("persistentvolumeclaims", "count"),
        ("longhorn.storageclass.storage.k8s.io/persistentvolumeclaims", "count"),
        ("pods", "count"),
        ("count/deployments.apps", "count"),
        ("requests.nvidia.com/gpu", "count"),
    ])
    def test_resource_unit(self, resource, unit):
        assert resource_unit(resource) == unit

    def test_storageclass_scoped_storage_is_reported_in_bytes(self):
        key = "longhorn.storageclass.storage.k8s.io/requests.storage"
        q = _quota("q", {key: "50Gi"}, {key: "20Gi"})
        cell = quota_report(NS, [q])["resources"][key]
        assert (cell["unit"], cell["hard_value"], cell["used_value"]) == (
            "bytes", 50 * 1024 ** 3, 20 * 1024 ** 3)

    @pytest.mark.parametrize("items", [[], None])
    def test_no_items_is_quota_not_found(self, items):
        """namespace 不存在與沒有 quota，kubectl 都回空 items——同一個 reason。"""
        assert quota_report(NS, items) == {
            "namespace": NS, "available": False, "reason": QUOTA_NOT_FOUND,
            "quotas": [], "resources": {},
        }

    @pytest.mark.parametrize("spec_extra", [
        {"scopes": ["BestEffort"]},
        {"scopeSelector": {"matchExpressions": [
            {"scopeName": "PriorityClass", "operator": "In", "values": ["high"]}]}},
    ])
    def test_scoped_quota_is_not_a_workspace_limit(self, spec_extra):
        """帶 scope 的 quota 只計部分 pod：只有它時＝沒有工作區上限可顯示。"""
        scoped = _quota("scoped", {"limits.cpu": "1", "pods": "2"},
                        {"limits.cpu": "1", "pods": "2"}, **spec_extra)
        assert quota_report(NS, [scoped])["reason"] == QUOTA_NOT_FOUND

        both = quota_report(NS, [scoped, PLATFORM_QUOTA])
        assert both["available"] is True
        assert both["quotas"] == [QUOTA_NAME]
        assert both["resources"]["limits.cpu"]["hard"] == "12", "更緊的 scoped quota 不得勝出"
        assert "pods" not in both["resources"]

    def test_empty_scopes_list_is_not_scoped(self):
        q = _quota("q", {"limits.cpu": "4"}, {"limits.cpu": "1"}, scopes=[])
        assert quota_report(NS, [q])["available"] is True

    def test_quota_names_are_sorted_and_deduplicated(self):
        zed = _quota("zed", {"limits.cpu": "4", "limits.memory": "8Gi"},
                     {"limits.cpu": "1", "limits.memory": "1Gi"})
        abe = _quota("abe", {"requests.storage": "10Gi"}, {"requests.storage": "1Gi"})
        report = quota_report(NS, [zed, abe])
        assert report["quotas"] == ["abe", "zed"]

    def test_unreadable_hard_only_is_quota_not_found(self):
        q = _quota("q", {"limits.cpu": "lots"}, {"limits.cpu": "1"})
        assert quota_report(NS, [q])["reason"] == QUOTA_NOT_FOUND

    def test_unparseable_used_counts_as_zero(self):
        q = _quota("q", {"limits.cpu": "4"}, {"limits.cpu": "some"})
        cell = quota_report(NS, [q])["resources"]["limits.cpu"]
        assert (cell["used"], cell["used_value"], cell["ratio"]) == ("some", 0.0, 0.0)

    def test_unavailable_report_shape(self):
        assert unavailable_report(NS, QUOTA_UNREADABLE) == {
            "namespace": NS, "available": False, "reason": QUOTA_UNREADABLE,
            "quotas": [], "resources": {},
        }

    def test_reason_strings_are_the_platform_contract(self):
        """平台 ``src/services/workspace_quota.py`` 認這兩個字面值。"""
        assert (QUOTA_NOT_FOUND, QUOTA_UNREADABLE) == ("quota_not_found", "quota_unreadable")


class TestGetResourceQuotas:
    def test_one_read_only_kubectl_with_the_given_timeout(self):
        svc = KubernetesService()
        calls = []

        def fake_run(args, **kwargs):
            calls.append((args, kwargs))
            out = MagicMock()
            out.stdout = json.dumps({"items": [PLATFORM_QUOTA]})
            return out

        with patch.object(svc, "_run_command", side_effect=fake_run):
            items = svc.get_resource_quotas(NS, timeout=5)
        assert calls == [
            (["get", "resourcequota", "--namespace", NS, "-o", "json"], {"timeout": 5}),
        ]
        assert items == [PLATFORM_QUOTA]

    def test_default_timeout_is_forwarded_too(self):
        svc = KubernetesService()
        with patch.object(svc, "_run_command",
                          return_value=MagicMock(stdout='{"items": []}')) as run:
            assert svc.get_resource_quotas(NS) == []
        assert run.call_args.kwargs == {"timeout": 30}

    def test_empty_stdout_is_no_items(self):
        svc = KubernetesService()
        with patch.object(svc, "_run_command", return_value=MagicMock(stdout="")):
            assert svc.get_resource_quotas(NS) == []

    def test_foreign_namespace_never_reaches_kubectl(self):
        svc = KubernetesService()
        with patch.object(svc, "_run_command") as run:
            with pytest.raises(ValueError):
                svc.get_resource_quotas("kube-system")
        run.assert_not_called()

    def test_kubectl_failure_and_bad_json_propagate(self):
        svc = KubernetesService()
        boom = KubectlException(message="boom", command="kubectl get", stderr="forbidden")
        with patch.object(svc, "_run_command", side_effect=boom):
            with pytest.raises(KubectlException):
                svc.get_resource_quotas(NS)
        with patch.object(svc, "_run_command", return_value=MagicMock(stdout="not json")):
            with pytest.raises(json.JSONDecodeError):
                svc.get_resource_quotas(NS)

    def test_read_timeout_sits_under_the_platform_budget(self):
        """階梯：前端 15s ＞ 平台 connect 3＋read 8 ＞ 本常數（平台端另有測試驗上兩層）。"""
        assert KubernetesService.NS_QUOTA_READ_TIMEOUT_SECONDS == 5
        assert KubernetesService.NS_QUOTA_READ_TIMEOUT_SECONDS < 8


@pytest.fixture
def client():
    with patch("src.main.settings") as mock_settings:
        mock_settings.api_key = "test-api-key"
        mock_settings.namespace_prefix = "paas-ws-"
        yield TestClient(app, headers={"X-API-Key": "test-api-key"})


class TestNamespaceQuotaEndpoint:
    URL = f"/api/namespaces/{NS}/quota"

    @patch("src.api.namespaces.k8s_service")
    def test_returns_the_report(self, mock_k8s, client):
        mock_k8s.get_resource_quotas.return_value = [PLATFORM_QUOTA]
        resp = client.get(self.URL)
        assert resp.status_code == 200, resp.text
        assert resp.json() == quota_report(NS, [PLATFORM_QUOTA])
        assert resp.json()["resources"]["limits.cpu"]["used_value"] == 9.5
        mock_k8s.get_resource_quotas.assert_called_once_with(NS, timeout=5)

    @patch("src.api.namespaces.k8s_service")
    def test_endpoint_makes_no_other_cluster_call(self, mock_k8s, client):
        """純唯讀：除了那一次 get，不得碰 k8s_service 的其他方法。"""
        mock_k8s.get_resource_quotas.return_value = [PLATFORM_QUOTA]
        client.get(self.URL)
        assert [c[0] for c in mock_k8s.method_calls] == ["get_resource_quotas"]

    @patch("src.api.namespaces.k8s_service")
    def test_namespace_without_quota_is_200_quota_not_found(self, mock_k8s, client):
        mock_k8s.get_resource_quotas.return_value = []
        resp = client.get(self.URL)
        assert resp.status_code == 200, resp.text
        assert resp.json() == unavailable_report(NS, QUOTA_NOT_FOUND)

    @pytest.mark.parametrize("timed_out", [False, True])
    @patch("src.api.namespaces.k8s_service")
    def test_kubectl_failure_is_200_quota_unreadable(self, mock_k8s, timed_out, client):
        mock_k8s.get_resource_quotas.side_effect = KubectlException(
            message="kubectl command timed out after 5s" if timed_out else "boom",
            command="kubectl get resourcequota", stderr="forbidden", timed_out=timed_out)
        resp = client.get(self.URL)
        assert resp.status_code == 200, resp.text
        assert resp.json() == unavailable_report(NS, QUOTA_UNREADABLE)

    @patch("src.api.namespaces.k8s_service")
    def test_unparseable_kubectl_output_is_unreadable_not_400(self, mock_k8s, client):
        """``json.JSONDecodeError`` 是 ValueError 的子類：不得被當成「前綴不合」回 400。"""
        mock_k8s.get_resource_quotas.side_effect = json.JSONDecodeError("bad", "doc", 0)
        resp = client.get(self.URL)
        assert resp.status_code == 200, resp.text
        assert resp.json() == unavailable_report(NS, QUOTA_UNREADABLE)

    @patch("src.api.namespaces.k8s_service")
    def test_foreign_namespace_is_400_and_never_reaches_the_service(self, mock_k8s, client):
        resp = client.get("/api/namespaces/kube-system/quota")
        assert resp.status_code == 400, resp.text
        assert "paas-ws-" in resp.json()["detail"]
        mock_k8s.get_resource_quotas.assert_not_called()
        assert mock_k8s.method_calls == []

    @patch("src.api.namespaces.k8s_service")
    def test_missing_api_key_is_rejected(self, mock_k8s):
        with patch("src.main.settings") as mock_settings:
            mock_settings.api_key = "test-api-key"
            mock_settings.namespace_prefix = "paas-ws-"
            resp = TestClient(app).get(self.URL)
        assert resp.status_code == 401
        mock_k8s.get_resource_quotas.assert_not_called()

    @patch("src.api.namespaces.k8s_service")
    def test_put_on_the_same_path_still_overwrites(self, mock_k8s, client):
        mock_k8s.update_namespace_quota.return_value = {"message": "ok"}
        resp = client.put(self.URL, json={
            "cpu_limit": "8", "memory_limit": "8Gi", "storage_limit": "100Gi"})
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"message": "ok"}
        mock_k8s.update_namespace_quota.assert_called_once()
        mock_k8s.get_resource_quotas.assert_not_called()

    def test_a_path_without_a_get_handler_answers_405(self, client):
        """比本端點舊的 operator 只替這個路徑註冊了 PUT ⇒ GET 得到的是 405（不是 404）。

        用同 router 上只有 POST 的 ``baseline-netpol`` 釘住這個框架行為：平台把 405 當成
        「operator 太舊」，測試裡的 mock 狀態碼要對齊這個真實回值。
        """
        resp = client.get(f"/api/namespaces/{NS}/baseline-netpol")
        assert resp.status_code == 405
