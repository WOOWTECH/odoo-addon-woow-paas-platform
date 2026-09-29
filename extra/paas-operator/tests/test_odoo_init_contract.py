"""#1129：從公開 initialize 入口驗證 woow-odoo 的 K8s exec 契約。"""

import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.config import settings
from src.main import app
from src.services.helm import KubectlException, KubernetesService
from src.services.odoo_init import OdooInitError, OdooInitService


def test_initialize_targets_web_pod_and_uses_image_python_launcher():
    """同 release 的 PG／cron／Job 不得取代 server；兩次 exec 都走共同 launcher。"""
    k8s = MagicMock()
    pods = [
        ("pg-0", {"app.kubernetes.io/instance": "tenant", "app.kubernetes.io/name": "odoo", "app.kubernetes.io/component": "postgresql"}),
        ("cron-0", {"app.kubernetes.io/instance": "tenant", "app.kubernetes.io/name": "odoo", "app.kubernetes.io/component": "cron"}),
        ("backup-job", {"app.kubernetes.io/instance": "tenant", "app.kubernetes.io/name": "odoo", "app.kubernetes.io/component": "db-backup"}),
        ("web-0", {"app.kubernetes.io/instance": "tenant", "app.kubernetes.io/name": "odoo", "app.kubernetes.io/component": "server"}),
    ]

    def get_pod_name(namespace, selector):
        assert namespace == "dev-sandbox-1129"
        requested = dict(part.split("=", 1) for part in selector.split(","))
        for name, labels in pods:
            if requested.items() <= labels.items():
                return name
        raise KubectlException("No pods", "get pods", "")

    k8s.get_pod_name.side_effect = get_pod_name
    k8s.exec_in_pod.side_effect = [
        MagicMock(stdout=json.dumps({"ready": True, "version": "18.0"})),
        MagicMock(stdout=json.dumps({"success": True, "added": 3, "updated": 2, "cleaned": 1})),
    ]

    result = OdooInitService(k8s).initialize("dev-sandbox-1129", "tenant")

    assert result == {
        "success": True,
        "modules_added": 3,
        "modules_updated": 2,
        "modules_cleaned": 1,
        "message": "Module list updated successfully",
    }
    k8s.get_pod_name.assert_called_once_with(
        "dev-sandbox-1129",
        "app.kubernetes.io/instance=tenant,app.kubernetes.io/component=server",
    )
    assert len(k8s.exec_in_pod.call_args_list) == 2
    for call in k8s.exec_in_pod.call_args_list:
        assert call.kwargs["pod_name"] == "web-0"
        assert call.kwargs["namespace"] == "dev-sandbox-1129"
        assert call.kwargs["container"] == "odoo"
        assert call.kwargs["command"][:2] == ["python3", "-c"]


def _legacy_pod():
    return {
        "metadata": {
            "name": "legacy-0",
            "namespace": "dev-sandbox-1129",
            "labels": {"app.kubernetes.io/instance": "tenant"},
        },
        "spec": {"containers": [{"name": "odoo"}]},
    }


def _fallback_k8s(pod):
    k8s = MagicMock()
    k8s.get_pod_name.side_effect = [
        KubectlException("No server", "get pods", ""), "legacy-0",
    ]
    k8s.get_pod.return_value = pod
    # cron 即使可以回應 version／更新成功，也不能當成 web。
    k8s.exec_in_pod.side_effect = [
        MagicMock(stdout='{"ready": true}'),
        MagicMock(stdout='{"success": true}'),
    ]
    return k8s


@pytest.mark.parametrize("case", [
    "cron", "postgres", "backup", "foreign-name", "job-owner", "job-label",
    "legacy-job-label", "no-odoo", "foreign-namespace", "foreign-instance",
    "missing-metadata", "missing-spec", "missing-namespace", "missing-instance",
    "wrong-pod-name", "get-failure",
])
def test_initialize_rejects_unverified_fallback_without_exec(case):
    pod = _legacy_pod()
    metadata = pod["metadata"]
    labels = metadata["labels"]
    if case in ("cron", "postgres", "backup"):
        labels["app.kubernetes.io/component"] = case
    elif case == "foreign-name":
        labels["app.kubernetes.io/name"] = "postgres"
    elif case == "job-owner":
        metadata["ownerReferences"] = [{"kind": "Job", "name": "backup"}]
    elif case == "job-label":
        labels["batch.kubernetes.io/job-name"] = "backup"
    elif case == "legacy-job-label":
        labels["job-name"] = "backup"
    elif case == "no-odoo":
        pod["spec"]["containers"] = [{"name": "postgres"}]
    elif case == "foreign-namespace":
        metadata["namespace"] = "dev-sandbox-other"
    elif case == "foreign-instance":
        labels["app.kubernetes.io/instance"] = "other"
    elif case == "missing-metadata":
        del pod["metadata"]
    elif case == "missing-spec":
        del pod["spec"]
    elif case == "missing-namespace":
        del metadata["namespace"]
    elif case == "missing-instance":
        del labels["app.kubernetes.io/instance"]
    elif case == "wrong-pod-name":
        metadata["name"] = "other-0"
    k8s = _fallback_k8s(pod)
    if case == "get-failure":
        k8s.get_pod.side_effect = KubectlException("Forbidden", "get pod", "")

    with pytest.raises(OdooInitError) as error:
        OdooInitService(k8s).initialize("dev-sandbox-1129", "tenant")

    assert error.value.step == "find_pod"
    k8s.exec_in_pod.assert_not_called()


def test_initialize_retains_verified_instance_only_legacy_fallback():
    k8s = _fallback_k8s(_legacy_pod())

    result = OdooInitService(k8s).initialize("dev-sandbox-1129", "tenant")

    assert result["success"] is True
    assert k8s.get_pod_name.call_args.args == (
        "dev-sandbox-1129", "app.kubernetes.io/instance=tenant",
    )
    k8s.get_pod.assert_called_once_with("dev-sandbox-1129", "legacy-0")
    assert all(c.kwargs["pod_name"] == "legacy-0" for c in k8s.exec_in_pod.call_args_list)


def test_fallback_reads_real_kubernetes_adapter_metadata(monkeypatch):
    """只替換 subprocess 邊界，避免 MagicMock 掩蓋 getter 沒有接上。"""
    monkeypatch.setattr(settings, "namespace_prefix", "dev-sandbox-1129")
    run = MagicMock(side_effect=[
        MagicMock(returncode=0, stdout=""),
        MagicMock(returncode=0, stdout="legacy-0"),
        MagicMock(returncode=0, stdout=json.dumps(_legacy_pod())),
        MagicMock(returncode=0, stdout='{"ready": true}'),
        MagicMock(returncode=0, stdout='{"success": true}'),
    ])
    monkeypatch.setattr("subprocess.run", run)

    assert OdooInitService(KubernetesService()).initialize(
        "dev-sandbox-1129", "tenant",
    )["success"] is True

    assert run.call_args_list[2].args[0] == [
        "kubectl", "get", "pod", "legacy-0", "--namespace", "dev-sandbox-1129",
        "--output", "json",
    ]


@pytest.mark.parametrize("pod_name", ["", "--all", "pod/other", "../pod", "UPPER", "x" * 254])
def test_fallback_rejects_invalid_pod_name_before_metadata_get(monkeypatch, pod_name):
    monkeypatch.setattr(settings, "namespace_prefix", "dev-sandbox-1129")
    k8s = KubernetesService()
    # discovery 回不合法名稱時，不可把它轉成 kubectl 參數。
    k8s.get_pod_name = MagicMock(side_effect=[
        KubectlException("No server", "get pods", ""), pod_name,
    ])
    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)

    with pytest.raises(OdooInitError) as error:
        OdooInitService(k8s).initialize("dev-sandbox-1129", "tenant")

    assert error.value.step == "find_pod"
    run.assert_not_called()


def test_fallback_getter_revalidates_namespace_before_get(monkeypatch):
    monkeypatch.setattr(settings, "namespace_prefix", "dev-sandbox-1129")
    k8s = KubernetesService()
    k8s.get_pod_name = MagicMock(side_effect=[
        KubectlException("No server", "get pods", ""), "legacy-0",
    ])
    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)

    with pytest.raises(OdooInitError) as error:
        OdooInitService(k8s).initialize("foreign-ns", "tenant")

    assert error.value.step == "find_pod"
    run.assert_not_called()


def test_initialize_without_any_pod_does_not_exec():
    k8s = MagicMock()
    k8s.get_pod_name.side_effect = KubectlException("No pods", "get pods", "")

    with pytest.raises(OdooInitError) as error:
        OdooInitService(k8s).initialize("dev-sandbox-1129", "tenant")

    assert error.value.step == "find_pod"
    k8s.exec_in_pod.assert_not_called()


# ---- #1263：init 回應的 error_step／error_kind（#1257 Q11）------------------------

def _post_init_odoo(k8s):
    """打真的 /init/odoo；只換掉 KubernetesService 邊界與 readiness 的 sleep。"""
    with patch("src.main.settings.api_key", "test-1263"), \
            patch("src.api.init.odoo_init_service.k8s", k8s), \
            patch("src.services.odoo_init.time.sleep"):
        return TestClient(app, headers={"X-API-Key": "test-1263"}).post(
            "/api/releases/dev-sandbox-1129/tenant/init/odoo",
        )


def _ready_k8s(update_result):
    """server Pod 找得到、readiness 通過；update_module_list 的結果由呼叫端決定。"""
    k8s = MagicMock()
    k8s.get_pod_name.return_value = "web-0"
    ready = MagicMock(stdout=json.dumps({"ready": True, "version": "18.0"}))
    if isinstance(update_result, Exception):
        k8s.exec_in_pod.side_effect = [ready, update_result]
    else:
        k8s.exec_in_pod.side_effect = [ready, MagicMock(stdout=update_result)]
    return k8s


def _not_found_k8s():
    k8s = MagicMock()
    k8s.get_pod_name.side_effect = KubectlException("No pods", "get pods", "")
    return k8s


def _never_ready_k8s():
    k8s = MagicMock()
    k8s.get_pod_name.return_value = "web-0"
    k8s.exec_in_pod.return_value = MagicMock(stdout=json.dumps({"ready": False}))
    return k8s


@pytest.mark.parametrize("k8s_factory, step, kind", [
    # readiness 探測本身唯讀 ⇒ 可以確定還沒動到租戶 DB。
    (_not_found_k8s, "find_pod", "not_executed"),
    (_never_ready_k8s, "wait_ready", "not_executed"),
    # 腳本自己回報失敗 ⇒ 確定失敗。
    (lambda: _ready_k8s(json.dumps({"success": False, "error": "boom"})),
     "update_module_list", "failed"),
    # 以下都不能證明容器內的 ORM 程序沒跑或已停 ⇒ 結果未知。
    (lambda: _ready_k8s(KubectlException("timed out", "exec", "", timed_out=True)),
     "update_module_list", "unknown"),
    (lambda: _ready_k8s(KubectlException("exec failed", "exec", "")),
     "update_module_list", "unknown"),
    (lambda: _ready_k8s(""), "update_module_list", "unknown"),
    (lambda: _ready_k8s("not json"), "update_module_list", "unknown"),
], ids=["find_pod", "wait_ready", "script-failed", "exec-timeout", "exec-failed",
        "empty-output", "invalid-json"])
def test_init_odoo_failure_carries_error_step_and_kind(k8s_factory, step, kind):
    response = _post_init_odoo(k8s_factory())

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is False
    assert body["error_step"] == step
    assert body["error_kind"] == kind
    # 舊平台 parse 的字串前綴維持不變。
    assert body["error"].startswith(f"[{step}] ")


def test_init_odoo_success_has_no_error_classification():
    response = _post_init_odoo(_ready_k8s(json.dumps({"success": True, "added": 1})))

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["error_step"] is None
    assert body["error_kind"] is None


def test_init_odoo_unexpected_exception_is_500_with_structured_unknown():
    k8s = MagicMock()
    k8s.get_pod_name.side_effect = RuntimeError("kaboom")

    response = _post_init_odoo(k8s)

    assert response.status_code == 500
    detail = response.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["error_kind"] == "unknown"
    assert detail["error_step"] is None
    assert "kaboom" in detail["message"]
    # error_type 是例外類別名稱，與 error_kind 是兩個不同的維度。
    assert response.json()["error_type"] == "HTTPException"
