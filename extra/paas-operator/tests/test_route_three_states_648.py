"""#648 (a)：expose 的結果必須是 disabled / failed / ok **三態可分辨**。

修前 `_create_cloudflare_route` 有 **5 條** `return None`（定義：該函式體內含巢狀的
`return None` AST 節點），語意卻只有兩類：

    1 條「合法的沒有」——Cloudflare 整合沒開
    4 條「失敗」——找不到 service／推不出 port／CloudflareException／未預期例外

呼叫端只看得到 `route is None`，兩類不可區分。而 `cloudflare_enabled` 的預設值是
**false**（`config.py`），且 `CloudflareService.__init__` 在缺 account_id / tunnel_id /
api_token / domain 任一個時會強制關回 False ⇒「沒開 CF」不是邊角，是預設狀態。
任何「沒回 route 就當失敗」的守線會讓那種環境**每一次部署**都翻 error。

本檔把三態釘住，並鎖住兩個容易被「順手簡化」掉的性質：

* `dns_failed` **只可能**在 DNS 管理真的啟用時為真（`create_dns_record` 在
  `dns_enabled` 為假時直接 return、不會拋）——「刻意關掉」不得偽裝成「失敗」。
* 失敗路徑的 `hostname` / `service_url` 是 **None 而不是空字串**——那兩條失敗發生在
  service 解析完成之前，真的沒有值。填 `""` 會讓呼叫端分不出「沒算出來」與「算出來
  是空的」，等於把本修法要消滅的病換個位置重演。

Refs #484 #648
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.api.releases import _create_cloudflare_route
from src.models.schemas import RouteStatus
from src.services.cloudflare import CloudflareException, CloudflareService, RouteOutcome


@pytest.fixture
def cf_service():
    svc = CloudflareService()
    svc.enabled = True
    svc.api_token = "test-token"
    svc.account_id = "test-account"
    svc.tunnel_id = "tun-648"
    svc.zone_id = "test-zone"
    svc.domain = "woowtech.io"
    svc.dns_enabled = True
    return svc


def _expose(subdomain="paas-cs-648", service_name="svc", service_port=8080):
    cfg = MagicMock()
    cfg.enabled = True
    cfg.subdomain = subdomain
    cfg.service_name = service_name
    cfg.service_port = service_port
    return cfg


# ====================================================================== #
#  create_route：DNS 那半的失敗必須被提上來                                #
# ====================================================================== #

class TestCreateRouteSurfacesDnsFailure:

    async def test_dns_failure_is_reported_not_swallowed(self, cf_service):
        """🔴 縫 (a) 的核心：DNS record 建不起來時 ingress 仍會建，呼叫端必須知道。

        修前 `create_route` 回 bare bool，這個事實只進 log 就消失了。
        """
        with patch.object(cf_service, "create_dns_record",
                          new=AsyncMock(side_effect=CloudflareException("boom"))), \
                patch.object(cf_service, "get_tunnel_config",
                             new=AsyncMock(return_value={"config": {"ingress": []}})), \
                patch.object(cf_service, "update_tunnel_config",
                             new=AsyncMock(return_value=True)) as upd:
            outcome = await cf_service.create_route("paas-cs-648", "http://svc:80")

        assert isinstance(outcome, RouteOutcome)
        assert outcome.dns_failed is True
        # 對照組：DNS 失敗**不得**讓 ingress 那半被跳過（那是既有的刻意行為）
        assert outcome.configured is True
        upd.assert_awaited_once()

    async def test_happy_path_reports_no_dns_failure(self, cf_service):
        """對照組：DNS 正常時不得留下 dns_failed（否則旗標恆真＝零鑑別力）。"""
        with patch.object(cf_service, "create_dns_record", new=AsyncMock(return_value=True)), \
                patch.object(cf_service, "get_tunnel_config",
                             new=AsyncMock(return_value={"config": {"ingress": []}})), \
                patch.object(cf_service, "update_tunnel_config",
                             new=AsyncMock(return_value=True)):
            outcome = await cf_service.create_route("paas-cs-648", "http://svc:80")

        assert outcome.dns_failed is False
        assert outcome.configured is True

    async def test_dns_disabled_never_reports_dns_failed(self, cf_service):
        """🔴 不變式：DNS 管理刻意關掉時，`dns_failed` 必須恆為 False。

        `dns_enabled = enabled and bool(zone_id)` 是第三個狀態（CF 開著、DNS 由人工
        管理）。把它算成「失敗」會讓那種部署的每一台服務都被呼叫端 fail-closed。
        這條靠的是 `create_dns_record` 在該狀態下直接 return 不拋——所以這裡**不**
        mock 它，跑真的那一條，否則測到的是 mock 不是不變式。
        """
        cf_service.zone_id = ""
        cf_service.dns_enabled = False

        with patch.object(cf_service, "get_tunnel_config",
                          new=AsyncMock(return_value={"config": {"ingress": []}})), \
                patch.object(cf_service, "update_tunnel_config",
                             new=AsyncMock(return_value=True)):
            outcome = await cf_service.create_route("paas-cs-648", "http://svc:80")

        assert outcome.dns_failed is False

    async def test_cloudflare_disabled_short_circuits(self, cf_service):
        """CF 整體關閉時不做任何事，且不宣稱設定成功。"""
        cf_service.enabled = False

        outcome = await cf_service.create_route("paas-cs-648", "http://svc:80")

        assert outcome.configured is False
        assert outcome.dns_failed is False


# ====================================================================== #
#  _create_cloudflare_route：三態                                         #
# ====================================================================== #

class TestExposeOutcomeIsThreeStates:

    async def test_disabled_returns_none(self):
        """狀態 1：CF 沒開 ⇒ `None`。**這不是失敗**，呼叫端不得 fail-closed。"""
        with patch("src.api.releases.cloudflare_service") as cf:
            cf.enabled = False
            result = await _create_cloudflare_route("ns", "rel", _expose())

        assert result is None

    async def test_no_services_returns_failed_with_unknown_fields_as_none(self):
        """狀態 2a：找不到可 expose 的 service ⇒ `status=failed`。

        ⚠ 這條藏在巢狀 `if` 裡（不在 `except`、不在函式頂層），是 5 條 `return None`
        中最容易被漏掉的一條。
        同時鎖住：此時 `service_url` 還沒被算出來 ⇒ 必須是 **None 不是 ''**。
        """
        with patch("src.api.releases.cloudflare_service") as cf, \
                patch("src.api.releases.k8s_service") as k8s, \
                patch("src.api.releases.settings") as st, \
                patch("src.api.releases.asyncio.sleep", new=AsyncMock()):
            cf.enabled = True
            cf.generate_subdomain = MagicMock(return_value="paas-cs-648")
            st.cloudflare_domain = "woowtech.io"
            k8s.get_services.return_value = []

            result = await _create_cloudflare_route(
                "ns", "rel", _expose(service_name=None, service_port=None))

        assert result is not None, "失敗不得再回 bare None——那正是本題的病"
        assert result.status == RouteStatus.FAILED
        assert result.enabled is False
        assert result.service_url is None, (
            "沒算出來就要是 None；填 '' 會讓呼叫端分不出『沒算出來』與『算出來是空的』")
        # hostname 在這條路上是知道的（subdomain 早就算好了）
        assert result.hostname == "paas-cs-648.woowtech.io"

    async def test_cloudflare_exception_returns_failed(self):
        """狀態 2b：CF API 炸掉 ⇒ `status=failed`，但 release 本身仍算成功。"""
        with patch("src.api.releases.cloudflare_service") as cf, \
                patch("src.api.releases.settings") as st:
            cf.enabled = True
            cf.generate_subdomain = MagicMock(return_value="paas-cs-648")
            cf.generate_service_url = MagicMock(return_value="http://svc:8080")
            cf.create_route = AsyncMock(side_effect=CloudflareException("nope"))
            st.cloudflare_domain = "woowtech.io"

            result = await _create_cloudflare_route("ns", "rel", _expose())

        assert result is not None
        assert result.status == RouteStatus.FAILED
        assert result.dns_failed is False, (
            "整條 route 失敗時我們並不知道 DNS 那一步的狀況——不知道就不要宣稱")

    async def test_ok_carries_dns_failed_through(self):
        """狀態 3：route 建起來了，但 DNS 沒有 ⇒ `status=ok` ＋ `dns_failed=True`。

        這是 #648 (a) 真正要傳到 Odoo 的那一格：tunnel ingress 在、hostname 不解析。
        """
        with patch("src.api.releases.cloudflare_service") as cf, \
                patch("src.api.releases.settings") as st:
            cf.enabled = True
            cf.generate_subdomain = MagicMock(return_value="paas-cs-648")
            cf.generate_service_url = MagicMock(return_value="http://svc:8080")
            cf.create_route = AsyncMock(
                return_value=RouteOutcome(configured=True, dns_failed=True))
            st.cloudflare_domain = "woowtech.io"

            result = await _create_cloudflare_route("ns", "rel", _expose())

        assert result.status == RouteStatus.OK
        assert result.dns_failed is True
        assert result.hostname == "paas-cs-648.woowtech.io"

    async def test_ok_without_dns_problem_is_clean(self):
        """對照組：一切正常時三個欄位都不得留下失敗痕跡。"""
        with patch("src.api.releases.cloudflare_service") as cf, \
                patch("src.api.releases.settings") as st:
            cf.enabled = True
            cf.generate_subdomain = MagicMock(return_value="paas-cs-648")
            cf.generate_service_url = MagicMock(return_value="http://svc:8080")
            cf.create_route = AsyncMock(
                return_value=RouteOutcome(configured=True, dns_failed=False))
            st.cloudflare_domain = "woowtech.io"

            result = await _create_cloudflare_route("ns", "rel", _expose())

        assert result.status == RouteStatus.OK
        assert result.dns_failed is False
        assert result.enabled is True
