"""#769 wire 契約守線（腿 2／三腿，平台側）：client 真的把參數送在哪裡。

三腿分工——每一腿都放在「跑得到」的地方，不互相假裝：

  腿 1  行為・operator 側   `extra/paas-operator/tests/test_tunnel_config_wire_769.py`
                            對真 FastAPI app 送 JSON body ⇒ 2xx。fastapi 只在 operator
                            自己的 venv／image 裡，所以那一腿只能住在那裡。
  腿 2  行為・平台側        **本檔**。攔在 `requests` 真正送出之前，斷言 client 組出來
                            的 `PreparedRequest` 把 hostname / service_url 放在 JSON
                            body、query string 是空的。只需要 `requests`（Odoo image
                            內建）。
  腿 3  結構・跨層          `scripts/audit/repo-lint.py` 的 `tunnel-config-wire-shape`。
                            斷言 operator endpoint 的參數綁定形狀。

🔴 **腿 3 為什麼不能放在本檔**：`extra/paas-operator/` **不在 paas-odoo image 裡**
——`Dockerfile.dev` 是 `COPY src/ /mnt/extra-addons/woow_paas_platform/`，operator 是獨立
服務、有自己的 `extra/paas-operator/Dockerfile` 與自己的 CI。任何在 `src/tests/` 裡去讀
`extra/paas-operator/src/api/tunnels.py` 的測試，在 image 內跑會找不到那個檔；而找不到時
若寫成 skip，就落進 #544／#684 那個家族——**skipped 算進 testsRun 卻不出現在 summary**，
「沒跑」與「通過」的輸出完全相同。repo-lint 跑在 repo 上，`extra/` 一定在。

🔴 **本檔的 class 為什麼必須繼承 Odoo 的 TestCase**：`TagsSelector.check()` 在
`hasattr(test, 'test_tags')` 為 False 時直接 `return False`，而那個屬性是 `odoo.tests.common`
的 `MetaCase` 掛上去的——純 `unittest.TestCase` 沒有 ⇒ **整個 class 被靜默排除**，訊息只記在
DEBUG level，`0 failed, 0 error(s) of N tests` 看起來完全正常。這不是假設：本檔 merge 進
develop 之後，腿 2 從落地那天起一條都沒有執行過（#806）。基底取 `BaseCase` 而不是
`TransactionCase`——本檔不碰 ORM、不需要 cursor / env，要的只是 `MetaCase` 掛的那組 tag 屬性
（`test_tags` / `test_module` / `test_class`），那是「能被收集」的最弱條件。
⚠ 換基底會連帶把 `BaseCase` 的對外請求守衛帶進來，見下方 `_request_handler` 的說明。

## 本檔守的是哪一半

`_do_suspend` / `_do_resume` 唯一的出海口是
`services/paas_operator.py` 的 `configure_tunnel()`。#769 的修法落在 **operator 側**
（把裸 `str` 收斂成 request body model），依據是：唯一呼叫者送 body、同 router 每個變更型
端點都吃 body model、且 stg 近 14 天 access log 顯示沒有第三方在用 query 形態。

⇒ 本檔鎖的是**不要往另一邊修**：任何把 client 改成 `params=` 的「修法」會讓這裡變紅。

⚠ **本檔在修正前後都是綠的**，這是刻意的：它鎖的是本來就正確的那一側。它的鑑別力不靠
「修好之前紅」，而靠注入紅燈證明——把 `configure_tunnel` 的 `data=` 改成 `params=`，
本檔必須立刻變紅。那個對照跑過、輸出貼在 PR 描述裡。既有的
`test_subscription_lifecycle.py::test_smarthome_suspend` 用 `MagicMock()` 當 client，
HTTP 序列化整層被 mock 掉 ⇒ 那支測試對這件事永遠是綠的，本檔才是這一側唯一的守線。
"""
import json
from urllib.parse import parse_qs, urlparse

import requests

from odoo.tests.common import BaseCase

TUNNEL_ID = "tun-769"
HOSTNAME = "ha-769.woowtech.io"
SUSPEND_SERVICE_URL = "http_status:503"          # _do_suspend 的招牌值
RESUME_SERVICE_URL = "http://localhost:8123"     # _do_resume 的還原值
BASE_URL = "http://paas-operator:8000"
API_KEY = "test-api-key"

EXPECTED_PATH = "/api/tunnels/%s/config" % TUNNEL_ID


class _CapturingAdapter(requests.adapters.HTTPAdapter):
    """攔在 `requests` 真正開 socket 之前。

    🔴 這**不是** mock 掉序列化：`PreparedRequest` 是 `requests` 自己完整組好的成品
    ——URL（含 query string）、body bytes、header 全部已經定案，只差沒送出去。斷言
    對象就是那份成品，所以「client 把欄位放在哪一邊」這件事是被真的量到的，不是被
    假設的。相對地，用 `MagicMock()` 取代整個 client 會讓這一層根本不存在。
    """

    def __init__(self, sink):
        self._sink = sink
        super().__init__()

    def send(self, request, **kwargs):  # noqa: D102 - requests adapter 介面
        self._sink.append(request)
        response = requests.Response()
        response.status_code = 200
        response._content = b"{}"
        response.url = request.url
        response.request = request
        return response


class TestConfigureTunnelWireShape(BaseCase):
    """`PaaSOperatorClient.configure_tunnel()` 送出的 wire 形狀。"""

    @classmethod
    def _request_handler(cls, s, r, /, **kw):
        """把分派交還給 session 自己 mount 的 adapter（探針就掛在那裡）。

        🔴 這不是「放寬」測試：`BaseCase.setUpClass` 會 patch
        `requests.Session.send`，讓所有請求繞過 adapter 直接進這支 handler
        （非 localhost 一律 `BlockedRequest`）。也就是說**光換基底，本檔的
        `_CapturingAdapter` 會整層失效**——4 條全部 error 在
        `PaaSOperatorConnectionError`，而那個訊息看起來像 client 壞了。

        這裡只把「本檔自己掛上去的那個 adapter」還原成分派目標；不是它的
        （例如某條測試日後另外開了 session）仍交回 `BaseCase` 擋下，Odoo 的
        對外請求守衛在本 class 不被整個關掉。`_CapturingAdapter` 從不開
        socket，所以還原之後也不會有任何封包離開行程。
        """
        adapter = s.get_adapter(r.url)
        if isinstance(adapter, _CapturingAdapter):
            return adapter.send(r, **kw)
        return super()._request_handler(s, r, **kw)

    def setUp(self):
        super().setUp()
        # 與 test_paas_operator.py 同樣延後 import，避開 module loading 期的 import 問題。
        from ..services.paas_operator import PaaSOperatorClient

        self._client = PaaSOperatorClient(base_url=BASE_URL, api_key=API_KEY)
        self._sent = []
        adapter = _CapturingAdapter(self._sent)
        self._client._session.mount("http://", adapter)
        self._client._session.mount("https://", adapter)

    def _capture(self, **kwargs):
        """跑一次真實的 `configure_tunnel`，回傳它組出來的 PreparedRequest。"""
        self._client.configure_tunnel(**kwargs)
        self.assertEqual(
            len(self._sent), 1,
            "configure_tunnel 應該剛好送出一個請求，實際 %d 個" % len(self._sent),
        )
        return self._sent[0]

    def test_fields_are_in_json_body(self):
        prepared = self._capture(
            tunnel_id=TUNNEL_ID,
            hostname=HOSTNAME,
            service_url=SUSPEND_SERVICE_URL,
        )

        self.assertEqual(prepared.method, "PUT")
        self.assertEqual(urlparse(prepared.url).path, EXPECTED_PATH)
        # 先斷言 body 存在再解析：body 為 None 時（例如 client 被改成只送
        # `params=`）直接 json.loads 會炸成 TypeError，訊息說不出發生了什麼。
        self.assertIsNotNone(
            prepared.body,
            "請求沒有 body。client 若改成只送 query param，operator 端的"
            " request model 會收不到值——#769 的裁決是 client 維持送 body。"
            " 目前 query string=%r" % urlparse(prepared.url).query,
        )
        self.assertEqual(
            json.loads(prepared.body),
            {"hostname": HOSTNAME, "service_url": SUSPEND_SERVICE_URL},
            "client 送出的 JSON body 形狀變了。operator 端"
            " `update_tunnel_config` 吃的是 request body model，改這裡會直接破壞 wire。",
        )

    def test_fields_are_not_in_query_string(self):
        """另一半：欄位**不得**出現在 query string。

        沒有這一格的話，「body 對了」但同時又塞了一份 query 的寫法會是綠的——而那正是
        #769 想避免的「契約有兩份」。
        """
        prepared = self._capture(
            tunnel_id=TUNNEL_ID,
            hostname=HOSTNAME,
            service_url=RESUME_SERVICE_URL,
        )

        query = parse_qs(urlparse(prepared.url).query)
        self.assertEqual(
            query, {},
            "client 把參數送進了 query string（%r）。#769 的裁決是修 operator 側、"
            "client 維持送 body——往這個方向「修」會讓 operator 側的 body model 收不到值。"
            % query,
        )

    def test_content_type_matches_json_body(self):
        """本次 request 的 body 與 Content-Type 必須共同構成 JSON。

        不能只量 session 的預設 header：若 client 漂成 query 或 form body，header
        仍可能宣稱 application/json。這一格要在 body 不存在、不可解析、payload
        錯誤或 media type 不符時各自變紅。
        """
        prepared = self._capture(
            tunnel_id=TUNNEL_ID,
            hostname=HOSTNAME,
            service_url=SUSPEND_SERVICE_URL,
        )
        self.assertIsNotNone(
            prepared.body,
            "請求宣稱 JSON 卻沒有 body；client 可能把欄位改送 query param。",
        )
        try:
            payload = json.loads(prepared.body)
        except (TypeError, json.JSONDecodeError) as exc:
            self.fail(
                "請求宣稱 JSON，但 body 無法解析：%r（%s）"
                % (prepared.body, exc)
            )
        self.assertEqual(
            payload,
            {"hostname": HOSTNAME, "service_url": SUSPEND_SERVICE_URL},
            "本次 JSON request 的 payload 與 configure_tunnel 契約不符。",
        )
        self.assertEqual(
            prepared.headers.get("Content-Type"),
            "application/json",
            "body 是 JSON，但本次 request 的 Content-Type 不正確。",
        )

    def test_capture_adapter_would_notice_a_query_param_form(self):
        """探針自檢（陽性對照）。

        上面三格都是**否定性**斷言（「不在 query」「就是這兩個欄位」）。否定性結論必配
        陽性對照：如果 `_CapturingAdapter` 根本沒攔到、或 `PreparedRequest.url` 拿不到
        query，那三格會在 client 真的改成 `params=` 時**照樣綠**。這一格用同一支 session
        送一個已知帶 query 的請求，證明探針量得到 query 這件事。
        """
        self._client._request(
            "PUT",
            EXPECTED_PATH,
            params={"hostname": HOSTNAME, "service_url": SUSPEND_SERVICE_URL},
        )
        self.assertEqual(len(self._sent), 1)
        query = parse_qs(urlparse(self._sent[0].url).query)
        self.assertEqual(
            query,
            {"hostname": [HOSTNAME], "service_url": [SUSPEND_SERVICE_URL]},
            "探針量不到 query string ⇒ 上面幾格的「query 是空的」是假綠。",
        )
