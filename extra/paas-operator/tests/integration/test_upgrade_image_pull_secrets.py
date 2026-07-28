"""#448 integration：upgrade（全量 reset values）後 podSpec 必須仍有 imagePullSecrets。

真 helm + 真 kubectl + 真 HTTP。unit 測試 mock 掉 ``helm_service``，只能驗
「endpoint 有把 default 塞進 values dict」；它驗不到 helm 真的把 values 檔
render 進 Deployment podSpec、也驗不到 upgrade ``--reset-values`` 真的會把
前一版 values 全部丟掉（正是 #448 的觸發機制：install 注入的 pull secret
在 #411 全量 reset 管線的第一次 upgrade 就被抹掉 → ImagePullBackOff 401）。

Run::

    HELM_BINARY=$(which helm) pytest -m integration \
        tests/integration/test_upgrade_image_pull_secrets.py -v
"""
import subprocess

import pytest

pytestmark = pytest.mark.integration

# 與 config.Settings.registry_secret_name 預設一致（operator 注入的 secret 名）。
_REGISTRY_SECRET = "paas-registry"

_CHART_YAML = """\
apiVersion: v2
name: ips-probe
description: minimal chart rendering top-level imagePullSecrets into podSpec
type: application
version: 0.1.0
appVersion: "1.0"
"""

# 與 mcp-server chart 同一消費慣例：top-level .Values.imagePullSecrets →
# spec.template.spec.imagePullSecrets（values 沒帶時整段不 render）。
_DEPLOYMENT_TMPL = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ .Release.Name }}-app
  labels:
    app.kubernetes.io/instance: {{ .Release.Name }}
spec:
  replicas: {{ .Values.replicaCount | default 1 }}
  selector:
    matchLabels:
      app: {{ .Release.Name }}
  template:
    metadata:
      labels:
        app: {{ .Release.Name }}
    spec:
      {{- with .Values.imagePullSecrets }}
      imagePullSecrets:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      containers:
      - name: app
        image: nginx:alpine
        resources:
          requests:
            cpu: 10m
            memory: 16Mi
"""


@pytest.fixture
def local_chart(tmp_path):
    """寫出一個消費 top-level imagePullSecrets 的最小 local chart。"""
    chart_dir = tmp_path / "ips-probe"
    (chart_dir / "templates").mkdir(parents=True)
    (chart_dir / "Chart.yaml").write_text(_CHART_YAML)
    (chart_dir / "templates" / "deployment.yaml").write_text(_DEPLOYMENT_TMPL)
    return str(chart_dir)


def _pod_spec_pull_secrets(namespace: str, deployment: str) -> str:
    return subprocess.check_output(
        ["kubectl", "get", "deployment", deployment, "-n", namespace,
         "-o", "jsonpath={.spec.template.spec.imagePullSecrets[*].name}"],
    ).decode()


def _assert_helm_revision_exists(namespace: str, release: str, revision: int) -> None:
    """用 helm release Secret（sh.helm.release.v1.<name>.v<rev>）證明該 revision 真的存在。

    操作端點回應的 ``revision`` 欄位另有既有解析 quirk（_parse_release_info 讀
    ``info.revision``，helm install/upgrade JSON 實為頂層 ``version``），不拿它當證據；
    release Secret 是 kubectl 可直接觀察的 stored-revision 本體。
    """
    subprocess.check_output(
        ["kubectl", "get", "secret",
         f"sh.helm.release.v1.{release}.v{revision}", "-n", namespace],
        stderr=subprocess.STDOUT,
    )


def test_upgrade_with_reset_values_keeps_image_pull_secrets(
    app_client, test_namespace, local_chart
):
    """install → upgrade（values 不帶 imagePullSecrets、reset_values）→
    podSpec 仍有 pull secret。

    upgrade 走 ``--reset-values``（#411 之後 Odoo 端 redeploy / reconfigure /
    reset-password 的統一管線），前一版 values 不會沿用；upgrade 端點若沒有
    自己注入 imagePullSecrets，這裡就會 render 出沒有 pull secret 的 podSpec
    —— 正是 #448 stg 實證的回歸。
    """
    release = "ips-probe-rel"
    deployment = f"{release}-app"

    # Step 1: install（values 不帶 imagePullSecrets → operator 注入 default）
    resp = app_client.post(
        "/api/releases",
        json={
            "namespace": test_namespace,
            "name": release,
            "chart": local_chart,
            "values": {"replicaCount": 1},
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "deployed"
    _assert_helm_revision_exists(test_namespace, release, 1)

    installed = _pod_spec_pull_secrets(test_namespace, deployment)
    assert _REGISTRY_SECRET in installed, (
        f"install 後 podSpec 應有 {_REGISTRY_SECRET!r}（a0e7c25a 的注入），"
        f"got {installed!r}"
    )

    # Step 2: upgrade —— 全量 reset values、values 一樣不帶 imagePullSecrets
    resp = app_client.patch(
        f"/api/releases/{test_namespace}/{release}",
        json={
            "chart": local_chart,
            "values": {"replicaCount": 1},
            "reset_values": True,
            "reuse_values": False,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "deployed"
    _assert_helm_revision_exists(test_namespace, release, 2)

    # Step 3: podSpec 必須仍有 pull secret（#448 修復前這裡會是空字串）
    upgraded = _pod_spec_pull_secrets(test_namespace, deployment)
    assert _REGISTRY_SECRET in upgraded, (
        f"upgrade（reset_values）後 podSpec 掉了 imagePullSecrets —— #448 的 "
        f"ImagePullBackOff 401 回歸；got {upgraded!r}"
    )


def test_upgrade_preserves_caller_supplied_pull_secrets(
    app_client, test_namespace, local_chart
):
    """呼叫端明帶 imagePullSecrets 時，upgrade 不可覆蓋（setdefault 語意）。"""
    release = "ips-custom-rel"
    deployment = f"{release}-app"

    resp = app_client.post(
        "/api/releases",
        json={
            "namespace": test_namespace,
            "name": release,
            "chart": local_chart,
            "values": {},
        },
    )
    assert resp.status_code == 201, resp.text

    resp = app_client.patch(
        f"/api/releases/{test_namespace}/{release}",
        json={
            "chart": local_chart,
            "values": {"imagePullSecrets": [{"name": "tenant-custom"}]},
            "reset_values": True,
            "reuse_values": False,
        },
    )
    assert resp.status_code == 200, resp.text

    upgraded = _pod_spec_pull_secrets(test_namespace, deployment)
    assert upgraded == "tenant-custom", (
        f"呼叫端自帶的 imagePullSecrets 必須原樣保留，got {upgraded!r}"
    )
