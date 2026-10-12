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


@pytest.fixture(autouse=True)
def credential_reset_statefulsets(request):
    """helm_secret reset 會多查一次「opt-in 重啟的 StatefulSet」——那也是真 kubectl。

    預設回 []（＝只有 Deployment，行為與 hermes-agent 0.3.0 之前相同）。要測
    StatefulSet 也被滾的測試把本 fixture 收進參數、改 ``return_value`` 即可。
    """
    if request.node.get_closest_marker("integration"):
        yield None
        return

    with patch(
        "src.api.releases.k8s_service.get_credential_reset_statefulset_names",
        return_value=[],
    ) as mock_names:
        yield mock_names


@pytest.fixture(autouse=True)
def storage_quota_snapshot(request):
    """#1348：``create_release`` 開通前會讀 namespace 的 ResourceQuota／PVC——那也是真 kubectl。

    與上面 ``sa_pull_secret_convergence`` 同一個理由擋掉：預設回「沒有 quota、沒有 PVC」
    （＝檢查放行，行為與加入檢查前相同）。要測配額已滿的測試把本 fixture 收進參數、
    改 ``return_value`` 即可。
    """
    if request.node.get_closest_marker("integration"):
        yield None
        return

    with patch(
        "src.api.releases.k8s_service.get_storage_quota_snapshot",
        return_value={"quotas": [], "pvcs": []},
    ) as mock_snapshot:
        yield mock_snapshot


@pytest.fixture(autouse=True)
def compute_quota_snapshot(request):
    """#1647：帶 ``check_compute_quota`` 的升級預檢會讀 namespace 的 ResourceQuota／LimitRange／
    Pod——那也是真 kubectl，同樣擋掉。

    預設回「沒有 quota、沒有 LimitRange、沒有 pod」（＝檢查放行）。不帶旗標的請求根本不會
    讀它（要斷言這件事的測試把本 fixture 收進參數 ``assert_not_called``）；要測放不下的測試
    把本 fixture 收進參數、改 ``return_value``／``side_effect``。
    """
    if request.node.get_closest_marker("integration"):
        yield None
        return

    with patch(
        "src.api.releases.k8s_service.get_compute_quota_snapshot",
        return_value={"quotas": [], "limitranges": [], "pods": []},
    ) as mock_snapshot:
        yield mock_snapshot


@pytest.fixture(autouse=True)
def orphaned_release_resources(request):
    """#1157：``create_release`` 開通前會查同名 release 的孤兒 PVC／DataVolume——真 kubectl。

    預設回「沒有孤兒資源」（＝檢查放行，行為與加入檢查前相同）。要測 409／503 的測試把本
    fixture 收進參數、改 ``return_value``／``side_effect`` 即可。
    """
    if request.node.get_closest_marker("integration"):
        yield None
        return

    with patch(
        "src.api.releases.k8s_service.list_orphaned_release_resources",
        return_value={"pvcs": [], "datavolumes": []},
    ) as mock_lookup:
        yield mock_lookup


@pytest.fixture(autouse=True)
def longhorn_capacity_snapshot(request):
    """#1415：``create_release`` 開通前與 ``/api/cluster/storage-capacity`` 會讀 Longhorn
    節點／設定與 StorageClass——真 kubectl，同樣擋掉。

    預設回「叢集沒有 Longhorn」（空 snapshot）＝容量預檢放行、而且不會走到 dry-run 渲染，
    行為與加入檢查前相同（既有測試對 ``helm_service.install`` 的呼叫次數斷言不受影響）。
    要測容量的測試把本 fixture 收進參數、改 ``return_value``。
    """
    if request.node.get_closest_marker("integration"):
        yield None
        return

    empty = {"nodes": [], "settings": [], "storage_classes": []}
    with patch(
        "src.api.releases.k8s_service.get_longhorn_capacity_snapshot", return_value=empty,
    ) as mock_snapshot, patch(
        "src.api.cluster.k8s_service.get_longhorn_capacity_snapshot", new=mock_snapshot,
    ):
        yield mock_snapshot


@pytest.fixture(autouse=True)
def live_objects_lookup(request):
    """#1407：install／upgrade 前讀 namespace 的 PVC／StatefulSet／Deployment——也是真 kubectl。

    預設兩個 seam 都回「讀不到」（renderer 不帶 live 快照、preflight 跳過），行為
    與 #1407 之前相同。要測這條路的測試把本 fixture 收進參數改 ``return_value``，
    或在測試內 ``patch`` 回真函式（搭配 mock 的 ``subprocess.run``）。
    """
    if request.node.get_closest_marker("integration"):
        yield None
        return

    with patch("src.services.helm._live_snapshot", return_value={}) as mock_snapshot, \
            patch("src.api.releases._live_objects", return_value=None) as mock_objects:
        yield {"snapshot": mock_snapshot, "objects": mock_objects}


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
