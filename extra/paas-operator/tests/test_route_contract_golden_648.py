"""#648 (a)：`ReleaseInfo.route` 的 wire 契約 golden（防兩端 shape drift）。

operator 產 `route`，Odoo 的 `_expose_failed()` 讀它。兩端跨 repo 邊界，而 Odoo 那側
的測試若自己手寫回應 dict，釘住的就只是作者的信念——欄位名、`str, Enum` 的序列化
形式、`None` 欄位有沒有被丟掉，任何一項不同都會讓 Odoo 測試全綠而 production 斷。

守法與 `test_reconcile_schema_golden.py` 同型：一份 checked-in golden，operator 端
斷言「現在的序列化 == golden」，Odoo 端**直接讀同一份檔**當 fixture ⇒ 任一端改欄位，
一端必紅。

golden 由 `tests/generate_route_contract_golden.py` **打真的端點**產生（`TestClient`
→ `response.json()`），不是憑印象選 `model_dump()` 還是 `model_dump(mode="json")`。
實測結論記在這裡免得下一個人再猜一次：**FastAPI 走的是 `mode="json"`**——同一個
`ReleaseInfo`，`model_dump()['status']` 是 `<ReleaseStatus.DEPLOYED: 'deployed'>`
（enum 物件），`model_dump(mode='json')['status']` 與 HTTP body 都是 `'deployed'`。

重現：
    cd extra/paas-operator
    /opt/homebrew/bin/python3.11 tests/generate_route_contract_golden.py

Refs #648
"""
import json
from pathlib import Path

import pytest

from src.models.schemas import ReleaseInfo, RouteInfo, RouteStatus

GOLDEN = (Path(__file__).resolve().parents[3]
          / "src" / "tests" / "fixtures" / "route_contract_golden.json")


def _cases():
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_golden_exists_and_covers_every_state():
    """golden 必須涵蓋三態（含 disabled 的「沒有 route」）。

    少一格的 golden 會讓對應的 Odoo 測試沒有權威形狀可依，而那正是本檔要堵的縫。
    """
    cases = _cases()
    assert set(cases) == {
        "ok_dns_healthy", "ok_dns_failed", "failed_cf_error",
        "failed_no_services", "cloudflare_disabled",
    }, "golden 的情形集合變了——是新增了狀態，還是產生腳本的某條分支沒被走到？"


@pytest.mark.parametrize("case", [
    "ok_dns_healthy", "ok_dns_failed", "failed_cf_error", "failed_no_services",
])
def test_route_serializes_to_golden(case):
    """由 golden 建模型再序列化，逐欄位必須 == golden（防 wire drift）。"""
    route = _cases()[case]["route"]
    assert RouteInfo(**route).model_dump(mode="json") == route, (
        "RouteInfo 序列化 shape 偏離 golden（%s）→ operator↔Odoo 契約漂移。"
        "改了欄位就要重跑 tests/generate_route_contract_golden.py。" % case)


def test_release_info_round_trips_the_whole_payload():
    """整包 `ReleaseInfo` 也要能來回——route 是巢狀欄位，單獨驗它蓋不到外層。"""
    for case, body in _cases().items():
        assert ReleaseInfo(**body).model_dump(mode="json") == body, case


def test_disabled_case_has_no_route_key_value():
    """🔴 「沒開 Cloudflare」在 wire 上就是 `route: null`，**不是**某種失敗狀態。

    Odoo 端的 fail-closed 判準依賴這個事實：缺席不得當失敗（否則沒開 CF 的環境
    每次部署都翻 error）。這條把它釘成契約。
    """
    assert _cases()["cloudflare_disabled"]["route"] is None


def test_failed_states_are_distinguishable_from_ok():
    """三態在 wire 上真的分得開——這是整個 (a) 的前提。"""
    cases = _cases()
    assert cases["ok_dns_healthy"]["route"]["status"] == RouteStatus.OK.value
    assert cases["ok_dns_failed"]["route"]["dns_failed"] is True
    assert cases["failed_cf_error"]["route"]["status"] == RouteStatus.FAILED.value
    # 沒算出來的欄位是 null 而不是空字串——填 "" 會讓呼叫端分不出
    # 「沒算出來」與「算出來是空的」。
    assert cases["failed_no_services"]["route"]["service_url"] is None
