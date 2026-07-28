"""#449 integration：reset-config 依 ``admin_password_carrier`` 分流（真 helm + 真 kubectl）。

unit 測試 mock 掉 ``k8s_service`` 與 ``helm_service``，只驗得到「handler 有沒有照
策略呼叫某些方法」。它驗不到本 issue 真正的兩個致命點，而這兩點都只在**真叢集**
現形（BUG-7 workload-type family，見 tests/integration/README.md）：

1. **多 Deployment**：hermes-agent / open-design 兩個 chart 都渲染 daemon + console
   兩顆 Deployment 共用同一個 ``app.kubernetes.io/instance``。單數版
   ``get_deployment_name`` 撞多筆 raise ``Ambiguous selector`` → 500。mock 回一個
   字串就永遠看不到這條分支。
2. **檔案佈局假設**：那些 chart 的 PVC 裡沒有 ``/data/config.json``，exec patch 必炸、
   備份未建成、補償 ``os.replace`` 跟著炸 → ``operator intervention required``。
   這正是 #424① E2E 實錄的症狀，本檔用一個「沒有 config.json 也沒有 python3」的
   多 Deployment release 原地重現它。

本地跑（無 CI runner）::

    HELM_BINARY=$(which helm) pytest -m integration \
        tests/integration/test_reset_config_carrier.py -v
"""
import base64
import subprocess

import pytest

pytestmark = pytest.mark.integration

# 與 config.Settings.registry_secret_name 預設一致（operator 注入的 secret 名）。
_REGISTRY_SECRET = "paas-registry"

_CHART_YAML = """\
apiVersion: v2
name: carrier-probe
description: two Deployments + a chart-managed Secret, mirroring hermes-agent/open-design
type: application
version: 0.1.0
appVersion: "1.0"
"""

_VALUES_YAML = """\
config:
  sensitive:
    admin_password: chart-default-password
"""

# chart 自管 Secret＝密碼的權威來源（hermes-agent secret.yaml 的最小鏡射：
# 同樣吃 .Values.config.sensitive.admin_password，同樣是 stringData.admin_password）。
_SECRET_TMPL = """\
apiVersion: v1
kind: Secret
metadata:
  name: {{ .Release.Name }}-auth
  labels:
    app.kubernetes.io/instance: {{ .Release.Name }}
type: Opaque
stringData:
  admin_password: {{ .Values.config.sensitive.admin_password | quote }}
"""

# ⚠ 刻意**沒有** checksum/secret annotation —— 與真 chart 一致（實查
# charts/hermes-agent 與 charts/open-design 的 deployment.yaml 只有
# checksum/config 與 checksum/authproxy-config）。因此改 Secret 不會讓 helm
# 滾 pod，密碼要生效**必須**由 operator 顯式 rollout restart。
_DEPLOYMENT_TMPL = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ .Release.Name }}-daemon
  labels:
    app.kubernetes.io/instance: {{ .Release.Name }}
    app.kubernetes.io/component: daemon
spec:
  replicas: 1
  selector:
    matchLabels:
      app: {{ .Release.Name }}-daemon
  template:
    metadata:
      labels:
        app: {{ .Release.Name }}-daemon
    spec:
      {{- with .Values.imagePullSecrets }}
      imagePullSecrets:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      containers:
      - name: app
        image: nginx:alpine
        env:
        - name: ADMIN_PASSWORD
          valueFrom:
            secretKeyRef:
              name: {{ .Release.Name }}-auth
              key: admin_password
        resources:
          requests:
            cpu: 10m
            memory: 16Mi
"""

_CONSOLE_DEPLOYMENT_TMPL = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ .Release.Name }}-console
  labels:
    app.kubernetes.io/instance: {{ .Release.Name }}
    app.kubernetes.io/component: console
spec:
  replicas: 1
  selector:
    matchLabels:
      app: {{ .Release.Name }}-console
  template:
    metadata:
      labels:
        app: {{ .Release.Name }}-console
    spec:
      {{- with .Values.imagePullSecrets }}
      imagePullSecrets:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      containers:
      - name: console
        image: nginx:alpine
        resources:
          requests:
            cpu: 10m
            memory: 16Mi
"""


@pytest.fixture
def carrier_chart(tmp_path):
    """兩顆 Deployment 共用 instance label ＋ chart 自管 Secret 的最小 chart。"""
    chart_dir = tmp_path / "carrier-probe"
    (chart_dir / "templates").mkdir(parents=True)
    (chart_dir / "Chart.yaml").write_text(_CHART_YAML)
    (chart_dir / "values.yaml").write_text(_VALUES_YAML)
    (chart_dir / "templates" / "secret.yaml").write_text(_SECRET_TMPL)
    (chart_dir / "templates" / "deployment.yaml").write_text(_DEPLOYMENT_TMPL)
    (chart_dir / "templates" / "console-deployment.yaml").write_text(
        _CONSOLE_DEPLOYMENT_TMPL)
    return str(chart_dir)


def _install(app_client, namespace, release, chart):
    resp = app_client.post(
        "/api/releases",
        json={
            "namespace": namespace,
            "name": release,
            "chart": chart,
            "values": {},
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "deployed"


def _reset_body(password, chart, carrier=None):
    body = {
        "chart": chart,
        "values": {},
        "sensitive_values": {"config": {"sensitive": {"admin_password": password}}},
        "reset_values": True,
        "reuse_values": False,
    }
    if carrier is not None:
        body["admin_password_carrier"] = carrier
    return body


def _secret_password(namespace, release):
    encoded = subprocess.check_output(
        ["kubectl", "get", "secret", f"{release}-auth", "-n", namespace,
         "-o", "jsonpath={.data.admin_password}"],
    ).decode()
    return base64.b64decode(encoded).decode()


def _restarted_at(namespace, deployment):
    return subprocess.check_output([
        "kubectl", "get", "deployment", deployment, "-n", namespace,
        "-o",
        "jsonpath={.spec.template.metadata.annotations.kubectl\\.kubernetes\\.io/restartedAt}",
    ]).decode().strip()


def _pod_spec_pull_secrets(namespace, deployment):
    return subprocess.check_output(
        ["kubectl", "get", "deployment", deployment, "-n", namespace,
         "-o", "jsonpath={.spec.template.spec.imagePullSecrets[*].name}"],
    ).decode()


def test_helm_secret_carrier_rotates_secret_and_restarts_every_deployment(
    app_client, test_namespace, carrier_chart
):
    """helm_secret 路：Secret 換新 ＋ 兩顆 Deployment 都真的被滾過。

    「兩顆都滾」不是潔癖：密碼是**啟動期**消費的（真 chart 用 initContainer 把它
    算成 nginx htpasswd、webui/dashboard 走 env），少滾一顆＝那個入口還吃舊密碼。
    而 chart 沒有 checksum/secret annotation，helm 自己不會滾。
    """
    release = "carrier-hs-rel"
    _install(app_client, test_namespace, release, carrier_chart)

    assert _secret_password(test_namespace, release) == "chart-default-password"
    for suffix in ("daemon", "console"):
        assert not _restarted_at(test_namespace, f"{release}-{suffix}"), (
            "install 後不該有 restartedAt")

    resp = app_client.post(
        f"/api/releases/{test_namespace}/{release}/reset-config",
        json=_reset_body("rotated-pw-449", carrier_chart, carrier="helm_secret"),
    )

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["admin_password_carrier"] == "helm_secret"
    assert payload["config_json_patched"] is False
    assert sorted(payload["restarted_deployments"]) == [
        f"{release}-console", f"{release}-daemon",
    ]

    assert _secret_password(test_namespace, release) == "rotated-pw-449"
    for suffix in ("daemon", "console"):
        assert _restarted_at(test_namespace, f"{release}-{suffix}"), (
            f"{suffix} Deployment 沒有 restartedAt —— 新密碼不會生效（helm 不會"
            f"因為 Secret 變了就滾 pod）")


def test_helm_secret_carrier_keeps_image_pull_secrets(
    app_client, test_namespace, carrier_chart
):
    """#448 的 gap 在 reset 路也要收掉（reset_values=True ⇒ 前一版 values 全丟）。"""
    release = "carrier-ips-rel"
    _install(app_client, test_namespace, release, carrier_chart)

    resp = app_client.post(
        f"/api/releases/{test_namespace}/{release}/reset-config",
        json=_reset_body("rotated-pw-ips", carrier_chart, carrier="helm_secret"),
    )
    assert resp.status_code == 200, resp.text

    for suffix in ("daemon", "console"):
        rendered = _pod_spec_pull_secrets(test_namespace, f"{release}-{suffix}")
        assert _REGISTRY_SECRET in rendered, (
            f"{suffix} podSpec 掉了 imagePullSecrets —— 私有 image 會 "
            f"ImagePullBackOff 401；got {rendered!r}")


def test_config_json_carrier_reproduces_the_449_failure_on_such_a_chart(
    app_client, test_namespace, carrier_chart
):
    """修復前的行為，對著真叢集釘住：這就是為什麼需要分流。

    對一個沒有 ``/data/config.json``（也沒有 python3）的多 Deployment release 走
    config_json 路 → exec patch 炸 → 備份沒建成 → 補償 ``os.replace`` 跟著炸 →
    500 ``operator intervention required``。#424① E2E 實錄的症狀。

    這條測試同時是「載體標錯的代價」的證據：把 hermes-agent 留在預設載體，租戶
    的密碼永遠輪替不了。
    """
    release = "carrier-cfg-rel"
    _install(app_client, test_namespace, release, carrier_chart)

    # 不帶 admin_password_carrier ＝ 舊平台的 payload（發版順序安全性的實測）。
    resp = app_client.post(
        f"/api/releases/{test_namespace}/{release}/reset-config",
        json=_reset_body("rotated-pw-cfg", carrier_chart),
    )

    assert resp.status_code == 500, resp.text
    assert "reset-config" in resp.json()["detail"]

    # fail-safe 邊界：helm 已回滾，Secret 回到舊密碼 —— 租戶不會被鎖死，只是
    # 換不了密碼（#449 描述的「fail-safe 有效不鎖死」）。
    assert _secret_password(test_namespace, release) == "chart-default-password"
