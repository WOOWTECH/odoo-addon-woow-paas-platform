"""TS-3a: operator 唯讀帳號級 inventory 端點 `GET /api/reconcile/inventory`（方向 A）。

operator 只做**唯讀反向發現**：列帳號級 CF 資源（tunnels / access apps / reusable policies）
回 Odoo 當右集 ground truth。差集 / 分類 / 刪除全在 Odoo，operator 永不在此路徑動手。

守的不變式：
- **env-pin fail-CLOSED（R3）**：`PAAS_ENV` 未鎖定（不在 prod|stg）→ 503，**不**沿用 operator
  既有 ns-label 的 fail-OPEN 預設（避免「漏設」偽裝成「成功讀 prod」→ 跨環境誤判孤兒）。
- **completeness gate（R4/B.2）**：任一類 list-all 失敗 → `partial=True` + `failed_kinds` 記該類，
  且該類資源缺漏不混入 `resources`（下游不得把缺漏類當『無孤兒』）。絕不靜默回空清單。
- **唯讀**：端點只有 GET（無 action / 無 dry_run=False 路徑；delete-gate 另在
  `test_reconcile_delete_gate.py` 掃 `src/api/reconcile.py` 禁破壞動詞）。

設計 doc Phase A.1 / B.2、§機械守線。
"""
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.services.cloudflare import CloudflareException


@pytest.fixture
def client_env_stg():
    """已認證 client + operator env-pin 鎖在 'stg'（reconcile 路徑 fail-closed 通過）。"""
    with patch("src.main.settings") as main_settings, \
         patch("src.api.reconcile.settings") as rec_settings:
        main_settings.api_key = "test-api-key"
        main_settings.namespace_prefix = "paas-ws-"
        rec_settings.paas_env = "stg"
        # operator 配置的 shared tunnel id（CLOUDFLARE_TUNNEL_ID）。inventory 回拋它當
        # Odoo reconciler 標 shared tunnel 本體 protected 的權威來源。
        rec_settings.cloudflare_tunnel_id = "55953bfa-1111-2222-3333-444455556666"
        yield TestClient(app, headers={"X-API-Key": "test-api-key"})


class TestInventoryListsAccountResources:
    @patch("src.api.reconcile.cloudflare_service")
    def test_lists_tunnels_apps_policies(self, mock_cf, client_env_stg):
        """三類帳號級 list-all 都成功 → 全量回、partial=False、paas_env 回拋。"""
        mock_cf.list_all_tunnels = AsyncMock(
            return_value=[{"id": "t1", "name": "paas-stg-sm-aaa"}]
        )
        mock_cf.list_all_access_apps = AsyncMock(
            return_value=[{"id": "a1", "name": "paas-stg-sa-bbb"}]
        )
        mock_cf.list_all_reusable_policies = AsyncMock(
            return_value=[{"id": "p1", "name": "paas-stg-sa-bbb-allow"}]
        )

        resp = client_env_stg.get("/api/reconcile/inventory")

        assert resp.status_code == 200
        data = resp.json()
        assert data["paas_env"] == "stg"
        assert data["partial"] is False
        assert data["failed_kinds"] == []
        by_kind_id = {(r["kind"], r["id"]) for r in data["resources"]}
        assert ("tunnel", "t1") in by_kind_id
        assert ("access_app", "a1") in by_kind_id
        assert ("access_policy", "p1") in by_kind_id

    @patch("src.api.reconcile.cloudflare_service")
    def test_empty_account_is_not_partial(self, mock_cf, client_env_stg):
        """帳號真的零資源（list 成功回空）→ partial 仍 False（空≠失敗）。"""
        mock_cf.list_all_tunnels = AsyncMock(return_value=[])
        mock_cf.list_all_access_apps = AsyncMock(return_value=[])
        mock_cf.list_all_reusable_policies = AsyncMock(return_value=[])

        resp = client_env_stg.get("/api/reconcile/inventory")

        assert resp.status_code == 200
        data = resp.json()
        assert data["partial"] is False
        assert data["resources"] == []
        assert data["failed_kinds"] == []


class TestEnvPinFailClosed:
    @patch("src.api.reconcile.cloudflare_service")
    def test_unpinned_env_returns_503(self, mock_cf):
        """PAAS_ENV 未鎖定（'')→ 503 fail-CLOSED，**不**回 inventory（即使 CF 撈得到）。"""
        mock_cf.list_all_tunnels = AsyncMock(return_value=[{"id": "t1"}])
        with patch("src.main.settings") as main_settings, \
             patch("src.api.reconcile.settings") as rec_settings:
            main_settings.api_key = "test-api-key"
            rec_settings.paas_env = ""  # 漏設 / fail-open 預設值
            client = TestClient(app, headers={"X-API-Key": "test-api-key"})
            resp = client.get("/api/reconcile/inventory")
        assert resp.status_code == 503, (
            "reconcile 路徑必須對 PAAS_ENV fail-CLOSED：未鎖定 env 時回 503，"
            f"不得沿用 operator ns-label 的 fail-OPEN（got {resp.status_code}）。"
        )

    @patch("src.api.reconcile.cloudflare_service")
    def test_bogus_env_returns_503(self, mock_cf):
        """env 是非白名單值（如 'dev'）→ 同樣 503（只認 exactly prod|stg）。"""
        mock_cf.list_all_tunnels = AsyncMock(return_value=[])
        with patch("src.main.settings") as main_settings, \
             patch("src.api.reconcile.settings") as rec_settings:
            main_settings.api_key = "test-api-key"
            rec_settings.paas_env = "dev"
            client = TestClient(app, headers={"X-API-Key": "test-api-key"})
            resp = client.get("/api/reconcile/inventory")
        assert resp.status_code == 503

    @patch("src.api.reconcile.cloudflare_service")
    def test_cloudflare_not_called_when_env_unpinned(self, mock_cf):
        """env 未鎖定時 fail-closed 須在打 CF **之前**擋下（不浪費 / 不洩漏部分清單）。"""
        mock_cf.list_all_tunnels = AsyncMock(return_value=[])
        mock_cf.list_all_access_apps = AsyncMock(return_value=[])
        mock_cf.list_all_reusable_policies = AsyncMock(return_value=[])
        with patch("src.main.settings") as main_settings, \
             patch("src.api.reconcile.settings") as rec_settings:
            main_settings.api_key = "test-api-key"
            rec_settings.paas_env = ""
            client = TestClient(app, headers={"X-API-Key": "test-api-key"})
            client.get("/api/reconcile/inventory")
        mock_cf.list_all_tunnels.assert_not_called()
        mock_cf.list_all_access_apps.assert_not_called()
        mock_cf.list_all_reusable_policies.assert_not_called()


class TestCompletenessGate:
    @patch("src.api.reconcile.cloudflare_service")
    def test_one_category_failure_marks_partial(self, mock_cf, client_env_stg):
        """某類 list-all raise（403 / 中途失敗）→ partial=True、failed_kinds 記該類、
        該類資源缺漏不混入，其他類照常回（B.2 completeness gate）。"""
        mock_cf.list_all_tunnels = AsyncMock(return_value=[{"id": "t1", "name": "x"}])
        mock_cf.list_all_access_apps = AsyncMock(
            side_effect=CloudflareException("token scope 不足", status_code=403)
        )
        mock_cf.list_all_reusable_policies = AsyncMock(return_value=[{"id": "p1", "name": "y"}])

        resp = client_env_stg.get("/api/reconcile/inventory")

        assert resp.status_code == 200
        data = resp.json()
        assert data["partial"] is True, "一類失敗整輪須標 partial"
        assert "access_app" in data["failed_kinds"]
        kinds = {r["kind"] for r in data["resources"]}
        assert "access_app" not in kinds, "失敗類的資源不得混入（避免被當完整）"
        assert "tunnel" in kinds and "access_policy" in kinds, "成功類仍須回"

    @patch("src.api.reconcile.cloudflare_service")
    def test_transport_error_marks_partial_not_500(self, mock_cf, client_env_stg):
        """非 CloudflareException 的傳輸層失敗（timeout / connection reset）也必須走 completeness
        gate：標 partial、記 failed_kinds、該類缺漏，**不**整個 500 把已撈到的其他類一起丟掉。
        design B.2 明列『含 timeout』；repo 有 operator cold-start CPU throttle 撞 timeout 前科。"""
        import httpx

        mock_cf.list_all_tunnels = AsyncMock(return_value=[{"id": "t1", "name": "x"}])
        mock_cf.list_all_access_apps = AsyncMock(
            side_effect=httpx.ConnectError("connection refused")
        )
        mock_cf.list_all_reusable_policies = AsyncMock(return_value=[{"id": "p1", "name": "y"}])

        resp = client_env_stg.get("/api/reconcile/inventory")

        assert resp.status_code == 200, (
            "傳輸層失敗（httpx.ConnectError）必須降級成 partial，不得整個 500 "
            "（會丟掉已撈到的其他類 + 無 partial 訊號 → 下游無法區分『沒掃到』與『無孤兒』）"
        )
        data = resp.json()
        assert data["partial"] is True
        assert "access_app" in data["failed_kinds"]
        kinds = {r["kind"] for r in data["resources"]}
        assert "access_app" not in kinds, "失敗類不得混入"
        assert "tunnel" in kinds and "access_policy" in kinds, "成功類仍須保留"

    @patch("src.api.reconcile.cloudflare_service")
    def test_malformed_item_marks_partial_not_500(self, mock_cf, client_env_stg):
        """某類回傳缺 id 的畸形 item → 該類無法可靠物化 → 標 partial + 整類排除，
        不得 KeyError 整個 500 丟掉其他已撈到的類（item 建構須在 per-category try 內）。"""
        mock_cf.list_all_tunnels = AsyncMock(return_value=[{"id": "t1", "name": "x"}])
        mock_cf.list_all_access_apps = AsyncMock(return_value=[{"name": "no-id-here"}])
        mock_cf.list_all_reusable_policies = AsyncMock(return_value=[{"id": "p1", "name": "y"}])

        resp = client_env_stg.get("/api/reconcile/inventory")

        assert resp.status_code == 200
        data = resp.json()
        assert data["partial"] is True
        assert "access_app" in data["failed_kinds"]
        kinds = {r["kind"] for r in data["resources"]}
        assert "access_app" not in kinds
        assert "tunnel" in kinds and "access_policy" in kinds

    @patch("src.api.reconcile.cloudflare_service")
    def test_partial_category_with_late_bad_item_excludes_whole_category(self, mock_cf, client_env_stg):
        """同一類前面 item 好、後面 item 壞 → 整類排除（不得只回前半當完整）。
        守『item 建構進 local list、整類成功才 extend』而非邊建邊 append 全域 list。"""
        mock_cf.list_all_tunnels = AsyncMock(
            return_value=[{"id": "t-good", "name": "ok"}, {"name": "t-bad-no-id"}]
        )
        mock_cf.list_all_access_apps = AsyncMock(return_value=[{"id": "a1", "name": "y"}])
        mock_cf.list_all_reusable_policies = AsyncMock(return_value=[])

        resp = client_env_stg.get("/api/reconcile/inventory")

        assert resp.status_code == 200
        data = resp.json()
        assert data["partial"] is True
        assert "tunnel" in data["failed_kinds"]
        ids = {r["id"] for r in data["resources"]}
        assert "t-good" not in ids, "壞 item 所在的整類須排除，半套資料不得外洩成『完整』"
        assert "a1" in ids, "其他成功類仍須保留"

    @patch("src.api.reconcile.cloudflare_service")
    def test_all_categories_fail_marks_partial_empty(self, mock_cf, client_env_stg):
        """三類全失敗 → partial=True、resources 空、三類都進 failed_kinds（絕不靜默回 200 空當無孤兒）。"""
        mock_cf.list_all_tunnels = AsyncMock(side_effect=CloudflareException("boom", status_code=500))
        mock_cf.list_all_access_apps = AsyncMock(side_effect=CloudflareException("boom", status_code=500))
        mock_cf.list_all_reusable_policies = AsyncMock(side_effect=CloudflareException("boom", status_code=500))

        resp = client_env_stg.get("/api/reconcile/inventory")

        assert resp.status_code == 200
        data = resp.json()
        assert data["partial"] is True
        assert data["resources"] == []
        assert set(data["failed_kinds"]) == {"tunnel", "access_app", "access_policy"}


class TestInventoryReportsSharedTunnelId:
    """TS-4 S1：inventory 回 operator 配置的 shared tunnel id（權威來源）。

    Odoo reconciler 用它把 shared tunnel 本體（必出現在 list_all_tunnels 右集、不在
    SM/SA 左集）標 protected。**取代**對完整 32-char UUID 永不命中的 8-char 硬編 denylist
    （否則 shared tunnel 落回泛用『結構孤兒候選』→ 人照報表刪 = cf-ns-orphan 事故重演）。
    """

    @patch("src.api.reconcile.cloudflare_service")
    def test_inventory_includes_operator_shared_tunnel_id(self, mock_cf, client_env_stg):
        """正常路徑：回 settings.cloudflare_tunnel_id（與 CloudflareService 同源）。"""
        mock_cf.list_all_tunnels = AsyncMock(return_value=[])
        mock_cf.list_all_access_apps = AsyncMock(return_value=[])
        mock_cf.list_all_reusable_policies = AsyncMock(return_value=[])

        resp = client_env_stg.get("/api/reconcile/inventory")

        assert resp.status_code == 200
        data = resp.json()
        assert data.get("shared_tunnel_id") == "55953bfa-1111-2222-3333-444455556666", (
            "inventory 必須回 operator 配置的 shared tunnel id（完整 UUID），"
            "供 Odoo reconciler 標 shared tunnel 本體 protected"
        )

    @patch("src.api.reconcile.cloudflare_service")
    def test_shared_tunnel_id_reflects_config_not_hardcoded(self, mock_cf):
        """非 vacuity：換一個 tunnel id 配置 → 回值跟著變（證明讀 config 非寫死）。"""
        mock_cf.list_all_tunnels = AsyncMock(return_value=[])
        mock_cf.list_all_access_apps = AsyncMock(return_value=[])
        mock_cf.list_all_reusable_policies = AsyncMock(return_value=[])
        with patch("src.main.settings") as main_settings, \
             patch("src.api.reconcile.settings") as rec_settings:
            main_settings.api_key = "test-api-key"
            rec_settings.paas_env = "prod"
            rec_settings.cloudflare_tunnel_id = "328c3a28-9999-8888-7777-666655554444"
            client = TestClient(app, headers={"X-API-Key": "test-api-key"})
            resp = client.get("/api/reconcile/inventory")
        assert resp.status_code == 200
        assert resp.json()["shared_tunnel_id"] == "328c3a28-9999-8888-7777-666655554444"


class TestReadOnlyContract:
    @patch("src.api.reconcile.cloudflare_service")
    def test_inventory_is_get_only(self, mock_cf, client_env_stg):
        """唯讀契約：POST 同路徑 → 405（無 action endpoint）。"""
        resp = client_env_stg.post("/api/reconcile/inventory")
        assert resp.status_code == 405
