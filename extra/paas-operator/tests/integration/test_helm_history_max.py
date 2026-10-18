"""INFRA-FIX-018-1／AI-HER-003-1 integration：helm 歷史上限——真 helm＋真 kubectl＋真 HTTP。

商用驗收：operator 沒帶 ``--history-max``，helm 預設只留 10 筆。Hermes 247 在 Redeploy 升到
新 chart 之後約 11 次設定修改／回復／值不變的 Redeploy，升級前的 rev 1 就不在歷史裡
（回復 API 回「Revision 1 not found」）；全部 15 個 QA 服務的歷史都只剩單一 chart 版本，
#1526 的「chart 版本下限」在正式環境無從觀察。unit 測試只看得到 argv，量不到兩件事：

* helm 3.13 真的照 ``--history-max`` 修剪（upgrade 與 rollback 各自修剪）；
* 一個「舊 chart 的 revision 在 10 次以上操作後仍在」的 release——這同時是 #1526 實機
  回歸要的 fixture：回復到舊 chart 的 revision 必須 409 ``rollback_chart_downgrade``，
  而不是 404「not found」。

chart 只渲染一個 ConfigMap（不排 Pod），整支測試不會在任何節點上起容器。

Run::

    OPERATOR_TEST_NAMESPACE_PREFIX=dev-sandbox-<topic>- KUBECONFIG=... \\
    HELM_BINARY=<helm 3.13.3> \\
        pytest -m integration tests/integration/test_helm_history_max.py -v
"""
import pytest

pytestmark = pytest.mark.integration

_CHART_NAME = "hist-max-probe"
RELEASE = "hist-max-rel"

_CHART_YAML = """\
apiVersion: v2
name: {name}
description: minimal chart rendering one ConfigMap
type: application
version: {version}
appVersion: "1.0"
"""

_CONFIGMAP_TMPL = """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ .Release.Name }}-cfg
data:
  chart: {{ .Chart.Version | quote }}
  step: {{ .Values.step | quote }}
"""


def _write_chart(tmp_path, version):
    chart_dir = tmp_path / f"chart-{version}"
    (chart_dir / "templates").mkdir(parents=True)
    (chart_dir / "Chart.yaml").write_text(_CHART_YAML.format(name=_CHART_NAME, version=version))
    (chart_dir / "templates" / "configmap.yaml").write_text(_CONFIGMAP_TMPL)
    return str(chart_dir)


def _revisions(app_client, namespace):
    resp = app_client.get(f"/api/releases/{namespace}/{RELEASE}/revisions")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _upgrade(app_client, namespace, chart, step):
    resp = app_client.patch(f"/api/releases/{namespace}/{RELEASE}", json={
        "chart": chart, "values": {"step": step}, "reset_values": True, "reuse_values": False,
    })
    assert resp.status_code == 200, resp.text


def _install(app_client, namespace, chart):
    resp = app_client.post("/api/releases", json={
        "namespace": namespace, "name": RELEASE, "chart": chart, "values": {"step": "0"},
    })
    assert resp.status_code == 201, resp.text


@pytest.fixture
def aged_release(app_client, test_namespace, tmp_path):
    """rev1＝A 0.1.0 → rev2..12＝B 0.2.0（11 次 upgrade，其中最後一次值不變＝no-op Redeploy）。

    共 12 筆，超過 helm 預設的 10：修正前 rev 1、2 會被清掉。
    """
    from src.config import settings
    assert settings.helm_history_max == 20, "前提：沒有被環境變數改掉的預設值"
    chart_a, chart_b = _write_chart(tmp_path, "0.1.0"), _write_chart(tmp_path, "0.2.0")
    _install(app_client, test_namespace, chart_a)
    for step in range(1, 11):
        _upgrade(app_client, test_namespace, chart_b, str(step))
    _upgrade(app_client, test_namespace, chart_b, "10")  # 值不變（AI-HER-003-1 的 no-op）
    return chart_b


def test_old_chart_revision_survives_more_than_ten_operations(app_client, test_namespace,
                                                              aged_release):
    data = _revisions(app_client, test_namespace)
    revs = [(r["revision"], r["chart"]) for r in data["revisions"]]
    assert data["history_max"] == 20
    assert [r for r, _ in revs] == list(range(1, 13)), revs
    assert revs[0] == (1, f"{_CHART_NAME}-0.1.0"), "升級前（舊 chart）的 revision 必須還在"


def test_rollback_to_old_chart_revision_is_refused_not_missing(app_client, test_namespace,
                                                               aged_release):
    """#1526 實機回歸：舊 chart 的 revision 還在 ⇒ 下限真的擋（409），而不是 404 找不到。"""
    before = _revisions(app_client, test_namespace)["revisions"]

    resp = app_client.post(f"/api/releases/{test_namespace}/{RELEASE}/rollback",
                           json={"revision": 1})

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error_code"] == "rollback_chart_downgrade"
    assert (detail["target_chart_version"], detail["current_chart_version"]) == ("0.1.0", "0.2.0")
    assert _revisions(app_client, test_namespace)["revisions"] == before


def test_rollback_does_not_prune_back_to_helm_default(app_client, test_namespace, aged_release):
    """rollback 也寫一筆新 revision、照自己的 --history-max 修剪；沒帶就修回 10 筆。"""
    resp = app_client.post(f"/api/releases/{test_namespace}/{RELEASE}/rollback",
                           json={"revision": 5})
    assert resp.status_code == 200, resp.text

    revs = [r["revision"] for r in _revisions(app_client, test_namespace)["revisions"]]
    assert revs == list(range(1, 14)), revs


def test_history_max_setting_is_what_helm_prunes_to(app_client, test_namespace, tmp_path,
                                                    monkeypatch):
    """上限真的由 HELM_HISTORY_MAX 決定（helm 照旗標修剪，留最新的 N 筆）。"""
    monkeypatch.setattr("src.services.helm.settings.helm_history_max", 3)
    chart = _write_chart(tmp_path, "0.1.0")
    _install(app_client, test_namespace, chart)
    for step in range(1, 5):
        _upgrade(app_client, test_namespace, chart, str(step))

    data = _revisions(app_client, test_namespace)
    assert [r["revision"] for r in data["revisions"]] == [3, 4, 5]
    assert data["history_max"] == 3
