"""#1685：純 ``helm rollback`` 也做 #1647 的 compute 配額預檢。

#1658 只涵蓋了「保留憑證的回復」（一次 ``helm upgrade``，有 dry-run 可算）。沒有平台憑證的
服務（例如 Odoo）走純 ``helm rollback``：平台照樣送 ``check_compute_quota``，operator 卻不
檢查就回復。目標 revision 的 limits 比現在大、工作區配額放不下時，``Recreate`` 的服務舊 pod
先被砍、新 pod 被 quota 擋下——與 2026-10-10 的事故相同。

做法：純 rollback 重放的是目標 revision 的 stored manifest，沒有 dry-run；改讀
``helm get manifest --revision <n>``，與 live workload 一起交給同一個 ``_compute_quota_check``。

本檔守：

1. 確定放不下 ⇒ 409 ``workspace_compute_quota_exceeded``，``helm rollback`` 沒有被呼叫；
   放得下 ⇒ rollback 照做。
2. 不帶旗標、或 kill switch 關 ⇒ 不多打任何 helm／kubectl（連 stored manifest 都不讀）。
3. stored manifest 讀不到／解析失敗、live objects 讀不到、檢查本身出錯 ⇒ 放行並 log warning。
4. ⛔ stored manifest 可能含 Secret：不進 log、不進回應。
5. 預檢＋rollback 佔同一個 helm 名額（可重入，不自己等自己）；排不到就 503，什麼都沒讀、
   什麼都沒套用。
"""
import logging
import threading
from unittest.mock import patch

import pytest
import yaml

from src.config import settings
from src.models.schemas import ReleaseRevision, ReleaseStatus
from src.services import compute_quota
from src.services.compute_quota import COMPUTE_QUOTA_ERROR_CODE
from src.services.helm import HelmException, HelmService
from src.services.helm_limiter import (
    OPERATOR_BUSY_ERROR_CODE,
    HelmOperationBusy,
    helm_operation_slot,
    helm_operation_stats,
    reset_helm_operation_limiter,
)
from tests.test_compute_quota_preflight_1647 import (
    _SECRET_MARKER,
    NS,
    RELEASE,
    _incident,
    _with_secrets,
)

_URL = f"/api/releases/{NS}/{RELEASE}/rollback"
_CHECKED = {"revision": 10, "check_compute_quota": True}
_PLAIN = {"revision": 10}


def _history(*revisions):
    return [
        ReleaseRevision(
            revision=r, updated="2026-10-14T00:00:00Z",
            status=ReleaseStatus.DEPLOYED if r == max(revisions) else ReleaseStatus.SUPERSEDED,
            chart="odoo-0.2.9", app_version="18.0", description="Upgrade complete",
        )
        for r in revisions
    ]


def _arrange(mock_helm, live_objects_lookup, compute_quota_snapshot, case=None):
    """``_incident`` 的形狀接到純 rollback 的 seam：stored manifest、live workload、quota snapshot。

    事故形狀：目標 revision（10）的 Deployment 要 8 cpu、現在 4 cpu，hard 12、平台外 pg 佔 8。
    """
    case = case or _incident()
    mock_helm.history.return_value = _history(10, 11, 12)
    mock_helm.get_revision_chart_version.return_value = "0.2.9"
    mock_helm.get_manifest.return_value = yaml.safe_dump_all(case["rendered_docs"])
    mock_helm.rollback.return_value = {"message": "Rollback was a success! Happy Helming!"}
    live_objects_lookup["objects"].return_value = case["live_items"]
    compute_quota_snapshot.return_value = {
        "quotas": case["quota_items"], "limitranges": case["limitrange_items"],
        "pods": case["pods"]}
    return case


# --- 判斷 --------------------------------------------------------------------------


@patch("src.api.releases.helm_service")
def test_checked_rollback_that_cannot_fit_is_refused_before_rolling_back(
        mock_helm, client, live_objects_lookup, compute_quota_snapshot):
    _arrange(mock_helm, live_objects_lookup, compute_quota_snapshot)
    r = client.post(_URL, json=_CHECKED)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["error_code"] == COMPUTE_QUOTA_ERROR_CODE
    assert detail["namespace"] == NS and detail["release"] == RELEASE
    assert detail["shortfalls"][0]["short_by"] == "4"
    # 讀的是**目標** revision 的 stored manifest
    mock_helm.get_manifest.assert_called_once_with(NS, RELEASE, 10)
    live_objects_lookup["objects"].assert_called_once_with(NS, "pvc,statefulsets,deployments")
    compute_quota_snapshot.assert_called_once_with(NS)
    # 什麼都沒套用
    mock_helm.rollback.assert_not_called()
    mock_helm.upgrade.assert_not_called()


@patch("src.api.releases.helm_service")
def test_checked_rollback_that_fits_is_rolled_back(
        mock_helm, client, live_objects_lookup, compute_quota_snapshot):
    _arrange(mock_helm, live_objects_lookup, compute_quota_snapshot, _incident(new_cpu="4"))
    r = client.post(_URL, json=_CHECKED)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "rollback" and body["target_revision"] == 10
    assert body["chart_version"] == "0.2.9"
    compute_quota_snapshot.assert_called_once_with(NS)
    mock_helm.rollback.assert_called_once_with(NS, RELEASE, 10)


@patch("src.api.releases.helm_service")
def test_checked_rollback_answers_like_an_unchecked_one(
        mock_helm, client, live_objects_lookup, compute_quota_snapshot):
    """回應不多帶 ``compute_quota``——與 PATCH、保留憑證的回復一樣，只有 409 表達「放不下」。"""
    _arrange(mock_helm, live_objects_lookup, compute_quota_snapshot, _incident(new_cpu="4"))
    checked = client.post(_URL, json=_CHECKED)
    plain = client.post(_URL, json=_PLAIN)
    assert checked.status_code == plain.status_code == 200
    assert checked.json() == plain.json()


@patch("src.api.releases.helm_service")
def test_revision_omitted_checks_the_previous_revision(
        mock_helm, client, live_objects_lookup, compute_quota_snapshot):
    """不帶 revision＝上一版：檢查的與真正回復的是解析過的同一個 revision。"""
    _arrange(mock_helm, live_objects_lookup, compute_quota_snapshot, _incident(new_cpu="4"))
    r = client.post(_URL, json={"check_compute_quota": True})
    assert r.status_code == 200, r.text
    mock_helm.get_manifest.assert_called_once_with(NS, RELEASE, 11)
    mock_helm.rollback.assert_called_once_with(NS, RELEASE, 11)


@patch("src.api.releases.helm_service")
def test_chart_downgrade_is_refused_before_the_quota_is_read(
        mock_helm, client, live_objects_lookup, compute_quota_snapshot):
    """#1526 的版本下限是確定性的拒絕，先回報；被擋下時不必再讀 manifest／quota。"""
    _arrange(mock_helm, live_objects_lookup, compute_quota_snapshot)
    mock_helm.get_revision_chart_version.side_effect = (
        lambda ns, name, revision: "0.2.8" if revision == 10 else "0.2.9")
    r = client.post(_URL, json=_CHECKED)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["error_code"] == "rollback_chart_downgrade"
    mock_helm.get_manifest.assert_not_called()
    compute_quota_snapshot.assert_not_called()
    mock_helm.rollback.assert_not_called()


# --- 不檢查的情況：行為與 #1685 之前相同 ---------------------------------------------


@patch("src.api.releases.helm_service")
def test_without_the_flag_nothing_extra_is_read_and_nothing_is_refused(
        mock_helm, client, live_objects_lookup, compute_quota_snapshot):
    """舊平台（不帶旗標）：同一個放不下的回復照舊送出，stored manifest／live／quota 都不讀。"""
    _arrange(mock_helm, live_objects_lookup, compute_quota_snapshot)
    r = client.post(_URL, json=_PLAIN)
    assert r.status_code == 200, r.text
    assert r.json() == {"message": "Rollback was a success! Happy Helming!", "mode": "rollback",
                        "target_revision": 10, "chart_version": "0.2.9"}
    mock_helm.get_manifest.assert_not_called()
    live_objects_lookup["objects"].assert_not_called()
    compute_quota_snapshot.assert_not_called()
    mock_helm.rollback.assert_called_once_with(NS, RELEASE, 10)


@patch("src.api.releases.helm_service")
def test_kill_switch_skips_the_check_without_reading_anything(
        mock_helm, client, live_objects_lookup, compute_quota_snapshot, monkeypatch):
    monkeypatch.setattr(settings, "compute_quota_precheck_enabled", False)
    _arrange(mock_helm, live_objects_lookup, compute_quota_snapshot)
    r = client.post(_URL, json=_CHECKED)
    assert r.status_code == 200, r.text
    mock_helm.get_manifest.assert_not_called()
    live_objects_lookup["objects"].assert_not_called()
    compute_quota_snapshot.assert_not_called()
    mock_helm.rollback.assert_called_once_with(NS, RELEASE, 10)


# --- 讀不到／出錯一律放行 --------------------------------------------------------------


@pytest.mark.parametrize("failure", [
    HelmException(message="Helm command failed with code 1", command="helm get manifest",
                  stderr="Error: release: not found"),
    OSError(12, "Cannot allocate memory"),
    RuntimeError("boom"),
])
@patch("src.api.releases.helm_service")
def test_unreadable_stored_manifest_does_not_block(
        mock_helm, failure, client, live_objects_lookup, compute_quota_snapshot, caplog):
    _arrange(mock_helm, live_objects_lookup, compute_quota_snapshot)
    mock_helm.get_manifest.side_effect = failure
    with caplog.at_level(logging.WARNING, logger="src.api.releases"):
        r = client.post(_URL, json=_CHECKED)
    assert r.status_code == 200, r.text
    assert "Compute quota pre-check skipped" in caplog.text
    compute_quota_snapshot.assert_not_called()
    mock_helm.rollback.assert_called_once_with(NS, RELEASE, 10)


@patch("src.api.releases.helm_service")
def test_unparseable_stored_manifest_does_not_block_and_is_not_echoed(
        mock_helm, client, live_objects_lookup, compute_quota_snapshot, caplog):
    """YAML 錯誤訊息會引用出錯的那一行——manifest 可能含 Secret，log 只能寫例外的型別。"""
    _arrange(mock_helm, live_objects_lookup, compute_quota_snapshot)
    mock_helm.get_manifest.return_value = (
        f"kind: Secret\nstringData:\n  admin_password: {_SECRET_MARKER}\n  bad: [unclosed\n")
    with caplog.at_level(logging.DEBUG, logger="src"):
        r = client.post(_URL, json=_CHECKED)
    assert r.status_code == 200, r.text
    assert "Compute quota pre-check skipped" in caplog.text
    assert _SECRET_MARKER not in caplog.text
    assert _SECRET_MARKER not in r.text
    compute_quota_snapshot.assert_not_called()
    mock_helm.rollback.assert_called_once_with(NS, RELEASE, 10)


@patch("src.api.releases.helm_service")
def test_unreadable_live_objects_do_not_block(
        mock_helm, client, live_objects_lookup, compute_quota_snapshot, caplog):
    _arrange(mock_helm, live_objects_lookup, compute_quota_snapshot)
    live_objects_lookup["objects"].return_value = None
    with caplog.at_level(logging.WARNING, logger="src.api.releases"):
        r = client.post(_URL, json=_CHECKED)
    assert r.status_code == 200, r.text
    assert "Compute quota pre-check skipped" in caplog.text
    compute_quota_snapshot.assert_not_called()
    mock_helm.rollback.assert_called_once_with(NS, RELEASE, 10)


@patch("src.api.releases.helm_service")
def test_unreadable_quota_snapshot_does_not_block(
        mock_helm, client, live_objects_lookup, compute_quota_snapshot):
    from src.services.helm import KubectlException

    _arrange(mock_helm, live_objects_lookup, compute_quota_snapshot)
    compute_quota_snapshot.side_effect = KubectlException(
        message="kubectl command failed with code 1", command="kubectl get",
        stderr="Error from server (Forbidden): limitranges is forbidden")
    r = client.post(_URL, json=_CHECKED)
    assert r.status_code == 200, r.text
    mock_helm.rollback.assert_called_once_with(NS, RELEASE, 10)


@pytest.mark.parametrize("failure", [
    compute_quota.Unknown("selector_unreadable", "Deployment/svc-app"),
    RuntimeError("boom"),
    KeyError("spec"),
])
@patch("src.api.releases.helm_service")
def test_a_failing_check_never_blocks_the_rollback(
        mock_helm, failure, client, live_objects_lookup, compute_quota_snapshot):
    _arrange(mock_helm, live_objects_lookup, compute_quota_snapshot)
    with patch.object(compute_quota, "evaluate", side_effect=failure) as evaluated:
        r = client.post(_URL, json=_CHECKED)
    assert r.status_code == 200, r.text
    evaluated.assert_called_once()
    mock_helm.rollback.assert_called_once_with(NS, RELEASE, 10)


@patch("src.api.releases.helm_service")
def test_unexpected_failure_around_the_check_never_blocks_the_rollback(
        mock_helm, client, live_objects_lookup, compute_quota_snapshot, caplog):
    """``_compute_quota_check`` 以外的意外（例：live objects 讀回來的形狀不對）一樣放行。"""
    _arrange(mock_helm, live_objects_lookup, compute_quota_snapshot)
    with patch("src.api.releases._compute_quota_check", side_effect=RuntimeError("boom")), \
            caplog.at_level(logging.WARNING, logger="src.api.releases"):
        r = client.post(_URL, json=_CHECKED)
    assert r.status_code == 200, r.text
    assert "Compute quota pre-check skipped" in caplog.text
    mock_helm.rollback.assert_called_once_with(NS, RELEASE, 10)


# --- ⛔ stored manifest 不外流 --------------------------------------------------------


@patch("src.api.releases.helm_service")
def test_refusal_leaks_no_manifest_content_to_the_response_or_the_log(
        mock_helm, client, live_objects_lookup, compute_quota_snapshot, caplog):
    _arrange(mock_helm, live_objects_lookup, compute_quota_snapshot, _with_secrets(_incident()))
    with caplog.at_level(logging.DEBUG, logger="src"):
        r = client.post(_URL, json=_CHECKED)
    assert r.status_code == 409
    assert _SECRET_MARKER not in r.text
    assert _SECRET_MARKER not in caplog.text


@patch("src.api.releases.helm_service")
def test_pass_leaks_no_manifest_content_to_the_response_or_the_log(
        mock_helm, client, live_objects_lookup, compute_quota_snapshot, caplog):
    _arrange(mock_helm, live_objects_lookup, compute_quota_snapshot,
             _with_secrets(_incident(new_cpu="4")))
    with caplog.at_level(logging.DEBUG, logger="src"):
        r = client.post(_URL, json=_CHECKED)
    assert r.status_code == 200
    assert "Compute quota pre-check passed" in caplog.text
    assert _SECRET_MARKER not in r.text
    assert _SECRET_MARKER not in caplog.text


# --- HelmService.get_manifest -----------------------------------------------------------


@patch("src.services.helm.subprocess.run")
def test_get_manifest_reads_the_stored_manifest_of_one_revision(mock_run, caplog):
    mock_run.return_value.returncode = 0
    mock_run.return_value.stdout = f"kind: Secret\nstringData:\n  p: {_SECRET_MARKER}\n"
    with caplog.at_level(logging.DEBUG, logger="src"):
        manifest = HelmService().get_manifest(NS, RELEASE, 10)
    assert _SECRET_MARKER in manifest
    cmd = mock_run.call_args.args[0]
    assert cmd[1:] == ["get", "manifest", RELEASE, "--namespace", NS, "--revision", "10"]
    assert _SECRET_MARKER not in caplog.text


def test_get_manifest_rejects_a_foreign_namespace():
    with pytest.raises(ValueError):
        HelmService().get_manifest("kube-system", RELEASE, 1)


# --- 併發：預檢＋rollback 佔同一個 helm 名額 --------------------------------------------


@pytest.fixture
def one_slot():
    saved = (settings.helm_max_concurrent_operations, settings.helm_queue_timeout_seconds)
    settings.helm_max_concurrent_operations = 1
    settings.helm_queue_timeout_seconds = 1
    reset_helm_operation_limiter()
    yield
    settings.helm_max_concurrent_operations, settings.helm_queue_timeout_seconds = saved
    reset_helm_operation_limiter()


def _slot_free_for_others():
    """另一條 thread（另一個請求）此刻拿不拿得到 helm 名額。"""
    result = {}

    def probe():
        try:
            with helm_operation_slot("probe", timeout=0.2):
                result["free"] = True
        except HelmOperationBusy:
            result["free"] = False

    t = threading.Thread(target=probe)
    t.start()
    t.join(5)
    return result["free"]


@patch("src.api.releases.helm_service")
def test_check_and_rollback_hold_one_slot_without_waiting_on_themselves(
        mock_helm, one_slot, client, live_objects_lookup, compute_quota_snapshot):
    """名額只有 1 個：讀 manifest 時與 rollback 時都佔著同一個名額，別的請求插不進來；
    rollback 自己再進名額（``HelmService.rollback`` 內部那一層）不會等自己而死結。"""
    _arrange(mock_helm, live_objects_lookup, compute_quota_snapshot, _incident(new_cpu="4"))
    manifest = mock_helm.get_manifest.return_value
    seen = {}

    def get_manifest(namespace, name, revision):
        seen["during_check"] = (helm_operation_stats()["active"], _slot_free_for_others())
        return manifest

    def rollback(namespace, name, revision):
        with helm_operation_slot("rollback"):  # 真的 HelmService.rollback 也這樣拿名額
            seen["during_rollback"] = (helm_operation_stats()["active"], _slot_free_for_others())
        return {"message": "ok"}

    mock_helm.get_manifest.side_effect = get_manifest
    mock_helm.rollback.side_effect = rollback
    r = client.post(_URL, json=_CHECKED)
    assert r.status_code == 200, r.text
    assert seen == {"during_check": (1, False), "during_rollback": (1, False)}
    assert helm_operation_stats()["active"] == 0


@patch("src.api.releases.helm_service")
def test_busy_operator_answers_503_before_reading_or_applying_anything(
        mock_helm, one_slot, client, live_objects_lookup, compute_quota_snapshot):
    _arrange(mock_helm, live_objects_lookup, compute_quota_snapshot, _incident(new_cpu="4"))
    result = {}
    with helm_operation_slot("held-by-test"):
        t = threading.Thread(target=lambda: result.setdefault(
            "r", client.post(_URL, json=_CHECKED)))
        t.start()
        t.join(10)
    r = result["r"]
    assert r.status_code == 503, r.text
    assert r.json()["detail"]["error_code"] == OPERATOR_BUSY_ERROR_CODE
    mock_helm.get_manifest.assert_not_called()
    compute_quota_snapshot.assert_not_called()
    mock_helm.rollback.assert_not_called()


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from src.main import app

    with patch("src.main.settings") as mock_settings:
        mock_settings.api_key = "test-api-key"
        mock_settings.namespace_prefix = "paas-ws-"
        yield TestClient(app, headers={"X-API-Key": "test-api-key"})
