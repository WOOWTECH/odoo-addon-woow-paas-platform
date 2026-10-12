"""#1513 integration：真 Longhorn validator 判斷 PVC 擴容；被拒時 helm 什麼都沒套用。

單元測試的 ``kubectl patch --dry-run=server`` 是 mock，看不到：

1. Longhorn ``validator.longhorn.io``（``sideEffects: None``）在 server dry-run 下真的被呼叫，
   拒絕訊息的形狀與 ``classify_expansion_rejection`` 的判準一致；
2. dry-run 真的不改 PVC；
3. 升級被 409 擋下時，同一個 release 的其他資源（Deployment）也沒被改——#1158 實測的
   「Deployment 換新、data PVC 沒擴、release failed」半套狀態不會再出現。

建一顆 1Gi 的 Longhorn PVC（共用叢集上的最小代價）；「放不下」的大小取
所有 disk 的最高 over-provision 排程上限再加 1 GiB，任何一顆磁碟都放不下這個增量。
先以 backend dry-run 確認拒絕，才跑真正的拒絕升級案例，避免負測誤佔排程額度。
叢集沒有 Longhorn 時整檔 skip。
"""
import json
import subprocess
import time

import pytest

pytestmark = pytest.mark.integration

GIB = 1024 ** 3


def _kubectl_json(*args):
    out = subprocess.run(["kubectl", *args, "-o", "json"], capture_output=True, text=True)
    if out.returncode != 0:
        return None
    return json.loads(out.stdout)


@pytest.fixture(scope="module", autouse=True)
def _require_longhorn():
    if _kubectl_json("get", "nodes.longhorn.io", "-n", "longhorn-system") is None:
        pytest.skip("no readable Longhorn in this cluster")


def _longhorn_class():
    classes = _kubectl_json("get", "storageclasses")["items"]
    names = [c["metadata"]["name"] for c in classes
             if c.get("provisioner") == "driver.longhorn.io" and c.get("allowVolumeExpansion")]
    for preferred in ("longhorn-delete", "longhorn"):
        if preferred in names:
            return preferred
    return names[0]


def _chart(chart_dir, storage_class):
    (chart_dir / "templates").mkdir(parents=True, exist_ok=True)
    (chart_dir / "Chart.yaml").write_text(
        "apiVersion: v2\nname: expansion-probe-1513\nversion: 0.1.0\n")
    (chart_dir / "values.yaml").write_text("size: 1Gi\nmarker: v1\n")
    (chart_dir / "templates" / "pvc.yaml").write_text(
        "apiVersion: v1\nkind: PersistentVolumeClaim\nmetadata:\n"
        "  name: {{ .Release.Name }}-data\n"
        f"spec:\n  storageClassName: {storage_class}\n  accessModes: [ReadWriteOnce]\n"
        "  resources:\n    requests:\n      storage: {{ .Values.size | quote }}\n")
    (chart_dir / "templates" / "cm.yaml").write_text(
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: {{ .Release.Name }}-marker\n"
        "data:\n  marker: {{ .Values.marker | quote }}\n")
    return str(chart_dir)


def _too_big_delta(app_client, namespace) -> int:
    body = app_client.get("/api/cluster/storage-capacity",
                          params={"storage_class": _longhorn_class()}).json()
    assert body["available"] is True, body
    # available/degraded 是目前保守可開通額度，不是 admission 的硬上限。
    # Longhorn 擴容仍可用 over-provision 額度；加固定 1TiB 不保證放不下。
    nodes = _kubectl_json("get", "nodes.longhorn.io", "-n", "longhorn-system")
    setting = _kubectl_json("get", "settings.longhorn.io",
                           "storage-over-provisioning-percentage", "-n", "longhorn-system")
    percent = int(setting["value"])
    maxima = [int(disk["storageMaximum"])
              for node in nodes["items"]
              for disk in node.get("status", {}).get("diskStatus", {}).values()
              if int(disk.get("storageMaximum", 0)) > 0]
    assert maxima and percent > 0
    delta = max(maxima) * max(percent, 100) // 100 + GIB
    requested = str(GIB + delta)
    dry = subprocess.run([
        "kubectl", "patch", "pvc", "probe-data", "-n", namespace,
        "--type=merge", "--dry-run=server", "-p",
        json.dumps({"spec": {"resources": {"requests": {"storage": requested}}}}),
    ], capture_output=True, text=True)
    assert dry.returncode != 0 and "validator.longhorn.io" in dry.stderr, (
        "negative expansion prerequisite must be rejected by Longhorn admission")
    assert _pvc_size(namespace) == "1Gi"
    return delta


@pytest.fixture
def probe_release(app_client, test_namespace, tmp_path):
    chart = _chart(tmp_path / "probe", _longhorn_class())
    r = app_client.post("/api/releases", json={
        "namespace": test_namespace, "name": "probe", "chart": chart})
    assert r.status_code == 201, r.text
    # PVC Pending 還没有對應的 Longhorn volume，server dry-run 可能跳過 validator。
    _bound_volume_name(test_namespace)
    return chart


def _pvc_size(namespace):
    return _kubectl_json("get", "pvc", "probe-data", "-n", namespace)["spec"]["resources"][
        "requests"]["storage"]


def _marker(namespace):
    return _kubectl_json("get", "configmap", "probe-marker", "-n", namespace)["data"]["marker"]


def _revision(namespace):
    from src.services.helm import HelmService

    out = HelmService()._run_command(["list", "--namespace", namespace, "-o", "json"])
    return [r["revision"] for r in json.loads(out.stdout or "[]")]


def _bound_volume_name(namespace, timeout=120):
    """等 PVC Bound，回傳 ``spec.volumeName``（Longhorn 是 Immediate，通常幾秒內）。"""
    deadline = time.monotonic() + timeout
    while True:
        pvc = _kubectl_json("get", "pvc", "probe-data", "-n", namespace) or {}
        if (pvc.get("status") or {}).get("phase") == "Bound":
            return pvc["spec"]["volumeName"]
        assert time.monotonic() < deadline, f"probe-data never bound: {pvc.get('status')}"
        time.sleep(2)


def test_preflight_accepts_a_small_expansion_and_changes_nothing(
    app_client, test_namespace, probe_release,
):
    volume = _bound_volume_name(test_namespace)
    r = app_client.post(f"/api/releases/{test_namespace}/probe/upgrade-preflight", json={
        "chart": probe_release, "reset_values": True, "values": {"size": "2Gi"}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True, body
    # 只比對平台用得到的欄位；87998eac 起每筆多一個 ``volume``（live PVC 的 spec.volumeName），
    # replica 排程檢查就是拿它去讀 volumes.longhorn.io——對真 PVC 驗它接對了（#1616）。
    [exp] = body["expansions"]
    assert {k: exp[k] for k in ("name", "live", "rendered")} == {
        "name": "probe-data", "live": "1Gi", "rendered": "2Gi"}, body
    assert exp.get("volume") == volume, body
    assert _pvc_size(test_namespace) == "1Gi"
    assert _revision(test_namespace) == ["1"]


def test_preflight_reports_longhorn_capacity_denial(app_client, test_namespace, probe_release):
    size = str(GIB + _too_big_delta(app_client, test_namespace))
    r = app_client.post(f"/api/releases/{test_namespace}/probe/upgrade-preflight", json={
        "chart": probe_release, "reset_values": True, "values": {"size": size}})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["error_code"] == "storage_expansion_rejected"
    assert detail["reason"] == "capacity", detail
    assert detail["pvcs"][0]["name"] == "probe-data"
    assert _pvc_size(test_namespace) == "1Gi"


def test_upgrade_refused_before_anything_is_applied(app_client, test_namespace, probe_release):
    size = str(GIB + _too_big_delta(app_client, test_namespace))
    r = app_client.patch(f"/api/releases/{test_namespace}/probe", json={
        "chart": probe_release, "reset_values": True,
        "values": {"size": size, "marker": "v2"}})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["reason"] == "capacity"
    # 其他資源也沒動、release 沒有新 revision（#1158 的半套狀態）
    assert _marker(test_namespace) == "v1"
    assert _pvc_size(test_namespace) == "1Gi"
    assert _revision(test_namespace) == ["1"]


def test_upgrade_that_fits_still_expands(app_client, test_namespace, probe_release):
    r = app_client.patch(f"/api/releases/{test_namespace}/probe", json={
        "chart": probe_release, "reset_values": True,
        "values": {"size": "2Gi", "marker": "v2"}})
    assert r.status_code == 200, r.text
    assert _pvc_size(test_namespace) == "2Gi"
    assert _marker(test_namespace) == "v2"
