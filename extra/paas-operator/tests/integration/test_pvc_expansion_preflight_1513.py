"""#1513 integration：真 Longhorn validator 判斷 PVC 擴容；被拒時 helm 什麼都沒套用。

單元測試的 ``kubectl patch --dry-run=server`` 是 mock，看不到：

1. Longhorn ``validator.longhorn.io``（``sideEffects: None``）在 server dry-run 下真的被呼叫，
   拒絕訊息的形狀與 ``classify_expansion_rejection`` 的判準一致；
2. dry-run 真的不改 PVC；
3. 升級被 409 擋下時，同一個 release 的其他資源（Deployment）也沒被改——#1158 實測的
   「Deployment 換新、data PVC 沒擴、release failed」半套狀態不會再出現。

建一顆 1Gi 的 Longhorn PVC（共用叢集上的最小代價）；「放不下」的大小取「目前最大可放的
volume」再加 1 TiB，任何一顆磁碟都放不下這個增量。叢集沒有 Longhorn 時整檔 skip。
"""
import json
import subprocess

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


def _too_big_delta(app_client) -> int:
    body = app_client.get("/api/cluster/storage-capacity",
                          params={"storage_class": _longhorn_class()}).json()
    assert body["available"] is True, body
    return body["largest_placeable_volume_degraded_bytes"] + 1024 * GIB


@pytest.fixture
def probe_release(app_client, test_namespace, tmp_path):
    chart = _chart(tmp_path / "probe", _longhorn_class())
    r = app_client.post("/api/releases", json={
        "namespace": test_namespace, "name": "probe", "chart": chart})
    assert r.status_code == 201, r.text
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


def test_preflight_accepts_a_small_expansion_and_changes_nothing(
    app_client, test_namespace, probe_release,
):
    r = app_client.post(f"/api/releases/{test_namespace}/probe/upgrade-preflight", json={
        "chart": probe_release, "reset_values": True, "values": {"size": "2Gi"}})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "expansions": [
        {"name": "probe-data", "live": "1Gi", "rendered": "2Gi"}]}
    assert _pvc_size(test_namespace) == "1Gi"
    assert _revision(test_namespace) == ["1"]


def test_preflight_reports_longhorn_capacity_denial(app_client, test_namespace, probe_release):
    size = str(GIB + _too_big_delta(app_client))
    r = app_client.post(f"/api/releases/{test_namespace}/probe/upgrade-preflight", json={
        "chart": probe_release, "reset_values": True, "values": {"size": size}})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["error_code"] == "storage_expansion_rejected"
    assert detail["reason"] == "capacity", detail
    assert detail["pvcs"][0]["name"] == "probe-data"
    assert _pvc_size(test_namespace) == "1Gi"


def test_upgrade_refused_before_anything_is_applied(app_client, test_namespace, probe_release):
    size = str(GIB + _too_big_delta(app_client))
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
