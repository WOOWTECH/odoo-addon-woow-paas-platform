"""#769 wire 契約守線：``PUT /api/tunnels/{tunnel_id}/config`` 收 JSON body。

這支測試存在的理由，是既有測試**結構上蓋不到**這條縫：

* operator 側 ``tests/`` 在本檔之前**沒有任何一支**打過 ``/{tunnel_id}/config``
  （全部 ``.put()`` 只打 ``/api/access/sync`` / ``/{id}/routes`` / ``/namespaces/*/quota``）。
* 平台側 ``src/tests/test_subscription_lifecycle.py`` 用 ``MagicMock()`` 當 client，
  斷言的是「``configure_tunnel`` 有沒有被呼叫、kwargs 對不對」——**HTTP 序列化那一層
  整個被 mock 掉了**，所以 client 把參數放在 body、endpoint 卻綁在 query 這件事，
  在那支測試裡永遠是綠的。

實跑取證（#769 第一輪，origin/develop @ 826f5f6d）：真的把 operator 跑起來、用平台側
真實的 ``PaaSOperatorClient.configure_tunnel()`` 送一次，得到

    HTTP 422
    {"detail":[{"type":"missing","loc":["query","hostname"],"msg":"Field required"},
               {"type":"missing","loc":["query","service_url"],"msg":"Field required"}]}

🔴 **判別式是 ``loc`` 不是那個數字**：FastAPI 抱怨的是 *query* 少了這兩個欄位——
它把裸 ``str`` 參數綁成 query param，而呼叫端送的是 JSON body。只看「422」沒有鑑別力
（少一個 path 參數、body 型別錯，都會是 422）。

同 router 的鄰居全部吃 request model（``POST /api/tunnels`` → ``TunnelCreateRequest``、
``PUT /{id}/routes`` → ``TunnelRoutesSyncRequest``），``/config`` 是唯一例外，所以本次
收斂成 body model 是回到既有形制，不是新增風格。

⚠ **刻意不做「body 與 query 都接受」**：實測 stg operator 近 14 天 13,172 行 access log，
非 ``/health`` 的來源只有平台 Odoo 一個 IP，``/config`` 只被打過 1 筆 ⇒ 沒有第三方在用
query 形態，雙接受只會讓契約表面積加倍。``test_rejects_query_param_form`` 就是這個決定
的守線。
"""
import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.main import app

TUNNEL_ID = "tun-769"
HOSTNAME = "ha-769.woowtech.io"
SERVICE_URL = "http_status:503"  # suspend 的招牌值
CONFIG_PATH = f"/api/tunnels/{TUNNEL_ID}/config"


@pytest.fixture
def client():
    """與 tests/test_tunnels.py 同形制（F-PC-4：真 key + 相符 header）。"""
    with patch("src.main.settings") as mock_settings:
        mock_settings.api_key = "test-api-key"
        mock_settings.namespace_prefix = "paas-ws-"
        yield TestClient(app, headers={"X-API-Key": "test-api-key"})


@pytest.fixture
def cf():
    """Cloudflare 這個**上游**是 mock 的；受測的 request 解析層完全沒被動過。"""
    with patch("src.api.tunnels.cloudflare_service") as mock_cf:
        mock_cf.api_token = "test-token"
        mock_cf.account_id = "test-account"
        mock_cf.configure_tunnel = AsyncMock(return_value=None)
        yield mock_cf


def _loc_pairs(response):
    """把 422 的 detail 壓成 [(in, field), ...]，用來斷言「它在抱怨哪一邊」。"""
    try:
        return [
            (d["loc"][0], d["loc"][-1])
            for d in response.json().get("detail", [])
            if isinstance(d, dict) and d.get("loc")
        ]
    except (ValueError, KeyError, TypeError):  # 非 JSON / 非 422 形狀
        return []


class TestTunnelConfigAcceptsJsonBody:
    """呼叫端（平台 Odoo client）送的就是 JSON body，端點必須收得下。"""

    def test_json_body_reaches_handler(self, cf, client):
        """RED 判準：本測試在修正前必須紅在 422 且 loc 指向 ``query``。"""
        response = client.put(
            CONFIG_PATH,
            json={"hostname": HOSTNAME, "service_url": SERVICE_URL},
        )

        # 失敗訊息刻意帶上 raw body：422 的 detail 會指名它在抱怨哪一邊，
        # 那才是判別式；只看 status code 分不出「契約破裂」與「路由打錯」。
        assert response.status_code == 200, (
            f"送 JSON body 被拒。raw response body: {response.text}\n"
            f"loc pairs: {_loc_pairs(response)}"
        )
        assert response.json() == {
            "message": f"Tunnel {TUNNEL_ID} configuration updated",
            "hostname": HOSTNAME,
            "service_url": SERVICE_URL,
        }
        cf.configure_tunnel.assert_awaited_once_with(
            tunnel_id=TUNNEL_ID,
            hostname=HOSTNAME,
            service_url=SERVICE_URL,
        )

    def test_body_is_not_bound_to_query(self, cf, client):
        """把 RED 的**理由**本身鎖起來，不只鎖 status code。

        沒有這一格的話，端點若哪天改成「required query + optional body」，
        上面那格會綠、契約卻仍然是壞的。
        """
        response = client.put(
            CONFIG_PATH,
            json={"hostname": HOSTNAME, "service_url": SERVICE_URL},
        )
        assert ("query", "hostname") not in _loc_pairs(response), (
            "endpoint 仍把 hostname 綁在 query（#769 的破裂形態）。"
            f" raw response body: {response.text}"
        )
        assert ("query", "service_url") not in _loc_pairs(response), (
            "endpoint 仍把 service_url 綁在 query（#769 的破裂形態）。"
            f" raw response body: {response.text}"
        )

    def test_missing_field_is_reported_against_body(self, cf, client):
        """缺欄位時，422 必須抱怨 ``body`` 而不是 ``query``。

        這是「已經收斂到 body model」的正向證據：光看「送全欄位會 200」還不夠，
        因為一個什麼都不驗的端點也會 200。
        """
        response = client.put(CONFIG_PATH, json={"hostname": HOSTNAME})

        assert response.status_code == 422, response.text
        assert ("body", "service_url") in _loc_pairs(response), (
            f"raw response body: {response.text}"
        )
        cf.configure_tunnel.assert_not_awaited()


class TestTunnelConfigSingleContract:
    """契約只有一份：不接受 query param 形態（刻意不做雙接受）。"""

    def test_rejects_query_param_form(self, cf, client):
        response = client.put(
            CONFIG_PATH,
            params={"hostname": HOSTNAME, "service_url": SERVICE_URL},
        )

        assert response.status_code == 422, (
            "query param 形態仍被接受 ⇒ 契約有兩份（雙接受）。"
            f" raw response body: {response.text}"
        )
        cf.configure_tunnel.assert_not_awaited()


class TestTunnelConfigOpenApiContract:
    """從**執行中 app 自己宣告的 OpenAPI** 鎖形狀。

    這一格不經請求，抓的是「宣告」而不是「行為」——兩者同時綠，才排除得掉
    「行為湊巧對、宣告仍錯」的中間態（例如 body 與 query 都收）。
    """

    def test_declares_request_body_and_no_required_query(self):
        spec = app.openapi()["paths"]["/api/tunnels/{tunnel_id}/config"]["put"]

        assert spec.get("requestBody"), (
            "端點沒有宣告 requestBody —— 裸 str 參數會被 FastAPI 綁成 query param。"
            f" 目前宣告：{json.dumps(spec.get('parameters'), ensure_ascii=False)}"
        )

        required_query = sorted(
            p["name"]
            for p in spec.get("parameters", [])
            if p.get("in") == "query" and p.get("required")
        )
        assert required_query == [], (
            f"端點仍有 required query param：{required_query}（#769 的破裂形態）"
        )

        # path 參數不該被順手改掉
        path_params = sorted(
            p["name"] for p in spec.get("parameters", []) if p.get("in") == "path"
        )
        assert path_params == ["tunnel_id"], path_params
