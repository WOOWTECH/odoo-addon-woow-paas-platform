"""#552 integration：``chart_version`` 必須是**真 helm 輸出**解析出來的值。

真 helm + 真 kubectl + 真 HTTP。unit 測試餵的是我們自己手寫的 JSON dict，所以
它驗不到本次改動真正的風險——**helm 的輸出形狀跟我們以為的一不一樣**：

* ``helm install/upgrade --output json`` 的版本到底在不在
  ``chart.metadata.version``（``_parse_release_info`` 讀的位置）；
* ``helm list -o json`` 的 ``chart`` 欄到底是不是 ``"<name>-<version>"``
  （``_parse_list_release_info`` 的 ``rsplit("-", 1)`` 賴以成立的前提）。

兩者只要有一項不成立，unit 測試會全綠而平台帳本會靜默記錯——這正是 CLAUDE.md
說 operator 改動必須跑 integration 的 contract-drift 類別。

⚠ chart 名稱刻意含 ``-``（``ver-probe-chart``）：``rsplit("-", 1)`` 對「名稱本身
沒有連字號」的樣本天生不會出錯，那種 fixture 零鑑別力。本 repo 實際在用的 chart
就是 ``mcp-server`` / ``woow-agent-runtime`` 這種形狀。

Run::

    KUBECONFIG=... HELM_BINARY=/tmp/helm3/darwin-arm64/helm \\
        pytest -m integration tests/integration/test_chart_version_reporting.py -v
"""
import pytest

pytestmark = pytest.mark.integration

_CHART_NAME = "ver-probe-chart"

_CHART_YAML = """\
apiVersion: v2
name: {name}
description: minimal chart used to observe helm's own version reporting
type: application
version: {version}
appVersion: "1.0"
"""

# ConfigMap 而非 Deployment：本檔驗的是 helm 的輸出解析，不需要排程／拉映像。
_CONFIGMAP_TMPL = """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ .Release.Name }}-probe
  labels:
    app.kubernetes.io/instance: {{ .Release.Name }}
data:
  marker: "552"
"""


def _write_chart(tmp_path, version):
    chart_dir = tmp_path / f"chart-{version}"
    (chart_dir / "templates").mkdir(parents=True)
    (chart_dir / "Chart.yaml").write_text(
        _CHART_YAML.format(name=_CHART_NAME, version=version))
    (chart_dir / "templates" / "configmap.yaml").write_text(_CONFIGMAP_TMPL)
    return str(chart_dir)


@pytest.fixture
def chart_v1(tmp_path):
    return _write_chart(tmp_path, "0.1.0")


@pytest.fixture
def chart_v2(tmp_path):
    return _write_chart(tmp_path, "0.2.0")


def test_install_get_and_upgrade_all_report_the_real_chart_version(
    app_client, test_namespace, chart_v1, chart_v2,
):
    """一條龍走完三個端點，三個都必須回報 helm 自己認定的版本。

    三個端點打到**兩個不同的 parser**：install / upgrade 走
    ``_parse_release_info``（``chart.metadata``），GET 走
    ``_parse_list_release_info``（``helm list`` 的 ``"<name>-<version>"``）。
    """
    release = "ver-probe-rel"

    # ── install：_parse_release_info（helm install --output json）────────────
    resp = app_client.post(
        "/api/releases",
        json={"namespace": test_namespace, "name": release, "chart": chart_v1},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["chart"] == _CHART_NAME, (
        "chart 名稱不得因為新增版本解析而變形（名稱含 '-'）")
    assert body["chart_version"] == "0.1.0", (
        "install 回應必須帶 helm 解析出來的 chart 版本；None 代表 "
        "chart.metadata.version 不在我們以為的位置 —— 平台帳本會靜默停在舊值")

    # ── GET：_parse_list_release_info（helm list -o json）────────────────────
    resp = app_client.get(f"/api/releases/{test_namespace}/{release}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["chart"], body["chart_version"]) == (_CHART_NAME, "0.1.0"), (
        "helm list 的 chart 欄實際內容與 rsplit('-', 1) 的前提不符 —— "
        f"got chart={body['chart']!r} chart_version={body['chart_version']!r}")

    # ── upgrade：換一個**不同版本**的 chart，回報值必須跟著動 ────────────────
    # 這一步是鑑別力所在：版本若是被寫死／回音，上面兩條也會綠。
    resp = app_client.patch(
        f"/api/releases/{test_namespace}/{release}",
        json={"chart": chart_v2, "reset_values": True, "reuse_values": False},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["chart_version"] == "0.2.0", (
        "upgrade 後回報的必須是**新** chart 的版本")

    # 帳本的最終來源（Odoo poll 走 GET）也要跟著動。
    resp = app_client.get(f"/api/releases/{test_namespace}/{release}")
    assert resp.json()["chart_version"] == "0.2.0"
