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
"""
from unittest.mock import patch

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
