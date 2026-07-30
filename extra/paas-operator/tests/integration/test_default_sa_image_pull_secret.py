"""#423 integration：租戶 ns 的 default ServiceAccount 必須真的被 patch 上 pull secret。

真 kubectl + 真 API server + 真 HTTP。unit 測試把 ``_run_command`` 換掉，只驗得到
「我們組了什麼 argv」；它驗不到 API server 對 ``kubectl patch --type merge`` 的**實際**
語意——而那正是本功能唯一會出錯的地方：JSON merge patch 對 array 是整份覆寫，實作若
沒有先讀既有清單再 append，真叢集上就會靜默洗掉租戶原有的 imagePullSecrets。同理，
「重跑不重複」也只有讓 API server 真的跑兩次才算證明。

Run::

    kubectl config use-context docker-desktop
    HELM_BINARY=$(which helm) pytest -m integration \\
        tests/integration/test_default_sa_image_pull_secret.py -v
"""
import json
import subprocess

import pytest

from src.services.helm import KubernetesService

pytestmark = pytest.mark.integration

# 與 config.Settings.registry_secret_name 預設一致（operator 注入的 secret 名）。
_REGISTRY_SECRET = "paas-registry"

_CHART_YAML = """\
apiVersion: v2
name: sa-probe
description: minimal chart; the SA convergence must not depend on chart content
type: application
version: 0.1.0
appVersion: "1.0"
"""

# 刻意**不**消費 .Values.imagePullSecrets：本檔驗的是 SA 那一層，跟 chart 有沒有
# 自己 render pull secret 無關（CDI importer pod 也不是任何 chart render 出來的）。
_DEPLOYMENT_TMPL = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ .Release.Name }}-app
spec:
  replicas: 1
  selector:
    matchLabels:
      app: {{ .Release.Name }}
  template:
    metadata:
      labels:
        app: {{ .Release.Name }}
    spec:
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
    chart_dir = tmp_path / "sa-probe"
    (chart_dir / "templates").mkdir(parents=True)
    (chart_dir / "Chart.yaml").write_text(_CHART_YAML)
    (chart_dir / "templates" / "deployment.yaml").write_text(_DEPLOYMENT_TMPL)
    return str(chart_dir)


def _sa_pull_secret_names(namespace: str) -> list:
    """該 ns 的 default SA 目前掛著的 imagePullSecrets 名字（依序）。"""
    out = subprocess.check_output(
        ["kubectl", "get", "serviceaccount", "default", "-n", namespace, "-o", "json"],
    ).decode()
    return [e["name"] for e in (json.loads(out).get("imagePullSecrets") or [])]


def _seed_sa_pull_secret(namespace: str, name: str) -> None:
    """模擬「租戶／其他機制已經放了一筆」的既有狀態。"""
    subprocess.run(
        ["kubectl", "patch", "serviceaccount", "default", "-n", namespace,
         "--type", "merge", "-p", json.dumps({"imagePullSecrets": [{"name": name}]})],
        check=True, capture_output=True,
    )


def test_converges_default_sa_on_real_cluster(test_namespace):
    """空的 default SA → patch 後真的帶著 paas-registry。"""
    assert _sa_pull_secret_names(test_namespace) == [], "前提：新 ns 的 default SA 是空的"

    assert KubernetesService().ensure_default_sa_image_pull_secret(test_namespace) is True

    assert _sa_pull_secret_names(test_namespace) == [_REGISTRY_SECRET]


def test_rerun_does_not_duplicate(test_namespace):
    """重跑不重複：第二次回 False，且 API server 上的清單長度不變。

    冪等在這裡是硬需求——install / upgrade / reset 每一次都會呼叫，不冪等的話
    SA 會累積出一長串重複項目。
    """
    k8s = KubernetesService()
    assert k8s.ensure_default_sa_image_pull_secret(test_namespace) is True
    first = _sa_pull_secret_names(test_namespace)

    assert k8s.ensure_default_sa_image_pull_secret(test_namespace) is False
    assert k8s.ensure_default_sa_image_pull_secret(test_namespace) is False

    assert _sa_pull_secret_names(test_namespace) == first == [_REGISTRY_SECRET]


def test_preserves_pre_existing_entries_through_real_merge_patch(test_namespace):
    """既有項目不得被洗掉——merge patch 對 array 是整份覆寫，這條是它的守線。

    實作若退化成「只送 [{'name': 'paas-registry'}]」，unit 測試若只斷言
    「paas-registry 在不在」會全綠，這裡卻會抓到 tenant-custom 消失。
    """
    _seed_sa_pull_secret(test_namespace, "tenant-custom")
    assert _sa_pull_secret_names(test_namespace) == ["tenant-custom"]

    assert KubernetesService().ensure_default_sa_image_pull_secret(test_namespace) is True

    assert _sa_pull_secret_names(test_namespace) == ["tenant-custom", _REGISTRY_SECRET]


def test_install_endpoint_converges_default_sa(app_client, test_namespace, local_chart):
    """真 HTTP install → ns default SA 被收斂（install 路徑接線的端到端證明）。

    這是 CDI importer 那條線的可測代理：importer pod 不是 chart render 出來的，
    平台唯一能替它備妥憑證的時機就是 install 之前把 SA patch 好。
    """
    assert _sa_pull_secret_names(test_namespace) == []

    resp = app_client.post(
        "/api/releases",
        json={
            "namespace": test_namespace,
            "name": "sa-probe-rel",
            "chart": local_chart,
            "values": {},
        },
    )
    assert resp.status_code == 201, resp.text

    assert _REGISTRY_SECRET in _sa_pull_secret_names(test_namespace), (
        "install 走完之後 ns default SA 應帶著 registry pull secret；"
        "缺了它，VM 型服務的 CDI importer 會卡在 ImportScheduled（#423）"
    )


def test_upgrade_endpoint_converges_default_sa(app_client, test_namespace, local_chart):
    """真 HTTP upgrade（全量 reset values）→ SA 一樣被收斂，且不重複。"""
    resp = app_client.post(
        "/api/releases",
        json={
            "namespace": test_namespace,
            "name": "sa-probe-up",
            "chart": local_chart,
            "values": {},
        },
    )
    assert resp.status_code == 201, resp.text

    resp = app_client.patch(
        f"/api/releases/{test_namespace}/sa-probe-up",
        json={
            "chart": local_chart,
            "values": {},
            "reset_values": True,
            "reuse_values": False,
        },
    )
    assert resp.status_code == 200, resp.text

    # install 已補過一次，upgrade 再跑一次也只能有一筆。
    assert _sa_pull_secret_names(test_namespace) == [_REGISTRY_SECRET]


def test_rejects_namespace_outside_prefix():
    """前綴守衛 fail-closed：非 paas ns 直接 raise。

    刻意**不**去讀 kube-system 來「證明沒被改到」——本 repo 對非平台 namespace 的
    鐵律是連碰都不碰，而 validate_namespace 在任何 kubectl 之前就擋掉了（零呼叫
    由 unit 測試 test_rejects_foreign_namespace 斷言）。
    """
    with pytest.raises(ValueError):
        KubernetesService().ensure_default_sa_image_pull_secret("kube-system")
