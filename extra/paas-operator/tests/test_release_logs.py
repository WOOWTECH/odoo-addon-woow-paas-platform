"""Tenant ownership, bounded snapshots, and safe failures for service logs."""
import json
import os
import stat
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml
from fastapi.testclient import TestClient

from src.api import releases
from src.config import settings
from src.main import app
from src.services.helm import KubectlException, KubernetesService
from src.services.release_logs import LOG_BYTE_LIMIT, LOG_COMMAND_TIMEOUT_SECONDS, ReleaseLogReader

NS, RELEASE = "paas-ws-logs-test", "svc-logs-test"


def pod(name="app-0", release=RELEASE, namespace=NS, phase="Running", deleting=False):
    return {
        "metadata": {"name": name, "namespace": namespace,
                     "labels": {"app.kubernetes.io/instance": release},
                     "deletionTimestamp": "2026-10-18T00:00:00Z" if deleting else None},
        "spec": {"containers": [{"name": "app"}, {"name": "proxy"}],
                 "initContainers": [{"name": "init-db"}]},
        "status": {"phase": phase},
    }


def completed(stdout=b"", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=b"private-credential")


def reader(items=None, text=b"2026-10-18T00:00:00Z started\n"):
    service = ReleaseLogReader()
    service._run = Mock(side_effect=[
        completed(json.dumps({"items": items if items is not None else [pod()]}).encode()),
        completed(text),
    ])
    return service


@pytest.fixture(autouse=True)
def namespace_boundary(monkeypatch):
    monkeypatch.setattr(settings, "namespace_prefix", "paas-ws-")
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.delenv("KUBERNETES_SERVICE_PORT", raising=False)


def test_snapshot_ownership_discovery_and_read_are_bounded():
    service = reader()
    result = service.read(NS, RELEASE)
    assert result.available and result.text.endswith("started\n")
    assert result.reason is None and not result.truncated
    assert result.pod == "app-0" and result.container == "app"
    assert result.sources[0].containers == ["app", "proxy", "init-db"]
    discovery, read = [call.args[0] for call in service._run.call_args_list]
    assert discovery[discovery.index("--selector") + 1] == f"app.kubernetes.io/instance={RELEASE}"
    assert discovery[discovery.index("--namespace") + 1] == NS
    assert read[:2] == ["logs", "app-0"]
    assert read[read.index("--namespace") + 1] == NS
    assert read[read.index("--container") + 1] == "app"
    assert "--timestamps=true" in read and "--tail=500" in read
    assert f"--limit-bytes={LOG_BYTE_LIMIT + 1}" in read
    assert "--follow" not in read


def test_defaults_are_deterministic_and_prefer_running_nonterminating_pods():
    service = reader([pod("a-old", deleting=True), pod("a-job", phase="Succeeded"), pod("z-running"), pod("b-running")])
    assert service.read(NS, RELEASE).pod == "b-running"


@pytest.mark.parametrize("foreign", [pod(release="other"), pod(release=RELEASE + "-suffix"), pod(namespace="paas-ws-other")])
def test_even_discovery_results_must_match_exact_namespace_and_release(foreign):
    service = reader([foreign])
    result = service.read(NS, RELEASE)
    assert not result.available and result.reason == "no_pods"
    assert not result.sources and not result.text
    assert service._run.call_count == 1


def test_arbitrary_same_namespace_pod_cannot_be_read():
    service = reader()
    result = service.read(NS, RELEASE, pod="other-release-0")
    assert not result.available and result.reason == "source_not_found"
    assert result.text == "" and result.pod is None
    assert service._run.call_count == 1


def test_arbitrary_container_cannot_be_read():
    service = reader()
    result = service.read(NS, RELEASE, pod="app-0", container="unknown")
    assert not result.available and result.reason == "container_not_found"
    assert result.container is None
    assert service._run.call_count == 1


def test_init_container_and_previous_logs_can_be_selected():
    service = reader()
    result = service.read(NS, RELEASE, pod="app-0", container="init-db", previous=True, tail_lines=2000)
    assert result.available and result.container == "init-db"
    args = service._run.call_args.args[0]
    assert "--previous=true" in args and "--tail=2000" in args


def test_missing_pods_and_empty_logs_are_distinct():
    assert reader([]).read(NS, RELEASE).reason == "no_pods"
    empty = reader(text=b"").read(NS, RELEASE)
    assert empty.available and empty.reason is None and empty.text == ""


@pytest.mark.parametrize("text", [b"x" * (LOG_BYTE_LIMIT + 1), b"x" * (LOG_BYTE_LIMIT - 1) + "雲".encode(), b"\xff" * (LOG_BYTE_LIMIT + 1)])
def test_byte_cap_holds_for_utf8_boundaries_and_malformed_bytes(text):
    result = reader(text=text).read(NS, RELEASE)
    assert result.available and result.truncated
    assert len(result.text.encode("utf-8")) <= LOG_BYTE_LIMIT


@pytest.mark.parametrize("namespace", ["kube-system", "paas-ws-stg-other"])
def test_namespace_rejection_happens_before_subprocess(namespace):
    service = reader()
    with pytest.raises(ValueError):
        service.read(namespace, RELEASE)
    service._run.assert_not_called()


@pytest.mark.parametrize("name", ["x,app.kubernetes.io/instance=foreign", "", "--selector=x", "a" * 54])
def test_selector_injection_rejected_before_subprocess(name):
    service = reader()
    with pytest.raises(ValueError):
        service.read(NS, name)
    service._run.assert_not_called()


@pytest.mark.parametrize("tail", [0, -1, 2001, True])
def test_invalid_tail_limits_rejected(tail):
    service = reader()
    with pytest.raises(ValueError):
        service.read(NS, RELEASE, tail_lines=tail)
    service._run.assert_not_called()


def test_command_discards_stderr_and_uses_short_subprocess_timeout(monkeypatch):
    run = Mock(return_value=completed())
    monkeypatch.setattr(subprocess, "run", run)
    ReleaseLogReader()._run(["logs", "app-0"])
    assert run.call_args.kwargs["stderr"] == subprocess.DEVNULL
    assert run.call_args.kwargs["timeout"] == LOG_COMMAND_TIMEOUT_SECONDS
    assert run.call_args.kwargs["stdout"] == subprocess.PIPE


def test_in_cluster_command_uses_projected_token_file_without_exposing_token(monkeypatch, tmp_path):
    from src.services import release_logs

    service_account = tmp_path / "serviceaccount"
    service_account.mkdir()
    token_file = service_account / "token"
    ca_file = service_account / "ca.crt"
    token_file.write_text("private-service-account-token")
    ca_file.write_text("test-ca")
    monkeypatch.setattr(release_logs, "_SERVICE_ACCOUNT_DIR", str(service_account))
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.43.0.1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")

    observed = {}

    def run(command, **kwargs):
        config_path = command[command.index("--kubeconfig") + 1]
        observed["command"] = command
        observed["config"] = yaml.safe_load(Path(config_path).read_text())
        observed["path"] = config_path
        observed["mode"] = stat.S_IMODE(Path(config_path).stat().st_mode)
        observed["kwargs"] = kwargs
        return completed(b"{}")

    monkeypatch.setattr(subprocess, "run", run)
    ReleaseLogReader()._run(["get", "pods"])

    config = observed["config"]
    assert observed["command"][:3] == ["kubectl", "--kubeconfig", observed["path"]]
    assert "private-service-account-token" not in observed["command"]
    assert config["clusters"][0]["cluster"] == {
        "server": "https://10.43.0.1:443",
        "certificate-authority": str(ca_file),
    }
    assert config["users"][0]["user"] == {"tokenFile": str(token_file)}
    assert "private-service-account-token" not in str(config)
    assert observed["mode"] == 0o600
    assert not os.path.exists(observed["path"])
    assert observed["kwargs"]["timeout"] == LOG_COMMAND_TIMEOUT_SECONDS
    assert observed["kwargs"]["stderr"] == subprocess.DEVNULL
    assert observed["kwargs"]["stdout"] == subprocess.PIPE


def test_invalid_in_cluster_port_fails_without_running_kubectl(monkeypatch):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.43.0.1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443;echo bad")
    run = Mock()
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(ValueError):
        ReleaseLogReader()._run(["get", "pods"])
    run.assert_not_called()


@pytest.mark.parametrize("failure,reason", [
    (completed(b"secret in failed stdout", returncode=1), "logs_unavailable"),
    (subprocess.TimeoutExpired(["kubectl"], 4, output=b"secret", stderr=b"secret"), "timeout"),
    (FileNotFoundError("private filesystem path"), "logs_unavailable"),
])
def test_log_failures_never_expose_stderr_or_partial_stdout(failure, reason, caplog):
    service = reader()
    service._run.side_effect = [completed(json.dumps({"items": [pod()]}).encode()), failure]
    result = service.read(NS, RELEASE)
    assert not result.available and result.reason == reason and not result.text
    assert "secret" not in result.model_dump_json() and "private" not in caplog.text


def test_discovery_failure_never_returns_stderr():
    service = reader()
    service._run.side_effect = [completed(b"secret", returncode=1)]
    result = service.read(NS, RELEASE)
    assert result.reason == "lookup_failed" and not result.text and not result.sources


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "api_key", "test-log-key")
    service = reader()
    monkeypatch.setattr(releases, "release_log_reader", service)
    return TestClient(app, headers={"X-API-Key": "test-log-key"}), service


def test_endpoint_is_authenticated_and_preserves_response_contract(client):
    http, service = client
    unauthorized = http.get(f"/api/releases/{NS}/{RELEASE}/logs", headers={"X-API-Key": "invalid"})
    assert unauthorized.status_code == 401
    service._run.assert_not_called()
    response = http.get(f"/api/releases/{NS}/{RELEASE}/logs")
    assert response.status_code == 200
    assert set(response.json()) == {"available", "reason", "pod", "container", "sources", "text", "truncated"}


@pytest.mark.parametrize("query", ["tail_lines=0", "tail_lines=2001", "previous=not-bool"])
def test_endpoint_rejects_invalid_options_before_discovery(client, query):
    http, service = client
    assert http.get(f"/api/releases/{NS}/{RELEASE}/logs?{query}").status_code == 422
    service._run.assert_not_called()


def test_endpoint_rejects_foreign_namespace_before_discovery(client):
    http, service = client
    assert http.get(f"/api/releases/kube-system/{RELEASE}/logs").status_code == 403
    service._run.assert_not_called()




def test_sensitive_command_errors_drop_both_stdout_and_stderr(monkeypatch, caplog):
    monkeypatch.setattr(subprocess, "run", Mock(return_value=completed(b"private-log-token", 1)))
    with pytest.raises(KubectlException) as raised:
        ReleaseLogReader()._run(["logs", "app-0"])
    assert raised.value.stdout == raised.value.stderr == ""
    assert "private" not in str(raised.value) and "private" not in caplog.text


def test_real_command_timeout_is_classified_without_partial_output(monkeypatch):
    run = Mock(side_effect=subprocess.TimeoutExpired(["kubectl"], 4, output=b"private", stderr=b"private"))
    monkeypatch.setattr(subprocess, "run", run)
    result = ReleaseLogReader().read(NS, RELEASE)
    assert result.reason == "timeout" and result.text == ""


def test_real_command_nonzero_is_classified_without_partial_output(monkeypatch):
    run = Mock(side_effect=[
        completed(json.dumps({"items": [pod()]}).encode()),
        completed(b"private-log-token", 1),
    ])
    monkeypatch.setattr(subprocess, "run", run)
    result = ReleaseLogReader().read(NS, RELEASE)
    assert result.reason == "logs_unavailable" and result.text == ""


@pytest.mark.parametrize("sensitive", [False, True])
@pytest.mark.parametrize("timeout", [None, 4, 47])
def test_output_mode_keeps_one_subprocess_and_the_same_timeout(monkeypatch, sensitive, timeout):
    run = Mock(return_value=completed())
    monkeypatch.setattr(subprocess, "run", run)
    options = {"sensitive_output": sensitive}
    if timeout is not None:
        options["timeout"] = timeout
    KubernetesService()._run_command(["get", "pods"], **options)
    run.assert_called_once()
    assert run.call_args.kwargs["timeout"] == (30 if timeout is None else timeout)
    if not sensitive:
        assert run.call_args.kwargs["capture_output"] is True
        assert run.call_args.kwargs["text"] is True
        assert "stdout" not in run.call_args.kwargs
        assert "stderr" not in run.call_args.kwargs
