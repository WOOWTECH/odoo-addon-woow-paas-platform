"""netpol-startup-window（#1497 補洞）：非 hook Job 的 egress gate，在真叢集上 install／upgrade。

#1778 的 post-renderer 對非 hook Job／bare Pod 一律不插 gate（template 不可變，改到已存在的
物件 ``helm upgrade`` 會 immutable 失敗）。補洞後 operator 每次 install／upgrade 讀一次
``jobs,pods``：叢集上沒有的照常插，已存在的照叢集上那份渲染。這裡用真的 helm、真的 API server
驗四件事（unit mock 驗不到 immutable 檢查與 API server 補預設值後的比對）：

1. 新安裝的非 hook Job 拿到 gate，而且 gate 在 ``paas-baseline-egress`` 生效後放行（Job 跑完）。
2. gate 設定之後改了（逾時 30→45），upgrade 仍成功，Job 保留叢集上那份 gate。
3. 對照組：假裝「叢集上沒有」（舊 gate 被換成新設定）→ API server 以 immutable 拒絕——證明 2 的
   「照叢集上那份渲染」是必要的。
4. 開關打開前就存在、沒有 gate 的 Job：upgrade 成功、不被加 gate。

跑法（真叢集、helm 3.13.3，見 tests/integration/README.md）::

    HELM_BINARY=/path/to/helm-3.13.3 OPERATOR_TEST_NAMESPACE_PREFIX=dev-sandbox-netgate-int- \\
    OPERATOR_TEST_AVOID_NODES=ubuntuserver-59mqj \\
    pytest -m integration tests/integration/test_tenant_egress_gate_immutable_net1.py
"""
import json
import os
import subprocess
import time
from unittest.mock import patch

import pytest

from src.config import settings
from src.services import helm as helm_module
from src.services.helm import HelmException, HelmService
from src.tenant_workload_policy import GATE_CONTAINER_NAME
from tests.namespace_scope import integration_namespace_prefix

pytestmark = pytest.mark.integration

AVOID = [n.strip() for n in os.environ.get("OPERATOR_TEST_AVOID_NODES", "").split(",") if n.strip()]

_BASELINE_NP = """
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: paas-baseline-egress
spec:
  podSelector: {}
  policyTypes: [Egress]
  egress:
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: kube-system
      ports:
        - {protocol: UDP, port: 53}
        - {protocol: TCP, port: 53}
"""

_JOB = """
apiVersion: batch/v1
kind: Job
metadata:
  name: {{ .Release.Name }}-init
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      securityContext:
        runAsNonRoot: true
        runAsUser: 65534
        seccompProfile: {type: RuntimeDefault}
      containers:
        - name: init
          image: busybox:1.36
          command: ["sh", "-c", "echo init-done"]
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities: {drop: [ALL]}
          resources:
            requests: {cpu: 5m, memory: 8Mi}
            limits: {cpu: 50m, memory: 32Mi}
"""


def _kubectl(*args, stdin=None, check=True):
    return subprocess.run(["kubectl", *args], input=stdin, capture_output=True, text=True,
                          check=check, timeout=120)


def _chart(chart_dir):
    (chart_dir / "templates").mkdir(parents=True, exist_ok=True)
    (chart_dir / "Chart.yaml").write_text("apiVersion: v2\nname: netgate\nversion: 0.1.0\n")
    (chart_dir / "values.yaml").write_text("marker: v1\n")
    (chart_dir / "templates" / "job.yaml").write_text(_JOB)
    (chart_dir / "templates" / "cm.yaml").write_text(
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: {{ .Release.Name }}-cm\n"
        "data:\n  marker: {{ .Values.marker | quote }}\n")
    return str(chart_dir)


def _job_pod_spec(namespace, name):
    job = json.loads(_kubectl("get", "job", name, "-n", namespace, "-o", "json").stdout)
    return job["spec"]["template"]["spec"]


def _gate(spec):
    return next((c for c in spec.get("initContainers") or [] if c["name"] == GATE_CONTAINER_NAME), None)


def _gate_env(spec, key):
    return next(e.get("value") for e in _gate(spec)["env"] if e["name"] == key)


def _wait_job_complete(namespace, name, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = json.loads(_kubectl("get", "job", name, "-n", namespace, "-o", "json").stdout)
        status = job.get("status") or {}
        if status.get("succeeded"):
            return True
        if status.get("failed"):
            return False
        time.sleep(2)
    return False


@pytest.fixture
def gated(test_namespace, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "namespace_prefix", integration_namespace_prefix())
    monkeypatch.setattr(settings, "tenant_baseline_egress_enabled", True)
    monkeypatch.setattr(settings, "tenant_egress_gate_enabled", True)
    monkeypatch.setattr(settings, "tenant_egress_gate_timeout_seconds", 30)
    monkeypatch.setattr(settings, "tenant_egress_gate_on_timeout", "fail")   # 沒放行就讓 Job 失敗，不要假綠
    monkeypatch.setattr(settings, "tenant_egress_gate_target", "")
    monkeypatch.setattr(settings, "tenant_avoid_nodes", ",".join(AVOID))
    _kubectl("apply", "-n", test_namespace, "-f", "-", stdin=_BASELINE_NP)
    return test_namespace, _chart(tmp_path / "netgate")


def test_new_plain_job_is_gated_and_survives_upgrades(gated, monkeypatch):
    ns, chart = gated
    helm = HelmService()
    helm.install(namespace=ns, name="ng", chart=chart)

    spec = _job_pod_spec(ns, "ng-init")
    assert spec["initContainers"][0]["name"] == GATE_CONTAINER_NAME            # 1. 新 Job 有 gate
    assert _gate_env(spec, "GATE_TIMEOUT") == "30"
    assert _wait_job_complete(ns, "ng-init"), "gate never released (baseline policy not observed)"
    pod = _kubectl("get", "pods", "-n", ns, "-l", "job-name=ng-init", "-o", "name").stdout.split()[0]
    gate_log = _kubectl("logs", "-n", ns, pod, "-c", GATE_CONTAINER_NAME).stdout
    assert "policy active" in gate_log
    if AVOID:
        node = _kubectl("get", "-n", ns, pod, "-o", "jsonpath={.spec.nodeName}").stdout
        assert node not in AVOID

    # 2. 設定改了也能 upgrade：Job 照叢集上那份（逾時仍是 30）。
    monkeypatch.setattr(settings, "tenant_egress_gate_timeout_seconds", 45)
    helm.upgrade(namespace=ns, name="ng", chart=chart, values={"marker": "v2"}, reuse_values=False)
    assert _gate_env(_job_pod_spec(ns, "ng-init"), "GATE_TIMEOUT") == "30"
    assert _kubectl("get", "cm", "ng-cm", "-n", ns, "-o", "jsonpath={.data.marker}").stdout == "v2"

    # 3. 對照組：不讀叢集現況、當成新物件（新設定的 gate）→ immutable 被拒。
    real = helm_module._tenant_policy

    def pretend_not_live(namespace, mirror_live=False):
        options, live = real(namespace, mirror_live=mirror_live)
        options["immutable_live"] = {}
        return options, live

    with patch.object(helm_module, "_tenant_policy", side_effect=pretend_not_live):
        with pytest.raises(HelmException) as exc:
            helm.upgrade(namespace=ns, name="ng", chart=chart, values={"marker": "v3"}, reuse_values=False)
    assert "immutable" in (exc.value.stderr or "").lower()


def test_existing_ungated_job_is_left_alone_on_upgrade(gated, monkeypatch):
    ns, chart = gated
    helm = HelmService()
    monkeypatch.setattr(settings, "tenant_egress_gate_enabled", False)   # 節點迴避維持（測試 pod 不落 59mqj）
    helm.install(namespace=ns, name="old", chart=chart)
    before = _job_pod_spec(ns, "old-init")
    assert _gate(before) is None

    # 4. 開關打開後第一次 upgrade（Redeploy）：既有 Job 不加 gate，upgrade 成功。
    monkeypatch.setattr(settings, "tenant_egress_gate_enabled", True)
    helm.upgrade(namespace=ns, name="old", chart=chart, values={"marker": "v2"}, reuse_values=False)
    spec = _job_pod_spec(ns, "old-init")
    assert _gate(spec) is None
    assert spec.get("affinity") == before.get("affinity")
    assert _kubectl("get", "cm", "old-cm", "-n", ns, "-o", "jsonpath={.data.marker}").stdout == "v2"
