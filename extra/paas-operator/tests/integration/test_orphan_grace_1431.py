"""#1431 integration：``DELETE /api/releases/{ns}/{name}?grace_period_days=N`` 真的把 N
標在留下來的 PVC 上（真 helm 3.13.3＋真 kubectl）。

單元測試 mock 掉 ``k8s_service``，看不到 kubectl 對 label 值的實際處理，也看不到 FastAPI
在真 HTTP 下對 query 參數的解析。這裡走 operator 自己的 HTTP 端點：

* 帶 ``grace_period_days=1`` ⇒ 留下的 PVC ``paas.woowtech.io/grace-period-days == "1"``；
* 不帶（舊平台）⇒ 照舊是 ``settings.pvc_grace_period_days``；
* 帶壞值 ⇒ 刪除照樣成功（200）、用預設。

chart 的 pvc-cleanup CronJob 讀每顆 PVC 自己的 ``grace-period-days`` 標籤（woow-paas-charts
``charts/paas-operator/templates/pvc-cleanup-cronjob.yaml``），所以 1 天的保留期只靠這個標籤。

Run::

    KUBECONFIG=<kubeconfig> HELM_BINARY=<helm 3.13.3> \
        pytest -m integration tests/integration/test_orphan_grace_1431.py -v
"""
import subprocess

import pytest

from src.config import settings
from tests.namespace_scope import integration_storage_class_yaml

pytestmark = pytest.mark.integration

_GRACE_JSONPATH = r"jsonpath={.metadata.labels.paas\.woowtech\.io/grace-period-days}"
_ORPHAN_JSONPATH = r"jsonpath={.metadata.labels.paas\.woowtech\.io/orphaned-at}"


def _write_pvc_chart(chart_dir) -> str:
    """最小 chart：一顆 chart-owned PVC（帶 helm instance 標籤，跟真實 chart 一樣）。"""
    (chart_dir / "templates").mkdir(parents=True, exist_ok=True)
    (chart_dir / "Chart.yaml").write_text(
        "apiVersion: v2\nname: grace-1431-probe\nversion: 0.1.0\n"
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


def _pvc_label(namespace: str, pvc: str, jsonpath: str) -> str:
    return subprocess.run(
        ["kubectl", "get", "pvc", pvc, "-n", namespace, "-o", jsonpath],
        capture_output=True, text=True,
    ).stdout


def _install_then_delete(app_client, namespace, chart, release, query):
    r = app_client.post("/api/releases", json={
        "namespace": namespace, "name": release, "chart": chart,
    })
    assert r.status_code == 201, r.text
    r = app_client.delete(f"/api/releases/{namespace}/{release}{query}")
    assert r.status_code == 200, r.text
    pvc = f"{release}-data"
    assert _pvc_label(namespace, pvc, _ORPHAN_JSONPATH).isdigit(), (
        "前提：PVC 撐過 uninstall 並被標孤兒（#317）")
    return _pvc_label(namespace, pvc, _GRACE_JSONPATH)


def test_never_running_delete_stamps_one_day(app_client, test_namespace, tmp_path):
    chart = _write_pvc_chart(tmp_path / "chart")
    grace = _install_then_delete(
        app_client, test_namespace, chart, "grace-one", "?grace_period_days=1")
    assert grace == "1", f"帶 grace_period_days=1 的刪除，PVC 標籤應為 1，實際 {grace!r}"


def test_delete_without_the_parameter_keeps_the_default(app_client, test_namespace, tmp_path):
    chart = _write_pvc_chart(tmp_path / "chart")
    grace = _install_then_delete(app_client, test_namespace, chart, "grace-default", "")
    assert grace == str(settings.pvc_grace_period_days), (
        f"舊平台（不帶參數）必須維持預設 {settings.pvc_grace_period_days}，實際 {grace!r}")


def test_invalid_value_falls_back_to_the_default(app_client, test_namespace, tmp_path):
    chart = _write_pvc_chart(tmp_path / "chart")
    grace = _install_then_delete(
        app_client, test_namespace, chart, "grace-bad", "?grace_period_days=abc")
    assert grace == str(settings.pvc_grace_period_days)
