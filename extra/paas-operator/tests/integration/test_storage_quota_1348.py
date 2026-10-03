"""#1348 integration：真 ResourceQuota admission ＋ 真 helm 下的儲存配額檢查。

單元測試 mock 掉 kubectl／helm，看不到兩件只有真叢集才有的事：

1. ResourceQuota 的 ``status.used`` 真的會把保留中的孤兒 PVC 算進去（#1348 的根因），
   事前檢查讀到的正是它；
2. helm 被 PVC admission 拒絕時 stderr 的真實措辭（``exceeded quota: …
   requested: persistentvolumeclaims=1``）——事後辨識的 regex 對的是這個字串。

PVC 一律用不存在的 StorageClass：PVC 停在 Pending、不 provision 任何 volume，但仍計入
quota（quota 計的是 PVC 物件與 requests，不看是否 Bound）。namespace 由 fixture 收掉。
"""
import json
import subprocess
import time

import pytest

pytestmark = pytest.mark.integration

_NO_PROVISION_SC = "woow-int-1348-no-provisioner"


def _kubectl_apply(manifest: str) -> None:
    subprocess.run(["kubectl", "apply", "-f", "-"], input=manifest, text=True,
                   check=True, capture_output=True)


def _quota_manifest(namespace: str, pvc_hard: str) -> str:
    return (
        "apiVersion: v1\nkind: ResourceQuota\nmetadata:\n"
        f"  name: {namespace}-quota\n  namespace: {namespace}\n"
        "spec:\n  hard:\n"
        f"    persistentvolumeclaims: \"{pvc_hard}\"\n"
        "    requests.storage: 1Gi\n"
    )


def _pvc_manifest(namespace: str, name: str, labels: dict) -> str:
    label_lines = "".join(f"    {k}: \"{v}\"\n" for k, v in labels.items())
    return (
        "apiVersion: v1\nkind: PersistentVolumeClaim\nmetadata:\n"
        f"  name: {name}\n  namespace: {namespace}\n"
        + ("  labels:\n" + label_lines if label_lines else "")
        + "spec:\n"
        f"  storageClassName: {_NO_PROVISION_SC}\n"
        "  accessModes: [ReadWriteOnce]\n"
        "  resources:\n    requests:\n      storage: 8Mi\n"
    )


def _wait_quota_used(namespace: str, expected: str, timeout: float = 60) -> None:
    """quota controller 非同步更新 status.used；事前檢查讀的就是它，要等它收斂。"""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        out = subprocess.run(
            ["kubectl", "get", "resourcequota", f"{namespace}-quota", "-n", namespace,
             "-o", "json"], capture_output=True, text=True)
        if out.returncode == 0:
            used = json.loads(out.stdout).get("status", {}).get("used", {})
            last = used.get("persistentvolumeclaims")
            if last == expected:
                return
        time.sleep(1)
    pytest.fail(f"quota status.used.persistentvolumeclaims 沒有收斂到 {expected}（最後 {last!r}）")


def _write_pvc_chart(chart_dir, pvc_count: int) -> str:
    chart_dir.mkdir(parents=True, exist_ok=True)
    (chart_dir / "Chart.yaml").write_text(
        "apiVersion: v2\nname: quota-probe-1348\nversion: 0.1.0\n")
    templates = chart_dir / "templates"
    templates.mkdir(exist_ok=True)
    for i in range(pvc_count):
        (templates / f"pvc{i}.yaml").write_text(
            "apiVersion: v1\nkind: PersistentVolumeClaim\nmetadata:\n"
            f"  name: {{{{ .Release.Name }}}}-data{i}\n"
            "  labels:\n    app.kubernetes.io/instance: {{ .Release.Name }}\n"
            "spec:\n"
            f"  storageClassName: {_NO_PROVISION_SC}\n"
            "  accessModes: [ReadWriteOnce]\n"
            "  resources:\n    requests:\n      storage: 8Mi\n")
    return str(chart_dir)


def _helm_releases(namespace: str) -> list:
    from src.services.helm import HelmService

    out = HelmService()._run_command(["list", "--namespace", namespace, "--all", "-o", "json"])
    return [r["name"] for r in json.loads(out.stdout or "[]")]


def test_full_quota_with_orphans_is_refused_before_helm(app_client, test_namespace, tmp_path):
    """配額被孤兒 PVC 佔滿 ⇒ 409＋error_code＋孤兒數，而且 helm 完全沒被呼叫（無半套 release）。"""
    _kubectl_apply(_quota_manifest(test_namespace, "1"))
    _kubectl_apply(_pvc_manifest(test_namespace, "old-svc-data", {
        "paas.woowtech.io/orphaned-at": "1759000000",
        "paas.woowtech.io/grace-period-days": "90",
        "paas.woowtech.io/release-name": "old-svc",
    }))
    _wait_quota_used(test_namespace, "1")

    chart = _write_pvc_chart(tmp_path / "chart", pvc_count=1)
    r = app_client.post("/api/releases", json={
        "namespace": test_namespace, "name": "new-svc", "chart": chart})

    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["error_code"] == "workspace_storage_quota_exhausted"
    assert detail["exhausted"] == ["persistentvolumeclaims"]
    assert detail["usage"]["persistentvolumeclaims"] == {"used": "1", "hard": "1"}
    assert detail["orphaned_pvc_count"] == 1
    assert detail["orphaned_releases"] == ["old-svc"]
    assert detail["next_release_epoch"] == 1759000000 + 90 * 86400
    assert _helm_releases(test_namespace) == [], "事前檢查必須在 helm install 之前擋下"


def test_helm_quota_rejection_maps_to_same_error_code(app_client, test_namespace, tmp_path):
    """剩 1 顆、chart 要 2 顆：事前檢查放行 → 真 admission 拒絕 → 真 stderr 被辨識成同一個 409。"""
    _kubectl_apply(_quota_manifest(test_namespace, "1"))
    _wait_quota_used(test_namespace, "0")

    chart = _write_pvc_chart(tmp_path / "chart2", pvc_count=2)
    r = app_client.post("/api/releases", json={
        "namespace": test_namespace, "name": "two-pvc", "chart": chart})

    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["error_code"] == "workspace_storage_quota_exhausted"
    assert detail["exhausted"] == ["persistentvolumeclaims"]


def test_headroom_installs_normally(app_client, test_namespace, tmp_path):
    """陽性對照：配額夠 ⇒ 照常 201（檢查不得變成新的誤擋）。"""
    _kubectl_apply(_quota_manifest(test_namespace, "5"))
    _wait_quota_used(test_namespace, "0")

    chart = _write_pvc_chart(tmp_path / "chart3", pvc_count=1)
    r = app_client.post("/api/releases", json={
        "namespace": test_namespace, "name": "fits", "chart": chart})
    assert r.status_code == 201, r.text
