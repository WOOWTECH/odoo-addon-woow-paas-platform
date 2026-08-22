"""Unit-suite 共用 fixture。

#423：``_ensure_image_pull_secrets`` 現在除了改 values，還會呼叫
``k8s_service.ensure_default_sa_image_pull_secret`` —— 那是一個**真的** kubectl。
單元測試絕不能對外連線：開發機的 current-context 很可能正指著共用叢集
（woow-k3s），而且沒有叢集時每次呼叫都要付 NotFound 重試視窗的等待
（實測：加上這個 seam 之前，tests/test_api.py 從 <1s 變成 81s）。

所以這裡統一把那個 seam 換成 mock。需要斷言「這條路徑真的收斂了 SA」的測試，
把 ``sa_pull_secret_convergence`` 收進參數即可拿到 mock；整組 ``k8s_service``
被 ``@patch`` 掉的測試也不受影響（那層 MagicMock 直接蓋過本 fixture）。

integration 測試要打真叢集，用 marker 明確排除。

#841：``block_real_outbound`` 在 transport 層擋掉所有真實 httpx 出網，
理由與守線形狀見該 fixture 的 docstring。
"""
from unittest.mock import patch

import httpx
import pytest


@pytest.fixture(autouse=True)
def sa_pull_secret_convergence(request):
    """擋住 unit test 走到真 kubectl；回傳 mock 供需要的測試斷言。"""
    if request.node.get_closest_marker("integration"):
        yield None
        return

    with patch(
        "src.api.releases.k8s_service.ensure_default_sa_image_pull_secret",
        return_value=True,
    ) as mock_ensure:
        yield mock_ensure


# ==================== #841 真實 outbound 攔截 ====================

#: 唯一的例外條件。用 **marker** 而不是路徑判斷是有理由的，見
#: ``block_real_outbound`` docstring 的「為什麼是 marker」。
OUTBOUND_EXEMPT_MARKER = "integration"

#: 攔截訊息的固定前綴。守線自己的測試用它做 ``pytest.raises(match=...)``，
#: 所以這是**契約**的一部分，改動要同步改 ``tests/test_outbound_guard.py``。
OUTBOUND_BLOCKED_PREFIX = "REAL OUTBOUND ATTEMPTED"


class RealOutboundAttempted(RuntimeError):
    """單元測試企圖對外發出真實網路請求。

    刻意繼承 ``RuntimeError`` 而不是 ``Exception``：被測碼裡有多處
    ``except CloudflareException`` 與 ``except httpx.HTTPError`` 的寬鬆補捉，
    本例外不屬於任何一支，所以不會被半路吞掉、變成靜默的降級路徑。
    """


def _blocked_message(request: httpx.Request) -> str:
    return (
        f"{OUTBOUND_BLOCKED_PREFIX}: {request.method} {request.url}\n"
        f"單元測試不得發出真實網路請求。本 repo 對真 Cloudflare 的紀律是"
        f"「唯讀可、任何寫入／刪除／repoint 一律不可」，而搶佔與刪除都不可逆。\n"
        f"最常見的成因：被測碼新增了一個原本不會走到的 httpx 呼叫，"
        f"而這支測試只 patch 了它當初用到的那幾個動詞"
        f"（``patch('httpx.AsyncClient.put')`` 擋不住新長出來的 GET）。\n"
        f"修法：把該動詞也放到假 CF 後面。\n"
        f"真的需要出網／連真叢集的測試請放 tests/integration/ 並在模組層宣告 "
        f"``pytestmark = pytest.mark.{OUTBOUND_EXEMPT_MARKER}``。"
    )


@pytest.fixture(autouse=True)
def block_real_outbound(request, monkeypatch):
    """任何真的 outbound 都當場炸掉（#841）。

    **為什麼攔在 transport 層**：套件既有的隔離手法是逐支自己
    ``patch("httpx.AsyncClient.get" / ".post" / ".put" / ".delete")``。凡是被
    patch 掉的動詞都到不了 transport，所以這道守線對正常測試零影響；只有
    「漏 patch 某個動詞」而**真的要出網**的請求才會走到這裡。
    #824 就是這樣洩漏的：實作多走了一次 GET，而該測試只 patch 了 PUT，
    於是那支測試真的對 api.cloudflare.com 發了請求 —— 而它照樣是綠的。

    **為什麼是 marker 不是路徑判斷**：路徑判斷（``"integration" in str(path)``）
    看起來更省事，但它的例外集合與「這支測試會不會在預設 run 執行」是兩件事，
    兩者一旦漂移就沒有訊號。改用 marker 之後例外集合**恰好等於**預設
    ``-m "not integration"`` 排除掉的那一組 —— 也就是說，拿得到例外的測試同時
    就是預設 run 不會執行的測試，這個等式是機器可驗的。
    「marker 可能漏標」這個風險本 repo 已經有守線在撐：
    ``tests/test_integration_marker_guard.py`` 用 AST 靜態掃描強制
    ``tests/integration/`` 底下每個 ``test_*.py`` 都要有 module-level 的
    ``pytestmark = pytest.mark.integration``；而 ``pytest.ini`` 開了
    ``--strict-markers`` 且該 marker 已註冊，打錯字會當場報錯而不是靜默失效。

    **範圍**：只擋 httpx（#841 的主張）。``subprocess`` 呼叫的 helm/kubectl 也會
    出網，但那是獨立決策，不在本守線內 —— 見 ``tests/test_outbound_guard.py``
    的 ``test_subprocess_egress_is_explicitly_out_of_scope``。

    **不受影響的**：``httpx.ASGITransport`` 與 starlette 的 ``_TestClientTransport``
    都不是 ``httpx.HTTPTransport`` 的子類（後者直接繼承 ``BaseTransport``），
    走 in-process ASGI、不碰 socket，因此 ``TestClient`` 測試完全不受本守線影響。
    """
    if request.node.get_closest_marker(OUTBOUND_EXEMPT_MARKER):
        yield
        return

    async def _boom_async(self, request_: httpx.Request, *args, **kwargs):
        raise RealOutboundAttempted(_blocked_message(request_))

    def _boom_sync(self, request_: httpx.Request, *args, **kwargs):
        raise RealOutboundAttempted(_blocked_message(request_))

    # 非同步路徑：src/services/cloudflare.py 全部的 httpx.AsyncClient。
    monkeypatch.setattr(
        httpx.AsyncHTTPTransport, "handle_async_request", _boom_async
    )
    # 同步路徑：src/services/webhook_dispatcher.py 的 httpx.post。
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _boom_sync)
    yield
