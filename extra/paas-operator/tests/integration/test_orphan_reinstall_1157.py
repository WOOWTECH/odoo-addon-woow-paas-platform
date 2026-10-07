"""#1157 integration：uninstall → 同名 install 不得靜默接回孤兒 PVC（真 helm 3.13.3＋真 kubectl）。

#1154 實測的事故路徑：``DELETE /api/releases/{ns}/{name}`` 讓 PVC 留下並打上
``orphaned-at``，接著同 namespace＋同 release name 的 ``POST /api/releases`` 回 201、零警告，
helm 接回同一顆舊 PVC，而孤兒標記還在 ⇒ 回收 CronJob 在保留期後刪掉活站資料。

單元測試 mock 掉 ``k8s_service``，看不到真 kubectl 的 label selector 是否真的選得到
uninstall 打上去的 label，也看不到 helm 真的會接回舊 PVC。這裡走 operator 自己的 HTTP 端點：

1. install → delete ⇒ PVC 留下且帶 orphaned-at（#317 前提）；
2. 同名 install ⇒ 409、``error_code`` 固定、列出那顆 PVC；PVC 沒被動到（label 與 uid 都不變）、
   也沒有新的 helm release；
3. 換新 release name ⇒ 照常 201（不影響正常流程，平台的 release name 每次都是新的）；
4. 依 409 訊息的出路先刪掉孤兒 PVC ⇒ 同名 install 成功，新 PVC 不帶任何孤兒標記。

Run::

    KUBECONFIG=<kubeconfig> HELM_BINARY=<helm 3.13.3> \
        pytest -m integration tests/integration/test_orphan_reinstall_1157.py -v
"""
import json
import subprocess

import pytest

from tests.namespace_scope import integration_storage_class_yaml

pytestmark = pytest.mark.integration

_ORPHAN_KEY = "paas.woowtech.io/orphaned-at"


def _write_pvc_chart(chart_dir) -> str:
    """最小 chart：一顆 chart-owned PVC（帶 helm instance 標籤，跟真實 chart 一樣）。"""
    (chart_dir / "templates").mkdir(parents=True, exist_ok=True)
    (chart_dir / "Chart.yaml").write_text(
        "apiVersion: v2\nname: reinstall-1157-probe\nversion: 0.1.0\n"
    )
    (chart_dir / "templates" / "pvc.yaml").write_text(
        "apiVersion: v1\n"
        "kind: PersistentVolumeClaim\n"
        "metadata:\n"
        "  name: {{ .Release.Name }}-data\n"
        "  labels:\n"
        "    app.kubernetes.io/instance: {{ .Release.Name }}\n"
        "spec:\n"
        f"{integration_storage_class_yaml()}"
        "  accessModes: [ReadWriteOnce]\n"
        "  resources:\n"
        "    requests:\n"
        "      storage: 8Mi\n"
    )
    return str(chart_dir)


def _pvc(namespace: str, name: str):
    res = subprocess.run(
        ["kubectl", "get", "pvc", name, "-n", namespace, "-o", "json"],
        capture_output=True, text=True,
    )
    return json.loads(res.stdout) if res.returncode == 0 else None


def _helm_release_secrets(namespace: str, release: str) -> str:
    return subprocess.run(
        ["kubectl", "get", "secret", "-n", namespace,
         "-l", f"owner=helm,name={release}", "-o", "name"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _install(app_client, namespace, chart, release):
    return app_client.post("/api/releases", json={
        "namespace": namespace, "name": release, "chart": chart,
    })


def test_same_name_reinstall_is_refused_until_orphans_are_deleted(
    app_client, test_namespace, tmp_path
):
    chart = _write_pvc_chart(tmp_path / "chart")
    release = "reinstall-1157"
    pvc = f"{release}-data"

    r = _install(app_client, test_namespace, chart, release)
    assert r.status_code == 201, r.text
    r = app_client.delete(f"/api/releases/{test_namespace}/{release}")
    assert r.status_code == 200, r.text
    before = _pvc(test_namespace, pvc)
    assert before and before["metadata"]["labels"].get(_ORPHAN_KEY, "").isdigit(), (
        "前提：PVC 撐過 uninstall 並被標孤兒（#317）")
    assert _helm_release_secrets(test_namespace, release) == ""

    # 2. 同名重裝 ⇒ 409，什麼都不動
    r = _install(app_client, test_namespace, chart, release)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["error_code"] == "orphaned_release_resources_exist"
    assert detail["pvcs"] == [pvc]
    after = _pvc(test_namespace, pvc)
    assert after["metadata"]["uid"] == before["metadata"]["uid"]
    assert after["metadata"]["labels"] == before["metadata"]["labels"], (
        "409 路徑不得改動孤兒 PVC 的 label（不做 B：不清標記接回）")
    assert _helm_release_secrets(test_namespace, release) == "", (
        "409 必須發生在 helm install 之前——不得留下半套 release")

    # 3. 換新名字 ⇒ 正常流程不受影響
    r = _install(app_client, test_namespace, chart, f"{release}-new")
    assert r.status_code == 201, r.text

    # 4. 照 409 訊息的出路：確認資料不要 ⇒ 先刪孤兒 PVC，再同名裝
    subprocess.run(
        ["kubectl", "delete", "pvc", pvc, "-n", test_namespace, "--wait=true",
         "--timeout=60s"],
        check=True, capture_output=True,
    )
    r = _install(app_client, test_namespace, chart, release)
    assert r.status_code == 201, r.text
    fresh = _pvc(test_namespace, pvc)
    assert fresh is not None
    assert fresh["metadata"]["uid"] != before["metadata"]["uid"]
    assert _ORPHAN_KEY not in fresh["metadata"].get("labels", {}), (
        "刪掉孤兒後重裝的新 PVC 不得帶孤兒標記")


def test_orphans_of_another_release_do_not_block(app_client, test_namespace, tmp_path):
    """per-workspace ns 由多個服務共用：別的 release 的孤兒不擋這次 install（selector 有綁 release）。"""
    chart = _write_pvc_chart(tmp_path / "chart")
    r = _install(app_client, test_namespace, chart, "gone-1157")
    assert r.status_code == 201, r.text
    assert app_client.delete(f"/api/releases/{test_namespace}/gone-1157").status_code == 200

    r = _install(app_client, test_namespace, chart, "other-1157")
    assert r.status_code == 201, r.text
