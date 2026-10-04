"""#1415 integration：真 Longhorn（唯讀）＋真 helm dry-run 下的叢集儲存額度預檢。

單元測試的 Longhorn 資料是 fixture、helm 是 mock，看不到：

1. 真 ``nodes.longhorn.io``／``settings.longhorn.io``／StorageClass 的形狀（kubectl 版本、
   v1.10 的 data-engine JSON 設定值），以及報表數字和直接用 kubectl 算出來的是否一致；
2. ``helm install --dry-run --output json`` 的真實輸出（含 post-renderer）能被解析，
   StatefulSet ``volumeClaimTemplates`` 的 PVC 名字照 k8s 規則展開；
3. 409 時叢集上**什麼都沒建**（沒有 release、沒有 PVC）。

⚠ 只讀 Longhorn，不建任何 Longhorn volume：會被擋的 chart 一定排不下（大小取「目前最大
可放」再加 1 TiB），放得下的情境只呼叫預檢函式本身、不 install。叢集沒有 Longhorn 時整檔 skip。
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
    names = [c["metadata"]["name"] for c in classes if c.get("provisioner") == "driver.longhorn.io"]
    for preferred in ("longhorn-delete", "longhorn"):
        if preferred in names:
            return preferred
    return names[0]


def _write_chart(chart_dir, templates: dict) -> str:
    chart_dir.mkdir(parents=True, exist_ok=True)
    (chart_dir / "Chart.yaml").write_text(
        "apiVersion: v2\nname: capacity-probe-1415\nversion: 0.1.0\n")
    tdir = chart_dir / "templates"
    tdir.mkdir(exist_ok=True)
    for name, body in templates.items():
        (tdir / name).write_text(body)
    return str(chart_dir)


def _pvc_template(storage_class: str, size_bytes: int) -> str:
    return (
        "apiVersion: v1\nkind: PersistentVolumeClaim\nmetadata:\n"
        "  name: {{ .Release.Name }}-data\n"
        "spec:\n"
        f"  storageClassName: {storage_class}\n"
        "  accessModes: [ReadWriteOnce]\n"
        f"  resources:\n    requests:\n      storage: \"{size_bytes}\"\n"
    )


def _sts_template(storage_class: str, size_bytes: int) -> str:
    return (
        "apiVersion: apps/v1\nkind: StatefulSet\nmetadata:\n"
        "  name: {{ .Release.Name }}-pg\n"
        "spec:\n  serviceName: {{ .Release.Name }}-pg\n  replicas: 1\n"
        "  selector:\n    matchLabels: {app: pg}\n"
        "  template:\n    metadata:\n      labels: {app: pg}\n"
        "    spec:\n      containers:\n      - name: c\n        image: busybox\n"
        "  volumeClaimTemplates:\n  - metadata:\n      name: data\n"
        f"    spec:\n      storageClassName: {storage_class}\n"
        "      accessModes: [ReadWriteOnce]\n"
        f"      resources:\n        requests:\n          storage: \"{size_bytes}\"\n"
    )


def _helm_releases(namespace: str) -> list:
    from src.services.helm import HelmService

    out = HelmService()._run_command(["list", "--namespace", namespace, "--all", "-o", "json"])
    return [r["name"] for r in json.loads(out.stdout or "[]")]


def test_report_matches_raw_longhorn_numbers(app_client):
    """報表的每節點上限＝直接用 kubectl 讀到的 (max−reserved)×over-provisioning%。"""
    body = app_client.get("/api/cluster/storage-capacity",
                          params={"storage_class": _longhorn_class()}).json()
    assert body["available"] is True, body

    raw_nodes = _kubectl_json("get", "nodes.longhorn.io", "-n", "longhorn-system")["items"]
    over = int(_kubectl_json(
        "get", "settings.longhorn.io", "storage-over-provisioning-percentage",
        "-n", "longhorn-system")["value"])
    assert body["settings"]["over_provisioning_percentage"] == over
    expected = {}
    for node in raw_nodes:
        limit = 0
        for disk_name, status in (node["status"].get("diskStatus") or {}).items():
            spec = node["spec"]["disks"].get(disk_name) or {}
            if spec.get("allowScheduling") and not spec.get("evictionRequested"):
                limit += int(float(status["storageMaximum"] - spec.get("storageReserved", 0))
                             * over / 100)
        expected[node["metadata"]["name"]] = limit
    assert {n["name"]: n["limit_bytes"] for n in body["nodes"]} == expected
    assert body["replicas"] >= 1
    assert 0 <= body["largest_placeable_volume_bytes"] <= body["largest_placeable_volume_degraded_bytes"]

    orphans = _kubectl_json("get", "pvc", "--all-namespaces", "-l",
                            "paas.woowtech.io/orphaned-at")["items"]
    assert body["orphaned_pvcs"]["count"] == len(orphans)


def _too_big(app_client) -> int:
    body = app_client.get("/api/cluster/storage-capacity",
                          params={"storage_class": _longhorn_class()}).json()
    return body["largest_placeable_volume_degraded_bytes"] + 1024 * GIB


def test_unplaceable_pvc_is_refused_and_nothing_is_created(app_client, test_namespace, tmp_path):
    sc = _longhorn_class()
    size = _too_big(app_client)
    chart = _write_chart(tmp_path / "huge", {"pvc.yaml": _pvc_template(sc, size)})
    r = app_client.post("/api/releases", json={
        "namespace": test_namespace, "name": "huge", "chart": chart})

    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["error_code"] == "cluster_storage_capacity_exhausted"
    assert [v["name"] for v in detail["unschedulable"]] == ["huge-data"]
    assert detail["unschedulable"][0]["storage_class"] == sc
    assert detail["unschedulable"][0]["replicas_placeable"] == 0
    assert detail["nodes"], "要附各節點額度給管理員"
    assert _helm_releases(test_namespace) == []
    assert _kubectl_json("get", "pvc", "-n", test_namespace)["items"] == []


def test_statefulset_claim_template_is_seen_through_real_render(
    app_client, test_namespace, tmp_path,
):
    sc = _longhorn_class()
    chart = _write_chart(tmp_path / "sts", {"sts.yaml": _sts_template(sc, _too_big(app_client))})
    r = app_client.post("/api/releases", json={
        "namespace": test_namespace, "name": "db", "chart": chart})
    assert r.status_code == 409, r.text
    assert [v["name"] for v in r.json()["detail"]["unschedulable"]] == ["data-db-pg-0"]
    assert _helm_releases(test_namespace) == []


def test_small_longhorn_pvc_passes_the_precheck(app_client, test_namespace, tmp_path):
    """陽性對照（只跑預檢、不 install，免得在共用叢集上真的建 Longhorn volume）。"""
    from src.api.releases import _cluster_capacity_conflict_detail
    from src.models.schemas import ReleaseCreateRequest

    chart = _write_chart(tmp_path / "small", {"pvc.yaml": _pvc_template(_longhorn_class(), GIB)})
    request = ReleaseCreateRequest(namespace=test_namespace, name="small", chart=chart)
    assert _cluster_capacity_conflict_detail(request) is None
    assert _helm_releases(test_namespace) == []


def test_non_longhorn_volume_is_not_gated(app_client, test_namespace, tmp_path):
    """非 Longhorn 的 SC（這裡是不存在的 SC，PVC 停在 Pending、不佈建）不算進額度。"""
    chart = _write_chart(tmp_path / "other", {
        "pvc.yaml": _pvc_template("woow-int-1415-no-provisioner", 10 ** 15)})
    r = app_client.post("/api/releases", json={
        "namespace": test_namespace, "name": "other", "chart": chart})
    assert r.status_code == 201, r.text
