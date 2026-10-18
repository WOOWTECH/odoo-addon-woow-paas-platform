"""#1685 integration：純 ``helm rollback`` 的 compute 配額預檢——真 helm＋真 kubectl＋真 admission。

單元測試 mock 掉 helm，量不到三件事：

1. ``helm get manifest --revision <n>`` 在真 release 上讀得出**目標** revision 的 workload，
   而且解析後交給 ``compute_quota.evaluate`` 算得出與真 ResourceQuota admission 一致的結論；
2. 被 409 擋下時 helm 什麼都沒做：沒有新 revision、Deployment 沒被改、舊 pod 還是同一顆；
3. 陽性對照：同一個回復不帶旗標照常送出——叢集真的把回復後的 pod 擋在 admission，
   ``Recreate`` 剩 0 個 pod（就是 2026-10-10 的事故，只是換成「回復版本」觸發）。

形狀：rev 1 要 1 cpu，升級成 rev 2 只要 500m，再把平台 quota 調成 800m（rev 2 放得下、
rev 1 放不下）⇒「回復目標比現在大」。chart 與 quota 的造法沿用
``test_compute_quota_preflight_1647.py``：所有 pod 帶不存在的 nodeSelector 停在 Pending（仍計入
quota、不拉 image、不在任何節點上起容器）。

Run::

    OPERATOR_TEST_NAMESPACE_PREFIX=dev-sandbox-<topic>- KUBECONFIG=... \\
    HELM_BINARY=<helm 3.13.3> \\
        pytest -m integration tests/integration/test_rollback_compute_quota_1685.py -v
"""
import json
import logging

import pytest

from tests.integration.test_compute_quota_preflight_1647 import (
    _DEPLOYMENT_TMPL,
    DEPLOYMENT,
    ERROR_CODE,
    RELEASE,
    _apply,
    _kubectl_json,
    _pods,
    _quota_rejections,
    _set_quota,
    _wait,
    _wait_used,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def quota_namespace(test_namespace):
    """空 namespace ＋ 平台的 ResourceQuota（cpu 2）與 LimitRange，同 ``create_namespace`` 寫的那兩個。"""
    from src.services.helm import KubernetesService

    _set_quota(test_namespace, "2")
    _apply(KubernetesService()._build_limitrange_manifest(test_namespace))
    return test_namespace


@pytest.fixture
def chart(tmp_path):
    chart_dir = tmp_path / "quota-probe"
    (chart_dir / "templates").mkdir(parents=True)
    (chart_dir / "Chart.yaml").write_text(
        "apiVersion: v2\nname: quota-probe-1685\nversion: 0.1.0\n")
    (chart_dir / "values.yaml").write_text(
        'cpu: 250m\nmarker: v1\nreplicas: 1\nstrategy: Recreate\n')
    (chart_dir / "templates" / "deployment.yaml").write_text(_DEPLOYMENT_TMPL)
    return str(chart_dir)


def _history(namespace):
    from src.services.helm import HelmService

    out = HelmService()._run_command(
        ["history", RELEASE, "--namespace", namespace, "-o", "json"])
    return [(r["revision"], r["status"]) for r in json.loads(out.stdout or "[]")]


def _deployment(namespace):
    return _kubectl_json("get", "deployment", DEPLOYMENT, "-n", namespace)


def _template_cpu(deployment):
    return deployment["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["cpu"]


def _rev1_big_rev2_small(app_client, ns, chart):
    """rev 1：1 cpu；rev 2：500m（``Recreate``）。回傳 rev 2 的 pod。"""
    r = app_client.post("/api/releases", json={
        "namespace": ns, "name": RELEASE, "chart": chart,
        "values": {"cpu": "1", "marker": "v1"}})
    assert r.status_code == 201, r.text
    [rev1_pod] = _wait(lambda: _pods(ns, DEPLOYMENT), "the rev 1 pod")
    _wait_used(ns, "1")

    r = app_client.patch(f"/api/releases/{ns}/{RELEASE}", json={
        "chart": chart, "reset_values": True, "values": {"cpu": "500m", "marker": "v2"}})
    assert r.status_code == 200, r.text
    [rev2_pod] = _wait(
        lambda: [p for p in _pods(ns, DEPLOYMENT)
                 if p["metadata"]["uid"] != rev1_pod["metadata"]["uid"]],
        "the rev 2 pod", timeout=180)
    assert rev2_pod["spec"]["containers"][0]["resources"]["limits"]["cpu"] == "500m"
    _wait_used(ns, "500m")
    assert [rev for rev, _ in _history(ns)] == [1, 2]
    return rev2_pod


def test_checked_plain_rollback_that_cannot_fit_is_refused_before_anything_applied(
        app_client, quota_namespace, chart):
    ns = quota_namespace
    rev2_pod = _rev1_big_rev2_small(app_client, ns, chart)
    _set_quota(ns, "800m")
    generation = _deployment(ns)["metadata"]["generation"]

    r = app_client.post(f"/api/releases/{ns}/{RELEASE}/rollback",
                        json={"revision": 1, "check_compute_quota": True})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["error_code"] == ERROR_CODE
    assert detail["namespace"] == ns and detail["release"] == RELEASE
    assert detail["quota"] == f"{ns}-quota" and detail["quota_source"] == "live"
    by_resource = {s["resource"]: s for s in detail["shortfalls"]}
    # 平台的 quota 把 requests.cpu 與 limits.cpu 設成同一個值，容器只寫 limits ⇒ 兩個都放不下。
    assert set(by_resource) == {"limits.cpu", "requests.cpu"}, detail
    limits = by_resource["limits.cpu"]
    # rev 1 的 1 cpu（來自 stored manifest，不是現在的 500m）；Recreate 先砍自己的舊 pod ⇒ 可用 0.8。
    assert (limits["required"], limits["available"], limits["short_by"]) == ("1", "0.8", "0.2")
    assert limits["release_current"] == "0.5" and limits["hard"] == "0.8"

    # 什麼都沒套用：沒有新 revision、Deployment 沒被改、舊 pod 還是同一顆。
    assert _history(ns) == [(1, "superseded"), (2, "deployed")]
    deployment = _deployment(ns)
    assert deployment["metadata"]["generation"] == generation
    assert _template_cpu(deployment) == "500m"
    [pod] = _pods(ns, DEPLOYMENT)
    assert pod["metadata"]["uid"] == rev2_pod["metadata"]["uid"]


def test_without_the_flag_the_same_rollback_goes_through_and_the_cluster_rejects_the_pod(
        app_client, quota_namespace, chart):
    """陽性對照：不帶旗標照常回復（行為與 #1685 之前相同）——而叢集真的擋下回復後的 pod。"""
    ns = quota_namespace
    _rev1_big_rev2_small(app_client, ns, chart)
    _set_quota(ns, "800m")

    r = app_client.post(f"/api/releases/{ns}/{RELEASE}/rollback", json={"revision": 1})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "rollback" and body["target_revision"] == 1
    assert "compute_quota" not in body
    assert [rev for rev, _ in _history(ns)] == [1, 2, 3]
    assert _template_cpu(_deployment(ns)) == "1"

    [event] = _wait(lambda: _quota_rejections(ns)[:1], "FailedCreate exceeded quota", timeout=120)
    assert f"exceeded quota: {ns}-quota" in event["message"]
    # Recreate：舊 pod 已經被砍、回復後的 pod 建不出來 ⇒ 0 個 pod。
    assert _pods(ns, DEPLOYMENT) == []


def test_checked_plain_rollback_that_fits_is_rolled_back(
        app_client, quota_namespace, chart, caplog):
    """放得下（quota 2）：預檢真的跑了、放行，而且回復後的 pod 真的建得出來。"""
    ns = quota_namespace
    rev2_pod = _rev1_big_rev2_small(app_client, ns, chart)

    with caplog.at_level(logging.INFO, logger="src.api.releases"):
        r = app_client.post(f"/api/releases/{ns}/{RELEASE}/rollback",
                            json={"revision": 1, "check_compute_quota": True})
    assert r.status_code == 200, r.text
    assert f"Compute quota pre-check passed for {ns}/{RELEASE}" in caplog.text
    assert "limits.cpu required=1 available=2" in caplog.text
    assert [rev for rev, _ in _history(ns)] == [1, 2, 3]

    [pod] = _wait(
        lambda: [p for p in _pods(ns, DEPLOYMENT)
                 if p["metadata"]["uid"] != rev2_pod["metadata"]["uid"]],
        "the rolled-back pod", timeout=180)
    assert pod["spec"]["containers"][0]["resources"]["limits"]["cpu"] == "1"
    _wait_used(ns, "1")
