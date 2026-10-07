"""#1552 integration：helm 併發上限＋``--disable-openapi-validation``（真 helm＋真 kubectl＋真 HTTP）。

unit 測試用假的 helm 子行程驗排隊邏輯；這裡驗只有真叢集看得到的部分：

1. 同時送多個「修改配置」（平台 reconfigure 的形狀：reset_values＋全量 values），真的 helm
   同時最多跑 ``HELM_MAX_CONCURRENT_OPERATIONS`` 個，而且**每一個都真的套用到叢集**
   （ConfigMap 內容＋``helm get values``），沒有任何一個被靜默丟掉；
2. helm 3.13.3 接受 ``--disable-openapi-validation``，而型別錯的 manifest 仍然被 API server
   擋下（server 端驗證還在，關掉的只是 client 端下載 schema）；
3. 排隊逾時回 503 ``operator_busy``，release 一個 revision 都沒動。

記憶體峰值／OOM 重現在 TestClient 裡量不到（同一個 process），見 PR 說明的沙盒量測。

本地跑（無 CI runner）::

    HELM_BINARY=$(which helm) pytest -m integration \
        tests/integration/test_helm_concurrency_1552.py -v
"""
import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from src.config import settings
from src.services.helm import HelmService
from src.services.helm_limiter import (
    OPERATOR_BUSY_ERROR_CODE,
    helm_operation_slot,
    reset_helm_operation_limiter,
)

pytestmark = pytest.mark.integration

_CHART_YAML = """\
apiVersion: v2
name: conc-probe
description: ConfigMap-only chart for #1552 concurrency tests (no pods, no PVCs)
type: application
version: 0.1.0
appVersion: "1.0"
"""

_CM_TMPL = """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ .Release.Name }}-cfg
  labels:
    app.kubernetes.io/instance: {{ .Release.Name }}
data:
  marker: {{ .Values.marker | default "initial" | quote }}
"""

# 型別錯：ConfigMap.data 只能是字串。client 端 schema 不下載之後，仍要由 API server 擋下。
_BAD_CM_TMPL = """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ .Release.Name }}-bad
data:
  count: 1
"""


@pytest.fixture
def conc_chart(tmp_path):
    chart_dir = tmp_path / "conc-probe"
    (chart_dir / "templates").mkdir(parents=True)
    (chart_dir / "Chart.yaml").write_text(_CHART_YAML)
    (chart_dir / "templates" / "cm.yaml").write_text(_CM_TMPL)
    return str(chart_dir)


@pytest.fixture
def bad_chart(tmp_path):
    chart_dir = tmp_path / "bad-probe"
    (chart_dir / "templates").mkdir(parents=True)
    (chart_dir / "Chart.yaml").write_text(_CHART_YAML.replace("conc-probe", "bad-probe"))
    (chart_dir / "templates" / "cm.yaml").write_text(_BAD_CM_TMPL)
    return str(chart_dir)


@pytest.fixture
def limiter(app_client):
    saved = (settings.helm_max_concurrent_operations, settings.helm_queue_timeout_seconds,
             settings.helm_disable_openapi_validation)

    def apply(limit, queue_timeout=120):
        settings.helm_max_concurrent_operations = limit
        settings.helm_queue_timeout_seconds = queue_timeout
        settings.helm_disable_openapi_validation = True
        reset_helm_operation_limiter()

    yield apply
    (settings.helm_max_concurrent_operations, settings.helm_queue_timeout_seconds,
     settings.helm_disable_openapi_validation) = saved
    reset_helm_operation_limiter()


class _Counter:
    """包住真的 ``_run_command``：記錄 helm install／upgrade 同時在跑幾個。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.running = 0
        self.peak = 0
        self.args = []
        self._real = HelmService._run_command

    def __call__(self, service, args, *a, **kw):
        heavy = args and args[0] in ("install", "upgrade")
        if heavy:
            with self.lock:
                self.running += 1
                self.peak = max(self.peak, self.running)
                self.args.append(list(args))
        try:
            return self._real(service, args, *a, **kw)
        finally:
            if heavy:
                with self.lock:
                    self.running -= 1


def _cm_marker(namespace, release):
    return subprocess.check_output(
        ["kubectl", "get", "configmap", f"{release}-cfg", "-n", namespace,
         "-o", "jsonpath={.data.marker}"]).decode()


def _helm_values_marker(namespace, release):
    out = subprocess.check_output(
        [settings.helm_binary, "get", "values", release, "-n", namespace, "-o", "json"])
    return (json.loads(out or b"null") or {}).get("marker")


def _helm_revision(namespace, release):
    out = subprocess.check_output(
        [settings.helm_binary, "list", "-n", namespace, "--filter", f"^{release}$", "-o", "json"])
    return int(json.loads(out)[0]["revision"])


def test_concurrent_reconfigures_are_bounded_and_all_applied(
        app_client, test_namespace, conc_chart, limiter):
    limiter(limit=2)
    releases = [f"conc-{i}" for i in range(5)]
    for rel in releases:
        resp = app_client.post("/api/releases", json={
            "namespace": test_namespace, "name": rel, "chart": conc_chart,
            "values": {"marker": "initial"}})
        assert resp.status_code == 201, resp.text

    counter = _Counter()

    def send(rel):
        return app_client.patch(f"/api/releases/{test_namespace}/{rel}", json={
            "chart": conc_chart, "values": {"marker": f"new-{rel}"},
            "reset_values": True, "reuse_values": False})

    def wrapped(service, args, *a, **kw):  # 函式才會被綁成 method（self 才會傳進來）
        return counter(service, args, *a, **kw)

    with patch.object(HelmService, "_run_command", wrapped):
        with ThreadPoolExecutor(max_workers=len(releases)) as ex:
            responses = list(ex.map(send, releases))

    assert [r.status_code for r in responses] == [200] * len(releases), [
        r.text for r in responses if r.status_code != 200]
    assert counter.peak == 2, f"peak concurrent helm upgrades {counter.peak}, limit 2"
    # 每個請求：一次預檢 dry-run＋一次真正 upgrade，都帶 --disable-openapi-validation。
    assert len(counter.args) == 2 * len(releases)
    assert all("--disable-openapi-validation" in a for a in counter.args)
    for rel in releases:
        assert _cm_marker(test_namespace, rel) == f"new-{rel}"
        assert _helm_values_marker(test_namespace, rel) == f"new-{rel}"
        assert _helm_revision(test_namespace, rel) == 2


def test_server_side_validation_still_rejects_bad_manifest(
        app_client, test_namespace, bad_chart, limiter):
    limiter(limit=2)
    resp = app_client.post("/api/releases", json={
        "namespace": test_namespace, "name": "bad-rel", "chart": bad_chart})
    assert resp.status_code == 500, resp.text
    missing = subprocess.run(
        ["kubectl", "get", "configmap", "bad-rel-bad", "-n", test_namespace],
        capture_output=True)
    assert missing.returncode != 0


def test_busy_returns_503_and_leaves_release_untouched(
        app_client, test_namespace, conc_chart, limiter):
    limiter(limit=1, queue_timeout=2)
    resp = app_client.post("/api/releases", json={
        "namespace": test_namespace, "name": "busy-rel", "chart": conc_chart,
        "values": {"marker": "initial"}})
    assert resp.status_code == 201, resp.text

    result = {}
    with helm_operation_slot("held-by-test"):
        t = threading.Thread(target=lambda: result.setdefault("r", app_client.patch(
            f"/api/releases/{test_namespace}/busy-rel",
            json={"chart": conc_chart, "values": {"marker": "never"},
                  "reset_values": True, "reuse_values": False})))
        t.start()
        t.join(30)
    r = result["r"]
    assert r.status_code == 503, r.text
    assert r.json()["detail"]["error_code"] == OPERATOR_BUSY_ERROR_CODE
    assert _helm_revision(test_namespace, "busy-rel") == 1
    assert _cm_marker(test_namespace, "busy-rel") == "initial"
