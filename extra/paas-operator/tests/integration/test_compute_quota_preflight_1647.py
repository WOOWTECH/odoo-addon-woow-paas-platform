"""#1647 integration：compute 配額預檢的算式與真 ResourceQuota admission 一致。

單元測試餵的是手寫的 quota／pod JSON，看不到：

1. 預檢擋下的升級，叢集**真的**會拒絕那顆 pod（陽性對照：同一個升級不帶旗標送出去，
   ReplicaSet 出現 ``FailedCreate … exceeded quota``、剩 0 個 pod——就是 2026-10-10 的事故）；
2. 預檢放行的升級，叢集**真的**建得出新 pod（``Recreate`` 先砍舊 pod、扣掉它之後放得下；
   ``RollingUpdate`` 同時調小 replicas 時，舊 pod 先被縮掉、之後才多建新的）；
3. 被 409 擋下時 helm 什麼都沒套用（Deployment 的 generation、舊 pod 的 UID 都沒變）；
4. 沒寫 resources 的容器，預檢算的需求等於 LimitRanger 真的注入 pod 的值；
5. ``kubectl get resourcequotas,limitranges,pods`` 的真實輸出形狀（``status.used`` 的寫法、
   pod 的 ownerReferences）接得上 ``compute_quota.evaluate``。

quota 與 LimitRange 用 operator 自己的組裝函式產生（與 ``create_namespace`` 寫進租戶
namespace 的是同一份）。所有 pod 都帶一個不存在的 nodeSelector 停在 Pending：Pending 的 pod
照樣計入 quota，不必拉 image、也不會在任何節點上起容器。

Run::

    OPERATOR_TEST_NAMESPACE_PREFIX=dev-sandbox-<topic>- KUBECONFIG=... \\
    HELM_BINARY=<helm 3.13.3> \\
        pytest -m integration tests/integration/test_compute_quota_preflight_1647.py -v
"""
import json
import subprocess
import time

import pytest

pytestmark = pytest.mark.integration

RELEASE = "probe"
DEPLOYMENT = f"{RELEASE}-app"
ERROR_CODE = "workspace_compute_quota_exceeded"
#: 不存在的節點 label：pod 永遠 Pending（仍計入 quota）。
_NO_SUCH_NODE = {"woow.tw/no-such-node-1647": "true"}

_DEPLOYMENT_TMPL = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ .Release.Name }}-app
spec:
  replicas: {{ .Values.replicas }}
  strategy:
    type: {{ .Values.strategy }}
  selector:
    matchLabels:
      app: {{ .Release.Name }}-app
  template:
    metadata:
      labels:
        app: {{ .Release.Name }}-app
    spec:
      nodeSelector:
        woow.tw/no-such-node-1647: "true"
      containers:
      - name: app
        image: registry.k8s.io/pause:3.9
        env:
        - name: MARKER
          value: {{ .Values.marker | quote }}
        {{- if .Values.cpu }}
        resources:
          limits:
            cpu: {{ .Values.cpu | quote }}
            memory: 64Mi
        {{- end }}
"""


def _kubectl(*args, input_text=None, check=True):
    return subprocess.run(
        ["kubectl", *args], input=input_text, capture_output=True, text=True, check=check)


def _kubectl_json(*args):
    return json.loads(_kubectl(*args, "-o", "json").stdout)


def _apply(manifest):
    _kubectl("apply", "-f", "-", input_text=json.dumps(manifest))


def _wait(probe, what, timeout=90):
    """輪詢到 ``probe()`` 回真值為止，回傳那個值。"""
    deadline = time.monotonic() + timeout
    while True:
        value = probe()
        if value:
            return value
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(1)


def _quota(namespace):
    return _kubectl_json("get", "resourcequota", f"{namespace}-quota", "-n", namespace)


def _set_quota(namespace, cpu):
    """套用平台的 ResourceQuota（``limits.cpu``＝``requests.cpu``＝``cpu``），等 quota controller 接手。"""
    from src.services.helm import KubernetesService
    from src.services.storage_quota import parse_quantity

    _apply(KubernetesService._build_quota_manifest(namespace, cpu, "4Gi", "10Gi"))
    _wait(lambda: parse_quantity(((_quota(namespace).get("status") or {}).get("hard") or {})
                                 .get("limits.cpu")) == parse_quantity(cpu),
          f"quota status.hard limits.cpu={cpu}")


def _wait_used(namespace, cpu):
    from src.services.storage_quota import parse_quantity

    _wait(lambda: parse_quantity(_quota(namespace)["status"].get("used", {})
                                 .get("limits.cpu")) == parse_quantity(cpu),
          f"quota status.used limits.cpu={cpu}")


def _pods(namespace, app):
    """``app`` 這個 workload 現在的 pod（不含正在終止的）。"""
    items = _kubectl_json("get", "pods", "-n", namespace, "-l", f"app={app}")["items"]
    return [p for p in items if not p["metadata"].get("deletionTimestamp")]


def _quota_rejections(namespace):
    events = _kubectl_json("get", "events", "-n", namespace,
                           "--field-selector", "reason=FailedCreate")["items"]
    return [e for e in events
            if e.get("involvedObject", {}).get("kind") == "ReplicaSet"
            and e["involvedObject"].get("name", "").startswith(f"{DEPLOYMENT}-")
            and "exceeded quota" in (e.get("message") or "")]


def _rejection_marks(namespace):
    """目前每一筆 exceeded-quota 拒絕事件的 ``(uid, count, lastTimestamp)``。

    同一個 ReplicaSet 反覆被拒時 k8s 把事件聚合成一筆、遞增 ``count``——只數筆數看不出
    「還在被拒」。
    """
    return sorted(
        (e["metadata"]["uid"], e.get("count") or 1, e.get("lastTimestamp") or "")
        for e in _quota_rejections(namespace))


def _assert_no_further_rejections(namespace, settle_seconds=8):
    """新 pod 已經在了：ReplicaSet 不該再去建 pod，所以不該再有**新的** quota 拒絕。

    在這之前的拒絕不算失敗（quota 的 ``status.used`` 回補慢一步時的暫時性事件，之後重試
    成功）——只印出來，``pytest -rP`` 看得到這一輪有沒有遇到。事件會被聚合，同一筆的
    ``count``／``lastTimestamp`` 變了也算「新增」。
    """
    settled = _rejection_marks(namespace)
    print(f"exceeded-quota rejections before the new pod was admitted: {len(settled)} {settled}")
    time.sleep(settle_seconds)
    assert _rejection_marks(namespace) == settled, "新 pod 建出來之後仍在被 quota 拒絕"


def _revisions(namespace):
    from src.services.helm import HelmService

    out = HelmService()._run_command(["list", "--namespace", namespace, "-o", "json"])
    return [r["revision"] for r in json.loads(out.stdout or "[]")]


def _body(chart, cpu, marker="v2", values=None, **extra):
    return {"chart": chart, "reset_values": True,
            "values": {"cpu": cpu, "marker": marker, **(values or {})}, **extra}


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
        "apiVersion: v2\nname: quota-probe-1647\nversion: 0.1.0\n")
    (chart_dir / "values.yaml").write_text(
        'cpu: 250m\nmarker: v1\nreplicas: 1\nstrategy: Recreate\n')
    (chart_dir / "templates" / "deployment.yaml").write_text(_DEPLOYMENT_TMPL)
    return str(chart_dir)


def _external_workload(namespace, cpu="1500m"):
    """平台外的 workload（直接用 kubectl 建的 Deployment）佔掉 ``cpu``，同事故裡手動建的 PG。"""
    _apply({
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": "external", "namespace": namespace},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": "external"}},
            "template": {
                "metadata": {"labels": {"app": "external"}},
                "spec": {
                    "nodeSelector": _NO_SUCH_NODE,
                    "containers": [{
                        "name": "app", "image": "registry.k8s.io/pause:3.9",
                        "resources": {"limits": {"cpu": cpu, "memory": "64Mi"}},
                    }],
                },
            },
        },
    })
    _wait(lambda: _pods(namespace, "external"), "the external workload's pod")


def _install(app_client, namespace, chart, cpu):
    r = app_client.post("/api/releases", json={
        "namespace": namespace, "name": RELEASE, "chart": chart,
        "values": {"cpu": cpu, "marker": "v1"}})
    assert r.status_code == 201, r.text
    [pod] = _wait(lambda: _pods(namespace, DEPLOYMENT), "the release's pod")
    return pod


def test_recreate_upgrade_refused_before_anything_applied(app_client, quota_namespace, chart):
    ns = quota_namespace
    _external_workload(ns, "1500m")
    old_pod = _install(app_client, ns, chart, "250m")
    _wait_used(ns, "1750m")
    generation = _kubectl_json("get", "deployment", DEPLOYMENT, "-n", ns)["metadata"]["generation"]

    r = app_client.patch(f"/api/releases/{ns}/{RELEASE}",
                         json=_body(chart, "1", check_compute_quota=True))
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["error_code"] == ERROR_CODE
    assert detail["namespace"] == ns and detail["release"] == RELEASE
    assert detail["quota"] == f"{ns}-quota" and detail["quota_source"] == "live"
    by_resource = {s["resource"]: s for s in detail["shortfalls"]}
    # 平台的 quota 把 requests.cpu 與 limits.cpu 設成同一個值，容器只寫 limits ⇒ 兩個都放不下。
    assert set(by_resource) == {"limits.cpu", "requests.cpu"}, detail
    assert by_resource["limits.cpu"] == {
        "resource": "limits.cpu", "dimension": "cpu", "kind": "limits",
        "phase": "steady", "workload": None,
        "hard": "2", "used": "1.75", "release_current": "0.25", "other": "1.5",
        "required": "1", "available": "0.5", "short_by": "0.5",
    }
    assert detail["consumers"] == [{"owner": "Deployment/external", "pods": 1, "value": "1.5"}]

    # 什麼都沒套用：Deployment 沒被改、舊 pod 還是同一顆、沒有新 revision。
    assert _kubectl_json("get", "deployment", DEPLOYMENT, "-n", ns)[
        "metadata"]["generation"] == generation
    [pod] = _pods(ns, DEPLOYMENT)
    assert pod["metadata"]["uid"] == old_pod["metadata"]["uid"]
    assert _revisions(ns) == ["1"]


def test_without_the_flag_the_cluster_really_rejects_the_pod(app_client, quota_namespace, chart):
    """陽性對照：預檢會擋的那個升級，不帶旗標送出去——叢集真的把新 pod 擋在 admission。"""
    ns = quota_namespace
    _external_workload(ns, "1500m")
    _install(app_client, ns, chart, "250m")
    _wait_used(ns, "1750m")

    r = app_client.patch(f"/api/releases/{ns}/{RELEASE}", json=_body(chart, "1"))
    assert r.status_code == 200, r.text

    [event] = _wait(lambda: _quota_rejections(ns)[:1], "FailedCreate exceeded quota", timeout=120)
    assert f"exceeded quota: {ns}-quota" in event["message"]
    assert "limits.cpu=1" in event["message"]
    # Recreate：舊 pod 已經被砍、新 pod 建不出來 ⇒ 0 個 pod（事故的樣子）。
    assert _pods(ns, DEPLOYMENT) == []


def test_recreate_fits_once_own_old_pod_is_subtracted(app_client, quota_namespace, chart):
    """used＋new 超過 hard（1.9＋0.5），但舊 pod 會先被砍：used−old＋new＝2.0 放得下。

    主斷言：預檢放行，而且新 pod **真的建得出來**、limits 是新的值，quota 最後剛好用滿。

    ⚠ 不斷言「整個過程沒有任何 ``FailedCreate … exceeded quota``」。admission 用的是 quota 的
    ``status.used``，它在舊 pod 刪掉之後由 quota controller **非同步**回補；``Recreate`` 一刪完
    舊 pod 就建新 pod，回補慢一步時 ReplicaSet 會先記一筆暫時性的拒絕、退避後重試成功。那是
    叢集的時序，不是預檢誤放。否定面只看「新 pod 建出來之後」：不再出現新的拒絕。
    """
    ns = quota_namespace
    _external_workload(ns, "1500m")
    old_pod = _install(app_client, ns, chart, "400m")
    _wait_used(ns, "1900m")

    r = app_client.patch(f"/api/releases/{ns}/{RELEASE}",
                         json=_body(chart, "500m", check_compute_quota=True))
    assert r.status_code == 200, r.text

    def _new_pod():
        return [p for p in _pods(ns, DEPLOYMENT)
                if p["metadata"]["uid"] != old_pod["metadata"]["uid"]]

    [pod] = _wait(_new_pod, "the upgraded pod", timeout=180)
    assert pod["spec"]["containers"][0]["resources"]["limits"]["cpu"] == "500m"
    # 舊 pod 的 400m 已回補、新 pod 的 500m 已入帳：1.5＋0.5 剛好等於上限 2。
    _wait_used(ns, "2")

    _assert_no_further_rejections(ns)
    [still] = _pods(ns, DEPLOYMENT)
    assert still["metadata"]["uid"] == pod["metadata"]["uid"]


def test_rolling_update_that_also_scales_down_gets_its_new_pod(app_client, quota_namespace, chart):
    """3 顆 × 600m（1.8，上限 2）改成 1 顆 × 900m，``RollingUpdate`` 預設值（先建後砍）。

    拿「現在的 1.8」加一顆新 pod 是 2.7，看起來放不下；但 replicas 變了是 scaling event，
    Deployment controller 先把舊 ReplicaSet 縮到 1 顆（0.6），之後才多建新 pod：0.6＋0.9＝1.5。
    預檢必須放行，而且叢集真的建得出那顆新 pod。

    pod 都停在 Pending（不存在的 nodeSelector），新 pod 永遠不會 Ready，所以舊的那 1 顆不會
    被換掉——最後停在「舊 1＋新 1」，正好是預檢算的那個並存狀態。
    """
    ns = quota_namespace
    rolling = {"strategy": "RollingUpdate"}
    r = app_client.post("/api/releases", json={
        "namespace": ns, "name": RELEASE, "chart": chart,
        "values": {"cpu": "600m", "marker": "v1", "replicas": 3, **rolling}})
    assert r.status_code == 201, r.text
    old_pods = _wait(lambda: len(_pods(ns, DEPLOYMENT)) == 3 and _pods(ns, DEPLOYMENT),
                     "the release's three pods")
    old_uids = {p["metadata"]["uid"] for p in old_pods}
    _wait_used(ns, "1800m")

    r = app_client.patch(f"/api/releases/{ns}/{RELEASE}", json=_body(
        chart, "900m", values={"replicas": 1, **rolling}, check_compute_quota=True))
    assert r.status_code == 200, r.text

    def _new_pod():
        return [p for p in _pods(ns, DEPLOYMENT) if p["metadata"]["uid"] not in old_uids]

    [pod] = _wait(_new_pod, "the upgraded pod", timeout=180)
    assert pod["spec"]["containers"][0]["resources"]["limits"]["cpu"] == "900m"
    # 並存狀態：舊 ReplicaSet 剩 1 顆（600m）＋新的 1 顆（900m）。
    _wait_used(ns, "1500m")
    survivors = [p for p in _pods(ns, DEPLOYMENT) if p["metadata"]["uid"] in old_uids]
    assert len(survivors) == 1, [p["metadata"]["name"] for p in survivors]

    _assert_no_further_rejections(ns)


def test_limitrange_defaults_match_what_the_api_server_injects(app_client, quota_namespace, chart):
    """沒寫 resources 的容器：預檢算的每顆 pod 需求＝LimitRanger 真的注入 pod 的值。"""
    from src.services.compute_quota import TRACKED_RESOURCES, format_quantity
    from src.services.storage_quota import parse_quantity

    ns = quota_namespace
    pod = _install(app_client, ns, chart, "")
    injected = pod["spec"]["containers"][0]["resources"]
    expected = {}
    for resource in TRACKED_RESOURCES:
        kind, dimension = resource.split(".", 1)
        if dimension in injected.get(kind, {}):
            expected[resource] = format_quantity(
                resource, parse_quantity(injected[kind][dimension]))
    assert {"limits.cpu", "limits.memory", "requests.cpu", "requests.memory"} <= set(expected), (
        "LimitRange 沒有補上預設值", injected)
    _wait_used(ns, injected["limits"]["cpu"])

    # 放得下：回應裡每個受 quota 管的資源，需求（1 個 replica）等於注入的值。
    r = app_client.post(f"/api/releases/{ns}/{RELEASE}/upgrade-preflight",
                        json=_body(chart, "", check_compute_quota=True))
    assert r.status_code == 200, r.text
    resources = r.json()["compute_quota"]["resources"]
    assert resources, r.json()
    assert {name: numbers["required"] for name, numbers in resources.items()} == {
        name: expected[name] for name in resources}

    # 把上限壓到放不下：409 的 detail 帶每顆 pod 的需求，逐項等於注入的值。
    _set_quota(ns, "100m")
    r = app_client.post(f"/api/releases/{ns}/{RELEASE}/upgrade-preflight",
                        json=_body(chart, "", check_compute_quota=True))
    assert r.status_code == 409, r.text
    [workload] = r.json()["detail"]["workloads"]
    assert workload["name"] == DEPLOYMENT and workload["strategy"] == "Recreate"
    assert workload["per_pod"] == expected


def test_pending_quota_lets_a_larger_plan_through(app_client, quota_namespace, chart):
    """換方案：live 上限放不下、平台之後會推送的上限放得下 ⇒ 預檢放行。"""
    ns = quota_namespace
    _external_workload(ns, "1500m")
    _install(app_client, ns, chart, "250m")
    _wait_used(ns, "1750m")
    url = f"/api/releases/{ns}/{RELEASE}/upgrade-preflight"

    r = app_client.post(url, json=_body(chart, "1", check_compute_quota=True))
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["error_code"] == ERROR_CODE

    r = app_client.post(url, json=_body(chart, "1", check_compute_quota=True, pending_quota={
        "cpu_limit": "4", "memory_limit": "8Gi", "storage_limit": "10Gi"}))
    assert r.status_code == 200, r.text
    compute = r.json()["compute_quota"]
    assert compute["status"] == "ok" and compute["quota_source"] == "pending"
    assert compute["resources"]["limits.cpu"]["hard"] == "4"
    # 預檢不套用任何東西，也沒有動叢集上的 quota。
    assert _revisions(ns) == ["1"]
    assert _quota(ns)["spec"]["hard"]["limits.cpu"] == "2"
