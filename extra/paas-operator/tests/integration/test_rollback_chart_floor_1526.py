"""#1526 integration：回復不降 chart、憑證維持現值——真 helm＋真 kubectl＋真 HTTP。

稽核重現（sbx-b #9／#16）的形狀：裝 A 版 → 升 B 版 → 換金鑰／密碼 → 回復到 A 的
revision。修正前 operator 以目標 revision 的 chart 版本重新渲染（或重放 stored manifest），
chart 被降回 A、平台的安全修正跟著撤掉。unit 測試 mock 掉 helm，量不到兩件事：

* helm 自己的 ``get all --template {{.Release.Chart.Metadata.Version}}`` 與 ``history``
  在真 release 上讀得出版本、下限判斷拿到的是真值；
* 被擋下的回復**真的沒有**在叢集留下任何東西（沒有新 revision、Secret／ConfigMap 不變），
  放行的回復**真的**把非憑證設定退回去、憑證維持現值。

chart 只渲染 Secret 與 ConfigMap（不排 Pod），整支測試不會在任何節點上起容器。
金鑰都是測試字串；斷言只比對值，不印出。

Run::

    OPERATOR_TEST_NAMESPACE_PREFIX=dev-sandbox-<topic>- KUBECONFIG=... \\
    HELM_BINARY=<helm 3.13.3> \\
        pytest -m integration tests/integration/test_rollback_chart_floor_1526.py -v
"""
import base64
import json
import subprocess

import pytest

pytestmark = pytest.mark.integration

_CHART_NAME = "rb-floor-probe"
RELEASE = "rb-floor-rel"
PRESERVE = ["config.sensitive", "auth.openrouterApiKey"]

_CHART_YAML = """\
apiVersion: v2
name: {name}
description: minimal chart rendering credentials into a Secret and settings into a ConfigMap
type: application
version: {version}
appVersion: "1.0"
"""

_SECRET_TMPL = """\
apiVersion: v1
kind: Secret
metadata:
  name: {{ .Release.Name }}-creds
type: Opaque
stringData:
  admin_password: {{ .Values.config.sensitive.admin_password | quote }}
  openrouter: {{ .Values.auth.openrouterApiKey | default "" | quote }}
"""

_CONFIGMAP_TMPL = """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ .Release.Name }}-cfg
data:
  chart: {{ .Chart.Version | quote }}
  model: {{ .Values.config.nonSensitive.model | quote }}
"""


def _write_chart(tmp_path, version):
    chart_dir = tmp_path / f"chart-{version}"
    (chart_dir / "templates").mkdir(parents=True)
    (chart_dir / "Chart.yaml").write_text(_CHART_YAML.format(name=_CHART_NAME, version=version))
    (chart_dir / "templates" / "secret.yaml").write_text(_SECRET_TMPL)
    (chart_dir / "templates" / "configmap.yaml").write_text(_CONFIGMAP_TMPL)
    return str(chart_dir)


def _values(key, password, model):
    return {
        "config": {"sensitive": {"admin_password": password}, "nonSensitive": {"model": model}},
        "auth": {"openrouterApiKey": key},
    }


def _kubectl_json(*args):
    out = subprocess.run(
        ["kubectl", *args, "-o", "json"], check=True, capture_output=True, text=True,
    ).stdout
    return json.loads(out)


def _state(namespace):
    """叢集上實際生效的值：（chart 版本、model、金鑰、密碼）。"""
    cm = _kubectl_json("get", "configmap", f"{RELEASE}-cfg", "-n", namespace)["data"]
    secret = _kubectl_json("get", "secret", f"{RELEASE}-creds", "-n", namespace)["data"]
    decode = lambda v: base64.b64decode(v).decode()  # noqa: E731
    return (cm["chart"], cm["model"], decode(secret["openrouter"]),
            decode(secret["admin_password"]))


def _revisions(app_client, namespace):
    resp = app_client.get(f"/api/releases/{namespace}/{RELEASE}/revisions")
    assert resp.status_code == 200, resp.text
    return [(r["revision"], r["chart"], r["status"]) for r in resp.json()["revisions"]]


@pytest.fixture
def upgraded_release(app_client, test_namespace, tmp_path):
    """rev1＝A 0.1.0（K1/P1/m1）→ rev2＝B 0.2.0（同值）→ rev3＝B（K2/P2/m2，換金鑰與密碼）。"""
    chart_a, chart_b = _write_chart(tmp_path, "0.1.0"), _write_chart(tmp_path, "0.2.0")
    resp = app_client.post("/api/releases", json={
        "namespace": test_namespace, "name": RELEASE, "chart": chart_a,
        "values": _values("KEY-1", "PW-1", "m1"),
    })
    assert resp.status_code == 201, resp.text
    for values in (_values("KEY-1", "PW-1", "m1"), _values("KEY-2", "PW-2", "m2")):
        resp = app_client.patch(f"/api/releases/{test_namespace}/{RELEASE}", json={
            "chart": chart_b, "values": values, "reset_values": True, "reuse_values": False,
        })
        assert resp.status_code == 200, resp.text
    assert _state(test_namespace) == ("0.2.0", "m2", "KEY-2", "PW-2"), "前提：B 版、金鑰 2"
    assert [r[1] for r in _revisions(app_client, test_namespace)] == [
        f"{_CHART_NAME}-0.1.0", f"{_CHART_NAME}-0.2.0", f"{_CHART_NAME}-0.2.0"]
    return chart_b


def test_rollback_to_older_chart_is_refused_in_both_modes(
    app_client, test_namespace, upgraded_release,
):
    chart_b = upgraded_release
    before = _revisions(app_client, test_namespace)

    preserving = app_client.post(f"/api/releases/{test_namespace}/{RELEASE}/rollback", json={
        "revision": 1, "chart": chart_b, "preserve_value_paths": PRESERVE,
    })
    plain = app_client.post(f"/api/releases/{test_namespace}/{RELEASE}/rollback",
                            json={"revision": 1})

    for resp in (preserving, plain):
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["error_code"] == "rollback_chart_downgrade"
        assert (detail["target_chart_version"], detail["current_chart_version"]) == (
            "0.1.0", "0.2.0"), "版本必須是 helm 從真 release 讀出來的值"
        assert "KEY-" not in resp.text and "PW-" not in resp.text
    assert _revisions(app_client, test_namespace) == before, "被擋下的回復不得留下新 revision"
    assert _state(test_namespace) == ("0.2.0", "m2", "KEY-2", "PW-2"), (
        "chart 不降、金鑰與管理員密碼維持現值")


def test_same_chart_rollback_restores_settings_but_keeps_credentials(
    app_client, test_namespace, upgraded_release,
):
    chart_b = upgraded_release

    resp = app_client.post(f"/api/releases/{test_namespace}/{RELEASE}/rollback", json={
        "revision": 2, "chart": chart_b, "preserve_value_paths": PRESERVE,
    })

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["mode"], body["chart_version"], body["revision"]) == (
        "upgrade_preserving", "0.2.0", 4)
    assert _state(test_namespace) == ("0.2.0", "m1", "KEY-2", "PW-2"), (
        "一般設定回到 rev2（m1）；金鑰（KEY-2）與管理員密碼（PW-2）維持現值、不復活舊值")
    assert _revisions(app_client, test_namespace)[-1][:2] == (4, f"{_CHART_NAME}-0.2.0")


def test_plain_same_chart_rollback_reports_the_chart_version(
    app_client, test_namespace, upgraded_release,
):
    """純 helm rollback（沒有平台憑證的模板）：同 chart 照常回復，並回報版本給平台帳本。"""
    resp = app_client.post(f"/api/releases/{test_namespace}/{RELEASE}/rollback",
                           json={"revision": 2})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["mode"], body["chart_version"], body["target_revision"]) == (
        "rollback", "0.2.0", 2)
    assert _state(test_namespace)[:2] == ("0.2.0", "m1")
