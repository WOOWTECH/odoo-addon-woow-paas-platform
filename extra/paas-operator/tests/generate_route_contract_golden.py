"""產生 `route` 的 wire 契約 golden——**打真的端點**，不是憑印象選 dump 模式。

## 為什麼要這支

`#648` 的 (a) 半橫跨兩個 repo 邊界：operator 產 `ReleaseInfo.route`，Odoo 的
`_expose_failed()` 讀它。Odoo 那邊的測試自己組回應 dict ⇒ **它釘住的是作者的信念，
不是 operator 真的回什麼**。只要序列化出來的形狀跟手寫的不同（欄位名、`str, Enum`
在 pydantic v2 下 `model_dump()` 與 `model_dump(mode="json")` 出來的東西不一樣、
FastAPI 走哪一條、`None` 欄位有沒有被 `exclude_none` 掉…），Odoo 測試照樣全綠而
production 直接斷。那是這張 PR 兩半之間唯一沒有守線的接縫。

## 判準：以**真的 HTTP body** 為權威

本檔用 `TestClient` 打 `POST /api/releases`，拿 `response.json()`——那是 production
實際會送出去的 bytes 經過的同一條路。順帶把 `model_dump()` 與
`model_dump(mode="json")` 兩種也算出來對照，讓「FastAPI 走的是哪一條」變成觀測結果
而不是記憶。

## 重現

    cd extra/paas-operator
    /opt/homebrew/bin/python3.11 -m pytest --version   # 依賴確認
    /opt/homebrew/bin/python3.11 tests/generate_route_contract_golden.py

輸出寫到 `src/tests/fixtures/route_contract_golden.json`（**Odoo 側**，因為 `extra/`
不會被打進 addon、沙盒也只掛 `src/`）。operator 端由
`tests/test_route_contract_golden_648.py` 斷言「現在的序列化 == golden」，Odoo 端直接
把 golden 當 fixture 讀 ⇒ 任一端改欄位，一端必紅。
"""
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient          # noqa: E402

from src.main import app                            # noqa: E402
from src.models.schemas import ReleaseInfo, ReleaseStatus  # noqa: E402
from src.services.cloudflare import CloudflareException, RouteOutcome  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
OUT = REPO_ROOT / "src" / "tests" / "fixtures" / "route_contract_golden.json"

_RELEASE = dict(
    name="svc-648",
    namespace="paas-ws-test-648",
    revision=1,
    status=ReleaseStatus.DEPLOYED,
    chart="chart",
    chart_version="1.0.0",
    app_version="1.0.0",
    updated="2026-08-22T00:00:00Z",
)
_HOST = "paas-cs-648a-abcd1234"


def _post(client, cf, k8s=None, autodetect=False):
    """打真的端點。

    ``autodetect=True`` ⇒ expose 不帶 service_name/service_port，逼 operator 走
    「自己去 K8s 找 service」那條路——那是 ``failed_no_services`` 唯一到得了的入口。
    第一版忘了這件事，帶著顯式的 service_name/port 去產那一格，結果拿到 ``status:
    ok``：那條分支**根本沒被走到**，而輸出看起來像一份正常的 golden。
    """
    expose = {"enabled": True, "subdomain": _HOST}
    if not autodetect:
        expose.update({"service_name": "svc", "service_port": 8080})
    with patch("src.api.releases.helm_service") as helm, \
            patch("src.api.releases.cloudflare_service", cf), \
            patch("src.api.releases.settings") as st, \
            patch("src.api.releases.asyncio.sleep", new=AsyncMock()), \
            patch("src.api.releases.k8s_service", k8s or MagicMock()), \
            patch("src.api.releases._ensure_image_pull_secrets",
                  side_effect=lambda ns, v: v):
        helm.install.return_value = ReleaseInfo(**_RELEASE)
        st.cloudflare_domain = "woowtech.io"
        return client.post("/api/releases", json={
            "namespace": "paas-ws-test-648",
            "name": "svc-648",
            "chart": "chart",
            "expose": expose,
        })


def _cf(**kw):
    cf = MagicMock()
    cf.enabled = kw.pop("enabled", True)
    cf.generate_subdomain = MagicMock(return_value=_HOST)
    cf.generate_service_url = MagicMock(
        return_value="http://svc.paas-ws-test-648.svc.cluster.local:8080")
    cf.create_route = AsyncMock(**kw) if kw else AsyncMock(
        return_value=RouteOutcome(configured=True, dns_failed=False))
    return cf


def main():
    with patch("src.main.settings") as ms:
        ms.api_key = "k"
        ms.namespace_prefix = "paas-ws-"
        client = TestClient(app, headers={"X-API-Key": "k"})

        cases = {}

        r = _post(client, _cf())
        cases["ok_dns_healthy"] = r.json()

        r = _post(client, _cf(return_value=RouteOutcome(configured=True,
                                                        dns_failed=True)))
        cases["ok_dns_failed"] = r.json()

        r = _post(client, _cf(side_effect=CloudflareException("cf is down")))
        cases["failed_cf_error"] = r.json()

        # 找不到可 expose 的 service：service_url 在那條路上**還沒被算出來**
        # ⇒ 這一格是驗 `service_url` 真的會是 null 的唯一入口。
        k8s = MagicMock()
        k8s.get_services.return_value = []
        r = _post(client, _cf(), k8s=k8s, autodetect=True)
        cases["failed_no_services"] = r.json()

        cf_off = _cf(); cf_off.enabled = False
        r = _post(client, cf_off)
        cases["cloudflare_disabled"] = r.json()

    # FastAPI 實際走哪一條序列化路徑——用觀測而不是記憶回答
    probe = ReleaseInfo(**_RELEASE)
    probe.route = None
    plain = probe.model_dump()
    as_json = probe.model_dump(mode="json")
    print("== dump 模式對照（只看 status 這個 str,Enum 欄位）==")
    print("  model_dump()['status']            =", repr(plain["status"]))
    print("  model_dump(mode='json')['status'] =", repr(as_json["status"]))
    print("  HTTP body['status']               =",
          repr(cases["ok_dns_healthy"]["status"]))

    # 🔴 自檢：每一格必須真的長成它宣稱的樣子。沒有這一步，一格「分支沒被走到」
    # 的錯誤會產出一份看起來很正常的 golden，然後被兩端當成契約釘住。
    expect = {
        "ok_dns_healthy":      lambda r: r and r["status"] == "ok" and r["dns_failed"] is False,
        "ok_dns_failed":       lambda r: r and r["status"] == "ok" and r["dns_failed"] is True,
        "failed_cf_error":     lambda r: r and r["status"] == "failed",
        "failed_no_services":  lambda r: r and r["status"] == "failed" and r["service_url"] is None,
        "cloudflare_disabled": lambda r: r is None,
    }
    bad = [k for k, ok in expect.items() if not ok(cases[k].get("route"))]
    if bad:
        raise SystemExit(
            "這幾格沒長成宣稱的樣子（分支沒被走到？）：%s\n%s"
            % (bad, json.dumps({k: cases[k].get("route") for k in bad},
                               indent=2, ensure_ascii=False)))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(cases, indent=2, ensure_ascii=False,
                              sort_keys=True) + "\n", encoding="utf-8")
    print("\n== 寫出 golden:", OUT)
    for k, v in sorted(cases.items()):
        print("\n---- %s ----" % k)
        print(json.dumps(v.get("route", "<no route key>"),
                         indent=2, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
