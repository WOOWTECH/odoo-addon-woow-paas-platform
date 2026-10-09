"""woow_paas_platform #1563 integration：park／unpark 對真叢集的那兩格。

單元測試把 ``k8s_service`` mock 掉，看不到：

* ``suspended_page_service_exists`` 的 ``kubectl get service --ignore-not-found`` 在
  Service 不存在時**真的** exit 0＋空輸出（而不是錯誤）——park 靠它拒絕把 hostname 指到
  一個不存在的位址；
* unpark 重算 service_url 時，``_resolve_route_target`` 對真 Service 清單（headless 在前、
  真 Service 在後）的自動偵測。

只建 Service（沒有 Pod、沒有 PVC），不會排到任何節點。Cloudflare 一律假在 httpx 的
GET／PUT（integration 不受單元的 outbound 守線保護，所以這裡**一定**要自己 patch；
任何真的 CF 呼叫都會打到不該打的 tunnel）。

Run::

    KUBECONFIG=<kubeconfig> pytest -m integration tests/integration/test_route_park_1563.py -v
"""
import copy
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.api import releases
from src.config import settings
from tests.integration.conftest import _apply_manifest

pytestmark = pytest.mark.integration

RELEASE = "svc-int1563"
SUBDOMAIN = "paas-cs-int1563"


def _service(namespace, name, release=None, headless=False, port=80):
    labels = f"\n  labels:\n    app.kubernetes.io/instance: {release}" if release else ""
    cluster_ip = "\n  clusterIP: None" if headless else ""
    return f"""
apiVersion: v1
kind: Service
metadata:
  name: {name}
  namespace: {namespace}{labels}
spec:{cluster_ip}
  selector:
    app: {name}
  ports:
  - name: http
    port: {port}
    targetPort: {port}
"""


class _FakeTunnel:
    def __init__(self, ingress):
        self.ingress = copy.deepcopy(ingress)
        self.puts = []

    @staticmethod
    def _resp(body):
        resp = MagicMock(status_code=200, text="ok")
        resp.json.return_value = body
        return resp

    async def get(self, url, headers=None, timeout=None):
        return self._resp({"success": True,
                           "result": {"config": {"ingress": copy.deepcopy(self.ingress)}}})

    async def put(self, url, headers=None, json=None, timeout=None):
        self.puts.append(copy.deepcopy(json))
        self.ingress = copy.deepcopy(json["config"]["ingress"])
        return self._resp({"success": True})


@pytest.fixture
def tunnel(test_namespace):
    hostname = f"{SUBDOMAIN}.woowtech.io"
    service_url = f"http://{RELEASE}.{test_namespace}.svc.cluster.local:80"
    fake = _FakeTunnel([
        {"hostname": "other.woowtech.io", "service": "http://o.paas-ws-other.svc.cluster.local:80"},
        {"hostname": hostname, "path": "^/api", "service": service_url},
        {"hostname": hostname, "service": service_url},
        {"service": "http_status:404"},
    ])
    svc = releases.cloudflare_service
    with patch.object(svc, "enabled", True), \
            patch.object(svc, "domain", "woowtech.io"), \
            patch.object(svc, "account_id", "acc-int"), \
            patch.object(svc, "tunnel_id", "tun-int"), \
            patch.object(svc, "api_token", "fake-token"), \
            patch.object(settings, "cloudflared_namespace", test_namespace), \
            patch.object(settings, "suspended_page_service", "paas-suspended-page"), \
            patch.object(settings, "suspended_page_port", 8080), \
            patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=fake.get)), \
            patch("httpx.AsyncClient.put", new=AsyncMock(side_effect=fake.put)):
        yield {"fake": fake, "service_url": service_url, "hostname": hostname,
               "page_url": (f"http://paas-suspended-page.{test_namespace}"
                            f".svc.cluster.local:8080")}


def _host_services(fake, hostname):
    return [r["service"] for r in fake.ingress if r.get("hostname") == hostname]


def test_park_refuses_while_the_page_service_does_not_exist(app_client, test_namespace, tunnel):
    response = app_client.post(
        f"/api/releases/{test_namespace}/{RELEASE}/route/park", json={"subdomain": SUBDOMAIN})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error_code"] == "suspended_page_unavailable"
    assert tunnel["fake"].puts == []


def test_park_unpark_round_trip_against_real_services(app_client, test_namespace, tunnel):
    # 說明頁 Service（放在「cloudflared namespace」＝本測試 ns）＋release 的 Service：
    # headless 排在前面，自動偵測必須跳過它。
    _apply_manifest(_service(test_namespace, "paas-suspended-page", port=8080))
    _apply_manifest(_service(test_namespace, f"{RELEASE}-hl", release=RELEASE, headless=True))
    _apply_manifest(_service(test_namespace, RELEASE, release=RELEASE))
    fake, hostname = tunnel["fake"], tunnel["hostname"]
    before = copy.deepcopy(fake.ingress)

    park = app_client.post(
        f"/api/releases/{test_namespace}/{RELEASE}/route/park", json={"subdomain": SUBDOMAIN})
    assert park.status_code == 200, park.text
    assert park.json()["status"] == "parked"
    assert _host_services(fake, hostname) == [tunnel["page_url"]] * 2
    assert fake.ingress[1]["path"] == "^/api"
    assert fake.ingress[-1] == {"service": "http_status:404"}

    again = app_client.post(
        f"/api/releases/{test_namespace}/{RELEASE}/route/park", json={"subdomain": SUBDOMAIN})
    assert again.json()["status"] == "already_parked"
    assert len(fake.puts) == 1

    unpark = app_client.post(
        f"/api/releases/{test_namespace}/{RELEASE}/route/unpark",
        json={"expose": {"enabled": True, "subdomain": SUBDOMAIN}})
    assert unpark.status_code == 200, unpark.text
    assert unpark.json()["status"] == "unparked"
    assert unpark.json()["service_url"] == tunnel["service_url"]
    assert fake.ingress == before

    again = app_client.post(
        f"/api/releases/{test_namespace}/{RELEASE}/route/unpark",
        json={"expose": {"enabled": True, "subdomain": SUBDOMAIN}})
    assert again.json()["status"] == "not_parked"
    assert len(fake.puts) == 2


def test_unpark_without_release_service_stays_parked(app_client, test_namespace, tunnel):
    fake, hostname = tunnel["fake"], tunnel["hostname"]
    for rule in fake.ingress:
        if rule.get("hostname") == hostname:
            rule["service"] = tunnel["page_url"]
    response = app_client.post(
        f"/api/releases/{test_namespace}/{RELEASE}/route/unpark",
        json={"expose": {"enabled": True, "subdomain": SUBDOMAIN}})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error_code"] == "route_target_unresolved"
    assert fake.puts == []
    assert _host_services(fake, hostname) == [tunnel["page_url"]] * 2


def test_secret_path_hostname_keeps_its_404_rule(app_client, test_namespace, tunnel):
    """正式站 MCP 公開網址的形狀：秘密 path → 服務、同 hostname 其他 path → http_status:404。

    park 只能動 path 那條；那條 404 若被改指說明頁，unpark 會把它換成服務 URL，等於拆掉秘密
    path 的保護。真 Service（自動偵測）下 round trip 必須逐字還原。
    """
    _apply_manifest(_service(test_namespace, "paas-suspended-page", port=8080))
    _apply_manifest(_service(test_namespace, RELEASE, release=RELEASE))
    fake, hostname = tunnel["fake"], tunnel["hostname"]
    fake.ingress[1:3] = [
        {"hostname": hostname, "path": "^/private_int(/|$)", "service": tunnel["service_url"]},
        {"hostname": hostname, "service": "http_status:404"},
    ]
    before = copy.deepcopy(fake.ingress)

    park = app_client.post(
        f"/api/releases/{test_namespace}/{RELEASE}/route/park", json={"subdomain": SUBDOMAIN})
    assert park.status_code == 200, park.text
    assert park.json()["changed"] == 1
    assert _host_services(fake, hostname) == [tunnel["page_url"], "http_status:404"]

    unpark = app_client.post(
        f"/api/releases/{test_namespace}/{RELEASE}/route/unpark",
        json={"expose": {"enabled": True, "subdomain": SUBDOMAIN}})
    assert unpark.status_code == 200, unpark.text
    assert fake.ingress == before
