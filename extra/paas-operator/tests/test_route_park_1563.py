"""woow_paas_platform #1563：停權說明頁的 park／unpark。

欠費停權（scale 0）時把服務的對外 hostname 指向共用的停權說明頁，復權（scale 1）後
指回服務。本檔釘住：

* **目標只從 settings 來**：park 的 body 帶什麼 URL 都沒用（schema 根本不收）。
* **同 hostname 的所有規則**（含 path 規則）一起改；其他 hostname、catch-all、
  ``path``／``originRequest``、規則順序都不動（整份 read-modify-write 的副作用面）。
* **冪等**：重送不 PUT；unpark 只動指向說明頁的規則。
* **拒絕的形狀**：指向別的 namespace（``route_not_owned``）、分流到多個 service
  （``route_multiple_targets``）、說明頁 Service 不在（``suspended_page_unavailable``）
  一律 409 且一條都沒改；**沒有任何一條路回 404**（平台把 404 當成舊 operator）。
* **unpark 重算 service_url** 用的是安裝時同一段邏輯（``_resolve_route_target``），
  而且不等那 3 秒。

Cloudflare 一律假在 httpx 的 GET／PUT 上（走真的 ``get_tunnel_config``／
``update_tunnel_config``，wire 形狀也一起驗）；kubectl 一律 mock。
"""
import copy
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.api import releases
from src.api.releases import (
    _service_url_in_namespace,
    _suspended_page_url,
)
from src.main import app
from src.services.cloudflare import (
    REFUSED_FOREIGN,
    REFUSED_MULTIPLE_TARGETS,
    RULE_FOREIGN,
    RULE_KEEP,
    RULE_RETARGET,
    CloudflareService,
)
from src.services.helm import KubectlException, KubernetesService

NS = "paas-ws-abc12345"
REL = "svc-1563"
SUB = "paas-cs-abc12345-1563"
HOST = f"{SUB}.woowtech.io"
SVC_URL = f"http://{REL}.{NS}.svc.cluster.local:80"
PAGE_URL = "http://paas-suspended-page.paas-stg-cloudflare-system.svc.cluster.local:8080"
CATCH_ALL = {"service": "http_status:404"}


def _base_ingress():
    """一份「別的租戶＋本服務兩條（含 path）＋catch-all」的 tunnel config。"""
    return [
        {"hostname": "other.woowtech.io", "service": "http://o.paas-ws-other.svc.cluster.local:80"},
        {"hostname": HOST, "path": "^/ws", "service": SVC_URL,
         "originRequest": {"connectTimeout": 30}},
        {"hostname": HOST, "service": SVC_URL},
        {"hostname": "zzz.woowtech.io", "service": "http://z.paas-ws-z.svc.cluster.local:8080"},
        dict(CATCH_ALL),
    ]


class FakeTunnel:
    """假的 Cloudflare tunnel configurations 端點（GET／PUT），記錄每一次 PUT。"""

    def __init__(self, ingress):
        self.ingress = copy.deepcopy(ingress)
        self.puts = []
        self.fail_get = False
        self.fail_put = False

    def _response(self, status_code, body):
        resp = MagicMock()
        resp.status_code = status_code
        resp.text = str(body)
        resp.json.return_value = body
        return resp

    async def get(self, url, headers=None, timeout=None):
        assert url.endswith("/configurations"), url
        if self.fail_get:
            return self._response(500, {"success": False})
        return self._response(200, {
            "success": True, "result": {"config": {"ingress": copy.deepcopy(self.ingress)}}})

    async def put(self, url, headers=None, json=None, timeout=None):
        assert url.endswith("/configurations"), url
        if self.fail_put:
            return self._response(500, {"success": False})
        self.puts.append(copy.deepcopy(json))
        self.ingress = copy.deepcopy(json["config"]["ingress"])
        return self._response(200, {"success": True})


@pytest.fixture
def cf():
    """``releases.cloudflare_service`` 開成 enabled，httpx GET／PUT 接到 FakeTunnel。"""
    svc = releases.cloudflare_service
    fake = FakeTunnel(_base_ingress())
    with patch.object(svc, "enabled", True), \
            patch.object(svc, "domain", "woowtech.io"), \
            patch.object(svc, "account_id", "acc"), \
            patch.object(svc, "tunnel_id", "tun-stg"), \
            patch.object(svc, "api_token", "test-token"), \
            patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=fake.get)), \
            patch("httpx.AsyncClient.put", new=AsyncMock(side_effect=fake.put)):
        yield fake


@pytest.fixture
def page_settings():
    """stg 環境：cloudflared ns 由 PAAS_ENV 推得，說明頁 Service 用預設名。"""
    with patch.object(releases.settings, "paas_env", "stg"), \
            patch.object(releases.settings, "cloudflared_namespace", ""), \
            patch.object(releases.settings, "suspended_page_service", "paas-suspended-page"), \
            patch.object(releases.settings, "suspended_page_port", 8080):
        yield


@pytest.fixture
def k8s():
    with patch("src.api.releases.k8s_service") as mock_k8s:
        mock_k8s.suspended_page_service_exists.return_value = True
        mock_k8s.get_services.return_value = [
            {"name": f"{REL}-headless", "clusterIP": "None", "ports": [{"port": 80}]},
            {"name": REL, "clusterIP": "10.43.0.10", "ports": [{"port": 80}]},
        ]
        yield mock_k8s


@pytest.fixture
def client():
    with patch("src.main.settings") as mock_settings:
        mock_settings.api_key = "test-api-key"
        mock_settings.namespace_prefix = "paas-ws-"
        yield TestClient(app, headers={"X-API-Key": "test-api-key"})


def _park(client, subdomain=SUB, **extra):
    return client.post(
        f"/api/releases/{NS}/{REL}/route/park", json={"subdomain": subdomain, **extra})


def _unpark(client, expose=None):
    return client.post(
        f"/api/releases/{NS}/{REL}/route/unpark",
        json={"expose": expose if expose is not None else {"enabled": True, "subdomain": SUB}})


def _host_services(ingress):
    return [r["service"] for r in ingress if r.get("hostname") == HOST]


# ====================================================================== #
#  設定：說明頁 URL 只從 settings 組                                      #
# ====================================================================== #

class TestSuspendedPageUrl:

    @pytest.mark.parametrize("env, expected_ns", [
        ("stg", "paas-stg-cloudflare-system"),
        ("prod", "paas-cloudflare-system"),
    ])
    def test_derived_from_paas_env(self, env, expected_ns):
        with patch.object(releases.settings, "paas_env", env), \
                patch.object(releases.settings, "cloudflared_namespace", ""):
            assert _suspended_page_url() == (
                f"http://paas-suspended-page.{expected_ns}.svc.cluster.local:8080")

    def test_explicit_cloudflared_namespace_and_port(self):
        with patch.object(releases.settings, "cloudflared_namespace", "cf-x"), \
                patch.object(releases.settings, "suspended_page_service", "page"), \
                patch.object(releases.settings, "suspended_page_port", 9090):
            assert _suspended_page_url() == "http://page.cf-x.svc.cluster.local:9090"

    def test_dev_without_connector_has_no_page(self):
        with patch.object(releases.settings, "paas_env", ""), \
                patch.object(releases.settings, "cloudflared_namespace", ""):
            assert _suspended_page_url() is None

    def test_empty_service_name_disables(self):
        with patch.object(releases.settings, "paas_env", "prod"), \
                patch.object(releases.settings, "cloudflared_namespace", ""), \
                patch.object(releases.settings, "suspended_page_service", ""):
            assert _suspended_page_url() is None


class TestSuspendedPageServiceExists:
    """kubectl 的 namespace／Service 名只來自 settings。"""

    def _svc(self, stdout):
        svc = KubernetesService.__new__(KubernetesService)
        svc._run_command = MagicMock(return_value=MagicMock(stdout=stdout))
        return svc

    def test_present(self):
        svc = self._svc("service/paas-suspended-page\n")
        with patch("src.services.helm.settings") as s:
            s.cloudflared_namespace = ""
            s.paas_env = "prod"
            s.suspended_page_service = "paas-suspended-page"
            assert svc.suspended_page_service_exists() is True
        args = svc._run_command.call_args.args[0]
        assert args[:3] == ["get", "service", "paas-suspended-page"]
        assert args[args.index("--namespace") + 1] == "paas-cloudflare-system"
        assert "--ignore-not-found" in args

    def test_absent(self):
        svc = self._svc("")
        with patch("src.services.helm.settings") as s:
            s.cloudflared_namespace = ""
            s.paas_env = "stg"
            s.suspended_page_service = "paas-suspended-page"
            assert svc.suspended_page_service_exists() is False

    def test_unconfigured_does_not_call_kubectl(self):
        svc = self._svc("service/x")
        with patch("src.services.helm.settings") as s:
            s.cloudflared_namespace = ""
            s.paas_env = ""
            s.suspended_page_service = "paas-suspended-page"
            assert svc.suspended_page_service_exists() is False
        svc._run_command.assert_not_called()


class TestServiceUrlInNamespace:

    @pytest.mark.parametrize("service", [
        f"http://svc.{NS}.svc.cluster.local:80",
        f"https://svc.{NS}.svc.cluster.local:8443",
        f"http://svc.{NS}.svc:80",
        f"http://svc.{NS}:80",
        f"http://svc.{NS}.svc.cluster.local",
    ])
    def test_inside(self, service):
        assert _service_url_in_namespace(service, NS) is True

    @pytest.mark.parametrize("service", [
        None,
        "",
        "http_status:503",
        "unix:/tmp/x.sock",
        "http://svc.paas-ws-other.svc.cluster.local:80",
        f"http://svc.{NS}-evil.svc.cluster.local:80",
        f"http://svc.x{NS}.svc.cluster.local:80",
        f"http://{NS}.svc.cluster.local:80",
        f"http://a.b.{NS}.svc.cluster.local:80",
        "https://example.com",
        f"tcp://svc.{NS}.svc.cluster.local:5432",
        "http://[::1",
    ])
    def test_outside(self, service):
        assert _service_url_in_namespace(service, NS) is False


# ====================================================================== #
#  CloudflareService.retarget_hostname_rules                              #
# ====================================================================== #

class TestRetargetHostnameRules:

    @pytest.fixture
    def svc(self):
        svc = CloudflareService()
        svc.enabled = True
        svc.domain = "woowtech.io"
        svc.account_id = "acc"
        svc.tunnel_id = "tun"
        svc.api_token = "test-token"
        return svc

    async def _run(self, svc, ingress, new_service, classify, **kw):
        # ⚠ 不用 asyncio.run()：它會把 pytest-asyncio 留給同步碼的 event loop 換掉而不關，
        # 之後 GC 時冒出 unclosed socket 的 ResourceWarning（filterwarnings=error ⇒ 別支測試紅）。
        fake = FakeTunnel(ingress)
        with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=fake.get)), \
                patch("httpx.AsyncClient.put", new=AsyncMock(side_effect=fake.put)):
            outcome = await svc.retarget_hostname_rules(SUB, new_service, classify, **kw)
        return outcome, fake

    async def test_all_rules_of_the_hostname_change_and_nothing_else(self, svc):
        outcome, fake = await self._run(
            svc, _base_ingress(), PAGE_URL, lambda r: RULE_RETARGET)
        assert (outcome.matched, outcome.changed, outcome.refused) == (2, 2, None)
        assert outcome.previous == [SVC_URL, SVC_URL]
        assert len(fake.puts) == 1
        expected = _base_ingress()
        expected[1]["service"] = PAGE_URL
        expected[2]["service"] = PAGE_URL
        # path／originRequest／順序／其他 hostname／catch-all 全部原樣
        assert fake.ingress == expected

    async def test_idempotent_no_put(self, svc):
        ingress = _base_ingress()
        ingress[1]["service"] = ingress[2]["service"] = PAGE_URL
        outcome, fake = await self._run(svc, ingress, PAGE_URL, lambda r: RULE_RETARGET)
        assert (outcome.matched, outcome.changed, outcome.already) == (2, 0, 2)
        assert fake.puts == []

    async def test_foreign_refuses_whole_hostname(self, svc):
        classify = MagicMock(side_effect=[RULE_RETARGET, RULE_FOREIGN])
        outcome, fake = await self._run(svc, _base_ingress(), PAGE_URL, classify)
        assert outcome.refused == REFUSED_FOREIGN
        assert outcome.changed == 0
        assert fake.puts == []

    async def test_multiple_targets_refused_only_with_single_source(self, svc):
        ingress = _base_ingress()
        ingress[1]["service"] = f"http://{REL}-ws.{NS}.svc.cluster.local:9000"
        outcome, fake = await self._run(
            svc, ingress, PAGE_URL, lambda r: RULE_RETARGET, single_source=True)
        assert outcome.refused == REFUSED_MULTIPLE_TARGETS
        assert fake.puts == []
        outcome, fake = await self._run(svc, ingress, PAGE_URL, lambda r: RULE_RETARGET)
        assert outcome.changed == 2

    async def test_keep_leaves_rule_alone(self, svc):
        classify = MagicMock(side_effect=[RULE_KEEP, RULE_RETARGET])
        outcome, fake = await self._run(svc, _base_ingress(), PAGE_URL, classify)
        assert outcome.changed == 1
        assert _host_services(fake.ingress) == [SVC_URL, PAGE_URL]

    async def test_unknown_hostname_is_no_op(self, svc):
        ingress = [r for r in _base_ingress() if r.get("hostname") != HOST]
        outcome, fake = await self._run(svc, ingress, PAGE_URL, lambda r: RULE_RETARGET)
        assert (outcome.matched, outcome.changed, outcome.refused) == (0, 0, None)
        assert fake.puts == []

    async def test_catch_all_guard_still_applies(self, svc):
        """config 少了 catch-all 時，PUT 回去的最後一條仍是 catch-all（update_tunnel_config 保底）。"""
        ingress = _base_ingress()[:-1]
        outcome, fake = await self._run(svc, ingress, PAGE_URL, lambda r: RULE_RETARGET)
        assert outcome.changed == 2
        assert fake.ingress[-1] == CATCH_ALL


# ====================================================================== #
#  POST /api/releases/{ns}/{name}/route/park                              #
# ====================================================================== #

class TestParkEndpoint:

    def test_parks_every_rule_of_the_hostname(self, client, cf, page_settings, k8s):
        response = _park(client)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body == {"hostname": HOST, "status": "parked", "matched": 2, "changed": 2,
                        "service_url": PAGE_URL}
        assert len(cf.puts) == 1
        assert _host_services(cf.ingress) == [PAGE_URL, PAGE_URL]
        assert cf.ingress[1]["path"] == "^/ws"
        assert cf.ingress[1]["originRequest"] == {"connectTimeout": 30}
        assert cf.ingress[0]["service"].startswith("http://o.paas-ws-other")
        assert cf.ingress[-1] == CATCH_ALL

    def test_second_park_is_a_no_op(self, client, cf, page_settings, k8s):
        assert _park(client).json()["status"] == "parked"
        response = _park(client)
        assert response.status_code == 200
        assert response.json()["status"] == "already_parked"
        assert response.json()["changed"] == 0
        assert len(cf.puts) == 1

    def test_caller_cannot_choose_the_target(self, client, cf, page_settings, k8s):
        """body 夾帶 service_url 也沒用：目標只從 settings 組。"""
        response = _park(client, service_url="http://evil.example.com:80")
        assert response.status_code == 200
        assert _host_services(cf.ingress) == [PAGE_URL, PAGE_URL]

    def test_unknown_hostname_is_not_found_200(self, client, cf, page_settings, k8s):
        response = _park(client, subdomain="paas-cs-nope")
        assert response.status_code == 200
        assert response.json()["status"] == "not_found"
        assert cf.puts == []

    def test_cloudflare_disabled(self, client, page_settings, k8s):
        with patch.object(releases.cloudflare_service, "enabled", False):
            response = _park(client)
        assert response.status_code == 200
        assert response.json()["status"] == "disabled"
        k8s.suspended_page_service_exists.assert_not_called()

    def test_page_service_missing_refuses(self, client, cf, page_settings, k8s):
        k8s.suspended_page_service_exists.return_value = False
        response = _park(client)
        assert response.status_code == 409
        assert response.json()["detail"]["error_code"] == "suspended_page_unavailable"
        assert cf.puts == []

    def test_page_not_configured_refuses(self, client, cf, k8s):
        with patch.object(releases.settings, "paas_env", ""), \
                patch.object(releases.settings, "cloudflared_namespace", ""):
            response = _park(client)
        assert response.status_code == 409
        assert response.json()["detail"]["error_code"] == "suspended_page_not_configured"
        assert cf.puts == []

    def test_page_lookup_failure_is_503(self, client, cf, page_settings, k8s):
        k8s.suspended_page_service_exists.side_effect = KubectlException(
            message="kubectl failed", command="kubectl get service", stderr="Forbidden")
        response = _park(client)
        assert response.status_code == 503
        assert response.json()["detail"]["error_code"] == "suspended_page_check_failed"
        assert cf.puts == []

    def test_rule_in_another_namespace_refuses(self, client, cf, page_settings, k8s):
        cf.ingress[1]["service"] = "http://x.paas-ws-other.svc.cluster.local:80"
        response = _park(client)
        assert response.status_code == 409
        assert response.json()["detail"]["error_code"] == "route_not_owned"
        assert cf.puts == []

    def test_http_status_rule_is_kept(self, client, cf, page_settings, k8s):
        """``http_status:*`` 不通往任何服務：原樣保留，其餘指向本 ns 的規則照常 park。"""
        cf.ingress[2]["service"] = "http_status:503"
        response = _park(client)
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "parked"
        assert response.json()["changed"] == 1
        assert _host_services(cf.ingress) == [PAGE_URL, "http_status:503"]

    def test_secret_path_hostname_round_trip(self, client, cf, page_settings, k8s):
        """MCP 公開網址的形狀：秘密 path → 服務、同 hostname 其他 path → http_status:404。

        park 只動 path 那條；http_status:404 若也被改指說明頁，unpark 會把它改成服務 URL，
        等於把秘密 path 的保護拆掉——所以它必須原樣留著，unpark 後整份 config 逐字還原。
        """
        cf.ingress[1:3] = [
            {"hostname": HOST, "path": "^/private_test(/|$)", "service": SVC_URL},
            {"hostname": HOST, "service": "http_status:404"},
        ]
        original = copy.deepcopy(cf.ingress)
        park = _park(client)
        assert park.json()["status"] == "parked"
        assert _host_services(cf.ingress) == [PAGE_URL, "http_status:404"]
        assert cf.ingress[1]["path"] == "^/private_test(/|$)"
        unpark = _unpark(client)
        assert unpark.json()["status"] == "unparked"
        assert unpark.json()["changed"] == 1
        assert cf.ingress == original

    def test_only_http_status_rules(self, client, cf, page_settings, k8s):
        cf.ingress[1]["service"] = cf.ingress[2]["service"] = "http_status:404"
        response = _park(client)
        assert response.status_code == 200
        assert response.json()["status"] == "no_service_rule"
        assert cf.puts == []

    def test_paths_split_across_services_refuses(self, client, cf, page_settings, k8s):
        cf.ingress[1]["service"] = f"http://{REL}-ws.{NS}.svc.cluster.local:9000"
        response = _park(client)
        assert response.status_code == 409
        assert response.json()["detail"]["error_code"] == "route_multiple_targets"
        assert cf.puts == []

    def test_cloudflare_error_is_502(self, client, cf, page_settings, k8s):
        cf.fail_put = True
        response = _park(client)
        assert response.status_code == 502
        assert response.json()["detail"]["error_code"] == "cloudflare_error"

    def test_namespace_outside_prefix_is_403(self, client, cf, page_settings, k8s):
        response = client.post(
            f"/api/releases/kube-system/{REL}/route/park", json={"subdomain": SUB})
        assert response.status_code == 403
        assert cf.puts == []

    @pytest.mark.parametrize("subdomain", ["", "A-B", "a.b", "-x", "x" * 64])
    def test_subdomain_must_be_one_dns_label(self, client, cf, page_settings, k8s, subdomain):
        response = _park(client, subdomain=subdomain)
        assert response.status_code == 422
        assert cf.puts == []


# ====================================================================== #
#  POST /api/releases/{ns}/{name}/route/unpark                            #
# ====================================================================== #

class TestUnparkEndpoint:

    def _parked(self, cf):
        cf.ingress[1]["service"] = PAGE_URL
        cf.ingress[2]["service"] = PAGE_URL

    def test_restores_install_time_target(self, client, cf, page_settings, k8s):
        self._parked(cf)
        resolver = AsyncMock(wraps=releases._resolve_route_target)
        with patch("src.api.releases._resolve_route_target", new=resolver):
            response = _unpark(client)
        assert response.status_code == 200, response.text
        assert response.json() == {"hostname": HOST, "status": "unparked", "matched": 2,
                                   "changed": 2, "service_url": SVC_URL}
        assert cf.ingress == _base_ingress()
        k8s.get_services.assert_called_once_with(
            namespace=NS, label_selector=f"app.kubernetes.io/instance={REL}")
        # 安裝時那段解析邏輯（共用），但復權時 release 早就在了，不等那 3 秒。
        assert resolver.await_args.kwargs == {"wait_for_services": False}

    def test_explicit_service_in_expose(self, client, cf, page_settings, k8s):
        self._parked(cf)
        response = _unpark(client, {"enabled": True, "subdomain": SUB,
                                    "service_name": REL, "service_port": 8072})
        assert response.status_code == 200
        target = f"http://{REL}.{NS}.svc.cluster.local:8072"
        assert _host_services(cf.ingress) == [target, target]
        k8s.get_services.assert_not_called()

    def test_generated_subdomain_when_expose_has_none(self, client, cf, page_settings, k8s):
        generated = f"{REL}-abc12345"
        cf.ingress.insert(0, {"hostname": f"{generated}.woowtech.io", "service": PAGE_URL})
        response = _unpark(client, {"enabled": True})
        assert response.status_code == 200
        assert response.json()["status"] == "unparked"
        assert cf.ingress[0]["service"] == SVC_URL

    def test_round_trip_park_then_unpark(self, client, cf, page_settings, k8s):
        assert _park(client).json()["status"] == "parked"
        assert _unpark(client).json()["status"] == "unparked"
        assert cf.ingress == _base_ingress()
        assert len(cf.puts) == 2

    def test_only_parked_rules_are_touched(self, client, cf, page_settings, k8s):
        cf.ingress[2]["service"] = PAGE_URL
        manual = f"http://{REL}-ws.{NS}.svc.cluster.local:9000"
        cf.ingress[1]["service"] = manual
        response = _unpark(client)
        assert response.json()["changed"] == 1
        assert _host_services(cf.ingress) == [manual, SVC_URL]

    def test_not_parked_is_no_op_and_resolves_nothing(self, client, cf, page_settings, k8s):
        response = _unpark(client)
        assert response.status_code == 200
        assert response.json()["status"] == "not_parked"
        assert cf.puts == []
        k8s.get_services.assert_not_called()

    def test_second_unpark_is_a_no_op(self, client, cf, page_settings, k8s):
        self._parked(cf)
        assert _unpark(client).json()["status"] == "unparked"
        response = _unpark(client)
        assert response.json()["status"] == "not_parked"
        assert len(cf.puts) == 1

    def test_unknown_hostname_is_not_found_200(self, client, cf, page_settings, k8s):
        response = _unpark(client, {"enabled": True, "subdomain": "paas-cs-nope"})
        assert response.status_code == 200
        assert response.json()["status"] == "not_found"

    def test_unresolvable_service_is_409_and_stays_parked(self, client, cf, page_settings, k8s):
        self._parked(cf)
        k8s.get_services.return_value = []
        response = _unpark(client)
        assert response.status_code == 409
        assert response.json()["detail"]["error_code"] == "route_target_unresolved"
        assert cf.puts == []
        assert _host_services(cf.ingress) == [PAGE_URL, PAGE_URL]

    def test_cloudflare_read_error_is_502(self, client, cf, page_settings, k8s):
        cf.fail_get = True
        response = _unpark(client)
        assert response.status_code == 502
        assert response.json()["detail"]["error_code"] == "cloudflare_error"

    def test_cloudflare_write_error_is_502(self, client, cf, page_settings, k8s):
        self._parked(cf)
        cf.fail_put = True
        response = _unpark(client)
        assert response.status_code == 502
        assert response.json()["detail"]["error_code"] == "cloudflare_error"

    def test_cloudflare_disabled(self, client, page_settings, k8s):
        with patch.object(releases.cloudflare_service, "enabled", False):
            response = _unpark(client)
        assert response.status_code == 200
        assert response.json()["status"] == "disabled"

    def test_unpark_works_without_page_configured(self, client, cf, k8s):
        """說明頁設定拿掉（或 dev）時 unpark 仍回 200，不會讓復權失敗。"""
        with patch.object(releases.settings, "paas_env", ""), \
                patch.object(releases.settings, "cloudflared_namespace", ""):
            response = _unpark(client)
        assert response.status_code == 200
        assert response.json()["status"] == "not_parked"

    def test_namespace_outside_prefix_is_403(self, client, cf, page_settings, k8s):
        response = client.post(
            f"/api/releases/kube-system/{REL}/route/unpark",
            json={"expose": {"enabled": True, "subdomain": SUB}})
        assert response.status_code == 403

    def test_invalid_subdomain_is_422(self, client, cf, page_settings, k8s):
        response = _unpark(client, {"enabled": True, "subdomain": "Not.A.Label"})
        assert response.status_code == 422
        assert cf.puts == []


class TestResolveRouteTarget:
    """``_resolve_route_target``：安裝與 unpark 共用的解析（自 ``_create_cloudflare_route`` 抽出）。"""

    def _expose(self, **kw):
        return MagicMock(**{"subdomain": SUB, "service_name": None, "service_port": None, **kw})

    async def test_auto_detect_skips_headless_and_prefers_http_port(self, k8s):
        k8s.get_services.return_value = [
            {"name": f"{REL}-hl", "clusterIP": "None", "ports": [{"port": 80}]},
            {"name": REL, "clusterIP": "10.43.0.9", "ports": [{"port": 5432}, {"port": 8080}]},
        ]
        with patch("src.api.releases.asyncio") as aio:
            aio.sleep = AsyncMock()
            result = await releases._resolve_route_target(NS, REL, self._expose())
        assert result == (SUB, f"http://{REL}.{NS}.svc.cluster.local:8080", None)
        aio.sleep.assert_awaited_once_with(3)  # 安裝路徑：既有的等待照舊

    async def test_no_wait_when_asked(self, k8s):
        with patch("src.api.releases.asyncio") as aio:
            aio.sleep = AsyncMock()
            await releases._resolve_route_target(
                NS, REL, self._expose(), wait_for_services=False)
        aio.sleep.assert_not_awaited()

    async def test_generated_subdomain(self, k8s):
        sub, _, _ = await releases._resolve_route_target(
            NS, REL, self._expose(subdomain=None, service_name=REL, service_port=80))
        assert sub == f"{REL}-abc12345"
        k8s.get_services.assert_not_called()

    async def test_failures_carry_no_service_url(self, k8s):
        k8s.get_services.return_value = []
        assert await releases._resolve_route_target(
            NS, REL, self._expose(), wait_for_services=False) == (
            SUB, None, "no services found for this release")
        k8s.get_services.return_value = [{"name": "x", "clusterIP": "None", "ports": []}]
        assert await releases._resolve_route_target(
            NS, REL, self._expose(), wait_for_services=False) == (
            SUB, None, "could not determine which service/port to expose")


class TestInstallSharesTheResolver:
    """安裝時建 route 與 unpark 用同一個 ``_resolve_route_target``（兩邊不得各算一份）。"""

    async def test_install_route_uses_shared_resolver(self):
        expose = MagicMock(enabled=True, subdomain=SUB, service_name=None, service_port=None)
        with patch.object(releases.cloudflare_service, "enabled", True), \
                patch("src.api.releases._resolve_route_target",
                      new=AsyncMock(return_value=(SUB, SVC_URL, None))) as resolver, \
                patch.object(releases.cloudflare_service, "create_route",
                             new=AsyncMock(return_value=MagicMock(dns_failed=False))) as create:
            route = await releases._create_cloudflare_route(NS, REL, expose)
        resolver.assert_awaited_once_with(NS, REL, expose)
        create.assert_awaited_once_with(subdomain=SUB, service_url=SVC_URL)
        assert route.service_url == SVC_URL
